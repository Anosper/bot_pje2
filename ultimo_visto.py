"""
ultimo_visto.py

Controle local (arquivo JSON) do ultimo processo capturado por tribunal.
Elimina a necessidade de consultar o Firestore para verificar duplicidade:
como cada tribunal e lido em ordem do mais recente para o mais antigo, basta
comparar o numero do processo mais recente da lista com o ultimo que ja foi
capturado.

Uso basico:

    from ultimo_visto import UltimoVisto

    uv = UltimoVisto()  # usa ultimo_visto.json na mesma pasta

    processo_recente = "0001234-56.2026.8.19.0001"  # primeiro item da lista do site

    if uv.eh_novo("TJRJ", processo_recente):
        # captura e grava normalmente no Firestore (via fm.set(...), etc.)
        ...
        uv.atualizar("TJRJ", processo_recente)
    else:
        # nada novo neste tribunal nesta passada -- nem chega a tocar no Firestore
        pass
"""

import json
import threading
from pathlib import Path

try:
    from filelock import FileLock
    _TEM_FILELOCK = True
except ImportError:
    _TEM_FILELOCK = False

ARQUIVO_PADRAO = Path(__file__).parent / "ultimo_visto.json"


class UltimoVisto:
    def __init__(self, arquivo=ARQUIVO_PADRAO):
        self.arquivo = Path(arquivo)
        self.lock_path = str(self.arquivo) + ".lock"
        self._lock = threading.Lock()
        if not self.arquivo.exists():
            self._escrever({})

    def _ler(self):
        try:
            with open(self.arquivo, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def _escrever(self, dados):
        with open(self.arquivo, "w", encoding="utf-8") as f:
            json.dump(dados, f, indent=2, ensure_ascii=False)

    def _com_lock(self, func):
        # Varios bots (PJe, eproc RJ/RS, eproc SC) podem rodar em paralelo e
        # escrever no mesmo arquivo -- o filelock evita corrida entre eles.
        # Tribunais diferentes sao chaves diferentes no mesmo JSON, entao o
        # lock so protege a escrita/leitura do arquivo, nao os dados em si.
        if _TEM_FILELOCK:
            with FileLock(self.lock_path, timeout=10):
                return func()
        with self._lock:
            return func()

    def obter(self, tribunal: str):
        """Retorna o numero do ultimo processo conhecido para o tribunal,
        ou None se nunca foi visto."""
        def _op():
            dados = self._ler()
            return dados.get(tribunal)
        return self._com_lock(_op)

    def eh_novo(self, tribunal: str, numero_processo: str) -> bool:
        """True se numero_processo e diferente do ultimo conhecido para esse
        tribunal (ou se o tribunal ainda nao tem nenhum registro)."""
        return self.obter(tribunal) != numero_processo

    def atualizar(self, tribunal: str, numero_processo: str):
        """Grava o novo 'ultimo visto' para o tribunal. Chame isso depois de
        capturar e salvar o processo com sucesso."""
        def _op():
            dados = self._ler()
            dados[tribunal] = numero_processo
            self._escrever(dados)
        self._com_lock(_op)
