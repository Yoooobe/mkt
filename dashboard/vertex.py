#!/usr/bin/env python3
"""4unik.mail — backend Vertex AI (Gemini) para as gerações de IA do dashboard.

Chama generateContent via REST (mesmo padrão do lib/llm.ts do projeto afiliados),
autenticando com a service account do projeto GCP mkt-4unik. JSON mode
(responseMimeType) garante que os fluxos existentes de parse continuem valendo.

A credencial nunca fica no repositório: VERTEX_CREDENTIALS_JSON aponta para um
arquivo local (ou contém o JSON inline, útil como secret em container).
"""
import json
import os
import re
import threading

import requests
from google.auth.transport.requests import Request
from google.oauth2 import service_account

import db

_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
_ENV_CACHE = None
_CREDS = None
_CREDS_LOCK = threading.Lock()


def env_get(key, default=""):
    """os.environ primeiro; senão dashboard/.env (o LaunchAgent não faz source)."""
    global _ENV_CACHE
    if key in os.environ:
        return os.environ[key]
    if _ENV_CACHE is None:
        cache = {}
        f = db.BASE / ".env"
        if f.exists():
            for line in f.read_text().splitlines():
                m = re.match(r'^\s*([A-Z][A-Z0-9_]*)\s*=\s*"?([^"#]*)"?\s*(#.*)?$', line)
                if m:
                    cache[m.group(1)] = m.group(2).strip()
        _ENV_CACHE = cache
    return _ENV_CACHE.get(key, default)


PROJECT_ID = env_get("VERTEX_PROJECT_ID", "mkt-4unik")
LOCATION = env_get("VERTEX_LOCATION", "global")
MODEL_PRO = env_get("VERTEX_MODEL_PRO", "gemini-2.5-pro")
MODEL_FLASH = env_get("VERTEX_MODEL_FLASH", "gemini-2.5-flash")


def _load_sa_info():
    raw = env_get("VERTEX_CREDENTIALS_JSON",
                  os.path.expanduser("~/.config/4unik/vertex-sa.json")).strip()
    if not raw:
        raise RuntimeError("VERTEX_CREDENTIALS_JSON não configurado (dashboard/.env)")
    if raw.startswith("{"):
        return json.loads(raw)
    path = os.path.expanduser(raw)
    if not os.path.exists(path):
        raise RuntimeError(f"credencial Vertex não encontrada: {path}")
    return json.loads(open(path).read())


def _token():
    """Access token da service account, com refresh thread-safe."""
    global _CREDS
    with _CREDS_LOCK:
        if _CREDS is None:
            _CREDS = service_account.Credentials.from_service_account_info(
                _load_sa_info(), scopes=_SCOPES)
        if not _CREDS.valid:
            _CREDS.refresh(Request())
        return _CREDS.token


def _endpoint(model):
    host = ("aiplatform.googleapis.com" if LOCATION == "global"
            else f"{LOCATION}-aiplatform.googleapis.com")
    return (f"https://{host}/v1/projects/{PROJECT_ID}/locations/{LOCATION}"
            f"/publishers/google/models/{model}:generateContent")


def generate_json(prompt, model=None, timeout=300, thinking_budget=None):
    """Gera e devolve um dict a partir de um prompt que pede JSON.

    thinking_budget=0 desliga o raciocínio interno (classificações rápidas);
    None mantém o default do modelo (gerações longas de campanha)."""
    model = model or MODEL_PRO
    gen_cfg = {"responseMimeType": "application/json", "maxOutputTokens": 32768}
    if thinking_budget is not None:
        gen_cfg["thinkingConfig"] = {"thinkingBudget": int(thinking_budget)}
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": gen_cfg,
    }
    r = requests.post(_endpoint(model), json=body, timeout=timeout,
                      headers={"Authorization": f"Bearer {_token()}"})
    if not r.ok:
        raise RuntimeError(f"Vertex {model} HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    cands = data.get("candidates") or []
    if not cands:
        block = (data.get("promptFeedback") or {}).get("blockReason", "sem candidates")
        raise RuntimeError(f"Vertex {model} não gerou resposta ({block})")
    cand = cands[0]
    txt = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts") or [])
    if not txt.strip():
        raise RuntimeError(f"Vertex {model} devolveu vazio (finishReason={cand.get('finishReason')})")
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", txt, re.S)
        if not m:
            raise RuntimeError(f"Vertex {model} não retornou JSON: {txt[:200]}")
        return json.loads(m.group(0))


def vertex_status():
    """Diagnóstico leve: valida credencial/token sem gastar tokens de modelo."""
    try:
        _token()
        return {"ok": True, "provider": "vertex",
                "modelo": f"{MODEL_PRO} / {MODEL_FLASH}",
                "auth": f"sa @ {PROJECT_ID} ({LOCATION})",
                "mensagem": "Vertex pronto para gerar rascunhos"}
    except Exception as exc:
        return {"ok": False, "provider": "vertex",
                "modelo": f"{MODEL_PRO} / {MODEL_FLASH}", "auth": "",
                "mensagem": f"Vertex indisponível: {str(exc)[:200]}"}
