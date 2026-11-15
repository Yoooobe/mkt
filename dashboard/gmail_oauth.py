import base64
import hashlib
import html
import json
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests
from cryptography.fernet import Fernet, InvalidToken
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

import db

_lock = threading.Lock()
_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"


def _env():
    values = dict(os.environ)
    path = db.BASE / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    return values


def _client():
    env = _env()
    client_id = env.get("GMAIL_OAUTH_CLIENT_ID", "").strip()
    client_secret = env.get("GMAIL_OAUTH_CLIENT_SECRET", "").strip()
    redirect_uri = env.get("GMAIL_OAUTH_REDIRECT_URI", "").strip()
    if not client_id or not client_secret or not redirect_uri:
        raise ValueError("OAuth remoto não configurado no Worker.")
    if not redirect_uri.startswith("https://"):
        raise ValueError("O callback OAuth remoto precisa usar HTTPS.")
    return client_id, client_secret, redirect_uri


def _fernet():
    secret = _env().get("GMAIL_OAUTH_ENCRYPTION_KEY", "").strip()
    if not secret:
        raise ValueError("Chave de criptografia OAuth não configurada no Worker.")
    try:
        return Fernet(secret.encode())
    except Exception as exc:
        raise ValueError("Chave de criptografia OAuth inválida no Worker.") from exc


def _token_key(sid):
    return f"gmail_oauth_token_{int(sid)}"


def _read_record(sid):
    raw = db.meta_get(_token_key(sid), "")
    if not raw:
        return None
    try:
        return json.loads(_fernet().decrypt(raw.encode()).decode())
    except InvalidToken as exc:
        raise ValueError("A credencial Gmail armazenada não pôde ser descriptografada.") from exc


def _save_record(sid, record):
    raw = _fernet().encrypt(json.dumps(record, separators=(",", ":")).encode()).decode()
    db.meta_set(_token_key(sid), raw)


def has_credentials(sid):
    try:
        record = _read_record(sid)
        return bool(record and record.get("refresh_token"))
    except ValueError:
        return False


def begin(sid):
    sender = db.sender_get(sid)
    if not sender or (sender.get("email") or "").strip().lower() != "genau@yoobe.co":
        raise ValueError("OAuth remoto está liberado somente para genau@yoobe.co.")
    client_id, _, redirect_uri = _client()
    _fernet()
    state = secrets.token_urlsafe(32)
    db.meta_set("gmail_oauth_pending", json.dumps({
        "sid": int(sid),
        "state_hash": hashlib.sha256(state.encode()).hexdigest(),
        "expires_at": int(time.time()) + 600,
    }))
    query = urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "login_hint": sender["email"],
        "state": state,
    })
    return "https://accounts.google.com/o/oauth2/v2/auth?" + query


def complete(code, state):
    if not code or not state:
        raise ValueError("O Google não retornou o código de autorização. Gere um novo link.")
    pending_raw = db.meta_get("gmail_oauth_pending", "")
    try:
        pending = json.loads(pending_raw)
    except Exception as exc:
        raise ValueError("Link de autorização expirado. Gere um novo no painel.") from exc
    state_hash = hashlib.sha256(state.encode()).hexdigest()
    if not secrets.compare_digest(state_hash, pending.get("state_hash", "")):
        raise ValueError("Estado OAuth inválido. Gere um novo link no painel.")
    if int(pending.get("expires_at", 0)) < int(time.time()):
        db.meta_set("gmail_oauth_pending", "")
        raise ValueError("Link de autorização expirado. Gere um novo no painel.")
    sid = int(pending["sid"])
    sender = db.sender_get(sid)
    if not sender or (sender.get("email") or "").strip().lower() != "genau@yoobe.co":
        raise ValueError("O remetente não está autorizado para esta conexão.")
    db.meta_set("gmail_oauth_pending", "")
    client_id, client_secret, redirect_uri = _client()
    response = requests.post(_TOKEN_URL, data={
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }, timeout=20)
    if not response.ok:
        raise ValueError("O Google recusou o código OAuth. Confira o callback cadastrado e gere um novo link.")
    data = response.json()
    access_token = data.get("access_token", "")
    refresh_token = data.get("refresh_token", "")
    if not refresh_token:
        old = _read_record(sid)
        refresh_token = (old or {}).get("refresh_token", "")
    if not access_token or not refresh_token:
        raise ValueError("O Google não forneceu token duradouro. Gere um novo link e aceite as permissões Gmail.")
    profile = requests.get(_PROFILE_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=20)
    if not profile.ok:
        raise ValueError("Não foi possível confirmar a conta Gmail autorizada.")
    email = (profile.json().get("emailAddress") or "").strip().lower()
    if email != "genau@yoobe.co":
        raise ValueError("A conta autorizada foi " + (email or "desconhecida") + "; conecte exatamente genau@yoobe.co.")
    _save_record(sid, {
        "refresh_token": refresh_token,
        "access_token": access_token,
        "expires_at": int(time.time()) + int(data.get("expires_in", 3600)),
        "client_id": client_id,
        "client_secret": client_secret,
        "token_uri": _TOKEN_URL,
        "scopes": data.get("scope", " ".join(_SCOPES)).split(),
    })
    return email


def access_token(sid):
    with _lock:
        record = _read_record(sid)
        if not record or not record.get("refresh_token"):
            raise RuntimeError("Gmail não conectado. Abra Remetentes e conecte genau@yoobe.co.")
        if int(record.get("expires_at", 0)) <= int(time.time()) + 60:
            creds = Credentials(
                token=record.get("access_token"),
                refresh_token=record["refresh_token"],
                token_uri=record.get("token_uri", _TOKEN_URL),
                client_id=record["client_id"],
                client_secret=record["client_secret"],
                scopes=record.get("scopes") or _SCOPES,
            )
            creds.refresh(Request())
            record["access_token"] = creds.token
            record["expires_at"] = int(creds.expiry.replace(tzinfo=timezone.utc).timestamp())
            _save_record(sid, record)
        return record["access_token"]


def access_token_for_home(home):
    slug = os.path.basename(str(home))
    c = db.conn()
    sender = c.execute("SELECT id FROM senders WHERE slug=?", (slug,)).fetchone()
    c.close()
    if not sender:
        raise RuntimeError("Perfil Gmail remoto não associado a um remetente.")
    return access_token(sender["id"])


def result_page(ok, message):
    title = "Conta conectada" if ok else "Falha ao conectar"
    color = "#14805e" if ok else "#a83232"
    return ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            f"<title>{title}</title><main style='font:16px system-ui;max-width:560px;margin:12vh auto;padding:24px'>"
            f"<h1 style='color:{color}'>{title}</h1><p>{html.escape(message)}</p>"
            "<p>Você pode fechar esta aba e voltar ao painel Yoobe Mail.</p></main>")
