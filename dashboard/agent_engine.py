#!/usr/bin/env python3
"""Yoobe.mail — cliente Vertex AI Agent Engine + fallback generate_json.

Quando VERTEX_AGENT_* está configurado, consulta o agente remoto (ADK deploy).
Caso contrário (ou em falha), usa vertex.generate_json com o mesmo prompt.
Registra tokens/latência em agent_usage.
"""
from __future__ import annotations

import json
import re
import threading
from typing import Any

import agent_usage
import vertex

_INIT_LOCK = threading.Lock()
_VERTEXAI_READY = False

_AGENT_ENV = {
    "name_analyzer": "VERTEX_AGENT_NAME_ANALYZER",
    "lead_validator": "VERTEX_AGENT_LEAD_VALIDATOR",
    "scrape_planner": "VERTEX_AGENT_SCRAPE_PLANNER",
    "campaign_writer": "VERTEX_AGENT_CAMPAIGN_WRITER",
    "reply_classifier": "VERTEX_AGENT_REPLY_CLASSIFIER",
}


def _agent_resource(kind: str) -> str:
    key = _AGENT_ENV.get(kind, "")
    return (vertex.env_get(key, "") or "").strip() if key else ""


def _ensure_vertexai():
    global _VERTEXAI_READY
    if _VERTEXAI_READY:
        return
    with _INIT_LOCK:
        if _VERTEXAI_READY:
            return
        import vertexai
        project = vertex.env_get("VERTEX_PROJECT_ID", "mkt-4unik") or "mkt-4unik"
        # Agent Engine vive em us-central1 (ver agents/README.md), distinto de
        # VERTEX_LOCATION=global usado no generateContent.
        location = vertex.env_get("VERTEX_AGENT_LOCATION", "us-central1") or "us-central1"
        vertexai.init(project=project, location=location)
        _VERTEXAI_READY = True


def _extract_json(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        raise RuntimeError("agente devolveu resposta vazia")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise RuntimeError(f"agente não devolveu JSON: {text[:200]}")
        return json.loads(m.group(0))


def _stream_agent_text(resource_name: str, message: str, user_id: str = "dashboard") -> str:
    _ensure_vertexai()
    from vertexai import agent_engines
    agent = agent_engines.get(resource_name)
    chunks: list[str] = []
    for event in agent.stream_query(user_id=user_id, message=message):
        if isinstance(event, dict):
            if event.get("content"):
                parts = event["content"].get("parts") if isinstance(event["content"], dict) else None
                if parts:
                    for p in parts:
                        if isinstance(p, dict) and p.get("text"):
                            chunks.append(p["text"])
                elif isinstance(event["content"], str):
                    chunks.append(event["content"])
            elif event.get("text"):
                chunks.append(str(event["text"]))
            elif event.get("output"):
                chunks.append(str(event["output"]))
            # Alguns SDKs expõem usage no evento final
            usage = event.get("usageMetadata") or event.get("usage") or {}
            if usage:
                event["_usage_captured"] = usage
        elif isinstance(event, str):
            chunks.append(event)
    return "".join(chunks).strip()


def _generate_fallback(prompt: str, timeout: int = 240, tier: str = "flash",
                       agent: str = "", task_type: str = "") -> dict:
    if tier == "flash":
        return vertex.generate_json(prompt, model=vertex.MODEL_FLASH,
                                    timeout=timeout, thinking_budget=0,
                                    agent=agent, task_type=task_type or "fallback")
    return vertex.generate_json(prompt, model=vertex.MODEL_PRO, timeout=timeout,
                                agent=agent, task_type=task_type or "fallback")


def query_agent_json(kind: str, prompt: str, timeout: int = 240,
                     tier: str = "flash", task_type: str = "") -> dict[str, Any]:
    """Consulta agente ADK (se configurado) ou cai no generate_json local."""
    resource = _agent_resource(kind)
    task = task_type or kind
    agent_usage.set_run_context(agent=kind, task_type=task)
    if resource:
        try:
            with agent_usage.timed_run() as tr:
                text = _stream_agent_text(resource, prompt)
            parsed = _extract_json(text)
            # Agent Engine stream nem sempre devolve usage — registra latência
            agent_usage.log_run(
                agent=kind, task_type=task, model=f"agent_engine:{tier}",
                latency_ms=tr.ms, ok=True, fallback=False,
                detalhe=f"Agent Engine {kind}",
                meta={"resource": resource[:80], "chars": len(text)})
            return parsed
        except Exception as exc:
            # fallback transparente — não quebra Qualidade sem deploy
            try:
                agent_usage.set_run_context(agent=kind, task_type=task, fallback=True)
                return _generate_fallback(prompt, timeout=timeout, tier=tier,
                                          agent=kind, task_type=task)
            except Exception:
                agent_usage.log_run(
                    agent=kind, task_type=task, latency_ms=0, ok=False,
                    fallback=True, erro=str(exc)[:300])
                raise RuntimeError(f"Agent Engine ({kind}) falhou: {exc}") from exc
    return _generate_fallback(prompt, timeout=timeout, tier=tier,
                              agent=kind, task_type=task)


def name_analyzer_json(prompt: str, timeout: int = 240) -> dict:
    return query_agent_json("name_analyzer", prompt, timeout=timeout, tier="flash",
                            task_type="analisar_nomes")


def lead_validator_json(prompt: str, timeout: int = 180) -> dict:
    return query_agent_json("lead_validator", prompt, timeout=timeout, tier="flash",
                            task_type="validar_lead")


def scrape_planner_json(prompt: str, timeout: int = 60) -> dict:
    return query_agent_json("scrape_planner", prompt, timeout=timeout, tier="flash",
                            task_type="planejar_scrape")
