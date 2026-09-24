import os
import re
import sys
import time
from datetime import datetime, timedelta
import pyotp
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
import firebase_admin
from firebase_admin import credentials
from firebase_admin import firestore
from google.api_core.exceptions import ResourceExhausted
from firebase_manager import FirebaseManager

# ============================================================
# SUPORTE A "PULAR ESPERA" APERTANDO ENTER NO TERMINAL
# ============================================================
IS_WINDOWS = os.name == "nt"

if IS_WINDOWS:
    import msvcrt
else:
    import select


def tecla_pular_pressionada():
    if IS_WINDOWS:
        pulou = False
        while msvcrt.kbhit():
            tecla = msvcrt.getch()
            if tecla in (b"\r", b"\n"):
                pulou = True
        return pulou
    else:
        pulou = False
        while select.select([sys.stdin], [], [], 0)[0]:
            linha = sys.stdin.readline()
            if linha is not None:
                pulou = True
        return pulou


class SessaoNavegador:
    def __init__(self, p, headless, arquivo_sessao):
        self.p = p
        self.arquivo_sessao = arquivo_sessao
        self.headless = headless
        self.navegador = None
        self.contexto = None
        self.pagina = None
        self._abrir()

    def _abrir(self):
        # --disable-blink-features=AutomationControlled + user agent
        # "normal" + esconder navigator.webdriver: alguns portais
        # (TJRN, e antes o TRF3) usam Akamai Bot Manager, que pode
        # bloquear ("Access Denied") sessões que parecem Chromium
        # automatizado. Isso reduz o fingerprint óbvio de automação,
        # mas não é garantia contra bloqueio por padrão de tráfego.
        self.navegador = self.p.chromium.launch(
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        contexto_kwargs = {
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "viewport": {"width": 1366, "height": 768},
            "locale": "pt-BR",
        }
        if os.path.exists(self.arquivo_sessao):
            self.contexto = self.navegador.new_context(storage_state=self.arquivo_sessao, **contexto_kwargs)
        else:
            self.contexto = self.navegador.new_context(**contexto_kwargs)
        self.contexto.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )
        self.pagina = self.contexto.new_page()
        self.pagina.on("dialog", tratar_dialogo)

    def salvar_sessao(self):
        try:
            self.contexto.storage_state(path=self.arquivo_sessao)
        except Exception as erro:
            print("Aviso ao salvar sessão:", erro)

    def trocar_para(self, headless):
        if headless == self.headless:
            return

        url_atual = None
        try:
            url_atual = self.pagina.url
        except Exception:
            pass

        self.salvar_sessao()

        try:
            self.navegador.close()
        except Exception:
            pass

        self.headless = headless
        self._abrir()

        if url_atual and url_atual not in ("about:blank", ""):
            try:
                self.pagina.goto(url_atual, wait_until="domcontentloaded", timeout=120000)
            except Exception as erro:
                print("Aviso ao reabrir a página após trocar de modo:", erro)

    def fechar(self):
        try:
            self.navegador.close()
        except Exception:
            pass


# ============================================================
# CONFIGURAÇÕES GERAIS
# ============================================================
load_dotenv()
USUARIO = os.getenv("PJE_USUARIO")
TOTP_SECRET_BRUTO = os.getenv("PJE_TOTP_SECRET") or ""
TOTP_SECRET = TOTP_SECRET_BRUTO.replace(" ", "").strip().upper()
HEADLESS = True
ARQUIVO_SESSAO = "sessao_pje_ma.json"
INTERVALO_ENTRE_VARREDURAS = 30

SEGUNDOS_ESPERA_CERTIFICADO_MANUAL = 90

CLASSES_JUDICIAIS = [
    "Busca e Apreensão",
    "Execução de Título Extrajudicial",
    "Monitória",
]

# ============================================================
# LOGIN POR PORTAL+CLIQUE (tribunais com "login_portal_click": True)
# ============================================================
# Alguns tribunais têm WAF/proteção de rede que bloqueia navegação
# direta (page.goto) para o domínio do PJe — só libera quando um
# clique de verdade acontece dentro do navegador. Por isso o login
# passa por um portal público e clica no link, em vez de ir direto
# pra url_login. Cada tribunal que usa esse fluxo define no seu
# dict em TRIBUNAIS:
#   - url_portal: página pública com o link de acesso ao PJe
#   - url_destino_clique: href exato do link a clicar
#   - texto_link_portal: regex alternativa pra achar o link por texto
#     (fallback se o seletor por href não achar)
#   - url_login_direta_fallback: URL de SSO de uso único, como
#     último recurso se o clique no portal falhar completamente
#     (tem "state" fixo — só funciona uma vez, pode dar "Cookie not
#     found" ou tela "já logado" se reusada)

# Textos que indicam que o Keycloak caiu na tela "Você já está
# logado." em vez de avançar sozinho pro PJe depois do 2FA.
TEXTOS_JA_LOGADO = [
    "você já está logado",
    "voce ja esta logado",
    "already logged in",
    "you are already logged in",
]

# ============================================================
# VALOR MÍNIMO DA CAUSA POR TRIBUNAL
# ============================================================
VALOR_MINIMO_CAUSA_POR_TRIBUNAL = {
    # O campo tem máscara monetária: os 2 últimos dígitos digitados
    # viram centavos. "1000000" -> exibido como R$ 10.000,00.
    "TJMA": "1000000",
    "TRF3": "1000000",
}

# ============================================================
# REGRAS DE SELEÇÃO DE PROCESSO (por tribunal + classe)
# ============================================================
REGRA_SELECAO_PROCESSO = {
    ("TJMA", "Execução de Título Extrajudicial"): {"indice": 1},
    ("TRF3", "Execução Fiscal"): {"classe_exigida": "Execução Fiscal"},
}

# ============================================================
# TRIBUNAIS
# ============================================================
# TRF3 — pausado por instabilidade de rede/WAF (timeout e
# ERR_HTTP2_PROTOCOL_ERROR intermitentes mesmo tentando vários
# caminhos: goto direto, portal+clique, URL de SSO direta). Login
# em si chegou a funcionar via SSO direta, mas não de forma
# sustentável pra rodar sozinho. Reavaliar depois.
# {
#     "nome": "TRF3",
#     "url_login": "https://pje1g.trf3.jus.br/pje/login.seam",
#     "url_pesquisa": "https://pje1g.trf3.jus.br/pje/Processo/ConsultaProcesso/listView.seam",
#     "senha_env": "PJE_SENHA_TRF3",
#     "login_portal_click": True,
#     "url_portal": "https://www.trf3.jus.br/pje/acesso-ao-sistema",
#     "url_destino_clique": "https://pje1g.trf3.jus.br",
#     "texto_link_portal": re.compile(r"Sistema\s+PJe\s*-\s*1[ºo]?\s*Grau", re.IGNORECASE),
#     "classes": [
#         "Execução Fiscal",
#         "Execução de Título Extrajudicial",
#         "Monitória",
#     ],
# },
TRIBUNAIS = [
    {
        # TJRN primeiro na lista de propósito — assim dá pra ver se
        # ele está indo bem sem esperar os outros rodarem primeiro.
        "nome": "TJRN",
        "url_login": "https://pje1g.tjrn.jus.br/pje/login.seam",
        "url_pesquisa": "https://pje1g.tjrn.jus.br/pje/Processo/ConsultaProcesso/listView.seam",
        "senha_env": "PJE_SENHA_TJRN",
        "login_portal_click": True,
        "url_portal": "https://www.tjrn.jus.br/",
        # Portal do TJRN esconde o link de acesso atrás de uma aba
        # ("Consulta Processual - PJe") que precisa ser clicada
        # primeiro pra revelar o link "Acessar sistema".
        "texto_clique_preliminar": re.compile(r"Consulta\s+Processual\s*-\s*PJe", re.IGNORECASE),
        "url_destino_clique": "https://pje1g.tjrn.jus.br/pje",
        "texto_link_portal": re.compile(r"Acessar\s+sistema", re.IGNORECASE),
        "url_login_direta_fallback": (
            "https://sso.cloud.pje.jus.br/auth/realms/pje/protocol/openid-connect/auth"
            "?response_type=code"
            "&client_id=pje-tjrn-1g"
            "&redirect_uri=https%3A%2F%2Fpje1g.tjrn.jus.br%2Fpje%2Flogin.seam"
            "&state=20a77679-4dd3-4cc6-8de0-9bc12c29ee59"
            "&login=true"
            "&scope=openid"
        ),
    },
    {
        "nome": "TJPI",
        "url_login": "https://pje.tjpi.jus.br/1g/login.seam",
        "url_pesquisa": "https://pje.tjpi.jus.br/1g/Processo/ConsultaProcesso/listView.seam",
        "senha_env": "PJE_SENHA_TJPI",
        "login_portal_click": True,
        "url_portal": "https://www.tjpi.jus.br/portaltjpi/pje/",
        "url_destino_clique": "https://pje.tjpi.jus.br/1g",
        "texto_link_portal": re.compile(r"Acessar\s+PJe\s*1[ºo]?\s*Grau", re.IGNORECASE),
        "url_login_direta_fallback": (
            "https://sso.cloud.pje.jus.br/auth/realms/pje/protocol/openid-connect/auth"
            "?response_type=code"
            "&client_id=pje-tjpi-1g"
            "&redirect_uri=https%3A%2F%2Fpje.tjpi.jus.br%2F1g%2Flogin.seam"
            "&state=6d2fa6a6-97fc-43b8-b775-3fafbb9c6841"
            "&login=true"
            "&scope=openid"
        ),
    },
    {
        "nome": "TJMA",
        "url_login": "https://pje.tjma.jus.br/pje/login.seam",
        "url_pesquisa": "https://pje.tjma.jus.br/pje/Processo/ConsultaProcesso/listView.seam",
        "senha_env": "PJE_SENHA_TJMA",
    },
]
# ============================================================
# FIREBASE
# ============================================================
print()
print("==========================================")
print(" CONECTANDO AO FIREBASE")
print("==========================================")
try:
    fm = FirebaseManager([
        {"name": "principal", "cred_path": "firebase-service-account.json"},
        {"name": "failover", "cred_path": "firebase-service-account-failover.json"},
        {"name": "failover2", "cred_path": "firebase-service-account-failover2.json"},
        {"name": "failover3", "cred_path": "firebase-service-account-failover3.json"},
    ])
    print(f"Firebase conectado com sucesso! Projeto ativo: {fm.active_project}")
except Exception as erro:
    print()
    print("==========================================")
    print(" ERRO AO CONECTAR AO FIREBASE")
    print("==========================================")
    print("Tipo:", type(erro).__name__)
    print("Detalhes:", erro)
    raise
# ============================================================
# FUNÇÃO — TRATAR ALERTAS DO PJE
# ============================================================
pje_office_nao_encontrado = False


def tratar_dialogo(dialog):
    global pje_office_nao_encontrado
    print()
    print("--- ALERTA DO PJE ---")
    print(dialog.message)
    print("---------------------")
    if "não foi possível encontrar o pje office" in dialog.message.strip().lower():
        pje_office_nao_encontrado = True
        print("(Detectado: PJe Office não encontrado — vou pular o certificado digital.)")
    try:
        dialog.accept()
        print("OK clicado automaticamente.")
    except Exception as erro:
        print("Não foi possível clicar no OK:", erro)
# ============================================================
# FUNÇÃO — GERAR CÓDIGO TOTP
# ============================================================
def gerar_codigo_totp():
    if not TOTP_SECRET:
        raise RuntimeError(
            "PJE_TOTP_SECRET não definido no .env. "
            "É a chave secreta mostrada ao lado do QR code "
            "quando você cadastrou o app autenticador."
        )
    return pyotp.TOTP(TOTP_SECRET).now()
# ============================================================
# FUNÇÃO — DETECTAR/AGUARDAR VERIFICAÇÃO ANTI-BOT
# ============================================================
def verificar_e_aguardar_cloudflare(sessao, timeout_segundos=180):
    indicadores = [
        "performing security verification",
        "executando verificação de segurança",
        "confirme que é humano",
        "confirmar que você é humano",
        "vamos confirmar que você é humano",
        "verifying you are human",
        "human verification",
        "conclua a verificação de segurança",
    ]
    try:
        texto = sessao.pagina.locator("body").inner_text(timeout=5000).lower()
    except Exception:
        return
    if not any(indicador in texto for indicador in indicadores):
        return

    print()
    print("==========================================")
    print(" VERIFICAÇÃO ANTI-BOT DETECTADA")
    print("==========================================")

    trocou_para_visivel = False
    if sessao.headless:
        print(
            "Abrindo uma janela visível do navegador automaticamente para "
            "você resolver esse desafio (assim que terminar, o bot volta "
            "sozinho pro modo headless/invisível)..."
        )
        sessao.trocar_para(False)
        trocou_para_visivel = True
    else:
        print("Resolva a verificação manualmente na janela do navegador que já está aberta.")

    print("A qualquer momento você pode apertar ENTER aqui no terminal para PULAR esta espera.")

    try:
        botao_iniciar = sessao.pagina.get_by_role(
            "button", name=re.compile("iniciar", re.IGNORECASE)
        )
        if botao_iniciar.count() > 0 and botao_iniciar.first.is_visible():
            botao_iniciar.first.click()
            print("Botão 'Iniciar' clicado automaticamente.")
            sessao.pagina.wait_for_timeout(1500)
    except Exception:
        pass

    print(f"Aguardando até {timeout_segundos}s...")
    for segundo in range(timeout_segundos):
        sessao.pagina.wait_for_timeout(1000)
        if tecla_pular_pressionada():
            print("ENTER pressionado — pulando a espera da verificação anti-bot.")
            break
        try:
            texto_atual = sessao.pagina.locator("body").inner_text(timeout=2000).lower()
        except Exception:
            continue
        if not any(indicador in texto_atual for indicador in indicadores):
            print("Verificação concluída, continuando...")
            break
        if (segundo + 1) % 15 == 0:
            restante = timeout_segundos - (segundo + 1)
            print(f"Ainda aguardando verificação... ({restante}s restantes — ENTER para pular)")
    else:
        print("Aviso: verificação não foi resolvida dentro do tempo.")

    if trocou_para_visivel:
        print("Voltando para modo headless (invisível)...")
        sessao.trocar_para(True)
# ============================================================
# FUNÇÃO — TENTAR LOGIN VIA CERTIFICADO DIGITAL (PJe Office)
# ============================================================
def tentar_login_certificado(sessao, tribunal):
    global pje_office_nao_encontrado
    pje_office_nao_encontrado = False

    print()
    print("------------------------------------------------")
    print(f" LOGIN VIA CERTIFICADO DIGITAL — {tribunal['nome']}")
    print("------------------------------------------------")

    # Deixamos você clicar em "Certificado digital" manualmente
    # (em vez do bot clicar sozinho) — por isso, se estava headless,
    # abrimos uma janela visível só para essa etapa.
    trocou_para_visivel = False
    if sessao.headless:
        print("Abrindo uma janela visível para você clicar em 'Certificado digital' manualmente...")
        sessao.trocar_para(False)
        trocou_para_visivel = True

    print()
    print(
        "Se quiser usar o certificado digital neste tribunal, clique você "
        "mesmo em 'Certificado digital' na tela e preencha o PIN no PJe "
        "Office se ele pedir."
    )
    print(
        "Se não tiver certificado ou não quiser usar agora, não precisa "
        "fazer nada: assim que o PJe Office avisar que não encontrou "
        "certificado (ou você apertar ENTER aqui no terminal), o bot segue "
        "sozinho com login por usuário e senha."
    )
    print(f"Aguardando até {SEGUNDOS_ESPERA_CERTIFICADO_MANUAL}s...")

    logou_com_certificado = False

    for segundo in range(SEGUNDOS_ESPERA_CERTIFICADO_MANUAL):
        sessao.pagina.wait_for_timeout(1000)

        if pje_office_nao_encontrado:
            print(
                f"[{tribunal['nome']}] PJe Office avisou que não encontrou "
                "certificado — seguindo com login por usuário e senha."
            )
            break

        if tecla_pular_pressionada():
            print(
                f"[{tribunal['nome']}] ENTER pressionado — seguindo com "
                "login por usuário e senha."
            )
            break

        if "sso.cloud.pje.jus.br" not in sessao.pagina.url:
            print(f"[{tribunal['nome']}] Login via certificado digital concluído.")
            logou_com_certificado = True
            break

        if (segundo + 1) % 15 == 0:
            restante = SEGUNDOS_ESPERA_CERTIFICADO_MANUAL - (segundo + 1)
            print(f"Ainda aguardando... ({restante}s restantes — ENTER para pular)")
    else:
        print(
            f"[{tribunal['nome']}] Tempo esgotado sem ação — seguindo com "
            "login por usuário e senha."
        )

    if trocou_para_visivel:
        print("Voltando para modo headless (invisível)...")
        sessao.trocar_para(True)

    # Se não logou via certificado, a tela pode ter mudado (você
    # pode ter clicado em algo manualmente) — recarregamos a página
    # de login antes de cair para usuário/senha, por segurança.
    precisa_recarregar_pagina = not logou_com_certificado

    return logou_com_certificado, precisa_recarregar_pagina
def pagina_bloqueada_por_waf(pagina):
    """Detecta a página de 'Access Denied' do Akamai (edgesuite.net),
    usada por alguns tribunais (TJRN, antes o TRF3) pra bloquear
    tráfego que parece automação. Serve pra parar de insistir em
    mais navegações quando a sessão/IP já foi bloqueada — continuar
    batendo no site nesse estado só reforça o bloqueio."""
    try:
        if "access denied" in pagina.title().lower():
            return True
    except Exception:
        pass
    try:
        texto = pagina.locator("body").inner_text(timeout=2000).lower()
        if "access denied" in texto and "permission to access" in texto:
            return True
    except Exception:
        pass
    return False


# ============================================================
# FUNÇÃO — DIAGNÓSTICO DA TELA DE LOGIN (quando #username falha)
# ============================================================
def diagnosticar_tela_de_login(pagina, tribunal_nome):
    print()
    print("==========================================")
    print(" DIAGNÓSTICO DA TELA DE LOGIN")
    print("==========================================")
    try:
        print("Título da página:", pagina.title())
    except Exception as erro:
        print("Não consegui pegar o título da página:", erro)

    try:
        campos = pagina.locator("input")
        total_campos = campos.count()
        print(f"Total de campos <input> encontrados na página: {total_campos}")
        for i in range(min(total_campos, 20)):
            campo = campos.nth(i)
            try:
                id_campo = campo.get_attribute("id") or "(sem id)"
                name_campo = campo.get_attribute("name") or "(sem name)"
                tipo_campo = campo.get_attribute("type") or "(sem type)"
                visivel = campo.is_visible()
                print(f"  [{i}] id={id_campo} | name={name_campo} | type={tipo_campo} | visível={visivel}")
            except Exception:
                continue
    except Exception as erro:
        print("Não consegui listar os campos <input>:", erro)

    try:
        texto_visivel = pagina.locator("body").inner_text(timeout=5000)
        print()
        print("--- TEXTO VISÍVEL DA PÁGINA (primeiros 500 caracteres) ---")
        print(texto_visivel[:500])
    except Exception as erro:
        print("Não consegui ler o texto da página:", erro)

    try:
        os.makedirs("debug_login", exist_ok=True)
        agora = datetime.now().strftime("%Y%m%d_%H%M%S")
        nome_arquivo = f"debug_login/{tribunal_nome}_{agora}.png".replace(" ", "_")
        pagina.screenshot(path=nome_arquivo, full_page=True)
        print()
        print(f"Screenshot salvo em: {nome_arquivo}")
    except Exception as erro:
        print("Não foi possível salvar screenshot:", erro)
    print("==========================================")


# ============================================================
# FUNÇÃO — NAVEGAR COM RETRY (protege contra erros de rede
# transitórios, ex: ERR_HTTP2_PROTOCOL_ERROR)
# ============================================================
def navegar_com_retry(pagina, url, tentativas=5, timeout=120000, wait_until="domcontentloaded", referer=None):
    ultimo_erro = None
    for tentativa in range(1, tentativas + 1):
        try:
            if referer:
                pagina.goto(url, wait_until=wait_until, timeout=timeout, referer=referer)
            else:
                pagina.goto(url, wait_until=wait_until, timeout=timeout)
            return True
        except Exception as erro:
            ultimo_erro = erro
            print(f"Erro ao navegar para {url} (tentativa {tentativa}/{tentativas}): {erro}")
            if tentativa < tentativas:
                pagina.wait_for_timeout(5000 * tentativa)
    print(f"Falha definitiva ao navegar para {url} após {tentativas} tentativas: {ultimo_erro}")
    return False
# ============================================================
# FUNÇÃO — LOGIN AUTOMÁTICO
# ============================================================
def _preencher_usuario_senha_e_2fa(sessao, tribunal):
    """Parte comum a todos os fluxos de login (depois de já estar na
    tela do Keycloak/SSO): preenche usuário/senha e o TOTP se pedido.
    Não oferece etapa de certificado digital — rodando como serviço,
    sem sessão gráfica, não tem como clicar manualmente."""
    senha = os.getenv(tribunal["senha_env"])
    if not senha:
        print(f"ERRO: variável {tribunal['senha_env']} não definida no .env")
        return False
    try:
        sessao.pagina.locator("#username").fill(USUARIO)
        sessao.pagina.locator("#password").fill(senha)
        sessao.pagina.locator("#kc-login").click()
        print(f"[{tribunal['nome']}] Login e senha preenchidos e enviados.")
    except Exception as erro:
        print("Erro ao preencher usuário/senha:", erro)
        print("URL atual:", sessao.pagina.url)
        diagnosticar_tela_de_login(sessao.pagina, tribunal["nome"])
        return False
    sessao.pagina.wait_for_timeout(2000)

    try:
        campo_totp = sessao.pagina.locator("#otp")
        if campo_totp.count() > 0 and campo_totp.first.is_visible(timeout=5000):
            codigo = gerar_codigo_totp()
            print("Tela de 2FA detectada — preenchendo TOTP...")
            campo_totp.first.fill(codigo)
            sessao.pagina.locator("#kc-login").click()
    except Exception as erro:
        print("Aviso: não encontrou/preencheu campo TOTP:", erro)
    return True


def fazer_login_simples(sessao, tribunal):
    """Fluxo padrão: vai direto pra url_login. Usado por todos os
    tribunais exceto os que têm 'login_portal_click': True (que
    precisam do fluxo especial abaixo)."""
    if not navegar_com_retry(sessao.pagina, tribunal["url_login"], tentativas=5, timeout=120000):
        print(f"[{tribunal['nome']}] Não foi possível abrir a tela de login — pulando este tribunal.")
        return False

    sessao.pagina.wait_for_timeout(2000)
    verificar_e_aguardar_cloudflare(sessao)
    if "sso.cloud.pje.jus.br" not in sessao.pagina.url:
        print("Sessão SSO já ativa — login automático (sem certificado nem senha).")
        return True

    if not _preencher_usuario_senha_e_2fa(sessao, tribunal):
        return False

    for _ in range(60):
        if "sso.cloud.pje.jus.br" not in sessao.pagina.url:
            print(f"[{tribunal['nome']}] Login concluído com SUCESSO via USUÁRIO E SENHA.")
            return True
        sessao.pagina.wait_for_timeout(1000)

    print(f"[{tribunal['nome']}] ERRO: não saiu da tela de SSO após tentar login via usuário e senha.")
    print("URL atual:", sessao.pagina.url)
    return False


def _promover_pagina_nova(sessao, pagina_nova):
    try:
        pagina_nova.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception:
        pass
    pagina_nova.on("dialog", tratar_dialogo)
    sessao.pagina = pagina_nova
    for pagina_extra in list(sessao.contexto.pages):
        if pagina_extra is not sessao.pagina:
            try:
                pagina_extra.close()
            except Exception:
                pass


def _clicar_com_captura_de_aba(sessao, tribunal, elemento):
    """Clica no elemento; se abrir aba nova, promove ela a
    sessao.pagina e fecha as demais."""
    try:
        with sessao.contexto.expect_page(timeout=6000) as info_pagina_nova:
            elemento.click()
        _promover_pagina_nova(sessao, info_pagina_nova.value)
        print(f"[{tribunal['nome']}] Clique abriu aba nova. URL atual:", sessao.pagina.url)
    except Exception:
        try:
            sessao.pagina.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass


def _reforcar_com_href(sessao, tribunal, href):
    """Se ainda estivermos presos no domínio de SSO depois de um
    clique, tenta navegar direto pro href como reforço (a sessão já
    deve estar autenticada nesse ponto, então costuma funcionar
    mesmo quando o goto direto falhava antes do login)."""
    if href and "sso.cloud.pje.jus.br" in sessao.pagina.url:
        if href.startswith("/"):
            href = tribunal["url_destino_clique"] + href
        print(f"[{tribunal['nome']}] Ainda no domínio de SSO — reforçando com navegação direta pro href: {href}")
        navegar_com_retry(sessao.pagina, href, tentativas=1, timeout=20000, referer=sessao.pagina.url)
        if "sso.cloud.pje.jus.br" in sessao.pagina.url:
            try:
                sessao.pagina.reload(wait_until="domcontentloaded", timeout=20000)
            except Exception as erro:
                print(f"[{tribunal['nome']}] Reload falhou: {erro}")


def tentar_passar_tela_ja_logado(sessao, tribunal):
    """Depois do 2FA, o Keycloak às vezes mostra 'Você já está
    logado.' em vez de avançar sozinho pro PJe. Detecta essa tela e
    tenta clicar automaticamente em qualquer link/botão visível pra
    prosseguir (esse clique também pode abrir aba nova)."""
    try:
        texto_pagina = sessao.pagina.locator("body").inner_text(timeout=3000).lower()
    except Exception:
        return False

    if not any(t in texto_pagina for t in TEXTOS_JA_LOGADO):
        return False

    print(f"[{tribunal['nome']}] Tela 'Você já está logado' detectada — tentando prosseguir automaticamente...")

    try:
        links = sessao.pagina.locator("a[href]")
        total = links.count()
        for i in range(total):
            link = links.nth(i)
            try:
                if link.is_visible():
                    href_link = link.get_attribute("href")
                    _clicar_com_captura_de_aba(sessao, tribunal, link)
                    _reforcar_com_href(sessao, tribunal, href_link)
                    return True
            except Exception:
                continue
    except Exception:
        pass

    try:
        botoes = sessao.pagina.locator("button")
        total = botoes.count()
        for i in range(total):
            botao = botoes.nth(i)
            try:
                if botao.is_visible():
                    _clicar_com_captura_de_aba(sessao, tribunal, botao)
                    return True
            except Exception:
                continue
    except Exception:
        pass

    print(f"[{tribunal['nome']}] Não consegui clicar em nada na tela 'já logado'.")
    return False


def fazer_login_portal_click(sessao, tribunal):
    """Tribunais com 'login_portal_click': True: tenta primeiro o
    caminho simples (goto direto pra url_login, igual aos outros
    tribunais) — alguns WAFs que exigem clique de verdade podem não
    se aplicar dependendo do ambiente/IP de onde o bot roda. Só cai
    pro portal+clique (o caminho mais sustentável pra rodar sozinho,
    sem depender de link de uso único) se o simples falhar."""
    print(f"[{tribunal['nome']}] Tentando caminho simples primeiro (goto direto pra url_login)...")
    if navegar_com_retry(sessao.pagina, tribunal["url_login"], tentativas=2, timeout=45000):
        sessao.pagina.wait_for_timeout(1500)
        verificar_e_aguardar_cloudflare(sessao)
        print(f"[{tribunal['nome']}] Caminho simples funcionou — sem precisar do portal.")
        return _continuar_login_trf3_apos_abrir_tela(sessao, tribunal)

    print(f"[{tribunal['nome']}] Caminho simples falhou — caindo pro portal+clique.")
    return _fazer_login_via_portal(sessao, tribunal)


def _fazer_login_via_portal(sessao, tribunal):
    """Fluxo de portal+clique: o servidor bloqueia navegação direta
    (page.goto) pro domínio do PJe — só libera com um clique de
    verdade dentro do navegador. Por isso passamos pelo portal
    público (tribunal['url_portal']) e clicamos no link
    (tribunal['url_destino_clique']), em vez de ir direto pra
    url_login."""

    url_portal = tribunal["url_portal"]
    url_destino_clique = tribunal["url_destino_clique"]
    texto_link_portal = tribunal["texto_link_portal"]
    url_login_direta_fallback = tribunal.get("url_login_direta_fallback")
    texto_clique_preliminar = tribunal.get("texto_clique_preliminar")

    if not navegar_com_retry(sessao.pagina, url_portal, tentativas=5, timeout=120000):
        print(f"[{tribunal['nome']}] Não foi possível abrir o portal — pulando este tribunal.")
        return False

    sessao.pagina.wait_for_timeout(3000)
    try:
        sessao.pagina.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass
    verificar_e_aguardar_cloudflare(sessao)

    # Alguns portais (ex.: TJRN) escondem o link de acesso atrás de
    # uma aba/botão que precisa ser clicado primeiro pra revelar o
    # link — tribunal['texto_clique_preliminar'] é o texto/regex
    # desse botão preliminar, se houver.
    if texto_clique_preliminar:
        try:
            alvo_preliminar = sessao.pagina.get_by_text(texto_clique_preliminar)
            if alvo_preliminar.count() > 0:
                alvo_preliminar.first.click()
                print(f"[{tribunal['nome']}] Clique preliminar no portal feito — aguardando link de acesso aparecer...")
                sessao.pagina.wait_for_timeout(1500)
        except Exception as erro:
            print(f"[{tribunal['nome']}] Aviso: clique preliminar no portal falhou: {erro}")

    clicou_automatico = False
    seletor_href = f"a[href='{url_destino_clique}']"
    try:
        sessao.pagina.wait_for_selector(seletor_href, state="visible", timeout=15000)
    except Exception:
        pass

    alvo_clique = sessao.pagina.locator(seletor_href)
    if alvo_clique.count() == 0:
        alvo_clique = sessao.pagina.get_by_text(texto_link_portal)

    if alvo_clique.count() > 0:
        url_antes_do_clique = sessao.pagina.url
        try:
            alvo_clique.first.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            pass

        # Tira o target="_blank" do link antes de clicar, pra forçar a
        # navegação a acontecer na MESMA aba — ainda é um clique de
        # verdade (então o WAF do TRF3 deve aceitar), só que sem o
        # risco da aba nova ficar presa em about:blank.
        try:
            alvo_clique.first.evaluate("el => el.removeAttribute('target')")
        except Exception:
            pass

        try:
            alvo_clique.first.click()
            sessao.pagina.wait_for_url(lambda url: url != url_antes_do_clique, timeout=20000)
            clicou_automatico = True
            print(f"[{tribunal['nome']}] Clique no portal funcionou — navegou na mesma aba. URL:", sessao.pagina.url)
        except Exception:
            print(f"[{tribunal['nome']}] Clique na mesma aba não navegou — tentando capturar aba nova (fallback)...")

        if not clicou_automatico:
            try:
                with sessao.contexto.expect_page(timeout=8000) as info_pagina_nova:
                    alvo_clique.first.click()
                pagina_nova = info_pagina_nova.value
                try:
                    pagina_nova.wait_for_url(lambda url: url not in ("about:blank", ""), timeout=15000)
                except Exception:
                    pass
                if pagina_nova.url in ("about:blank", ""):
                    print(f"[{tribunal['nome']}] Aba nova abriu mas ficou em about:blank — não vou usar ela.")
                    try:
                        pagina_nova.close()
                    except Exception:
                        pass
                else:
                    pagina_nova.on("dialog", tratar_dialogo)
                    sessao.pagina = pagina_nova
                    clicou_automatico = True
                    print(f"[{tribunal['nome']}] Clique no portal funcionou — abriu em aba nova. URL:", pagina_nova.url)
            except Exception:
                pass

    if not clicou_automatico and url_login_direta_fallback:
        print(f"[{tribunal['nome']}] Clique automático não funcionou — abrindo diretamente: {url_login_direta_fallback}")
        if navegar_com_retry(sessao.pagina, url_login_direta_fallback, tentativas=3, timeout=60000):
            clicou_automatico = True

    if not clicou_automatico:
        print(f"[{tribunal['nome']}] Não consegui abrir a tela de login de nenhuma forma — pulando este tribunal.")
        diagnosticar_tela_de_login(sessao.pagina, f"{tribunal['nome']}_erro_abrir_login")
        return False

    for pagina_extra in list(sessao.contexto.pages):
        if pagina_extra is not sessao.pagina:
            try:
                pagina_extra.close()
            except Exception:
                pass

    return _continuar_login_trf3_apos_abrir_tela(sessao, tribunal)


def _continuar_login_trf3_apos_abrir_tela(sessao, tribunal):
    """Parte comum aos dois caminhos de entrada do TRF3 (URL direta
    de SSO ou portal+clique): a partir daqui já estamos numa página
    que deve ser a tela de login/SSO (ou já logada)."""
    try:
        sessao.pagina.wait_for_load_state("domcontentloaded", timeout=30000)
    except Exception:
        pass
    sessao.pagina.wait_for_timeout(1000)
    sessao.pagina.bring_to_front()

    sessao.pagina.wait_for_timeout(2000)
    verificar_e_aguardar_cloudflare(sessao)

    if "sso.cloud.pje.jus.br" not in sessao.pagina.url:
        print("Sessão SSO já ativa — login automático (sem certificado nem senha).")
        return True

    if not _preencher_usuario_senha_e_2fa(sessao, tribunal):
        return False

    for segundo in range(60):
        if "sso.cloud.pje.jus.br" not in sessao.pagina.url:
            print(f"[{tribunal['nome']}] Login concluído com SUCESSO via USUÁRIO E SENHA.")
            return True

        if tentar_passar_tela_ja_logado(sessao, tribunal):
            sessao.pagina.wait_for_timeout(2000)
            if "sso.cloud.pje.jus.br" not in sessao.pagina.url:
                print(f"[{tribunal['nome']}] Login concluído com SUCESSO (após passar pela tela 'já logado').")
                return True
            continue

        sessao.pagina.wait_for_timeout(1000)

    print(f"[{tribunal['nome']}] ERRO: não saiu da tela de SSO após tentar login via usuário e senha.")
    print("URL atual:", sessao.pagina.url)
    diagnosticar_tela_de_login(sessao.pagina, f"{tribunal['nome']}_erro_pos_2fa")
    return False


def fazer_login(sessao, tribunal):
    print()
    print("==========================================")
    print(f" LOGIN — {tribunal['nome']}")
    print("==========================================")

    if tribunal.get("login_portal_click"):
        return fazer_login_portal_click(sessao, tribunal)
    return fazer_login_simples(sessao, tribunal)
# ============================================================
# FUNÇÃO — VERIFICAR PROCESSO NO FIREBASE
# ============================================================
def processo_existe_no_firebase(numero):
    if not numero:
        return False
    for tentativa in range(2):
        try:
            ref = fm.client().collection("processos").document(numero)
            documento = fm.get(ref)
            if documento.exists:
                print(f"[JÁ EXISTE] {numero}")
                return True
            print(f"[NOVO] {numero}")
            return False
        except ResourceExhausted:
            # fm.get já marcou o projeto atual como esgotado e trocou
            # o ativo — tenta de novo (uma vez) com o próximo projeto.
            continue
        except Exception as erro:
            print("ERRO AO CONSULTAR FIREBASE:", type(erro).__name__, erro)
            return True
    print("ERRO AO CONSULTAR FIREBASE: cota esgotada em todos os projetos configurados.")
    return True
# ============================================================
# FUNÇÃO — SALVAR PROCESSO NO FIREBASE
# ============================================================
def salvar_processo_no_firebase(dados, tribunal_origem):
    numero = dados.get("numero")
    if not numero:
        print()
        print("ERRO: processo sem número.")
        return False
    dados["tribunal_origem_consulta"] = tribunal_origem
    dados["dataCaptacao"] = datetime.now()

    # Campo de expiração para a política de TTL do Firestore (ver
    # instruções abaixo) — o Firestore apaga o documento sozinho
    # depois dessa data, sem precisar de nenhum código rodando.
    dados["dataExpiracao"] = dados["dataCaptacao"] + timedelta(days=10)

    # A data de autuação/distribuição vem em formatos diferentes em
    # cada tribunal e normalizar todos é frágil demais para valer a
    # pena. Como a deduplicação usa o NÚMERO do processo (não a
    # data) — um processo de ontem que já está no Firebase não seria
    # "pego como novo" de qualquer forma — gravamos a data de
    # distribuição igual à data de captação, que é confiável e
    # sempre no mesmo formato.
    dados["data_distribuicao"] = dados["dataCaptacao"]

    for tentativa in range(2):
        try:
            ref = fm.client().collection("processos").document(numero)
            fm.set(ref, dados)
            print()
            print("==========================================")
            print(" PROCESSO SALVO NO FIREBASE")
            print("==========================================")
            print("Número:", dados.get("numero"))
            print("Réu:", dados.get("reu"))
            print("CPF/CNPJ:", dados.get("documento_reu"))
            print("Classe:", dados.get("classe"))
            print("Tribunal (extraído do número):", dados.get("tribunal"))
            print("Tribunal (consulta):", tribunal_origem)
            print("Autor:", dados.get("autor"))
            print("Valor:", dados.get("valor_causa"))
            print("Autuação (= data de captação):", dados.get("data_distribuicao"))
            print("Data de captação:", dados.get("dataCaptacao"))
            return True
        except ResourceExhausted:
            # fm.set já marcou o projeto atual como esgotado e trocou
            # o ativo — tenta de novo (uma vez) com o próximo projeto.
            continue
        except Exception as erro:
            print()
            print("ERRO AO SALVAR NO FIREBASE:", type(erro).__name__, erro)
            return False
    print()
    print("ERRO AO SALVAR NO FIREBASE: cota esgotada em todos os projetos configurados.")
    return False
# ============================================================
# PADRÃO — TAG DE PAPEL DA PARTE (reconhece parênteses aninhados)
# ============================================================
# O PJe costuma marcar o papel da parte como "(EXECUTADO(A))",
# "(AUTOR(A))", "(REQUERIDO(A))" etc — com um "(A)" aninhado dentro
# do parêntese externo para indicar flexão de gênero. Um padrão
# simples tipo \([A-ZÀ-Ú]{2,}\) não reconhece esse aninhamento e
# acaba capturando muito mais texto do que devia (ícones, datas,
# movimentações) até achar outro parêntese qualquer mais à frente.
PADRAO_TAG_PAPEL = r"\([A-ZÀ-Ú]{2,}(?:\([A-Za-zÀ-ú]+\))?\)"
# ============================================================
# FUNÇÃO — EXTRAIR DOCUMENTO (CPF/CNPJ) COM VALIDAÇÃO
# ============================================================
def extrair_documento_do_texto(texto, inicio, fim=None):
    fim = fim if fim is not None else inicio + 1500
    trecho = texto[inicio:fim]
    padrao_doc = re.compile(
        r"(?:CPF|CNPJ)\s*(?:/\s*(?:CPF|CNPJ))?\s*:?\s*"
        r"(\d{3}\.?\d{3}\.?\d{3}-?\d{2}|\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2})",
        re.IGNORECASE,
    )
    for match in padrao_doc.finditer(trecho):
        numero = match.group(1).strip()
        digitos = re.sub(r"\D", "", numero)
        if len(digitos) in (11, 14):
            return numero
    return None
# ============================================================
# FUNÇÃO — EXTRAIR DADOS DO PROCESSO
# ============================================================
def extrair_dados_processo(pagina):
    print()
    print("==========================================")
    print(" EXTRAINDO DADOS DO PROCESSO")
    print("==========================================")
    padrao_processo_check = re.compile(r"\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}")
    texto = ""
    for tentativa in range(2):
        print()
        print(f"Procurando 'Mais detalhes' (tentativa {tentativa + 1})...")
        mais_detalhes = pagina.locator('a[title="Mais detalhes"]')
        quantidade_detalhes = mais_detalhes.count()
        print("Links 'Mais detalhes' encontrados:", quantidade_detalhes)
        if quantidade_detalhes > 0:
            try:
                mais_detalhes.first.click()
                print("Painel de detalhes clicado.")
            except Exception as erro:
                print("Erro ao abrir 'Mais detalhes':", erro)
        else:
            print("Aviso: 'Mais detalhes' não encontrado.")
        for _ in range(16):
            pagina.wait_for_timeout(500)
            try:
                texto = pagina.locator("body").inner_text().replace("\xa0", " ")
            except Exception:
                continue
            if padrao_processo_check.search(texto):
                break
        if padrao_processo_check.search(texto):
            break
        print("Conteúdo do processo ainda não apareceu, tentando novamente...")
    print()
    print("--- TEXTO VISÍVEL APÓS ABRIR DETALHES ---")
    print(texto)
    dados = {
        "numero": None,
        "reu": None,
        "documento_reu": None,
        "classe": None,
        "tribunal": None,
        "autor": None,
        "valor_causa": None,
        "data_distribuicao": None,
    }
    padrao_processo = re.compile(r"\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}")
    resultado = padrao_processo.search(texto)
    if resultado:
        dados["numero"] = resultado.group(0)
    tribunais_codigo_estadual = {
        "01": "TJAC", "02": "TJAL", "03": "TJAP", "04": "TJAM", "05": "TJBA",
        "06": "TJCE", "07": "TJDFT", "08": "TJES", "09": "TJGO", "10": "TJMA",
        "11": "TJMT", "12": "TJMS", "13": "TJMG", "14": "TJPA", "15": "TJPB",
        "16": "TJPR", "17": "TJPE", "18": "TJPI", "19": "TJRJ", "20": "TJRN",
        "21": "TJRS", "22": "TJRO", "23": "TJRR", "24": "TJSC", "25": "TJSE",
        "26": "TJSP", "27": "TJTO",
    }
    tribunais_codigo_federal = {
        "01": "TRF1", "02": "TRF2", "03": "TRF3",
        "04": "TRF4", "05": "TRF5", "06": "TRF6",
    }
    if dados["numero"]:
        partes = dados["numero"].split(".")
        if len(partes) >= 4:
            segmento = partes[2]
            codigo = partes[3]
            if segmento == "8":
                dados["tribunal"] = tribunais_codigo_estadual.get(codigo, f"Código {codigo}")
            elif segmento == "4":
                dados["tribunal"] = tribunais_codigo_federal.get(codigo, f"TRF{codigo}")
            else:
                dados["tribunal"] = f"Segmento {segmento} / Código {codigo}"
    resultado = re.search(r"Classe judicial\s*\n(.+?)\s*\nAssunto", texto, re.IGNORECASE)
    if resultado:
        dados["classe"] = resultado.group(1).strip()
    resultado = re.search(
        r"Autuação\s*\n(.+?)\s*\nÚltima distribuição", texto, re.IGNORECASE
    )
    if resultado:
        dados["data_distribuicao"] = resultado.group(1).strip()
    resultado = re.search(
        r"Valor da causa\s*\n(.+?)\s*\n", texto, re.IGNORECASE
    )
    if resultado:
        dados["valor_causa"] = resultado.group(1).strip()
    resultado = re.search(
        r"Polo ativo\s*\n(.+?)" + PADRAO_TAG_PAPEL, texto, re.DOTALL
    )
    if resultado:
        linha_autor = re.sub(r"\s+", " ", resultado.group(1).strip())
        linha_autor = re.sub(
            r"\s*-\s*(CPF|CNPJ):\s*[\d./-]+", "", linha_autor, flags=re.IGNORECASE
        )
        dados["autor"] = linha_autor.strip()

    match_polo_passivo = re.search(r"Polo passivo\s*\n", texto, re.IGNORECASE)
    if match_polo_passivo:
        inicio_bloco = match_polo_passivo.end()

        resultado_nome = re.search(
            r"(.+?" + PADRAO_TAG_PAPEL + r")", texto[inicio_bloco:], re.IGNORECASE | re.DOTALL
        )
        if resultado_nome:
            bloco_completo = resultado_nome.group(1)
            linha_reu = re.sub(r"\s+", " ", bloco_completo.strip())
            linha_reu = re.sub(
                r"\s*-?\s*(CPF/CNPJ|CPF|CNPJ)\s*:?\s*[\d./-]+",
                "",
                linha_reu,
                flags=re.IGNORECASE,
            )
            linha_reu = re.sub(r"\s*" + PADRAO_TAG_PAPEL + r"\s*$", "", linha_reu).strip()
            dados["reu"] = linha_reu.strip()

            fim_bloco = inicio_bloco + resultado_nome.end()
            dados["documento_reu"] = extrair_documento_do_texto(
                texto, inicio_bloco, fim_bloco + 200
            )
        else:
            dados["documento_reu"] = extrair_documento_do_texto(texto, inicio_bloco)

    return dados
# ============================================================
# FUNÇÃO — PREENCHER VALOR MÍNIMO
# ============================================================
def preencher_valor_minimo(pagina, tribunal_nome):
    try:
        valor_minimo = VALOR_MINIMO_CAUSA_POR_TRIBUNAL.get(tribunal_nome, "1000000")
        print(f"Valor mínimo configurado para {tribunal_nome}: {valor_minimo}")
        valor = pagina.locator("#fPP\\:valorDaCausaDecoration\\:valorCausaInicial")
        valor.click()
        valor.press("Control+A")
        valor.press("Backspace")
        valor.type(valor_minimo)
        valor.press("Tab")
        pagina.wait_for_timeout(500)
        print("Valor exibido no campo:", valor.input_value())
        return True
    except Exception as erro:
        print("ERRO ao preencher valor mínimo:", erro)
        return False
# ============================================================
# FUNÇÃO — SELECIONAR SUGESTÃO DO AUTOCOMPLETE
# ============================================================
def selecionar_sugestao_autocomplete(pagina, texto_esperado):
    seletores_sugestao = [
        ".ui-autocomplete-items li",
        ".ui-autocomplete-panel li",
        "ul[id$='_panel'] li",
        "[role='option']",
    ]
    for seletor in seletores_sugestao:
        opcoes = pagina.locator(seletor)
        try:
            total = opcoes.count()
        except Exception:
            total = 0
        if total == 0:
            continue
        for i in range(total):
            opcao = opcoes.nth(i)
            try:
                if not opcao.is_visible():
                    continue
                texto_opcao = opcao.inner_text().strip()
            except Exception:
                continue
            if texto_esperado.lower() in texto_opcao.lower():
                try:
                    opcao.click()
                    return True
                except Exception:
                    continue
        try:
            opcoes.first.click()
            return True
        except Exception:
            continue
    return False
# ============================================================
# FUNÇÃO — PREENCHER CLASSE
# ============================================================
def preencher_classe(pagina, classe):
    campo = pagina.get_by_label(re.compile("Classe Judicial", re.IGNORECASE))
    try:
        if campo.count() == 0:
            campo = pagina.locator("#fPP\\:j_id268\\:classeJudicial")
    except Exception:
        campo = pagina.locator("#fPP\\:j_id268\\:classeJudicial")
    try:
        campo.click()
        campo.press("Control+A")
        campo.press("Backspace")
    except Exception as erro:
        print("ERRO ao limpar campo de Classe Judicial:", erro)
        return False
    if len(classe) > 6:
        texto_parcial = classe[:-3]
    else:
        texto_parcial = classe[:max(1, len(classe) - 1)]
    try:
        campo.type(texto_parcial, delay=80)
    except Exception as erro:
        print("ERRO ao digitar Classe Judicial:", erro)
        return False
    pagina.wait_for_timeout(1000)
    selecionou_sugestao = selecionar_sugestao_autocomplete(pagina, classe)
    if selecionou_sugestao:
        print(f"Classe '{classe}' selecionada via sugestão de autocomplete.")
        return True
    print(f"Aviso: nenhuma sugestão encontrada para '{classe}' — completando o texto manualmente.")
    try:
        campo.type(classe[len(texto_parcial):], delay=50)
    except Exception:
        pass
    return True
# ============================================================
# FUNÇÃO — AGUARDAR OVERLAY AJAX
# ============================================================
def aguardar_overlay_ajax(pagina, timeout_ms=45000):
    seletores_overlay = [".rich-mp-container"]
    for seletor in seletores_overlay:
        try:
            overlay = pagina.locator(seletor)
            if overlay.count() > 0 and overlay.first.is_visible():
                print("Aguardando overlay de carregamento sumir...")
                overlay.first.wait_for(state="hidden", timeout=timeout_ms)
        except Exception:
            continue
# ============================================================
# FUNÇÃO — FECHAR POPUPS BLOQUEANTES (avisos informativos)
# ============================================================
def fechar_popups_bloqueantes(pagina, timeout_ms=15000):
    # Alguns tribunais mostram avisos informativos (ex: TJES —
    # "certificado próximo de expirar") que ficam bloqueando cliques
    # em campos até serem fechados. Cobre qualquer modal RichFaces
    # visível (classe diferente da usada em aguardar_overlay_ajax),
    # tentando fechar com um botão comum; se não achar botão, espera
    # o overlay sumir sozinho.
    seletores_mascara = [
        ".rich-mpnl-mask-div-opaque",
        ".rich-mpnl-mask-div",
        "[id$='MaskDiv']",
    ]
    for seletor in seletores_mascara:
        try:
            mascara = pagina.locator(seletor)
            if mascara.count() == 0 or not mascara.first.is_visible():
                continue
        except Exception:
            continue

        print(f"Popup bloqueante detectado ({seletor}) — tentando fechar...")

        fechou = False
        for texto_botao in ["Fechar", "OK", "Entendi", "Cancelar", "Não"]:
            try:
                botao = pagina.get_by_role("button", name=re.compile(texto_botao, re.IGNORECASE))
                if botao.count() > 0 and botao.first.is_visible():
                    botao.first.click()
                    print(f"Clicado em '{texto_botao}' para fechar o popup.")
                    fechou = True
                    break
            except Exception:
                continue

        if not fechou:
            try:
                mascara.first.wait_for(state="hidden", timeout=timeout_ms)
            except Exception:
                # Último recurso: força esconder via JS. O popup é só
                # um aviso informativo (ex: certificado expirando) —
                # não precisamos que ele seja "resolvido" de verdade,
                # só que pare de bloquear cliques nos campos.
                print("Popup não fechou por botão nem sumiu sozinho — forçando ocultar via JS.")
                try:
                    mascara.first.evaluate("el => el.style.display = 'none'")
                except Exception as erro:
                    print("Não foi possível ocultar o popup via JS:", erro)
# ============================================================
# FUNÇÃO — PREENCHER FILTROS
# ============================================================
def preencher_filtros(pagina, classe, tribunal_nome):
    print(f"Preenchendo filtros — tribunal: {tribunal_nome} | classe: {classe}")
    fechar_popups_bloqueantes(pagina)
    aguardar_overlay_ajax(pagina)
    ok_valor = preencher_valor_minimo(pagina, tribunal_nome)
    aguardar_overlay_ajax(pagina)
    ok_classe = preencher_classe(pagina, classe)
    return ok_valor and ok_classe
# ============================================================
# FUNÇÃO — PESQUISAR PROCESSOS
# ============================================================
def pesquisar_processos(pagina, tribunal_nome=None, classe_nome=None):
    botao_pesquisar = pagina.locator("#fPP\\:searchProcessos")
    botao_pesquisar.wait_for(state="visible", timeout=120000)
    botao_pesquisar.click()
    pagina.wait_for_timeout(10000)
    padrao_processo = re.compile(r"\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}")
    processos = pagina.locator("a").filter(has_text=padrao_processo)
    quantidade = processos.count()
    print(f"Processos encontrados: {quantidade}")
    if quantidade == 0:
        salvar_screenshot_diagnostico(pagina, tribunal_nome, classe_nome)
    return processos, quantidade
# ============================================================
# FUNÇÃO — IR PARA UMA PÁGINA ESPECÍFICA DE RESULTADOS
# ============================================================
def ir_para_pagina_resultados(pagina, numero_pagina):
    print(f"Tentando navegar para a página {numero_pagina} de resultados...")
    try:
        link_pagina = pagina.get_by_text(str(numero_pagina), exact=True)
        total = link_pagina.count()
    except Exception:
        total = 0

    if total == 0:
        print(f"Não encontrei o link/botão da página {numero_pagina} na paginação.")
        return False

    try:
        link_pagina.last.click()
        pagina.wait_for_timeout(3000)
        print(f"Cliquei na página {numero_pagina}.")
        return True
    except Exception as erro:
        print(f"Erro ao clicar na página {numero_pagina}:", erro)
        return False
# ============================================================
# FUNÇÃO — SCREENSHOT DE DIAGNÓSTICO
# ============================================================
def salvar_screenshot_diagnostico(pagina, tribunal_nome, classe_nome):
    try:
        os.makedirs("debug_zero_resultados", exist_ok=True)
        agora = datetime.now().strftime("%Y%m%d_%H%M%S")
        nome_arquivo = f"debug_zero_resultados/{tribunal_nome}_{classe_nome}_{agora}.png"
        nome_arquivo = nome_arquivo.replace(" ", "_")
        pagina.screenshot(path=nome_arquivo, full_page=True)
        print("Screenshot de diagnóstico salvo em:", nome_arquivo)
    except Exception as erro:
        print("Não foi possível salvar screenshot:", erro)
# ============================================================
# FUNÇÃO — NORMALIZAR NOME DE CLASSE (ignora código entre parênteses)
# ============================================================
def normalizar_classe(nome_classe):
    sem_codigo = re.sub(r"\s*\(\d+\)\s*$", "", (nome_classe or "")).strip()
    return sem_codigo.lower()


def processar_pagina_de_processo(processo_pagina, tribunal_nome, classe, classe_exigida=None):
    if processo_pagina is None:
        print("ERRO: nenhuma janela de processo encontrada.")
        return
    processo_pagina.on("dialog", tratar_dialogo)
    try:
        processo_pagina.wait_for_load_state("domcontentloaded", timeout=120000)
    except Exception:
        pass
    processo_pagina.wait_for_timeout(2000)
    try:
        dados = extrair_dados_processo(processo_pagina)
    except Exception as erro:
        print("ERRO NA EXTRAÇÃO:", type(erro).__name__, erro)
        try:
            processo_pagina.close()
        except Exception:
            pass
        return
    if not dados.get("numero"):
        print("Número não encontrado de primeira, tentando novamente em 3s...")
        processo_pagina.wait_for_timeout(3000)
        try:
            dados = extrair_dados_processo(processo_pagina)
        except Exception as erro:
            print("Erro na segunda tentativa:", type(erro).__name__, erro)
    if not dados.get("numero"):
        print("ERRO: não foi possível identificar o número do processo.")
        try:
            processo_pagina.close()
        except Exception:
            pass
        return

    if classe_exigida:
        classe_encontrada = normalizar_classe(dados.get("classe"))
        classe_esperada = normalizar_classe(classe_exigida)
        if classe_encontrada != classe_esperada:
            print(
                f"Classe do processo é '{dados.get('classe')}', mas "
                f"esperávamos exatamente '{classe_exigida}'. "
                "Pulando (não será salvo) — seguindo para a próxima pesquisa."
            )
            try:
                processo_pagina.close()
            except Exception:
                pass
            return

    numero = dados["numero"]
    if processo_existe_no_firebase(numero):
        print("Processo já estava cadastrado. Não será salvo novamente.")
    else:
        salvar_processo_no_firebase(dados, tribunal_nome)
    try:
        processo_pagina.close()
        print("Janela do processo fechada.")
    except Exception as erro:
        print("Erro ao fechar janela do processo:", erro)
# ============================================================
# FUNÇÃO — PROCESSAR COMBINAÇÃO
# ============================================================
def processar_combinacao(pagina, tribunal, classe):
    if not preencher_filtros(pagina, classe, tribunal["nome"]):
        return

    processos, quantidade = pesquisar_processos(pagina, tribunal["nome"], classe)
    if quantidade == 0:
        return

    regra = REGRA_SELECAO_PROCESSO.get((tribunal["nome"], classe))

    if regra and "pagina" in regra:
        if not ir_para_pagina_resultados(pagina, regra["pagina"]):
            print(
                f"Não foi possível navegar até a página {regra['pagina']} "
                "de resultados — pulando esta pesquisa."
            )
            return

        padrao_processo = re.compile(r"\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}")
        processos = pagina.locator("a").filter(has_text=padrao_processo)
        quantidade = processos.count()

        indice = regra["indice_na_pagina"]
        if quantidade <= indice:
            print(
                f"Página {regra['pagina']} tem apenas {quantidade} processo(s) "
                f"— não há um {indice + 1}º processo. Pulando esta pesquisa."
            )
            return

        primeiro = processos.nth(indice)
        classe_exigida = regra.get("classe_exigida")

    elif regra:
        indice = regra.get("indice", 0)
        if quantidade <= indice:
            print(
                f"Encontrados apenas {quantidade} processo(s) — não há um "
                f"{indice + 1}º processo para {tribunal['nome']} / {classe}. "
                "Pulando esta pesquisa."
            )
            return
        primeiro = processos.nth(indice)
        classe_exigida = regra.get("classe_exigida")

    else:
        primeiro = processos.nth(0)
        classe_exigida = None

    numero_processo = primeiro.inner_text().strip()

    if processo_existe_no_firebase(numero_processo):
        return

    print()
    print("==========================================")
    print(" PROCESSO NOVO DETECTADO")
    print("==========================================")
    print("Tribunal:", tribunal["nome"])
    print("Classe:", classe)
    print("Número:", numero_processo)

    try:
        with pagina.expect_popup(timeout=30000) as popup_info:
            primeiro.click()
        processo_pagina = popup_info.value
    except Exception as erro:
        print("Erro ao abrir processo:", type(erro).__name__, erro)
        return

    processar_pagina_de_processo(
        processo_pagina, tribunal["nome"], classe, classe_exigida=classe_exigida
    )
# ============================================================
# FUNÇÃO — ABRIR TELA DE CONSULTA (com fallback de URL)
# ============================================================
def _tentar_clicar_para_consulta_trf3(sessao, tribunal):
    """TRF3: goto() direto pra ConsultaProcesso dá
    ERR_HTTP2_PROTOCOL_ERROR — procura um link de verdade na página
    logada (menu) e clica nele, do mesmo jeito que funcionou pro
    login. Retorna True se achou e clicou em algo."""
    try:
        candidatos = sessao.pagina.locator(
            "a[href*='ConsultaProcesso'], a:has-text('Consulta Processual'), "
            "a:has-text('Consulta processual')"
        )
        total = candidatos.count()
    except Exception:
        total = 0

    for i in range(total):
        link = candidatos.nth(i)
        try:
            if not link.is_visible():
                continue
            try:
                link.evaluate("el => el.removeAttribute('target')")
            except Exception:
                pass
            url_antes = sessao.pagina.url
            try:
                link.click()
                sessao.pagina.wait_for_url(lambda url: url != url_antes, timeout=15000)
                print(f"[{tribunal['nome']}] Cliquei num link pra abrir a consulta (mesma aba).")
                return True
            except Exception:
                _clicar_com_captura_de_aba(sessao, tribunal, link)
                print(f"[{tribunal['nome']}] Cliquei num link pra abrir a consulta (com captura de aba).")
                return True
        except Exception:
            continue
    return False


def abrir_tela_de_consulta(sessao, tribunal):
    # Alguns tribunais (TJES, TJRN, TJPI, TJRO) demoram mais para
    # carregar essa tela — timeout generoso para não desistir cedo
    # demais. O retry de rede já cobre falhas mais rápidas.
    TIMEOUT_BOTAO_PESQUISA = 60000

    def campo_de_busca_visivel():
        try:
            verificar_e_aguardar_cloudflare(sessao)
            sessao.pagina.locator("#fPP\\:searchProcessos").wait_for(
                state="visible", timeout=TIMEOUT_BOTAO_PESQUISA
            )
            return True
        except Exception as erro:
            print("Tela de consulta não ficou pronta a tempo:", erro)
            return False

    def tentar(url):
        if not navegar_com_retry(sessao.pagina, url, tentativas=2, timeout=60000):
            return False
        return campo_de_busca_visivel()

    if tribunal.get("login_portal_click"):
        # Alguns tribunais (TRF3, e agora confirmado o TJRN) bloqueiam
        # goto() "frio" direto pra tela de consulta com um Access
        # Denied do WAF (Akamai) — e o bloqueio não fica só nessa URL:
        # ele contamina a sessão/IP inteira por um tempo, derrubando
        # até a url_login que segundos antes funcionava. Por isso,
        # pra esses tribunais, NUNCA arriscamos esse goto primeiro —
        # tentamos achar e clicar num link de menu na própria página
        # logada (pós-login), que é navegação "de verdade" e não
        # dispara o bloqueio.
        print(f"[{tribunal['nome']}] Tentando achar um link de consulta pra clicar (evitando o goto direto que trava o WAF)...")
        if _tentar_clicar_para_consulta_trf3(sessao, tribunal) and campo_de_busca_visivel():
            return True
        print(f"[{tribunal['nome']}] Não achei link pra clicar (ou não funcionou) — tentando goto direto como último recurso...")

    if tentar(tribunal["url_pesquisa"]):
        return True

    url_alternativa = tribunal.get("url_pesquisa_alternativa")
    if url_alternativa:
        print(f"Tentando URL alternativa de consulta para {tribunal['nome']}...")
        if tentar(url_alternativa):
            return True

    print(f"ERRO: não consegui abrir a tela de consulta em {tribunal['nome']}.")
    print("URL atual:", sessao.pagina.url)
    diagnosticar_tela_de_login(sessao.pagina, f"{tribunal['nome']}_consulta")
    return False


# ============================================================
# EXECUÇÃO PRINCIPAL
# ============================================================
with sync_playwright() as p:
    sessao = SessaoNavegador(p, HEADLESS, ARQUIVO_SESSAO)

    print()
    print("==========================================")
    print(" BOT PJE TJRN + TJPI + TJMA INICIADO")
    print("==========================================")
    print("Tribunais:", ", ".join(t["nome"] for t in TRIBUNAIS))
    for _t in TRIBUNAIS:
        print(f"  Classes ({_t['nome']}):", ", ".join(_t.get("classes", CLASSES_JUDICIAIS)))

    while True:
        print()
        print("##########################################")
        print(" NOVA VARREDURA COMPLETA")
        print("##########################################")

        try:
            for tribunal in TRIBUNAIS:
                nome_tribunal = tribunal["nome"]

                try:
                    logado = fazer_login(sessao, tribunal)
                except Exception as erro:
                    print(f"ERRO inesperado ao logar em {nome_tribunal}:", type(erro).__name__, erro)
                    logado = False

                sessao.salvar_sessao()

                if not logado:
                    print(f"Pulando {nome_tribunal} — falha no login.")
                    continue

                try:
                    abriu_consulta = abrir_tela_de_consulta(sessao, tribunal)
                except Exception as erro:
                    print(f"ERRO inesperado ao abrir tela de consulta em {nome_tribunal}:", erro)
                    abriu_consulta = False

                if not abriu_consulta:
                    continue

                classes_deste_tribunal = tribunal.get("classes", CLASSES_JUDICIAIS)

                for classe in classes_deste_tribunal:
                    print()
                    print(f"--- {nome_tribunal} / {classe} ---")
                    try:
                        processar_combinacao(sessao.pagina, tribunal, classe)
                    except Exception as erro:
                        print(f"ERRO ao processar {nome_tribunal} / {classe}:", type(erro).__name__, erro)

            print()
            print("==========================================")
            print("Varredura completa.")
            print(f"Aguardando {INTERVALO_ENTRE_VARREDURAS}s...")
            print("==========================================")
            time.sleep(INTERVALO_ENTRE_VARREDURAS)

        except KeyboardInterrupt:
            print()
            print("BOT ENCERRADO PELO USUÁRIO")
            break

        except Exception as erro:
            print()
            print("ERRO GERAL NO LOOP PRINCIPAL:", type(erro).__name__, erro)
            time.sleep(INTERVALO_ENTRE_VARREDURAS)

    sessao.fechar()
