#!/usr/bin/env python3
"""Yoobe.mail — cliente Grok (Vertex OpenAPI partner + fallback xAI direto).

Grok na Vertex usa Chat Completions OpenAI-compat em
`.../endpoints/openapi/chat/completions` (não o generateContent do Gemini).
Credencial Vertex = mesma SA de vertex.py. Se Vertex falhar e
XAI_API_KEY/GROK_API_KEY existir, tenta https://api.x.ai/v1.

Default de modelo no projeto mkt-4unik: xai/grok-4.20-reasoning (habilitado).
Troque para xai/grok-4.5 em VERTEX_GROK_MODEL quando o card estiver no Model Garden.
"""
from __future__ import annotations

import json
import re

import requests

import agent_usage
import vertex

GROK_ENABLED = (vertex.env_get("GROK_ENABLED", "1") or "1").strip().lower() in (
    "1", "true", "yes", "on",
)
VERTEX_GROK_MODEL = (vertex.env_get("VERTEX_GROK_MODEL", "xai/grok-4.20-reasoning")
                     or "xai/grok-4.20-reasoning").strip()
VERTEX_GROK_LOCATION = (
    vertex.env_get("VERTEX_GROK_LOCATION", "")
    or vertex.env_get("VERTEX_LOCATION", "global")
    or "global"
).strip()
# Aceita XAI_API_KEY ou GROK_API_KEY (alias usado em outros projetos locais).
XAI_API_KEY = (
    vertex.env_get("XAI_API_KEY", "")
    or vertex.env_get("GROK_API_KEY", "")
    or ""
).strip()
XAI_GROK_MODEL = (vertex.env_get("XAI_GROK_MODEL", "grok-4.5") or "grok-4.5").strip()
XAI_BASE = "https://api.x.ai/v1"


def enabled() -> bool:
    return GROK_ENABLED


def _parse_json_text(txt: str) -> dict:
    txt = (txt or "").strip()
    if not txt:
        raise RuntimeError("Grok devolveu resposta vazia")
    try:
        parsed = json.loads(txt)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", txt, re.S)
        if not m:
            raise RuntimeError(f"Grok não retornou JSON: {txt[:200]}")
        parsed = json.loads(m.group(0))
    if not isinstance(parsed, dict):
        raise RuntimeError("Grok devolveu JSON que não é objeto")
    return parsed


def _usage_openai(data: dict) -> dict:
    u = (data or {}).get("usage") or {}
    prompt = int(u.get("prompt_tokens") or u.get("input_tokens") or 0)
    completion = int(u.get("completion_tokens") or u.get("output_tokens") or 0)
    total = int(u.get("total_tokens") or 0)
    if total <= 0:
        total = prompt + completion
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}


def _chat_body(prompt: str, model: str) -> dict:
    return {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Você responde SOMENTE com um único objeto JSON válido, "
                    "sem markdown, sem comentários e sem texto fora do JSON."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
    }


def _vertex_openapi_url() -> str:
    loc = VERTEX_GROK_LOCATION
    host = ("aiplatform.googleapis.com" if loc == "global"
            else f"{loc}-aiplatform.googleapis.com")
    return (f"https://{host}/v1/projects/{vertex.PROJECT_ID}/locations/{loc}"
            f"/endpoints/openapi/chat/completions")


def _call_vertex(prompt: str, timeout: int) -> tuple[dict, str, dict]:
    token = vertex._token()
    r = requests.post(
        _vertex_openapi_url(),
        json=_chat_body(prompt, VERTEX_GROK_MODEL),
        timeout=timeout,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    if not r.ok:
        raise RuntimeError(
            f"Vertex Grok HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"Vertex Grok sem choices: {str(data)[:200]}")
    msg = (choices[0].get("message") or {}).get("content") or ""
    return _parse_json_text(msg), VERTEX_GROK_MODEL, data


def _call_xai(prompt: str, timeout: int) -> tuple[dict, str, dict]:
    if not XAI_API_KEY:
        raise RuntimeError("XAI_API_KEY não configurada")
    r = requests.post(
        f"{XAI_BASE}/chat/completions",
        json=_chat_body(prompt, XAI_GROK_MODEL),
        timeout=timeout,
        headers={
            "Authorization": f"Bearer {XAI_API_KEY}",
            "Content-Type": "application/json",
        },
    )
    if not r.ok:
        raise RuntimeError(f"xAI Grok HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"xAI Grok sem choices: {str(data)[:200]}")
    msg = (choices[0].get("message") or {}).get("content") or ""
    return _parse_json_text(msg), XAI_GROK_MODEL, data


def generate_json(prompt: str, timeout: int = 180,
                  agent: str = "grok_critic", task_type: str = "",
                  detalhe: str = "") -> dict:
    """Gera JSON via Vertex Grok; se falhar e houver XAI_API_KEY, tenta api.x.ai."""
    if not GROK_ENABLED:
        raise RuntimeError("Grok desabilitado (GROK_ENABLED=0)")

    last_err = None
    with agent_usage.timed_run() as tr:
        # 1) Vertex OpenAPI
        try:
            parsed, model, raw = _call_vertex(prompt, timeout)
            usage = _usage_openai(raw)
            agent_usage.log_run(
                agent=agent or "grok_critic", task_type=task_type,
                model=model, latency_ms=tr.ms, ok=True,
                detalhe=detalhe or "vertex-openapi",
                meta={"via": "vertex"}, **usage)
            parsed["_via"] = "vertex"
            parsed["_model"] = model
            return parsed
        except Exception as exc:
            last_err = exc
            agent_usage.log_run(
                agent=agent or "grok_critic", task_type=task_type,
                model=VERTEX_GROK_MODEL, latency_ms=tr.ms, ok=False,
                erro=str(exc)[:300], detalhe=detalhe or "vertex-openapi",
                meta={"via": "vertex"})

        # 2) Fallback xAI direto
        if XAI_API_KEY:
            try:
                parsed, model, raw = _call_xai(prompt, timeout)
                usage = _usage_openai(raw)
                agent_usage.log_run(
                    agent=agent or "grok_critic", task_type=task_type,
                    model=model, latency_ms=tr.ms, ok=True, fallback=True,
                    detalhe=detalhe or "xai-direct",
                    meta={"via": "xai"}, **usage)
                parsed["_via"] = "xai"
                parsed["_model"] = model
                return parsed
            except Exception as exc:
                last_err = exc
                agent_usage.log_run(
                    agent=agent or "grok_critic", task_type=task_type,
                    model=XAI_GROK_MODEL, latency_ms=tr.ms, ok=False,
                    fallback=True, erro=str(exc)[:300],
                    detalhe=detalhe or "xai-direct",
                    meta={"via": "xai"})

    raise RuntimeError(f"Grok indisponível: {last_err}")


def grok_status() -> dict:
    """Diagnóstico leve: enabled + auth Vertex (+ xAI se houver chave)."""
    st = {
        "ok": False,
        "provider": "grok",
        "enabled": GROK_ENABLED,
        "modelo": VERTEX_GROK_MODEL,
        "auth": "",
        "xai_fallback": bool(XAI_API_KEY),
        "mensagem": "",
    }
    if not GROK_ENABLED:
        st["mensagem"] = "Grok desabilitado (GROK_ENABLED=0)"
        return st
    try:
        vertex._token()
        st["ok"] = True
        st["auth"] = f"sa @ {vertex.PROJECT_ID} ({VERTEX_GROK_LOCATION})"
        st["mensagem"] = (
            f"Grok pronto via Vertex OpenAPI ({VERTEX_GROK_MODEL})"
            + (" · fallback xAI configurado" if XAI_API_KEY else "")
        )
    except Exception as exc:
        if XAI_API_KEY:
            st["ok"] = True
            st["auth"] = "XAI_API_KEY"
            st["modelo"] = XAI_GROK_MODEL
            st["mensagem"] = (
                f"Vertex SA falhou ({str(exc)[:80]}); fallback xAI disponível"
            )
        else:
            st["mensagem"] = f"Grok indisponível: {str(exc)[:200]}"
    return st
