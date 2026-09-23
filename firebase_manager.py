"""
firebase_manager.py

Gerenciador de failover entre multiplos projetos Firebase, com controle de
cota diaria (leituras/escritas) baseado em contagem local persistida em disco
(com lock de arquivo para uso seguro entre processos concorrentes) e troca
automatica para o projeto secundario quando o principal se aproxima do limite
(ou lanca ResourceExhausted).

Uso basico:

    from firebase_manager import FirebaseManager

    fm = FirebaseManager([
        {"name": "principal", "cred_path": "firebase-service-account.json"},
        {"name": "secundario", "cred_path": "firebase-service-account-2.json"},
    ])

    db = fm.client()  # firestore.Client ativo no momento

    # leituras/escritas contabilizadas automaticamente:
    ref = db.collection("processos").document("0001234-56.2024.8.19.0001")
    doc = fm.get(ref)
    fm.set(ref, dados)

Instale a dependencia extra:
    pip install filelock
"""

import json
import logging
import os
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

import firebase_admin
from firebase_admin import credentials, firestore
from google.api_core.exceptions import ResourceExhausted

try:
    from filelock import FileLock
except ImportError:
    raise SystemExit("Instale a dependencia: pip install filelock")

logger = logging.getLogger("firebase_manager")

# Limites do plano gratuito (Spark). Deixamos margem de seguranca para trocar
# de projeto ANTES de estourar de verdade (o Firestore corta na hora).
DEFAULT_READ_LIMIT = 50_000
DEFAULT_WRITE_LIMIT = 20_000
DEFAULT_DELETE_LIMIT = 20_000
SAFETY_MARGIN = 0.9  # troca de projeto ao atingir 90% do limite

STATE_FILE = Path(__file__).parent / "firebase_quota_state.json"
LOCK_FILE = str(STATE_FILE) + ".lock"


def _today_str():
    # A cota do Firebase reseta a meia-noite Pacific Time. Aproximamos com
    # UTC-8 fixo (suficiente para uso diario / nao critico em DST).
    return (datetime.now(timezone.utc) - timedelta(hours=8)).strftime("%Y-%m-%d")


class FirebaseManager:
    def __init__(self, projects, read_limit=DEFAULT_READ_LIMIT,
                 write_limit=DEFAULT_WRITE_LIMIT, delete_limit=DEFAULT_DELETE_LIMIT,
                 safety_margin=SAFETY_MARGIN):
        if not projects:
            raise ValueError("Forneca ao menos um projeto Firebase")

        self.projects = projects  # lista de {"name":..., "cred_path":...}
        self.read_limit = read_limit
        self.write_limit = write_limit
        self.delete_limit = delete_limit
        self.safety_margin = safety_margin
        self._lock = threading.Lock()
        self._apps = {}
        self._clients = {}
        self._active_name = None

        self._init_all_apps()
        self._pick_active_project()

    # ---------- inicializacao ----------

    def _init_all_apps(self):
        for proj in self.projects:
            name = proj["name"]
            cred_path = proj["cred_path"]
            if not os.path.exists(cred_path):
                logger.warning("Credencial nao encontrada para %s: %s", name, cred_path)
                continue
            try:
                cred = credentials.Certificate(cred_path)
                app = firebase_admin.initialize_app(cred, name=name)
                self._apps[name] = app
                self._clients[name] = firestore.client(app)
                logger.info("Projeto Firebase '%s' inicializado.", name)
            except Exception as e:
                logger.error("Falha ao inicializar projeto '%s': %s", name, e)

    # ---------- estado de cota (compartilhado entre processos) ----------

    def _read_state(self):
        if not STATE_FILE.exists():
            return {}
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def _write_state(self, state):
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)

    def _get_usage(self, name):
        with FileLock(LOCK_FILE, timeout=10):
            state = self._read_state()
            today = _today_str()
            return state.get(today, {}).get(name, {"reads": 0, "writes": 0, "deletes": 0})

    def _increment(self, name, kind, n=1):
        with FileLock(LOCK_FILE, timeout=10):
            state = self._read_state()
            today = _today_str()
            state.setdefault(today, {})
            state[today].setdefault(name, {"reads": 0, "writes": 0, "deletes": 0})
            state[today][name][kind] = state[today][name].get(kind, 0) + n
            # limpa dias antigos para o arquivo nao crescer indefinidamente
            for old_day in list(state.keys()):
                if old_day != today:
                    del state[old_day]
            self._write_state(state)

    def _is_near_limit(self, name):
        usage = self._get_usage(name)
        if usage["reads"] >= self.read_limit * self.safety_margin:
            return True
        if usage["writes"] >= self.write_limit * self.safety_margin:
            return True
        if usage["deletes"] >= self.delete_limit * self.safety_margin:
            return True
        return False

    def _mark_exhausted(self, name):
        """Forca o projeto a parecer no limite, mesmo que a contagem local
        nao tenha percebido ainda (usado quando o Firestore ja recusou uma
        operacao com ResourceExhausted)."""
        with FileLock(LOCK_FILE, timeout=10):
            state = self._read_state()
            today = _today_str()
            state.setdefault(today, {})
            state[today][name] = {
                "reads": self.read_limit,
                "writes": self.write_limit,
                "deletes": self.delete_limit,
            }
            self._write_state(state)

    # ---------- selecao do projeto ativo ----------

    def _pick_active_project(self):
        for proj in self.projects:
            name = proj["name"]
            if name not in self._clients:
                continue
            if not self._is_near_limit(name):
                if self._active_name != name:
                    logger.info("Usando projeto Firebase ativo: '%s'", name)
                self._active_name = name
                return
        # todos perto do limite: usa o ultimo da lista mesmo assim (evita crash total)
        fallback = self.projects[-1]["name"]
        logger.warning(
            "Todos os projetos Firebase estao perto do limite diario. "
            "Continuando com '%s' mesmo assim.", fallback
        )
        self._active_name = fallback

    def client(self):
        with self._lock:
            self._pick_active_project()
            return self._clients[self._active_name]

    @property
    def active_project(self):
        return self._active_name

    # ---------- operacoes contabilizadas ----------

    def get(self, doc_ref):
        """doc_ref.get() contabilizando 1 leitura, com failover automatico.
        Se estourar a cota, marca o projeto como esgotado e propaga a
        excecao -- o chamador deve pegar um novo client() (fm.client()) e
        tentar de novo (o proximo projeto ja estara ativo)."""
        name = self._active_name
        try:
            result = doc_ref.get()
            self._increment(name, "reads", 1)
            return result
        except ResourceExhausted:
            logger.warning("Cota de leitura estourada em '%s'. Trocando de projeto.", name)
            self._mark_exhausted(name)
            self._pick_active_project()
            raise

    def set(self, doc_ref, data, merge=False):
        name = self._active_name
        try:
            result = doc_ref.set(data, merge=merge)
            self._increment(name, "writes", 1)
            return result
        except ResourceExhausted:
            logger.warning("Cota de escrita estourada em '%s'. Trocando de projeto.", name)
            self._mark_exhausted(name)
            self._pick_active_project()
            raise

    def usage_summary(self):
        return {
            proj["name"]: self._get_usage(proj["name"])
            for proj in self.projects
            if proj["name"] in self._clients
        }
