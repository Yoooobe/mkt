#!/usr/bin/env python3
"""Yoobe.mail — telemetria de agentes Vertex (tokens, tarefas, conhecimento).

Persiste cada chamada LLM em `agent_runs` e insights curtos em `agent_knowledge`.
O dashboard `#/agentes` consome estes dados para orquestrar e auditar gastos.
Catálogo carregado de `config/agents.json` (fallback embutido).
"""
from __future__ import annotations

import contextvars
import json
import time
from pathlib import Path
from typing import Any, Optional

import db

# Contexto por thread/async task — preenchido pelos workers de campanha/qualidade.
_run_ctx: contextvars.ContextVar[dict] = contextvars.ContextVar("ai_run_ctx", default={})

_CONFIG_PATH = Path(__file__).resolve().parent / "config" / "agents.json"

# Fallback se o JSON não existir (mesma forma do catálogo antigo).
_FALLBACK_AGENTS = [
    {"id": "name_analyzer", "nome": "Name Analyzer", "tier": "flash",
     "via": "agent_engine", "env": "VERTEX_AGENT_NAME_ANALYZER",
     "papel": "Qualidade de nomes + enriquecimento", "skills": [], "pipeline": []},
    {"id": "lead_validator", "nome": "Lead Validator", "tier": "flash",
     "via": "agent_engine", "env": "VERTEX_AGENT_LEAD_VALIDATOR",
     "papel": "Validação de leads com evidência", "skills": [], "pipeline": []},
    {"id": "scrape_planner", "nome": "Scrape Planner", "tier": "flash",
     "via": "agent_engine", "env": "VERTEX_AGENT_SCRAPE_PLANNER",
     "papel": "Planeja uso mínimo do Firecrawl", "skills": [], "pipeline": []},
    {"id": "campaign_writer", "nome": "Campaign Writer", "tier": "pro",
     "via": "generate_json", "env": "VERTEX_AGENT_CAMPAIGN_WRITER",
     "papel": "Copy de campanha (E1…En)",
     "skills": ["email-creative-builder (aaron-he-zhu, Apache-2.0)",
                "copywriting (community)", "email-sequence (community)"],
     "pipeline": ["criar"]},
    {"id": "reply_classifier", "nome": "Reply Classifier", "tier": "flash",
     "via": "generate_json", "env": "VERTEX_AGENT_REPLY_CLASSIFIER",
     "papel": "Classifica respostas inbound", "skills": [], "pipeline": []},
    {"id": "estrategista", "nome": "Estrategista", "tier": "pro",
     "via": "generate_json", "env": "",
     "papel": "Plano / briefing / portfolio",
     "skills": ["gtm-engineer-growth-specialist (Yoobe/GTM, interno)",
                "email-sequence (community)", "growth-engine (community)"],
     "pipeline": ["criar", "melhorias"]},
    {"id": "copywriter", "nome": "Copywriter", "tier": "pro",
     "via": "generate_json", "env": "",
     "papel": "Escreve e-mails da sequência (assuntos ousados)",
     "skills": ["email-creative-builder (aaron-he-zhu, Apache-2.0)",
                "copywriting (community)", "email-sequence (community)"],
     "pipeline": ["criar", "melhorias"]},
    {"id": "editor", "nome": "Editor", "tier": "pro",
     "via": "generate_json", "env": "",
     "papel": "Revisa gancho/CTA com barra do email-creative-builder + copy-editing",
     "skills": ["email-creative-builder (aaron-he-zhu, Apache-2.0)",
                "copy-editing (community)"],
     "pipeline": ["criar"]},
    {"id": "estrategista_gtm", "nome": "Estrategista GTM", "tier": "pro",
     "via": "generate_json", "env": "",
     "papel": "Briefing de lançamento e go/no-go",
     "skills": ["gtm-engineer-growth-specialist (Yoobe/GTM, interno)",
                "growth-engine (community)"],
     "pipeline": ["criar"]},
    {"id": "validador", "nome": "Validador", "tier": "flash",
     "via": "generate_json", "env": "",
     "papel": "Revisa patches de melhoria",
     "skills": ["copy-editing (community)"], "pipeline": ["melhorias"]},
    {"id": "planner", "nome": "Planner de melhorias", "tier": "flash",
     "via": "generate_json", "env": "",
     "papel": "Classifica sugestões e conflitos", "skills": [], "pipeline": ["melhorias"]},
    {"id": "grok_critic", "nome": "Grok Critic", "tier": "pro",
     "via": "grok", "env": "GROK_ENABLED",
     "papel": "Segundo parecer: portfólio, melhorias e SEO/GEO",
     "skills": ["gtm-engineer-growth-specialist (Yoobe/GTM, interno)",
                "growth-engine (community)", "copywriting (community)"],
     "pipeline": ["criar", "melhorias", "seo_geo"]},
]

_FALLBACK_CATEGORIES = {
    "copy": {"agent_key": "copywriter", "max_iterations": 3,
             "guardrails": ["no_budget", "no_audience", "no_schedule", "no_legal"]},
    "compliance": {"agent_key": "validador", "max_iterations": 2,
                   "guardrails": ["always_human"]},
    "outro": {"agent_key": None, "max_iterations": 0, "guardrails": ["always_human"]},
}

_catalog_cache: Optional[dict] = None

# USD por 1M tokens (aprox. Gemini 2.5 — ajustável via meta).
_DEFAULT_RATES = {
    "flash": {"in": 0.15, "out": 0.60},
    "pro": {"in": 1.25, "out": 10.00},
}


def _load_config() -> dict:
    """Recarrega agents.json quando o mtime muda (evita cache stale após editar skills)."""
    global _catalog_cache
    mtime = _CONFIG_PATH.stat().st_mtime if _CONFIG_PATH.is_file() else 0.0
    cached = _catalog_cache
    if cached is not None and cached.get("_mtime") == mtime:
        return cached
    if _CONFIG_PATH.is_file():
        try:
            data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            data["_mtime"] = mtime
            _catalog_cache = data
            return _catalog_cache
        except Exception:
            pass
    _catalog_cache = {"agents": list(_FALLBACK_AGENTS), "categories": dict(_FALLBACK_CATEGORIES),
                      "_mtime": mtime}
    return _catalog_cache


def reload_catalog():
    global _catalog_cache
    _catalog_cache = None
    return _load_config()


def agent_catalog() -> list[dict]:
    cfg = _load_config()
    return list(cfg.get("agents") or _FALLBACK_AGENTS)


def category_map() -> dict:
    cfg = _load_config()
    return dict(cfg.get("categories") or _FALLBACK_CATEGORIES)


# Compat: código antigo importa AGENT_CATALOG
AGENT_CATALOG = agent_catalog()


def set_run_context(**kw):
    cur = dict(_run_ctx.get() or {})
    cur.update({k: v for k, v in kw.items() if v is not None})
    _run_ctx.set(cur)


def clear_run_context():
    _run_ctx.set({})


def get_run_context() -> dict:
    return dict(_run_ctx.get() or {})


def _rates() -> dict:
    raw = db.meta_get("vertex_token_rates", "")
    if raw:
        try:
            return {**_DEFAULT_RATES, **json.loads(raw)}
        except Exception:
            pass
    return dict(_DEFAULT_RATES)


def estimate_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    rates = _rates()
    tier = "pro" if "pro" in (model or "").lower() else "flash"
    r = rates.get(tier) or _DEFAULT_RATES["flash"]
    return round((prompt_tokens / 1_000_000) * float(r.get("in", 0))
                 + (completion_tokens / 1_000_000) * float(r.get("out", 0)), 6)


def brl_from_usd(usd: float) -> float:
    """Conversão opcional via meta usd_brl_rate (default 5.5)."""
    try:
        rate = float(db.meta_get("usd_brl_rate", "5.5") or 5.5)
    except Exception:
        rate = 5.5
    return round(float(usd or 0) * rate, 4)


def log_run(*, agent: str = "", task_type: str = "", model: str = "",
            prompt_tokens: int = 0, completion_tokens: int = 0,
            total_tokens: int = 0, latency_ms: int = 0,
            campaign_id: Optional[int] = None, ok: bool = True,
            fallback: bool = False, erro: str = "",
            detalhe: str = "", knowledge: str = "",
            meta: Optional[dict] = None) -> int:
    """Grava uma execução. Mescla com contexto da thread (campaign_id, agent…)."""
    ctx = get_run_context()
    agent = agent or ctx.get("agent") or "desconhecido"
    task_type = task_type or ctx.get("task_type") or ""
    if campaign_id is None:
        campaign_id = ctx.get("campaign_id")
    if not fallback and ctx.get("fallback"):
        fallback = True
    if total_tokens <= 0:
        total_tokens = int(prompt_tokens or 0) + int(completion_tokens or 0)
    cost = estimate_cost_usd(model, int(prompt_tokens or 0), int(completion_tokens or 0))
    payload = json.dumps(meta or {}, ensure_ascii=False) if meta else ""
    c = db.conn()
    cur = c.execute(
        """INSERT INTO agent_runs(
             agent, task_type, model, prompt_tokens, completion_tokens, total_tokens,
             cost_usd, latency_ms, campaign_id, ok, fallback, erro, detalhe, meta_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (agent, task_type, model or "", int(prompt_tokens or 0),
         int(completion_tokens or 0), int(total_tokens or 0), cost,
         int(latency_ms or 0), campaign_id, 1 if ok else 0, 1 if fallback else 0,
         (erro or "")[:400], (detalhe or "")[:500], payload[:4000]))
    run_id = cur.lastrowid
    if knowledge and ok:
        c.execute(
            """INSERT INTO agent_knowledge(agent, campaign_id, run_id, titulo, corpo, tags)
               VALUES(?,?,?,?,?,?)""",
            (agent, campaign_id, run_id,
             (detalhe or task_type or agent)[:120],
             knowledge[:8000],
             task_type or ""))
    c.commit()
    c.close()
    return int(run_id or 0)


def parse_usage_metadata(data: dict) -> dict:
    """Extrai usageMetadata do generateContent REST."""
    u = (data or {}).get("usageMetadata") or {}
    prompt = int(u.get("promptTokenCount") or u.get("prompt_token_count") or 0)
    completion = int(u.get("candidatesTokenCount") or u.get("candidates_token_count")
                     or u.get("completionTokenCount") or 0)
    total = int(u.get("totalTokenCount") or u.get("total_token_count") or 0)
    if total <= 0:
        total = prompt + completion
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}


def _active_by_agent() -> dict[str, list[dict]]:
    """Deriva agentes ativos a partir de builds/rewrites/improvement runs em andamento."""
    active: dict[str, list[dict]] = {}
    c = db.conn()
    # Builds / rewrites: meta keys build_<id>
    for row in c.execute("SELECT key, value FROM meta WHERE key LIKE 'build_%'").fetchall():
        key = row["key"] or ""
        if not key.startswith("build_"):
            continue
        try:
            cid = int(key.split("_", 1)[1])
        except (ValueError, IndexError):
            continue
        try:
            p = json.loads(row["value"] or "{}")
        except Exception:
            continue
        if p.get("status") not in ("gerando", "gerando_rascunhos"):
            continue
        agent = (p.get("agente") or "").strip()
        if not agent:
            continue
        active.setdefault(agent, []).append({
            "kind": "build",
            "campaign_id": cid,
            "etapa": p.get("etapa") or "",
            "href": f"#/campanha/{cid}",
        })
    # Improvement runs
    try:
        for row in c.execute(
            """SELECT id, campaign_id, status FROM improvement_runs
               WHERE status IN ('queued','planning','running','applying','regenerating_templates')"""
        ).fetchall():
            tasks = c.execute(
                """SELECT assigned_agent_key, id, status FROM improvement_tasks
                   WHERE improvement_run_id=? AND status IN ('queued','running','pending')""",
                (row["id"],)).fetchall()
            for t in tasks:
                agent = (t["assigned_agent_key"] or "planner").strip()
                active.setdefault(agent, []).append({
                    "kind": "improvement",
                    "campaign_id": row["campaign_id"],
                    "run_id": row["id"],
                    "task_id": t["id"],
                    "etapa": t["status"],
                    "href": f"#/campanha/{row['campaign_id']}",
                })
            if not tasks:
                active.setdefault("planner", []).append({
                    "kind": "improvement",
                    "campaign_id": row["campaign_id"],
                    "run_id": row["id"],
                    "etapa": row["status"],
                    "href": f"#/campanha/{row['campaign_id']}",
                })
    except Exception:
        pass  # tabela ainda não migrada
    c.close()
    return active


def _stats_window(c, agent_id: str, hours: int) -> dict:
    row = c.execute(
        """SELECT COUNT(*) n, COALESCE(SUM(total_tokens),0) tok,
                  COALESCE(SUM(cost_usd),0) cost
           FROM agent_runs
           WHERE agent=? AND criado_em >= datetime('now', ?, 'localtime')""",
        (agent_id, f"-{int(hours)} hours")).fetchone()
    return {
        "runs_24h": int(row["n"] or 0),
        "tokens_24h": int(row["tok"] or 0),
        "cost_usd_24h": float(row["cost"] or 0),
    }


def catalog_with_status() -> list[dict]:
    import vertex
    out = []
    c = db.conn()
    active_map = _active_by_agent()
    for a in agent_catalog():
        env_val = ""
        if a.get("env"):
            env_val = (vertex.env_get(a["env"], "") or "").strip()
        row = c.execute(
            """SELECT COUNT(*) n, COALESCE(SUM(total_tokens),0) tok,
                      COALESCE(SUM(cost_usd),0) cost,
                      MAX(criado_em) last_at
               FROM agent_runs WHERE agent=?""", (a["id"],)).fetchone()
        win = _stats_window(c, a["id"], 24)
        jobs = active_map.get(a["id"]) or []
        # approval rate from improvement_tasks when available
        approval = None
        try:
            ap = c.execute(
                """SELECT
                     SUM(CASE WHEN status='applied' THEN 1 ELSE 0 END) applied,
                     SUM(CASE WHEN status IN ('applied','rejected') THEN 1 ELSE 0 END) decided
                   FROM improvement_tasks WHERE assigned_agent_key=?""",
                (a["id"],)).fetchone()
            decided = int(ap["decided"] or 0)
            if decided:
                approval = round(100.0 * int(ap["applied"] or 0) / decided, 1)
        except Exception:
            approval = None
        out.append({
            **a,
            "resource_configured": bool(env_val),
            "resource_preview": (env_val[:48] + "…") if len(env_val) > 48 else env_val,
            "runs": int(row["n"] or 0),
            "tokens": int(row["tok"] or 0),
            "cost_usd": float(row["cost"] or 0),
            "cost_brl": brl_from_usd(float(row["cost"] or 0)),
            "last_at": row["last_at"] or "",
            "status": "ativo" if jobs else "ocioso",
            "em_andamento": jobs,
            "aprovacao_pct": approval,
            **win,
            "cost_brl_24h": brl_from_usd(win["cost_usd_24h"]),
        })
    c.close()
    # refresh module-level for importers
    global AGENT_CATALOG
    AGENT_CATALOG = agent_catalog()
    return out


def resumo(days: int = 30) -> dict[str, Any]:
    c = db.conn()
    tot = c.execute(
        """SELECT COUNT(*) n, COALESCE(SUM(total_tokens),0) tok,
                  COALESCE(SUM(prompt_tokens),0) pin,
                  COALESCE(SUM(completion_tokens),0) pout,
                  COALESCE(SUM(cost_usd),0) cost,
                  COALESCE(SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END),0) erros,
                  COALESCE(SUM(fallback),0) fallbacks
           FROM agent_runs
           WHERE criado_em >= datetime('now', ?, 'localtime')""",
        (f"-{int(days)} days",)).fetchone()
    h24 = c.execute(
        """SELECT COUNT(*) n, COALESCE(SUM(total_tokens),0) tok,
                  COALESCE(SUM(cost_usd),0) cost
           FROM agent_runs
           WHERE criado_em >= datetime('now', '-24 hours', 'localtime')"""
    ).fetchone()
    por_agente = [dict(r) for r in c.execute(
        """SELECT agent, COUNT(*) n, COALESCE(SUM(total_tokens),0) tokens,
                  COALESCE(SUM(cost_usd),0) cost_usd,
                  COALESCE(AVG(latency_ms),0) latency_ms_avg
           FROM agent_runs
           WHERE criado_em >= datetime('now', ?, 'localtime')
           GROUP BY agent ORDER BY tokens DESC""",
        (f"-{int(days)} days",)).fetchall()]
    por_dia = [dict(r) for r in c.execute(
        """SELECT substr(criado_em,1,10) dia, COUNT(*) n,
                  COALESCE(SUM(total_tokens),0) tokens,
                  COALESCE(SUM(cost_usd),0) cost_usd
           FROM agent_runs
           WHERE criado_em >= datetime('now', ?, 'localtime')
           GROUP BY dia ORDER BY dia DESC LIMIT 40""",
        (f"-{int(days)} days",)).fetchall()]
    por_campanha = [dict(r) for r in c.execute(
        """SELECT campaign_id, COUNT(*) n, COALESCE(SUM(total_tokens),0) tokens,
                  COALESCE(SUM(cost_usd),0) cost_usd
           FROM agent_runs
           WHERE campaign_id IS NOT NULL
             AND criado_em >= datetime('now', ?, 'localtime')
           GROUP BY campaign_id ORDER BY tokens DESC LIMIT 20""",
        (f"-{int(days)} days",)).fetchall()]
    c.close()
    cost = float(tot["cost"] or 0)
    return {
        "days": days,
        "total_runs": int(tot["n"] or 0),
        "total_tokens": int(tot["tok"] or 0),
        "prompt_tokens": int(tot["pin"] or 0),
        "completion_tokens": int(tot["pout"] or 0),
        "cost_usd": cost,
        "cost_brl": brl_from_usd(cost),
        "erros": int(tot["erros"] or 0),
        "fallbacks": int(tot["fallbacks"] or 0),
        "runs_24h": int(h24["n"] or 0),
        "tokens_24h": int(h24["tok"] or 0),
        "cost_usd_24h": float(h24["cost"] or 0),
        "cost_brl_24h": brl_from_usd(float(h24["cost"] or 0)),
        "por_agente": por_agente,
        "por_dia": por_dia,
        "por_campanha": por_campanha,
        "rates": _rates(),
        "active": _active_by_agent(),
    }


def list_runs(limit: int = 80, agent: str = "", campaign_id: Optional[int] = None) -> list[dict]:
    c = db.conn()
    q = """SELECT id, agent, task_type, model, prompt_tokens, completion_tokens,
                  total_tokens, cost_usd, latency_ms, campaign_id, ok, fallback,
                  erro, detalhe, criado_em
           FROM agent_runs WHERE 1=1"""
    args: list[Any] = []
    if agent:
        q += " AND agent=?"; args.append(agent)
    if campaign_id is not None:
        q += " AND campaign_id=?"; args.append(campaign_id)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    rows = [dict(r) for r in c.execute(q, args).fetchall()]
    c.close()
    return rows


def list_knowledge(limit: int = 40, agent: str = "") -> list[dict]:
    c = db.conn()
    q = """SELECT id, agent, campaign_id, run_id, titulo, corpo, tags, criado_em
           FROM agent_knowledge WHERE 1=1"""
    args: list[Any] = []
    if agent:
        q += " AND agent=?"; args.append(agent)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    rows = [dict(r) for r in c.execute(q, args).fetchall()]
    c.close()
    return rows


def save_knowledge(*, agent: str, titulo: str, corpo: str,
                   campaign_id: Optional[int] = None, tags: str = "",
                   run_id: Optional[int] = None) -> int:
    c = db.conn()
    cur = c.execute(
        """INSERT INTO agent_knowledge(agent, campaign_id, run_id, titulo, corpo, tags)
           VALUES(?,?,?,?,?,?)""",
        (agent, campaign_id, run_id, (titulo or "")[:160],
         (corpo or "")[:8000], tags or ""))
    kid = cur.lastrowid
    c.commit()
    c.close()
    return int(kid or 0)


class timed_run:
    """Context manager: mede latency; `.ms` disponível durante e após o bloco."""

    def __init__(self):
        self.t0 = 0.0
        self._ms = 0

    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    @property
    def ms(self) -> int:
        if self.t0:
            return int((time.perf_counter() - self.t0) * 1000)
        return int(self._ms)

    def __exit__(self, *exc):
        self._ms = int((time.perf_counter() - self.t0) * 1000) if self.t0 else 0
        return False
