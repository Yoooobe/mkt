#!/usr/bin/env python3
"""Yoobe.mail — motor de fila persistente, resiliência e cadência de campanhas.

Princípios (mesmos do proposal_monitor.py): a IA sugere, o CÓDIGO decide.
- Nada roda só em memória: todo trabalho vira item na tabela queue.
- Offline (sem rede/bateria): nenhum item é marcado como processado; tudo
  fica pending com backoff e o catch-up roda sozinho na reconexão.
- Envio de campanha só com campanha ATIVA (ativação explícita do Genal no
  dashboard), template aprovado, contato sem resposta na thread, máx. 3
  toques por contato a cada 30 dias, janela comercial, log em sent_log.txt.
"""
import base64
import json
import os
import random
import re
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from email.utils import parseaddr
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import ai
import db
import sync

sys.path.insert(0, str(db.PROPOSALS))
import email_templates  # noqa: E402  (shell(), EMAILS — biblioteca existente)

LOG_DIR = db.BASE / "logs"
LOG_DIR.mkdir(exist_ok=True)
GAP_AVISO_HORAS = 2
MAX_ATTEMPTS = 5
MAX_TOQUES_30D = 7          # teto de e-mails por contato a cada 30 dias (vale p/ todas as campanhas)
ESPACAMENTO_ENVIOS_MIN = 1  # 1 envio/min (≤540/dia na janela 9-18h, longe do teto ~2k/dia do Gmail)


def _load_env_files():
    """zapi-config.env + monitor_config.env + dashboard/.env (LaunchAgent não faz source)."""
    env = {}
    for f in (db.GWS_DIR / "zapi-config.env", db.PROPOSALS / "monitor_config.env", db.BASE / ".env"):
        if f.exists():
            for line in f.read_text().splitlines():
                m = re.match(r'^\s*([A-Z][A-Z0-9_]*)\s*=\s*"?([^"#]*)"?\s*(#.*)?$', line)
                if m:
                    env[m.group(1)] = m.group(2).strip()
    return env


ENV = _load_env_files()
DEFAULT_REPLY_TO_EMAIL = ENV.get("DEFAULT_REPLY_TO_EMAIL", "corporate@yoobe.co").strip().lower()

# Automação de respostas (reusa monitor_config.env). FASE 1 = AUTO_REPLY=off (só notifica).
AUTO_REPLY_ON = ENV.get("AUTO_REPLY", "off") == "on"
AUTO_CLASSES = set(x.strip() for x in ENV.get("AUTO_REPLY_CLASSES", "").split(",") if x.strip())
CONF_MIN = float(ENV.get("CONF_MIN", "0.8") or 0.8)
MAX_AUTOREPLIES_DIA = int(ENV.get("MAX_AUTOREPLIES_DIA", "10") or 10)
MAX_AUTOREPLIES_THREAD = int(ENV.get("MAX_AUTOREPLIES_POR_THREAD_DIA", "1") or 1)
STATE_DIR = db.PROPOSALS / "monitor_state"
DRAFTS_DIR = db.PROPOSALS / "drafts"
# Sistema / NDR: nunca tratar como resposta de lead (evita auto-reply para postmaster).
SKIP_FROM_REPLY = re.compile(
    r"mailer-daemon|postmaster@|noreply|no-reply|do-not-reply|donotreply|"
    r"notifications@|calendar-notification|mailerdaemon|delivery.?status",
    re.I)
BOUNCE_SUBJECT_RE = re.compile(
    r"undeliverable|delivery\s*status\s*notification|failure\s*notice|"
    r"returned\s*mail|mail\s*delivery\s*failed|não\s*foi\s*poss[ií]vel\s*entregar|"
    r"delivery\s*failure|non[- ]delivery",
    re.I)
# Destinatário falho em NDRs Microsoft / genéricos (além de Final-Recipient:).
BOUNCE_TO_RE = re.compile(
    r"(?:your message to|couldn't be delivered to|address not found for)\s*"
    r"<?([\w.+%-]+@[\w.-]+\.\w+)>?",
    re.I)
# trava de conteúdo: valor/desconto/contrato só passam nos caminhos aprovados
PRECO_RE = re.compile(r"R\$\s*\d|\bdesconto\b|\bcontrato\b|cláusula|clausula|\bgrátis\b", re.I)

# Rastreio pela API pública do site. Sem TRACKING_BASE_URL/SECRET, nada é injetado e as
# métricas de abertura/clique ficam zeradas — degradação graciosa.
TRACK_BASE = ENV.get("TRACKING_BASE_URL", "").rstrip("/")
TRACK_SECRET = ENV.get("TRACKING_SECRET", "")
TRACK_SYNC_KEY = ENV.get("TRACKING_SYNC_KEY", "")
TRACKING_ON = bool(TRACK_BASE and TRACK_SECRET)


def _add_utm(url, utm):
    """Acrescenta utm_* na URL de destino. Se já houver utm_source (UTM manual do
    template), não mexe em nada — o que o autor escreveu vence."""
    if not utm or not url.startswith(("http://", "https://")):
        return url
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
    parts = urlsplit(url)
    q = parse_qsl(parts.query, keep_blank_values=True)
    if any(k == "utm_source" for k, _ in q):
        return url
    q += [(k, v) for k, v in utm.items() if v]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), parts.fragment))


def inject_tracking(html, track_id, email, campaign_id, etapa, utm=None):
    """Embrulha CTAs externos em /click?token=... e injeta /open?token=... antes de </body>.
    UTMs entram na URL original ANTES do token — viajam dentro do HMAC e sobrevivem
    ao redirect da API."""
    if not TRACKING_ON:
        if utm:
            return re.sub(r'href="((?:https?://|mailto:)[^"]+)"',
                          lambda m: f'href="{_add_utm(m.group(1), utm)}"', html)
        return html

    def _tok(url=""):
        return db.sign_token({"sid": track_id, "email": email, "cid": campaign_id,
                              "etapa": etapa, "u": url}, TRACK_SECRET)

    def _wrap(m):
        url = m.group(1)
        if url.startswith(TRACK_BASE):     # links da própria API (ex.: unsubscribe) não são embrulhados
            return m.group(0)
        return f'href="{TRACK_BASE}/click?token={_tok(_add_utm(url, utm))}"'

    html = re.sub(r'href="((?:https?://|mailto:)[^"]+)"', _wrap, html)
    pixel = (f'<img src="{TRACK_BASE}/open?token={_tok()}" width="1" height="1" '
             f'alt="" style="display:none;border:0" />')
    if "</body>" in html.lower():
        idx = html.lower().rfind("</body>")
        return html[:idx] + pixel + html[idx:]
    return html + pixel


def log(msg):
    line = f"{db.now()} | {msg}"
    print(line)
    f = LOG_DIR / "engine.log"
    if f.exists() and f.stat().st_size > 2_000_000:  # rotação simples
        f.rename(LOG_DIR / "engine.log.1")
    with open(f, "a") as fh:
        fh.write(line + "\n")


def online():
    """Check de conectividade antes de cada ciclo. FORCE_OFFLINE=1 simula queda."""
    import os
    if os.environ.get("FORCE_OFFLINE") == "1":
        return False
    try:
        socket.getaddrinfo("gmail.googleapis.com", 443)
        return True
    except OSError:
        return False


def whatsapp(texto):
    url, token, phone = ENV.get("ZAPI_URL"), ENV.get("ZAPI_CLIENT_TOKEN"), ENV.get("ZAPI_PHONE")
    if not (url and token and phone):
        log("ZAPI não configurado — notificação pulada")
        return False
    r = subprocess.run(["curl", "-s", "-X", "POST", url,
                        "-H", "Content-Type: application/json",
                        "-H", f"Client-Token: {token}",
                        "-d", json.dumps({"phone": phone, "message": texto})],
                       capture_output=True, timeout=30)
    return r.returncode == 0


# Keep isolated profiles beside the project directory.  This matches the
# account-onboarding wizard and avoids placing credentials inside the project.
ACCOUNTS_DIR = db.GWS_DIR.parent / "gws_accounts"


def gws_home(sender):
    """HOME do perfil gws de um remetente (None = conta default/frank, HOME real)."""
    if not sender:
        return None
    slug = (sender.get("slug") or "").strip()
    return str(ACCOUNTS_DIR / slug) if slug else None


def _gws(args, body=None, home=None):
    cmd = ["gws", "gmail"] + args
    if body is not None:
        cmd += ["--json", json.dumps(body)]
    env = None
    if home:
        import os
        Path(home).mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["HOME"] = home
        env["GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND"] = "file"
        if db.dashboard_env() == "operacional":
            import gmail_oauth
            env["GOOGLE_WORKSPACE_CLI_TOKEN"] = gmail_oauth.access_token_for_home(home)
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=90, env=env)
    try:
        return json.loads(out.stdout)
    except Exception:
        return None


def preparar_perfil(sid):
    """Cria o diretório do perfil gws do remetente e copia o client_secret. O login
    (interativo, navegador) o Genal roda no terminal com o comando retornado."""
    import os
    import shutil
    s = db.sender_get(sid)
    if not s or not (s.get("slug") or "").strip():
        return {"ok": False, "erro": "a conta principal usa o perfil da máquina — "
                                     "conecte pelo botão Conectar conta (login OAuth)"}
    home = gws_home(s)
    cfg = os.path.join(home, ".config", "gws")
    os.makedirs(cfg, exist_ok=True)
    src = os.path.expanduser("~/.config/gws/client_secret.json")
    dst = os.path.join(cfg, "client_secret.json")
    if os.path.exists(src) and not os.path.exists(dst):
        shutil.copy(src, dst)
    return {"ok": True, "home": home,
            "comando": f"HOME={home} GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND=file gws auth login -s gmail",
            "instrucao": (f"No terminal, rode:  ! HOME={home} GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND=file gws auth login -s gmail  "
                          f"— logue no navegador como {s['email']} e depois clique em Verificar.")}


def _clear_gws_token_cache(home):
    """Remove token_cache.json stale (comum após re-login OAuth)."""
    if not home:
        # Conta default (slug vazio): perfil da máquina.
        path = Path.home() / ".config" / "gws" / "token_cache.json"
    else:
        path = Path(home) / ".config" / "gws" / "token_cache.json"
    try:
        if path.exists():
            path.unlink()
            return True
    except OSError:
        pass
    return False


def _gws_auth_error_hint(err):
    e = (err or "").lower()
    if "invalid_grant" in e or "invalid authentication" in e or "autherror" in e:
        return " — reconecte no Mac (Remetentes → Conectar) e autorize de novo"
    if "insufficient" in e and "scope" in e:
        return " — faça login de novo pedindo escopo Gmail (gws auth login -s gmail)"
    if "decrypt" in e:
        return " — credentials.enc de outra máquina; logout + login neste Mac"
    return ""


def sender_auth_status(sender):
    """🟢/🔴 — teste REAL de conexão: chama a API Gmail (getProfile) no perfil.

    Sem atalhos nem status 'mocado': antes a conta default retornava 'conectado'
    hardcoded e o envio saía por QUALQUER conta logada na máquina (o Gmail
    reescrevia o From). O getProfile valida as três coisas que importam de uma
    vez: credencial válida, conta EXATA do remetente e permissão efetiva da API
    (ex.: 403 de projeto GCP aparece aqui, não na hora do disparo)."""
    home = gws_home(sender)
    if home is not None and not (Path(home) / ".config" / "gws" / "credentials.enc").exists():
        if db.dashboard_env() != "operacional":
            return {"conectado": False, "motivo": "perfil ainda não criado/logado", "conta_real": ""}
        import gmail_oauth
        if not gmail_oauth.has_credentials(sender["id"]):
            return {"conectado": False, "motivo": "Gmail ainda não autorizado no painel remoto", "conta_real": ""}

    def _probe():
        try:
            return _gws(["users", "getProfile", "--params", '{"userId":"me"}'], home=home) or {}
        except Exception as ex:
            return {"error": {"message": f"não consegui checar a conexão ({str(ex)[:80]})"}}

    r = _probe()
    err = ((r.get("error") or {}).get("message") or "").strip()
    # Após re-OAuth, gws às vezes mantém token_cache.json velho → 401.
    # Uma limpeza + retry costuma bastar sem novo login.
    if err and re.search(r"invalid authentication|authError|invalid_grant", err, re.I):
        if _clear_gws_token_cache(home):
            r = _probe()
            err = ((r.get("error") or {}).get("message") or "").strip()

    conta_real = (r.get("emailAddress") or "").strip().lower()
    esperado = (sender.get("email") or "").strip().lower()
    if err:
        short = err.split("\n")[0][:160]
        return {"conectado": False, "conta_real": "",
                "motivo": f"API Gmail recusou: {short}{_gws_auth_error_hint(err)}"}
    if not conta_real:
        return {"conectado": False, "motivo": "precisa login", "conta_real": ""}
    if conta_real != esperado:
        return {"conectado": False, "conta_real": conta_real,
                "motivo": f"logado como {conta_real} — conecte a conta certa"}
    return {"conectado": True, "motivo": "ok", "conta_real": conta_real}


# ---------------- login OAuth pelo dashboard (link clicável/copiável) ----------------
# `gws auth login` abre um servidor local aguardando o callback OAuth. Rodamos o
# processo em background com BROWSER neutro (não abre navegador no servidor),
# capturamos a URL do stdout e devolvemos para a UI — o usuário clica/copia,
# autoriza como a conta do remetente e o processo grava as credenciais no perfil.

_LOGIN_PROCS = {}


def iniciar_login(sid):
    """Inicia o login OAuth do perfil do remetente e devolve a URL de autorização."""
    s = db.sender_get(sid)
    if not s:
        return {"ok": False, "erro": "remetente não encontrado"}
    home = gws_home(s)
    if home:
        preparar_perfil(sid)               # garante diretório + client_secret.json
    # Evita 401 por access token antigo misturado com credentials.enc novo.
    _clear_gws_token_cache(home)
    antigo = _LOGIN_PROCS.get(sid)
    if antigo and antigo["proc"].poll() is None:
        antigo["proc"].kill()
    env = os.environ.copy()
    if home:
        env["HOME"] = home
        env["GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND"] = "file"
    env["BROWSER"] = "/usr/bin/true"       # a URL é para o usuário, não para o servidor
    proc = subprocess.Popen(["gws", "auth", "login", "-s", "gmail"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, env=env)
    info = {"proc": proc, "url": "", "linhas": []}
    _LOGIN_PROCS[sid] = info

    def _ler():
        for linha in proc.stdout:
            info["linhas"].append(linha.rstrip())
            m = re.search(r"https://accounts\.google\.com/\S+", linha)
            if m and not info["url"]:
                info["url"] = m.group(0)
    threading.Thread(target=_ler, daemon=True).start()
    for _ in range(40):                    # espera a URL aparecer (até ~10s)
        if info["url"] or proc.poll() is not None:
            break
        time.sleep(0.25)
    if not info["url"]:
        detalhe = "\n".join(info["linhas"])[-400:] or "o gws não devolveu a URL de login"
        return {"ok": False, "erro": detalhe}
    return {"ok": True, "url": info["url"], "email": s["email"],
            "nota": (f"Abra o link NESTA máquina e autorize como {s['email']}. "
                     "Depois clique em Verificar.")}


# ---------------- templates: builtin (email_templates.py) + IA (SQLite) ----------------

def template_get(ref):
    """ref: 'builtin:trilha-1' ou 'ai:<id>'. Retorna dict normalizado ou None."""
    kind, _, key = ref.partition(":")
    if kind == "builtin":
        for e in email_templates.EMAILS:
            if e["id"] == key:
                return {"ref": ref, "nome": e["assunto"], "assunto": e["assunto"],
                        "preheader": e["preheader"], "cta": e["cta"], "cta_url": email_templates.WA,
                        "inner_html": e["inner"], "texto_plano": "", "status": "aprovado",
                        "grupo": e["grupo"], "cadencia": e.get("cadencia", "")}
    if kind == "ai":
        c = db.conn()
        r = c.execute("SELECT * FROM templates_ai WHERE id=?", (key,)).fetchone()
        c.close()
        if r:
            d = dict(r)
            d["ref"] = ref
            d["grupo"] = "Criados com IA" if d["origem"] == "ia" else "Campanha (seed)"
            d["cadencia"] = ""
            return d
    return None


def primeiro_nome(nome, fallback="tudo bem por aí"):
    """Só o primeiro nome para saudação em e-mails (nunca nome completo).

    Aceita CRM com nome completo / ALL CAPS / 'João e Maria' / títulos (Dr./Dra.).
    """
    raw = (nome or "").strip()
    if not raw:
        return fallback
    raw = raw.split(" e ")[0].split(",")[0].strip()
    parts = [p for p in raw.split() if p]
    # Remove títulos comuns no início
    titulos = {
        "dr", "dra", "sr", "sra", "srta", "prof", "profa",
        "dr.", "dra.", "sr.", "sra.", "srta.", "prof.", "profa.",
    }
    while parts and parts[0].lower() in titulos:
        parts = parts[1:]
    if not parts:
        return fallback
    first = parts[0]
    # CRM às vezes grava "LETICIA GOIS…" — capitaliza só quando vier monolítico
    if first.isupper() or first.islower():
        first = first[:1].upper() + first[1:].lower()
    return first


def render_vars(texto, contato):
    contato = contato or {}
    modulos = (contato.get("modulos") or "").split("|")
    valores = {
        "nome": primeiro_nome(contato.get("nome")),
        "empresa": contato.get("empresa") or "sua empresa",
        "modulo": modulos[0].strip() if modulos[0].strip() else "que conversamos",
        "data_proposta": contato.get("primeiro_envio") or "nossa última conversa",
        "validade": contato.get("validade") or "a data combinada",
        "mensalidade": contato.get("mensalidades") or "",
        # enriquecimento RD inbound (cohort): usar só quando o público tiver o dado
        "cargo": contato.get("cargo") or "",
        "porte": (contato.get("porte_assinantes") or "").replace("clientes", "assinantes").strip(),
        "erp": contato.get("erp") or "",
    }
    for k, v in valores.items():
        texto = texto.replace("{{" + k + "}}", v)
    return texto


def _unsub_url(email, cid=None):
    """URL de descadastro (one-click) deste contato, pela API pública. '' se rastreio off.
    Carrega o campaign_id (cid) para o descadastro ser atribuível por campanha."""
    if not (TRACKING_ON and email):
        return ""
    payload = {"email": email, "act": "u"}
    if cid is not None:
        payload["cid"] = cid
    tok = db.sign_token(payload, TRACK_SECRET)
    return f"{TRACK_BASE}/unsubscribe?token={tok}"


def _inner_tem_botao(inner):
    """True quando o miolo já traz o próprio botão de CTA (âncora estilizada como botão).
    Nesse caso a moldura NÃO deve repetir o botão — era isso que gerava dois CTAs
    idênticos no mesmo e-mail (um no corpo, outro da shell())."""
    return bool(re.search(r"<a\b[^>]*display\s*:\s*inline-block", inner or "", re.I))


def render_html(ref, contato=None, sender=None, cid=None):
    t = template_get(ref)
    if not t:
        return None, None
    assinatura = (sender or {}).get("signature_url") or None
    unsub = _unsub_url((contato or {}).get("email", ""), cid)
    # dedup de CTA: se o corpo já tem botão, a moldura não adiciona o dela
    cta_label = "" if _inner_tem_botao(t["inner_html"]) else t["cta"]
    html = email_templates.shell(t["preheader"], t["inner_html"], cta_label,
                                 t.get("cta_url") or email_templates.WA, assinatura, unsub)
    return render_vars(t["assunto"], contato), render_vars(html, contato)


def render_draft_html(preheader="", inner_html="", cta="", cta_url="",
                      contato=None, sender=None, cid=None):
    """Renderiza miolo ainda não salvo (editor / prévia ao vivo)."""
    assinatura = (sender or {}).get("signature_url") or None
    unsub = _unsub_url((contato or {}).get("email", ""), cid)
    cta_label = "" if _inner_tem_botao(inner_html) else (cta or "")
    html = email_templates.shell(
        preheader or "", inner_html or "", cta_label,
        cta_url or email_templates.WA, assinatura, unsub)
    return render_vars(html, contato)


# ---------------- guardrails de envio ----------------

def _toques_30d(c, email):
    return c.execute("SELECT COUNT(*) AS n FROM sends WHERE email=? AND data >= date('now','-30 days')",
                     (email,)).fetchone()["n"]


def pode_enviar(c, camp, contato, cc, tpl, manual_immediate=False):
    if (db.PROPOSALS / "monitor_state" / "PAUSE").exists():
        return False, "PAUSE global ativo"
    if camp["status"] != "ativa":
        return False, f"campanha '{camp['status']}' (envio só com ativação explícita)"
    if cc["status"] not in ("pendente", "ativo"):
        return False, f"contato {cc['status']}"
    if (contato["respondeu"] or "").lower() == "sim":
        return False, "contato respondeu — sequência interrompida"
    _sup = set(db.tags_list(contato.get("tags", ""))) & set(db.TAGS_SUPRIMIDAS)
    if _sup:
        if db.TAG_EMAIL_INVALIDO in _sup:
            motivo = "e-mail inválido (hard bounce)"
        elif db.TAG_SOFT_SUPRIMIDO in _sup:
            motivo = f"soft bounce reincidente (≥{SOFT_BOUNCE_LIMITE}×)"
        elif db.TAG_CONTATO_DESATUALIZADO in _sup:
            motivo = "desatualizado (não está mais na empresa)"
        elif db.TAG_EMAIL_OBSOLETO in _sup:
            motivo = "e-mail obsoleto (validação pré-envio)"
        else:
            motivo = "descadastrou (unsubscribe)"
        return False, f"contato {motivo} — não recebe mais"
    _vstatus = (contato.get("validacao_status") or "").strip().lower()
    if _vstatus in db.VALIDACAO_BLOQUEIA:
        return False, f"contato validação={_vstatus} — não recebe mais"
    # defesa em profundidade: cliente Yoobe só recebe se o Genal confirmou QUERO_ENVIAR
    if db.contato_eh_cliente(contato.get("tags", "")) and not camp["clientes_confirmados"]:
        return False, "contato é cliente Yoobe — exige confirmação QUERO_ENVIAR na ativação"
    if not tpl or tpl["status"] != "aprovado":
        return False, "template não aprovado pelo Genal"
    if _toques_30d(c, contato["email"]) >= MAX_TOQUES_30D:
        return False, f"teto de {MAX_TOQUES_30D} toques/30 dias atingido"
    agora = datetime.now()
    # Exceção deliberada: um disparo manual confirmado na ficha do lead pode
    # sair imediatamente. Todo o restante (opt-out, resposta, cliente,
    # aprovação, remetente e teto de toques) continua obrigatório.
    if not manual_immediate and (agora.weekday() > 4 or not (9 <= agora.hour < 18)):
        return False, "fora da janela comercial (seg-sex 9h-18h) — reagendado"
    return True, "ok"


def _msg_id_rfc(gmail_msgid, home=None):
    if not gmail_msgid:
        return "", ""
    m = _gws(["users", "messages", "get", "--params",
              json.dumps({"userId": "me", "id": gmail_msgid, "format": "metadata"})], home=home)
    if not m:
        return "", ""
    hdrs = {h["name"].lower(): h["value"] for h in m.get("payload", {}).get("headers", [])}
    return hdrs.get("message-id", ""), hdrs.get("subject", "")


def _thread_na_caixa(thread_id, home):
    """True se a thread existe na caixa do remetente (HOME do perfil gws). Evita reusar um
    threadId de OUTRA conta — o Gmail recusa e o send volta sem id. Quando a campanha sai de
    um remetente != dono da thread, devolvemos False para abrir thread nova.
    Ver memory campanha-thread-cross-mailbox."""
    if not thread_id:
        return False
    t = _gws(["users", "threads", "get", "--params",
              json.dumps({"userId": "me", "id": thread_id, "format": "minimal"})], home=home)
    return bool(t and t.get("id"))


def enviar_email(contato, assunto, html, texto_plano, thread_id="", campaign_id=None, etapa="", sender=None):
    import uuid
    if campaign_id and (sender or {}).get("email", "").strip().lower() != "genau@yoobe.co":
        raise RuntimeError("Campanhas novas só podem ser enviadas por genau@yoobe.co")
    track_id = uuid.uuid4().hex
    utm = {"utm_source": "yoobe-mail", "utm_medium": "email"}
    if campaign_id:
        utm["utm_campaign"] = db.campaign_slug(campaign_id)
        if etapa:
            utm["utm_content"] = f"etapa{etapa}"
    html = inject_tracking(html, track_id, contato["email"], campaign_id, etapa, utm=utm)
    home = gws_home(sender)
    de = f"{sender['nome']} <{sender['email']}>" if sender else "Yoobe <genau@yoobe.co>"
    msg = MIMEMultipart("alternative")
    msg["From"] = de
    msg["Reply-To"] = DEFAULT_REPLY_TO_EMAIL
    msg["To"] = contato["email"]
    # cabeçalhos de descadastro (entregabilidade / exigência Gmail p/ envio em volume)
    unsub = _unsub_url(contato["email"], campaign_id)
    if unsub:
        mailto = DEFAULT_REPLY_TO_EMAIL
        msg["List-Unsubscribe"] = f"<{unsub}>, <mailto:{mailto}?subject=unsubscribe>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    body = {}
    if thread_id and _thread_na_caixa(thread_id, home):
        mid, subj_orig = _msg_id_rfc(contato.get("msgid", ""), home=home)
        if mid:
            msg["In-Reply-To"] = mid
            msg["References"] = mid
        if subj_orig and not assunto.lower().startswith("re:"):
            assunto = f"Re: {subj_orig}"
        body["threadId"] = thread_id
    msg["Subject"] = assunto
    plain = texto_plano or re.sub(r"<[^>]+>", " ", html)
    canais = "Yoobe: https://corporate.yoobe.co/\nCatálogo: https://catalogo.yoobe.co/\nE-mail: corporate@yoobe.co"
    if "corporate@yoobe.co" not in plain:
        plain = plain.rstrip() + "\n\n" + canais
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    body["raw"] = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    r = _gws(["users", "messages", "send", "--params", '{"userId":"me"}'], body, home=home)
    return (r or {}).get("id"), (r or {}).get("threadId", thread_id), (track_id if TRACKING_ON else "")


# ---------------- agendamento de campanha ----------------

def soma_dias_uteis(base, n):
    """Soma n dias ÚTEIS a `base`, pulando sábado/domingo. n=0 retorna `base` (mesmo horário).
    offset_dias dos passos de campanha são interpretados como dias úteis (cadência sem fim de semana)."""
    d = base
    restantes = int(n or 0)
    while restantes > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            restantes -= 1
    return d


def clamp_comercial(dt):
    """Move dt para o PRÓXIMO instante dentro da janela comercial (seg-sex 09:00-18:00).
    Se já está dentro, retorna igual. Fora do horário/fim de semana → pula para o próximo
    dia útil às 09:00. É o que garante que nada seja agendado para a noite ou o fim de semana."""
    for _ in range(14):
        if dt.weekday() > 4:                       # sáb/dom → segunda 09:00
            dt = (dt + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        elif dt.hour < 9:                          # antes das 9 → hoje 09:00
            dt = dt.replace(hour=9, minute=0, second=0, microsecond=0)
        elif dt.hour >= 18:                        # depois das 18 → próximo dia 09:00
            dt = (dt + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        else:
            return dt
    return dt


def proximo_envio(camp, base=None):
    base = base or datetime.now()
    if camp["cad_min_dias"] is not None and camp["cad_max_dias"] is not None:
        dias = random.randint(camp["cad_min_dias"], camp["cad_max_dias"])
    else:
        dias = 0
    alvo = base + timedelta(days=dias)
    if alvo.hour >= 18 or alvo.hour < 9:
        alvo = alvo.replace(hour=9, minute=random.randint(5, 55)) + (
            timedelta(days=1) if alvo.hour >= 18 else timedelta())
    while alvo.weekday() > 4:
        alvo += timedelta(days=1)
    return alvo.strftime("%Y-%m-%d %H:%M:%S")


def agendar_campanha(campaign_id):
    """Na ativação: define next_send_at da primeira etapa pendente de cada contato."""
    c = db.conn()
    camp = c.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    steps = c.execute("SELECT * FROM campaign_steps WHERE campaign_id=? ORDER BY ordem",
                      (campaign_id,)).fetchall()
    if not camp or not steps:
        c.close()
        return 0
    n = 0
    base = datetime.now()
    # data/hora do 1º envio: usa inicio_em (se setado e futuro), senão agora
    inicio = base
    if camp["inicio_em"]:
        try:
            dt0 = datetime.strptime(camp["inicio_em"], "%Y-%m-%d %H:%M:%S")
            if dt0 > base:
                inicio = dt0
        except Exception:
            pass
    # cursor comercial: o 1º envio de cada contato é espaçado SÓ dentro de 9-18h em dias úteis
    cursor = clamp_comercial(inicio)
    for cc in c.execute("""SELECT * FROM campaign_contacts WHERE campaign_id=?
                           AND status IN ('pendente','ativo')""", (campaign_id,)).fetchall():
        prox = steps[0] if cc["etapa_atual"] == 0 else next(
            (s for s in steps if s["ordem"] == cc["etapa_atual"] + 1), None)
        if not prox and camp["tipo"] == "infinita":
            prox = steps[cc["etapa_atual"] % len(steps)]
        if not prox:
            c.execute("UPDATE campaign_contacts SET status='concluido' WHERE campaign_id=? AND email=?",
                      (campaign_id, cc["email"]))
            continue
        if cc["etapa_atual"] == 0:
            # primeiro envio: espaçado dentro da janela comercial (cursor pula noite/fim de semana)
            quando = cursor.strftime("%Y-%m-%d %H:%M:%S")
            cursor = clamp_comercial(cursor + timedelta(minutes=ESPACAMENTO_ENVIOS_MIN))
        elif prox["offset_dias"] is not None:
            alvo = soma_dias_uteis(base, prox["offset_dias"]) + timedelta(minutes=n * ESPACAMENTO_ENVIOS_MIN)
            quando = alvo.strftime("%Y-%m-%d %H:%M:%S")
        else:
            quando = proximo_envio(camp, base + timedelta(minutes=n * ESPACAMENTO_ENVIOS_MIN))
        c.execute("""UPDATE campaign_contacts SET status='ativo', next_send_at=?
                     WHERE campaign_id=? AND email=?""", (quando, campaign_id, cc["email"]))
        n += 1
    c.commit()
    c.close()
    return n


def forcar_disparo_imediato(campaign_id):
    """Inicia a 1ª etapa agora: ignora ``inicio_em`` e a janela comercial.

    Contatos ainda na etapa 0 (pendente/ativo) recebem ``next_send_at`` escalonado
    a partir de agora e entram na fila com ``immediate=True`` (mesmo guardrail do
    disparo manual na ficha do lead). Demais etapas seguem a cadência normal.
    """
    c = db.conn()
    camp = c.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    steps = c.execute(
        "SELECT 1 FROM campaign_steps WHERE campaign_id=? LIMIT 1", (campaign_id,)
    ).fetchone()
    if not camp or camp["status"] != "ativa" or not steps:
        c.close()
        return {"agendados": 0, "enfileirados": 0, "processados": 0}
    # Limpa horário futuro para não prender reativações / reexibição na UI.
    c.execute("UPDATE campaigns SET inicio_em='' WHERE id=?", (campaign_id,))
    c.execute(
        "DELETE FROM queue WHERE tipo='campaign_send' AND status='pending' AND payload LIKE ?",
        (f'%\"campaign_id\": {int(campaign_id)},%',),
    )
    contatos = c.execute(
        """SELECT email FROM campaign_contacts
           WHERE campaign_id=? AND status IN ('pendente','ativo') AND etapa_atual=0
           ORDER BY email""",
        (campaign_id,),
    ).fetchall()
    base = datetime.now()
    for i, cc in enumerate(contatos):
        quando = (base + timedelta(minutes=i * ESPACAMENTO_ENVIOS_MIN)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        c.execute(
            """UPDATE campaign_contacts SET status='ativo', next_send_at=?
               WHERE campaign_id=? AND email=?""",
            (quando, campaign_id, cc["email"]),
        )
    c.commit()
    c.close()

    enfileirados = 0
    qids = []
    for i, cc in enumerate(contatos):
        quando = (base + timedelta(minutes=i * ESPACAMENTO_ENVIOS_MIN)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        qid = db.enqueue_once(
            "campaign_send",
            {
                "campaign_id": int(campaign_id),
                "email": cc["email"],
                "etapa": 1,
                "immediate": True,
            },
            quando,
        )
        if qid is not None:
            enfileirados += 1
            qids.append(qid)

    processados = 0
    agora_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for qid in qids:
        c = db.conn()
        item = c.execute("SELECT * FROM queue WHERE id=?", (qid,)).fetchone()
        c.close()
        if not item:
            continue
        # Processa só o que já venceu; o resto fica para o gatilho de ~10 s / ciclo.
        if (item["run_at"] or "") > agora_s:
            continue
        claimed, _failed = _process_queue_item(item)
        if claimed:
            processados += 1

    log(
        f"DISPARO IMEDIATO campanha {campaign_id}: {len(contatos)} contato(s) "
        f"agendados, {enfileirados} na fila, {processados} processado(s) agora"
    )
    return {
        "agendados": len(contatos),
        "enfileirados": enfileirados,
        "processados": processados,
    }


# ---------------- handlers da fila ----------------

def _h_check_thread(payload):
    r = sync.check_thread_via_gws(payload["thread"], payload["email"])
    if r is None:
        raise RuntimeError("gws falhou ao ler thread")
    if r > 0:
        log(f"resposta detectada na thread {payload['thread']} ({payload['email']}) — sequência pausada")
    return f"{r} msg(s) externas"


def _h_whatsapp(payload):
    if not whatsapp(payload["texto"]):
        raise RuntimeError("falha Z-API")
    return "enviado"


def _h_sync(payload):
    return json.dumps(sync.sync_all(), ensure_ascii=False)


def _h_track_sync(payload):
    """Puxa eventos da API (cursor opaco), grava em events e dispara o gancho de clique.
    O cursor só avança depois que todo o lote foi persistido localmente."""
    if not (TRACK_BASE and TRACK_SYNC_KEY):
        return "rastreio não configurado"
    from urllib.parse import urlencode
    cursor = db.meta_get("tracking_cursor", "")
    query = urlencode({"cursor": cursor, "limit": 500})
    # /usr/bin/curl: LaunchAgent/uvicorn às vezes sobe com PATH mínimo e o
    # binário "curl" some do path → returncode≠0 com stderr vazio.
    out = subprocess.run(["/usr/bin/curl", "-sS", "--max-time", "30",
                          "-H", f"X-Sync-Key: {TRACK_SYNC_KEY}",
                          f"{TRACK_BASE}/events?{query}"],
                         capture_output=True, text=True, timeout=40)
    if out.returncode:
        detail = (out.stderr or out.stdout or f"curl exit {out.returncode}")[:160]
        raise RuntimeError(f"falha ao consultar API de rastreio: {detail}")
    try:
        data = json.loads(out.stdout)
    except Exception:
        raise RuntimeError(f"resposta inválida da API de rastreio: {out.stdout[:120] or out.stderr[:120]}")
    if data.get("error"):
        raise RuntimeError(f"API de rastreio recusou a sincronização: {data['error']}")
    eventos = data.get("events", [])
    if not eventos:
        return "0 eventos novos"
    c = db.conn()
    novos_cliques, novos_unsub = [], []
    for e in eventos:
        cur = c.execute("""INSERT OR IGNORE INTO events
            (worker_id,type,track_id,email,campaign_id,etapa,url,ts,ua,cf_country,prefetch)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (e["id"], e.get("type"), e.get("track_id"), e.get("email"), e.get("campaign_id"),
             str(e.get("etapa", "")), e.get("url", ""), e.get("ts", ""), e.get("ua", ""),
             e.get("cf_country", ""), e.get("prefetch", 0)))
        if not cur.rowcount:
            continue
        if e.get("type") == "click" and e.get("email"):
            novos_cliques.append(e)
        elif e.get("type") == "unsubscribe" and e.get("email"):
            db.marcar_descadastro(c, e["email"])     # tag + pausa cadência (código decide)
            novos_unsub.append(e)
    c.commit()
    c.close()
    db.meta_set("tracking_cursor", str(data.get("cursor", cursor)))
    # Gancho autônomo (FASE 1 = só notifica): clique é sinal alto — mas só de HUMANO. Cliques de
    # scanner de segurança (clique em <N s do envio) são pulados: não notificam nem contam.
    c = db.conn()
    bots = db.click_bot_trackids(c, BOT_CLICK_JANELA_SEG)
    c.close()
    cliques_bot = 0
    for e in novos_cliques:
        if e.get("track_id") in bots:
            cliques_bot += 1
            log(f"gancho clique pulado (scanner: {bots[e['track_id']]}): {e['email']}")
            continue
        _hook_clique(e)
        _track_contact_goal(e)
    for e in novos_unsub:
        log(f"unsubscribe: {e['email']} descadastrado (tag + cadência pausada)")
        whatsapp(f"🚫 *Descadastro* — {e['email']} clicou em unsubscribe e saiu de toda a base de envio.")
    return (f"{len(eventos)} evento(s), {len(novos_cliques) - cliques_bot} clique(s) humano(s)"
            f"{f' (+{cliques_bot} scanner)' if cliques_bot else ''}, "
            f"{len(novos_unsub)} descadastro(s)")


def poll_tracking():
    """Pull leve dos eventos da API (sem o ciclo pesado de campanhas).
    Seguro para rodar a cada minuto: é só um GET baseado em cursor."""
    if not (TRACKING_ON and online()):
        return {"ok": False, "motivo": "rastreio desligado ou offline"}
    try:
        nota = _h_track_sync({})
        return {"ok": True, "nota": nota}
    except Exception as e:
        log(f"poll_tracking erro: {e}")
        return {"ok": False, "erro": str(e)}


def _hook_clique(ev):
    """Clique de um contato → notifica o Genal no WhatsApp NA HORA (base p/ reações autônomas)."""
    chave = f"hook_click_{ev['track_id']}_{ev.get('url','')[:40]}"
    if db.meta_get(chave, ""):       # evita notificar o mesmo clique-URL duas vezes
        return
    c = db.conn()
    contato = c.execute("SELECT nome, empresa FROM contacts WHERE email=?", (ev["email"],)).fetchone()
    c.close()
    quem = (contato["empresa"] or ev["email"]) if contato and contato["empresa"] else ev["email"]
    nome = contato["nome"] if contato else ""
    texto = (f"🖱️ *Clique em e-mail — {quem}*\n\n{nome} ({ev['email']}) clicou em:\n{ev.get('url','')}\n\n"
             f"Sinal alto de interesse — bom momento para um toque pessoal.")
    db.meta_set(chave, "1")
    if not whatsapp(texto):          # envio imediato; se a Z-API falhar, cai na fila (retry/offline)
        db.enqueue("whatsapp", {"texto": texto})
    log(f"gancho clique: {ev['email']} → {ev.get('url','')[:60]}")


def _track_contact_goal(ev):
    """Espelha cliques nos canais oficiais como eventos-chave GA4, sem enviar PII."""
    url = (ev.get("url") or "").lower()
    if "wa.me/5541987607512" in url:
        event_name, method = "contact_whatsapp", "whatsapp"
    elif url.startswith("mailto:corporate@yoobe.co"):
        event_name, method = "contact_email", "email"
    else:
        return False
    try:
        import ga4
        cid = ev.get("campaign_id")
        slug = db.campaign_slug(cid) if cid else ""
        return ga4.mp_send_event(event_name, {
            "contact_method": method, "campaign_id": cid or 0,
            "campaign_slug": slug, "utm_campaign": slug,
            "utm_source": "yoobe-mail", "utm_medium": "email",
            "etapa": str(ev.get("etapa") or ""), "engagement_time_msec": 1,
            "page_location": ("https://corporate.yoobe.co/?utm_source=yoobe-mail"
                              f"&utm_medium=email&utm_campaign={slug}"),
            "page_title": "Contato comercial via campanha de e-mail",
        })
    except Exception as exc:
        log(f"ga4 goal {event_name} erro: {exc}")
        return False


# =====================================================================
# Automação de respostas de campanha: detecta → IA interpreta → código decide
# =====================================================================

def _corpo_texto(payload):
    """Extrai o text/plain (base64url) de uma mensagem, recursivamente."""
    stack = [payload]
    while stack:
        p = stack.pop()
        if p.get("mimeType") == "text/plain" and p.get("body", {}).get("data"):
            d = p["body"]["data"].replace("-", "+").replace("_", "/")
            d += "=" * (-len(d) % 4)
            return base64.b64decode(d).decode("utf-8", "replace")
        stack.extend(p.get("parts", []))
    return ""


def _hdr(msg, nome):
    for h in msg.get("payload", {}).get("headers", []):
        if h["name"].lower() == nome.lower():
            return h["value"]
    return ""


def _is_our_message(msg, sender=None, known_msgids=None, sender_emails=None):
    """Reconhece mensagens enviadas por nós sem depender do nome da marca.

    O Gmail marca cópias da caixa de saída com o label SENT. O fallback por
    endereço cobre respostas retornadas pela API sem labels e aliases dos
    remetentes cadastrados.
    """
    if msg.get("id") in (known_msgids or set()):
        return True
    if "SENT" in set(msg.get("labelIds") or []):
        return True
    frm = parseaddr(_hdr(msg, "From"))[1].strip().lower()
    nossos = {str(x or "").strip().lower() for x in (sender_emails or set()) if x}
    if sender and sender.get("email"):
        nossos.add(sender["email"].strip().lower())
    return bool(frm and frm in nossos)


def _html_resposta(paragrafos, botao=None, assinatura=None):
    assinatura = assinatura or email_templates.ASSINATURA
    ps = "".join(f'<p style="margin:0 0 14px">{p}</p>' for p in paragrafos if p.strip())
    btn = ""
    if botao:
        rotulo, url = botao
        btn = (f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin:18px 0 8px"><tr>'
               f'<td style="border-radius:10px;background-color:#45bbd8;background-image:linear-gradient(90deg,#45bbd8,#50d9fb)">'
               f'<a href="{url}" style="display:inline-block;padding:13px 30px;font-family:Montserrat,Arial,sans-serif;'
               f'font-size:14px;font-weight:700;color:#22093a;text-decoration:none">{rotulo}</a></td></tr></table>')
    return (f'<div style="font-family:\'DM Sans\',Arial,sans-serif;font-size:15px;color:#1a0a2e;line-height:1.6;max-width:600px">'
            f'{ps}{btn}<p style="margin:18px 0 6px">Atenciosamente,</p>'
            f'<img src="{assinatura}" width="320" height="85" alt="Yoobe" '
            f'style="display:block;border-radius:8px;max-width:320px"></div>')


def _responder_lead(reply_to_msg, thread_id, texto, botao=None, sender=None):
    """FASE 2: envia a resposta na thread (In-Reply-To + assinatura) como o remetente."""
    # Defesa: nunca responder NDR/postmaster (evita Re: Undeliverable → postmaster).
    if _is_bounce_or_system(reply_to_msg):
        log("AUTO-RESPOSTA abortada: mensagem é bounce/sistema")
        return None
    to_addr = parseaddr(_hdr(reply_to_msg, "From"))[1].strip().lower()
    if to_addr and SKIP_FROM_REPLY.search(to_addr):
        log(f"AUTO-RESPOSTA abortada: destino sistema {to_addr}")
        return None
    subj_chk = _hdr(reply_to_msg, "Subject") or ""
    if BOUNCE_SUBJECT_RE.search(subj_chk):
        log(f"AUTO-RESPOSTA abortada: assunto bounce ({subj_chk[:60]})")
        return None
    home = gws_home(sender)
    de = f"{sender['nome']} <{sender['email']}>" if sender else "Yoobe <genau@yoobe.co>"
    assinatura = (sender or {}).get("signature_url") or None
    msg = MIMEMultipart("alternative")
    msg["From"] = de
    msg["Reply-To"] = DEFAULT_REPLY_TO_EMAIL
    msg["To"] = _hdr(reply_to_msg, "From")
    subj = _hdr(reply_to_msg, "Subject")
    msg["Subject"] = subj if subj.lower().startswith("re:") else f"Re: {subj}"
    mid = _hdr(reply_to_msg, "Message-ID")
    if mid:
        msg["In-Reply-To"] = mid
        msg["References"] = (_hdr(reply_to_msg, "References") + " " + mid).strip()
    paras = [p for p in (texto or "").split("\n") if p.strip()]
    msg.attach(MIMEText(texto, "plain", "utf-8"))
    msg.attach(MIMEText(_html_resposta(paras, botao, assinatura), "html", "utf-8"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    r = _gws(["users", "messages", "send", "--params", '{"userId":"me"}'],
             {"raw": raw, "threadId": thread_id}, home=home)
    return (r or {}).get("id")


CLASSES_SEGURAS = {
    "agendar": "Pediu reunião/call → manda o link do Calendly",
    "interessado": "Demonstrou interesse claro → manda o link do Calendly",
    "pergunta_simples": "Dúvida factual/técnica que está nos KBs → responde com base no KB",
    "recusa_encerrar": "Recusa clara → encerra educadamente",
}


def _limites():
    """Tetos/confiança globais (meta), comuns a todos os remetentes."""
    return {
        "conf_min": float(db.meta_get("conf_min", str(CONF_MIN)) or CONF_MIN),
        "max_dia": int(db.meta_get("max_dia", str(MAX_AUTOREPLIES_DIA)) or MAX_AUTOREPLIES_DIA),
        "max_thread": int(db.meta_get("max_thread", str(MAX_AUTOREPLIES_THREAD)) or MAX_AUTOREPLIES_THREAD),
    }


def reply_config(sender=None):
    """Config de auto-resposta. Por remetente (colunas de senders) + limites globais."""
    lim = _limites()
    if sender is None:
        sender = db.sender_get(1) or {}  # default = frank (compat com a aba Respostas)
    return {
        "auto_reply": (sender.get("auto_reply") or "off") == "on",
        "classes": [x.strip() for x in (sender.get("auto_reply_classes") or "").split(",") if x.strip()],
        **lim,
    }


def set_reply_config(auto_reply=None, classes=None):
    """Compat (aba Respostas): controla o remetente default (frank, id 1)."""
    return set_sender_autoreply(1, auto_reply, classes)


def set_sender_autoreply(sender_id, auto_reply=None, classes=None):
    """Liga/desliga e ajusta os assuntos da auto-resposta de UM remetente (db.senders)."""
    c = db.conn()
    if auto_reply is not None:
        c.execute("UPDATE senders SET auto_reply=? WHERE id=?",
                  ("on" if auto_reply else "off", sender_id))
    if classes is not None:
        validas = [x for x in classes if x in CLASSES_SEGURAS]
        c.execute("UPDATE senders SET auto_reply_classes=? WHERE id=?", (",".join(validas), sender_id))
    c.commit()
    c.close()
    return reply_config(db.sender_get(sender_id))


def _pode_auto_responder(classe, conf, precisa_humano, thread_id, sender=None):
    """Guardrails determinísticos (código decide), lendo a config do remetente."""
    cfg = reply_config(sender)
    if (STATE_DIR / "PAUSE").exists():
        return False, "PAUSE ativo"
    if not cfg["auto_reply"]:
        return False, "auto-resposta desligada (só notifica)"
    if precisa_humano:
        return False, "IA marcou precisa_humano"
    if classe not in cfg["classes"]:
        return False, f"classe '{classe}' não está na auto-resposta"
    if conf < cfg["conf_min"]:
        return False, f"confiança {conf:.2f} < {cfg['conf_min']}"
    hoje = datetime.now().strftime("%Y-%m-%d")
    cnt_file = STATE_DIR / f"autoreplies_{hoje}.json"
    cnt = json.loads(cnt_file.read_text()) if cnt_file.exists() else {}
    if sum(cnt.values()) >= cfg["max_dia"]:
        return False, f"teto diário ({cfg['max_dia']})"
    if cnt.get(thread_id, 0) >= cfg["max_thread"]:
        return False, "teto por thread atingido hoje"
    return True, "ok"


def _registrar_autoreply(thread_id):
    STATE_DIR.mkdir(exist_ok=True)
    hoje = datetime.now().strftime("%Y-%m-%d")
    cnt_file = STATE_DIR / f"autoreplies_{hoje}.json"
    cnt = json.loads(cnt_file.read_text()) if cnt_file.exists() else {}
    cnt[thread_id] = cnt.get(thread_id, 0) + 1
    cnt_file.write_text(json.dumps(cnt))


def _escalar(ct, classe, conf, resumo, sugerida, gmail_link, motivo, frm, corpo):
    """Salva rascunho + notifica o Genal no WhatsApp com o link do Gmail."""
    DRAFTS_DIR.mkdir(exist_ok=True)
    empresa = ct.get("empresa") or ct["email"]
    slug = re.sub(r"\W+", "-", empresa.lower())[:30]
    draft = DRAFTS_DIR / f"resp-{slug}-{ct['thread'][:8]}.md"
    draft.write_text(
        f"# Resposta de campanha — {empresa}\n\nDe: {frm}\nClasse: {classe} ({conf:.0%}) — {motivo}\n"
        f"Gmail: {gmail_link}\n\n## Resumo\n{resumo}\n\n## Mensagem do lead\n{corpo[:2000]}\n\n"
        f"## Rascunho sugerido pela IA\n{sugerida}\n", encoding="utf-8")
    texto = (f"📩 *Resposta de campanha — {empresa}*\n\n"
             f"De: {frm}\n🏷️ {classe} (confiança {conf:.0%})\n📝 {resumo}\n\n"
             f"🧑‍⚖️ Precisa de você ({motivo})\n\n✍️ *Rascunho sugerido:*\n{sugerida[:600]}\n\n🔗 {gmail_link}")
    if not whatsapp(texto):
        db.enqueue("whatsapp", {"texto": texto})
    log(f"resposta ESCALADA [{classe}/{conf:.2f}] {ct['email']} ({motivo})")


def processar_resposta(ct, historico, corpo, msg, frm, sender=None):
    """Classifica a resposta com IA e decide: auto-responder (FASE 2) ou escalar."""
    res = ai.classificar_resposta(ct, historico, corpo) or {}
    classe = res.get("classificacao", "outro")
    conf = float(res.get("confianca", 0) or 0)
    precisa_humano = bool(res.get("precisa_humano", True))
    resumo = res.get("resumo", "") or corpo[:200]
    sugerida = res.get("resposta_sugerida", "") or ""
    msgid = msg["id"]
    email = ct["email"]
    gmail_link = f"https://mail.google.com/mail/u/0/#all/{msgid}"

    # trava de conteúdo: valor/desconto/contrato só nos caminhos aprovados
    if PRECO_RE.search(sugerida) and classe not in ("agendar", "interessado", "pergunta_simples"):
        precisa_humano = True

    # SEMPRE: registra a resposta, marca respondeu e pausa a cadência do contato
    c = db.conn()
    c.execute("""INSERT OR IGNORE INTO replies(msgid,thread,email,data,classe,confianca,resumo)
                 VALUES(?,?,?,?,?,?,?)""",
              (msgid, ct["thread"], email, datetime.now().strftime("%Y-%m-%d"), classe, conf, resumo))
    c.execute("UPDATE contacts SET respondeu='sim', classe_monitor=? WHERE email=?", (classe, email))
    db.aplicar_tags_resposta(c, email, classe)   # tags automáticas: respondeu / agendou / descartado
    c.execute("UPDATE campaign_contacts SET status='pausado_resposta' "
              "WHERE email=? AND status IN ('pendente','ativo')", (email,))
    c.commit()
    c.close()

    ok, motivo = _pode_auto_responder(classe, conf, precisa_humano, ct["thread"], sender)
    quem = (sender or {}).get("email", "corporate@yoobe.co")
    acao = "escalada"
    if ok:
        botao = ("📅 Escolher horário na agenda", ai.CALENDLY) if classe in ("agendar", "interessado") else None
        sent = _responder_lead(msg, ct["thread"], sugerida, botao, sender)
        if sent:
            _registrar_autoreply(ct["thread"])
            with open(db.PROPOSALS / "sent_log.txt", "a") as f:
                f.write(f"{datetime.now():%Y-%m-%d} | {sent} | {frm} | auto-resp-{classe} ({quem}) | thread:{ct['thread']}\n")
            wa = (f"🤖 *AUTO-RESPOSTA enviada por {quem} — {ct.get('empresa') or email}*\n\n"
                  f"🏷️ {classe} (confiança {conf:.0%})\n📝 {resumo}\n\n🔗 {gmail_link}")
            if not whatsapp(wa):
                db.enqueue("whatsapp", {"texto": wa})
            log(f"AUTO-RESPOSTA [{classe}] {email} → {sent}")
            acao = "auto-respondida"
        else:
            _escalar(ct, classe, conf, resumo, sugerida, gmail_link, "falha no envio automático", frm, corpo)
    else:
        _escalar(ct, classe, conf, resumo, sugerida, gmail_link, motivo, frm, corpo)
    cu = db.conn()
    cu.execute("UPDATE replies SET acao=? WHERE msgid=?", (acao, msgid))
    cu.commit()
    cu.close()


def scan_replies(only_thread=""):
    """Varre as threads das campanhas atrás de respostas novas de leads (após o nosso envio)."""
    if not online():
        return {"online": False}
    c = db.conn()
    rows = c.execute("""SELECT s.thread, s.msgid, s.email, cp.sender_id,
                               ct.nome, ct.empresa, ct.modulos, ct.mensalidades,
                               ct.primeiro_envio, ct.validade
                        FROM sends s JOIN contacts ct ON ct.email=s.email
                        LEFT JOIN campaigns cp ON cp.id=s.campaign_id
                        WHERE s.campaign_id IS NOT NULL AND s.thread != ''
                        ORDER BY s.id DESC""").fetchall()
    vistos = set(r["msgid"] for r in c.execute("SELECT msgid FROM replies").fetchall())
    senders = {s["id"]: dict(s) for s in c.execute("SELECT * FROM senders").fetchall()}
    c.close()
    nossos_ids = {}
    for r in rows:
        nossos_ids.setdefault(r["thread"], set()).add(r["msgid"])
    sender_emails = {s.get("email", "").lower() for s in senders.values() if s.get("email")}
    feitos, novas = set(), 0
    for r in rows:
        tid = r["thread"]
        if tid in feitos or (only_thread and tid != only_thread):
            continue
        feitos.add(tid)
        sender = senders.get(r["sender_id"] or 1) or db.sender_default()
        # lê a caixa do remetente daquela campanha (resposta ao marcos@ está na caixa do marcos@)
        t = _gws(["users", "threads", "get", "--params",
                  json.dumps({"userId": "me", "id": tid, "format": "full"})], home=gws_home(sender))
        if not t:
            continue
        msgs = t.get("messages", [])
        nosso_dt, nosso_ultimo = 0, ""
        for m in msgs:
            if _is_our_message(m, sender, nossos_ids.get(tid), sender_emails):
                d = int(m.get("internalDate", "0") or 0)
                if d >= nosso_dt:
                    nosso_dt, nosso_ultimo = d, _corpo_texto(m["payload"])
        for m in msgs:
            frm = _hdr(m, "From")
            if (_is_our_message(m, sender, nossos_ids.get(tid), sender_emails)
                    or m["id"] in vistos):
                continue
            if int(m.get("internalDate", "0") or 0) <= nosso_dt:
                continue  # mensagem anterior ao nosso envio — não é resposta a esta campanha
            # NDR/postmaster na thread: registra bounce e NÃO classifica/auto-responde
            if _is_bounce_or_system(m) or SKIP_FROM_REPLY.search(frm or ""):
                try:
                    registrar_bounce_de_mensagem(m, sender=sender)
                except Exception as ex:
                    log(f"bounce na thread {tid[:12]}: falha ao registrar ({ex})")
                vistos.add(m["id"])
                continue
            ct = dict(r)
            ct["thread"] = tid
            processar_resposta(ct, nosso_ultimo, _corpo_texto(m["payload"]), m, frm, sender)
            vistos.add(m["id"])
            novas += 1
    return {"online": True, "novas": novas}


def _msg_html(payload):
    """Extrai a parte text/html de uma mensagem (base64url), recursivamente."""
    stack = [payload]
    while stack:
        p = stack.pop()
        if p.get("mimeType") == "text/html" and p.get("body", {}).get("data"):
            d = p["body"]["data"].replace("-", "+").replace("_", "/")
            d += "=" * (-len(d) % 4)
            return base64.b64decode(d).decode("utf-8", "replace")
        stack.extend(p.get("parts", []))
    return ""


def scan_calendly():
    """Detecta agendamentos REAIS pelas notificações do Calendly.
    Casa o e-mail do convidado com um contato → tag 'agendou' (+ agendado_em)."""
    if not online():
        return {"online": False}
    res = _gws(["users", "messages", "list", "--params",
                json.dumps({"userId": "me",
                            "q": 'from:calendly.com (subject:"Novo evento" OR subject:"Cancelado")',
                            "maxResults": 40})])
    msgs = (res or {}).get("messages", [])
    c = db.conn()
    vistos = set(r["msgid"] for r in c.execute("SELECT msgid FROM bookings").fetchall())
    c.close()
    novos = 0
    for mm in msgs:
        mid = mm["id"]
        if mid in vistos:
            continue
        full = _gws(["users", "messages", "get", "--params",
                     json.dumps({"userId": "me", "id": mid, "format": "full"})])
        if not full:
            continue
        subj = _hdr(full, "Subject")
        html = _msg_html(full["payload"])
        # e-mail do convidado: preferir o mailto: do corpo (exclui calendly e o host)
        email = ""
        for e in re.findall(r"mailto:([\w.+-]+@[\w.-]+\.\w+)", html):
            el = e.lower()
            if "calendly" not in el and "Yoobe" not in el:
                email = el
                break
        if not email:
            for e in re.findall(r"[\w.+-]+@[\w.-]+\.\w+", re.sub(r"<[^>]+>", " ", html)):
                el = e.lower()
                if "calendly" not in el and "Yoobe" not in el and "sentry" not in el:
                    email = el
                    break
        partes = [p.strip() for p in subj.split(" - ")]
        cancelado = subj.lower().startswith("cancelado")
        nome = (partes[0].split(":", 1)[-1].strip() if partes else "")
        quando = partes[1] if len(partes) > 1 else ""
        c = db.conn()
        c.execute("INSERT OR IGNORE INTO bookings(msgid,email,nome,quando,status) VALUES(?,?,?,?,?)",
                  (mid, email, nome, quando, "cancelado" if cancelado else "agendado"))
        casou = bool(email) and c.execute("SELECT 1 FROM contacts WHERE email=?", (email,)).fetchone()
        if casou:
            if cancelado:
                db.desmarcar_agendou(c, email)
                log(f"Calendly: cancelamento de {email}")
            else:
                db.marcar_agendou(c, email, quando)
                log(f"Calendly: AGENDOU {email} ({quando})")
        c.commit()
        c.close()
        vistos.add(mid)
        novos += 1
    return {"online": True, "novos": novos}


def poll_calendly():
    if not online():
        return {"ok": False, "motivo": "offline"}
    try:
        return {"ok": True, **scan_calendly()}
    except Exception as e:
        log(f"poll_calendly erro: {e}")
        return {"ok": False, "erro": str(e)}


# ---------------- bounces (auto-supressão de e-mails inexistentes) ----------------

# HARD = endereço permanentemente inválido (não existe) → suprime. Cobre os códigos de
# endereço ruim (5.0.0, 5.1.0-5.1.3, 5.4.x routing/recipient-not-found do M365) + sinais textuais.
# NÃO inclui 5.2.x (caixa cheia/quota), 5.7.x (policy/spam) nem 4.x.x (temporário) — esses não
# significam que o e-mail não existe e não devem suprimir.
BOUNCE_HARD = re.compile(
    r"\b5\.1\.[0-3]\b|\b5\.4\.\d\b|\b5\.0\.0\b|"
    r"no such user|user unknown|does(n't| not) exist|address not found|"
    r"recipient.*not found|no such recipient|unknown recipient|n[ãa]o encontrad|"
    r"mailbox unavailable|dns error|domain not found|host or domain name not found|"
    r"recipient address rejected", re.I)
BOUNCE_SOFT = re.compile(r"\b4\.\d\.\d\b|\b5\.2\.\d\b|\b5\.7\.\d\b|\b5\.3\.\d\b|quota|"
                         r"mailbox full|caixa cheia|over quota|temporar|try again|policy|spam", re.I)
BOUNCE_FAILED_RE = re.compile(r"Final-Recipient:\s*rfc822;\s*([\w.+%-]+@[\w.-]+\.\w+)", re.I)
BOUNCE_CODE_RE = re.compile(r"\b([45]\.\d\.\d+)\b")


def _payload_mime_hint(payload):
    """Content-Type do payload Gmail (raiz) em minúsculas."""
    if not isinstance(payload, dict):
        return ""
    for h in payload.get("headers") or []:
        if (h.get("name") or "").lower() == "content-type":
            return (h.get("value") or "").lower()
    return (payload.get("mimeType") or "").lower()


def _is_bounce_or_system(msg):
    """True se a mensagem é NDR/sistema e não deve virar reply de lead."""
    if not msg:
        return True
    frm = _hdr(msg, "From") or ""
    if SKIP_FROM_REPLY.search(frm):
        return True
    subj = _hdr(msg, "Subject") or ""
    if BOUNCE_SUBJECT_RE.search(subj):
        return True
    auto = (_hdr(msg, "Auto-Submitted") or "").strip().lower()
    if auto and auto not in ("no",):
        return True
    if (_hdr(msg, "X-MS-Exchange-Message-Is-Ndr") or "").strip() != "":
        return True
    # Return-Path vazio é típico de DSN (header pode vir como <>).
    rp = (_hdr(msg, "Return-Path") or "").strip()
    if rp in ("<>", ""):
        # Só trata como bounce se houver outro sinal (subject/from/mime) — evita
        # falsos positivos em mensagens sem Return-Path na API. Já coberto acima
        # na maioria dos NDRs; reforço via mime:
        mime = _payload_mime_hint(msg.get("payload") or {})
        if "multipart/report" in mime or "delivery-status" in mime:
            return True
    mime = _payload_mime_hint(msg.get("payload") or {})
    if "multipart/report" in mime and "delivery-status" in mime:
        return True
    return False


def _extract_failed_recipient(msg, corpo=""):
    """Destinatário que falhou no NDR (não o postmaster)."""
    failed = (_hdr(msg, "X-Failed-Recipients") or "").strip().lower()
    if failed and "@" in failed:
        return parseaddr(failed)[1].strip().lower() or failed.split()[0]
    corpo = corpo or _corpo_texto((msg or {}).get("payload") or {}) or ""
    m = BOUNCE_FAILED_RE.search(corpo)
    if m:
        return m.group(1).lower()
    m = BOUNCE_TO_RE.search(corpo)
    if m:
        return m.group(1).lower()
    # "v.magalhaes wasn't found at brms.com.br"
    m = re.search(
        r"([\w.+%-]+) wasn't found at ([\w.-]+\.\w+)", corpo, re.I)
    if m:
        return f"{m.group(1).lower()}@{m.group(2).lower()}"
    return ""


def registrar_bounce_de_mensagem(msg, sender=None, failed_email=""):
    """Persiste bounce a partir de uma msg Gmail (idempotente por msgid).
    Retorna dict {novo, email, tipo, suprimido} ou None se inválida."""
    if not msg or not msg.get("id"):
        return None
    mid = msg["id"]
    corpo = _corpo_texto(msg.get("payload") or {}) or ""
    failed = (failed_email or _extract_failed_recipient(msg, corpo) or "").strip().lower()
    if not failed or "Yoobe" in failed:
        return None
    tipo = "hard" if BOUNCE_HARD.search(corpo) else "soft"
    # Subject Undeliverable + wasn't found → forçar hard
    subj = _hdr(msg, "Subject") or ""
    if BOUNCE_SUBJECT_RE.search(subj) and (
            BOUNCE_HARD.search(corpo) or "wasn't found" in corpo.lower()
            or "couldn't be delivered" in corpo.lower()):
        tipo = "hard"
    mcod = BOUNCE_CODE_RE.search(corpo)
    codigo = mcod.group(1) if mcod else ""
    sid = (sender or {}).get("id")
    c = db.conn()
    exists = c.execute("SELECT 1 FROM bounces WHERE msgid=?", (mid,)).fetchone()
    if exists:
        c.close()
        return {"novo": False, "email": failed, "tipo": tipo, "suprimido": False}
    c.execute("""INSERT OR IGNORE INTO bounces(msgid,email,tipo,codigo,diagnostico,sender_id)
                 VALUES(?,?,?,?,?,?)""",
              (mid, failed, tipo, codigo, re.sub(r"\s+", " ", corpo)[:300], sid))
    suprimido = False
    ehcontato = c.execute("SELECT 1 FROM contacts WHERE email=?", (failed,)).fetchone()
    if tipo == "hard" and ehcontato:
        db.marcar_email_invalido(c, failed)
        suprimido = True
    c.commit()
    c.close()
    if suprimido:
        log(f"bounce NDR: {failed} marcado email_invalido (msgid={mid[:12]})")
    return {"novo": True, "email": failed, "tipo": tipo, "suprimido": suprimido}

# Soft bounce reincidente: nº de devoluções temporárias do mesmo endereço a partir do qual o
# contato é REMOVIDO da base (deixa de receber). Soft não prova que o e-mail não existe, então
# só suprime por reincidência — não no 1º toque.
SOFT_BOUNCE_LIMITE = 3

# Cliques-fantasma: clique em menos de N segundos do envio = scanner de segurança (SafeLinks,
# Proofpoint, antivírus do empresa), não humano. Ver db.click_bot_trackids.
BOT_CLICK_JANELA_SEG = 90


def scan_bounces():
    """Varre as caixas dos remetentes atrás de devoluções (mailer-daemon). HARD bounce
    (e-mail não existe) → marca o contato como email_invalido e o tira de toda audiência/envio.
    SOFT bounce (temporário) → só registra, não suprime. Idempotente (dedup por msgid)."""
    if not online():
        return {"online": False}
    c = db.conn()
    vistos = set(r["msgid"] for r in c.execute("SELECT msgid FROM bounces").fetchall())
    senders = [dict(s) for s in c.execute("SELECT * FROM senders WHERE ativo=1").fetchall()]
    c.close()
    novos, suprimidos = 0, []
    for sender in senders:
        # Paginação manual (pageToken) — o limite de 50 sem paginação deixava devoluções de
        # fora em dias de pico. Cap de segurança de 10 páginas (~1000 msgs) por remetente.
        mensagens, token, paginas = [], None, 0
        while paginas < 10:
            params = {"userId": "me", "maxResults": 100,
                      "q": "from:(mailer-daemon OR postmaster) newer_than:14d"}
            if token:
                params["pageToken"] = token
            res = _gws(["users", "messages", "list", "--params", json.dumps(params)],
                       home=gws_home(sender))
            mensagens += (res or {}).get("messages", [])
            token = (res or {}).get("nextPageToken")
            paginas += 1
            if not token:
                break
        for mm in mensagens:
            mid = mm["id"]
            if mid in vistos:
                continue
            full = _gws(["users", "messages", "get", "--params",
                         json.dumps({"userId": "me", "id": mid, "format": "full"})], home=gws_home(sender))
            if not full:
                continue
            vistos.add(mid)
            r = registrar_bounce_de_mensagem(full, sender=sender)
            if not r:
                continue
            if r.get("novo"):
                novos += 1
            if r.get("suprimido"):
                suprimidos.append(r["email"])
    # Varredura de soft REINCIDENTE: qualquer endereço com ≥ N devoluções temporárias vira lixo
    # (existe, mas nunca entrega) e é removido da base. Cobre backlog + novos, idempotente (não
    # remexe em quem já tem a tag). Soft não prova inexistência, por isso só suprime por reincidência.
    soft_removidos = _sweep_soft_reincidente()
    if suprimidos:
        log(f"bounces: {len(suprimidos)} e-mail(s) inválido(s) (hard) suprimido(s): {', '.join(suprimidos[:8])}")
    if soft_removidos:
        log(f"bounces: {len(soft_removidos)} e-mail(s) removido(s) por soft reincidente "
            f"(≥{SOFT_BOUNCE_LIMITE}×): {', '.join(soft_removidos[:8])}")
    return {"online": True, "novos": novos, "suprimidos": len(suprimidos),
            "soft_removidos": len(soft_removidos)}


def _sweep_soft_reincidente():
    """Remove da base todo contato com ≥ SOFT_BOUNCE_LIMITE devoluções soft (reincidência).
    Idempotente: ignora quem já está marcado. Roda no fim de cada scan e cobre o backlog."""
    c = db.conn()
    alvos = c.execute(
        """SELECT b.email FROM bounces b
           JOIN contacts ct ON ct.email=b.email
           WHERE b.tipo='soft' AND instr(','||ct.tags||',', ?)=0
           GROUP BY b.email HAVING COUNT(*) >= ?""",
        (f",{db.TAG_SOFT_SUPRIMIDO},", SOFT_BOUNCE_LIMITE)).fetchall()
    removidos = []
    for r in alvos:
        if db.marcar_soft_suprimido(c, r["email"]):
            removidos.append(r["email"])
    c.commit()
    c.close()
    return removidos


def poll_bounces():
    if not online():
        return {"ok": False, "motivo": "offline"}
    try:
        return {"ok": True, **scan_bounces()}
    except Exception as e:
        log(f"poll_bounces erro: {e}")
        return {"ok": False, "erro": str(e)}


def backfill_threads():
    """Copia sends.thread → contacts.thread para contatos de campanha sem thread (1x)."""
    c = db.conn()
    n = c.execute("""UPDATE contacts SET thread=(
            SELECT s.thread FROM sends s
            WHERE s.email=contacts.email AND s.campaign_id IS NOT NULL AND s.thread!=''
            ORDER BY s.id DESC LIMIT 1)
        WHERE (thread='' OR thread IS NULL) AND email IN (
            SELECT email FROM sends WHERE campaign_id IS NOT NULL AND thread!='')""").rowcount
    c.commit()
    c.close()
    return n


def poll_replies():
    """Loop leve de detecção de respostas (claude -p só roda quando há resposta nova)."""
    if not online():
        return {"ok": False, "motivo": "offline"}
    try:
        return {"ok": True, **scan_replies()}
    except Exception as e:
        log(f"poll_replies erro: {e}")
        return {"ok": False, "erro": str(e)}


def _h_campaign_send(payload):
    cid, email, etapa = payload["campaign_id"], payload["email"], payload["etapa"]
    c = db.conn()
    camp = c.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
    contato = c.execute("SELECT * FROM contacts WHERE email=?", (email,)).fetchone()
    cc = c.execute("SELECT * FROM campaign_contacts WHERE campaign_id=? AND email=?",
                   (cid, email)).fetchone()
    steps = c.execute("SELECT * FROM campaign_steps WHERE campaign_id=? ORDER BY ordem",
                      (cid,)).fetchall()
    if not (camp and contato and cc and steps):
        c.close()
        return "item órfão — ignorado"
    sender = db.sender_get(camp["sender_id"] or 0)
    if not sender or (sender.get("email") or "").strip().lower() != "genau@yoobe.co":
        c.execute("UPDATE campaign_contacts SET status='pendente',next_send_at=NULL WHERE campaign_id=? AND email=?",
                  (cid, email))
        c.commit()
        c.close()
        return "bloqueado: campanha legado não envia por remetente antigo"
    # Uma tarefa antiga/duplicada nunca pode reenviar uma etapa já avançada.
    # A fila é protegida atomicamente, e esta é a segunda trava junto ao estado
    # atual do contato na campanha.
    etapa_esperada = cc["etapa_atual"] + 1
    if cc["status"] != "ativo" or etapa != etapa_esperada:
        c.close()
        return (f"envio obsoleto — etapa solicitada {etapa}, "
                f"etapa esperada {etapa_esperada}, status {cc['status']}")
    # Campanhas finitas nunca repetem uma etapa. Nas infinitas, repetir depois
    # de completar a volta é comportamento intencional; a reserva atômica e o
    # estado acima continuam protegendo contra duas execuções simultâneas.
    if camp["tipo"] != "infinita":
        ja_enviado = c.execute(
            "SELECT msgid FROM sends WHERE campaign_id=? AND email=? AND etapa=? LIMIT 1",
            (cid, email, f"camp{cid}-etapa{etapa}"),
        ).fetchone()
        if ja_enviado:
            c.close()
            return f"já enviado — duplicidade bloqueada (msg {ja_enviado['msgid']})"
    step = next((s for s in steps if s["ordem"] == etapa), None)
    if not step:
        c.close()
        return "etapa inexistente"
    tpl = template_get(step["template_ref"])
    contato_d = dict(contato)

    ok, motivo = pode_enviar(c, camp, contato_d, cc, tpl,
                             manual_immediate=bool(payload.get("immediate")))
    if not ok:
        if "janela comercial" in motivo:
            # fora da janela (noite/fim de semana): NÃO descarta — reagenda p/ o próximo dia
            # útil às 9h e devolve o contato à fila quando vencer (sem queimar tentativas).
            prox = clamp_comercial(datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
            c.execute("UPDATE campaign_contacts SET next_send_at=? WHERE campaign_id=? AND email=?",
                      (prox, cid, email))
            c.commit()
            c.close()
            log(f"fora da janela [{camp['nome']} → {email}] — reagendado para {prox}")
            return f"fora da janela — reagendado para {prox}"
        # Bloqueios definitivos não podem deixar next_send_at vencido. Caso
        # contrário, o gatilho de 10 s recria indefinidamente a mesma tarefa e
        # a interface parece dizer que a campanha "não para". Resposta pausa
        # explicitamente o contato; demais bloqueios devolvem-no a pendente e
        # exigem uma nova ativação/agendamento depois que a causa for revisada.
        novo_status = ("pausado_resposta" if "contato respondeu" in motivo else "pendente")
        c.execute("UPDATE campaign_contacts SET status=?, next_send_at=NULL "
                  "WHERE campaign_id=? AND email=?", (novo_status, cid, email))
        c.commit()
        c.close()
        log(f"envio bloqueado [{camp['nome']} → {email} etapa {etapa}]: {motivo}")
        return f"bloqueado: {motivo}"

    # gate final: reler a thread ANTES de enviar — resposta interrompe a sequência
    if contato_d.get("thread"):
        externas = sync.check_thread_via_gws(contato_d["thread"], email)
        if externas is None:
            c.close()
            raise RuntimeError("não consegui verificar a thread antes do envio")
        if externas > 0:
            c.close()
            return "contato respondeu na thread — sequência interrompida"

    assunto, html = render_html(step["template_ref"], contato_d, sender, cid)
    plain = render_vars(tpl.get("texto_plano") or "", contato_d)
    sent_id, thread_id, track_id = enviar_email(contato_d, assunto, html, plain,
                                                contato_d.get("thread", ""), cid, str(etapa), sender)
    if not sent_id:
        c.close()
        raise RuntimeError("gws messages send não retornou id")

    hoje = datetime.now().strftime("%Y-%m-%d")
    enviado_em = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")  # UTC, casa com events.ts
    with open(db.PROPOSALS / "sent_log.txt", "a") as f:
        f.write(f"{hoje} | {sent_id} | {email} | camp{cid}-etapa{etapa} | thread:{thread_id or 'nova'}\n")
    c.execute("INSERT OR IGNORE INTO sends(data,msgid,email,etapa,thread,campaign_id,track_id,enviado_em) "
              "VALUES(?,?,?,?,?,?,?,?)",
              (hoje, sent_id, email, f"camp{cid}-etapa{etapa}", thread_id, cid, track_id, enviado_em))
    # guarda a thread/msg DO ENVIO (na caixa do remetente) para a detecção de respostas e o
    # threading do próximo follow-up. Sempre sobrescreve: se a thread antiga era de outra caixa
    # (cross-mailbox), abrimos uma nova e precisamos consolidar aqui. Ver memory
    # campanha-thread-cross-mailbox.
    if thread_id:
        c.execute("UPDATE contacts SET envios=envios+1, ultimo_envio=?, thread=?, msgid=? WHERE email=?",
                  (hoje, thread_id, sent_id, email))
    else:
        c.execute("UPDATE contacts SET envios=envios+1, ultimo_envio=? WHERE email=?", (hoje, email))

    total_steps = len(steps)
    if camp["tipo"] == "finita" and etapa >= total_steps:
        c.execute("UPDATE campaign_contacts SET etapa_atual=?, status='concluido', next_send_at=NULL "
                  "WHERE campaign_id=? AND email=?", (etapa, cid, email))
    else:
        prox = next((s for s in steps if s["ordem"] == etapa + 1), None)
        if not prox and camp["tipo"] == "infinita":
            prox = steps[etapa % total_steps]
        if prox and prox["offset_dias"] is not None:
            # gap entre etapas contado em dias ÚTEIS (não envia sáb/dom)
            gap = (prox["offset_dias"] or 0) - (step["offset_dias"] or 0)
            quando = soma_dias_uteis(datetime.now(), gap).strftime("%Y-%m-%d %H:%M:%S")
        else:
            quando = proximo_envio(dict(camp))
        # etapa_atual reflete o PRÓXIMO step (o enfileirador usa etapa_atual+1).
        # No infinito, após a última etapa prox volta à ordem 1 → etapa_atual=0 → recicla.
        nova_etapa = (prox["ordem"] - 1) if prox else etapa
        c.execute("UPDATE campaign_contacts SET etapa_atual=?, next_send_at=? "
                  "WHERE campaign_id=? AND email=?", (nova_etapa, quando, cid, email))
    c.commit()
    c.close()
    log(f"ENVIADO camp {cid} etapa {etapa} → {email} (msg {sent_id})")
    return f"msg {sent_id}"


HANDLERS = {"check_thread": _h_check_thread, "whatsapp": _h_whatsapp,
            "sync": _h_sync, "campaign_send": _h_campaign_send, "track_sync": _h_track_sync}


# ---------------- ciclo principal (cron/30min + thread do servidor) ----------------

def has_active_campaigns():
    """True se existe ao menos uma campanha ativa — gate para os loops de background."""
    c = db.conn()
    row = c.execute("SELECT 1 FROM campaigns WHERE status='ativa' LIMIT 1").fetchone()
    c.close()
    return bool(row)


def has_recent_sends(days=14):
    """True se houve envio nos últimos `days` dias — threads ainda podem receber resposta."""
    c = db.conn()
    row = c.execute("SELECT 1 FROM sends WHERE data >= date('now','localtime',?) LIMIT 1",
                    (f"-{int(days)} days",)).fetchone()
    c.close()
    return bool(row)


def _enfileirar_envios_devidos(c, campaign_id=None):
    """Campanhas ativas: contatos com next_send_at vencido viram itens campaign_send."""
    n = 0
    filtro_campanha = " AND cc.campaign_id=?" if campaign_id is not None else ""
    params = (campaign_id,) if campaign_id is not None else ()
    rows = c.execute("""SELECT cc.campaign_id, cc.email, cc.etapa_atual, cp.tipo
                        FROM campaign_contacts cc JOIN campaigns cp ON cp.id=cc.campaign_id
                        WHERE cp.status='ativa' AND cc.status='ativo'
                          AND cc.next_send_at IS NOT NULL
                          AND cc.next_send_at <= datetime('now','localtime')""" + filtro_campanha,
                     params).fetchall()
    for r in rows:
        etapa = r["etapa_atual"] + 1
        run_at = (datetime.now() + timedelta(minutes=n * ESPACAMENTO_ENVIOS_MIN)).strftime("%Y-%m-%d %H:%M:%S")
        qid = db.enqueue_once(
            "campaign_send",
            {"campaign_id": r["campaign_id"], "email": r["email"], "etapa": etapa},
            run_at,
        )
        if qid is not None:
            n += 1
    return n


def _claim_queue_item(item_id):
    """Reserva uma tarefa para um único worker antes de qualquer efeito externo."""
    c = db.conn()
    changed = c.execute(
        "UPDATE queue SET status='processing', updated_at=? "
        "WHERE id=? AND status='pending'",
        (db.now(), item_id),
    ).rowcount
    c.commit()
    c.close()
    return changed == 1


def _process_queue_item(item):
    """Processa uma tarefa já selecionada, com claim atômico e retry controlado."""
    if not _claim_queue_item(item["id"]):
        return False, False
    payload = json.loads(item["payload"])
    try:
        nota = HANDLERS[item["tipo"]](payload)
        cu = db.conn()
        cu.execute("UPDATE queue SET status='done', updated_at=?, error=? WHERE id=?",
                   (db.now(), str(nota)[:300], item["id"]))
        cu.commit()
        cu.close()
        log(f"fila #{item['id']} {item['tipo']} ok: {nota}")
        return True, False
    except Exception as e:
        attempts = item["attempts"] + 1
        cu = db.conn()
        if attempts >= MAX_ATTEMPTS:
            cu.execute("UPDATE queue SET status='failed', attempts=?, updated_at=?, error=? WHERE id=?",
                       (attempts, db.now(), str(e)[:300], item["id"]))
            whatsapp(f"⚠️ *Yoobe.mail*: item #{item['id']} ({item['tipo']}) falhou "
                     f"{MAX_ATTEMPTS}x e foi marcado como failed.\nErro: {str(e)[:150]}")
        else:
            backoff = (datetime.now() + timedelta(minutes=5 * attempts)).strftime("%Y-%m-%d %H:%M:%S")
            cu.execute("UPDATE queue SET status='pending', attempts=?, run_at=?, updated_at=?, error=? WHERE id=?",
                       (attempts, backoff, db.now(), str(e)[:300], item["id"]))
        cu.commit()
        cu.close()
        log(f"fila #{item['id']} {item['tipo']} ERRO ({attempts}/{MAX_ATTEMPTS}): {e}")
        return True, True


def process_due_campaign_sends(campaign_id=None):
    """Gatilho leve: dispara envios vencidos sem esperar o ciclo de 30 minutos."""
    resultado = {"online": online(), "enfileirados": 0, "processados": 0, "falhas": 0}
    if not resultado["online"]:
        return resultado
    c = db.conn()
    resultado["enfileirados"] = _enfileirar_envios_devidos(c, campaign_id)
    filtro = " AND payload LIKE ?" if campaign_id is not None else ""
    params = (f'%"campaign_id": {campaign_id},%',) if campaign_id is not None else ()
    itens = c.execute(
        "SELECT * FROM queue WHERE tipo='campaign_send' AND status='pending' "
        "AND run_at <= datetime('now','localtime')" + filtro + " ORDER BY run_at LIMIT 50",
        params,
    ).fetchall()
    c.close()
    for item in itens:
        claimed, failed = _process_queue_item(item)
        if claimed:
            resultado["processados"] += 1
        if failed:
            resultado["falhas"] += 1
    return resultado


def process_cycle():
    db.init()
    resultado = {"online": online(), "processados": 0, "falhas": 0, "enfileirados": 0,
                 "continuados": 0, "aguardando_frank": 0}
    c = db.conn()
    pendentes = c.execute("SELECT COUNT(*) AS n FROM queue WHERE status='pending'").fetchone()["n"]

    if not resultado["online"]:
        # offline: NADA é marcado como processado; itens seguem pending para o catch-up
        c.close()
        log(f"OFFLINE — ciclo encerrado sem processar ({pendentes} item(ns) preservados na fila)")
        resultado["pendentes"] = pendentes
        return resultado

    last_ok = db.meta_get("last_successful_run", "")
    gap_h = 0.0
    if last_ok:
        gap_h = (datetime.now() - datetime.strptime(last_ok, "%Y-%m-%d %H:%M:%S")).total_seconds() / 3600

    resultado["enfileirados"] = _enfileirar_envios_devidos(c)

    # rastreio: um track_sync por ciclo (se ainda não houver um pendente)
    if TRACKING_ON and not c.execute(
            "SELECT 1 FROM queue WHERE tipo='track_sync' AND status='pending'").fetchone():
        db.enqueue("track_sync", {})

    itens = c.execute("""SELECT * FROM queue WHERE status='pending'
                         AND run_at <= datetime('now','localtime')
                         ORDER BY run_at LIMIT 50""").fetchall()
    c.close()
    for item in itens:
        claimed, failed = _process_queue_item(item)
        if claimed:
            resultado["processados"] += 1
            if item["tipo"] == "campaign_send":
                resultado["continuados"] += 1
        if failed:
            resultado["falhas"] += 1

    cq = db.conn()
    resultado["aguardando_frank"] = cq.execute(
        "SELECT COUNT(*) AS n FROM campaign_contacts WHERE status='pausado_resposta'").fetchone()["n"]
    cq.close()

    db.meta_set("last_successful_run", db.now())
    if gap_h > GAP_AVISO_HORAS:
        whatsapp(f"🔌 *Yoobe.mail de volta*: fiquei {gap_h:.1f}h offline.\n"
                 f"Processei {resultado['processados']} item(ns) da fila "
                 f"({resultado['continuados']} envio(s) continuados, "
                 f"{resultado['aguardando_frank']} contato(s) aguardando você), "
                 f"reagendei {resultado['enfileirados']} envio(s) de campanha.")
        log(f"gap de {gap_h:.1f}h notificado no WhatsApp")
    return resultado


if __name__ == "__main__":
    print(json.dumps(process_cycle(), ensure_ascii=False, indent=2))
