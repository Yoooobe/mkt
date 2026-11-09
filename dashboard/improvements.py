#!/usr/bin/env python3
"""Yoobe.mail — orquestrador de melhorias de campanha (planner → copy → validador → apply).

Sugestões do briefing (riscos / checklist / recomendar ajustar) viram tasks executáveis.
Agentes só produzem proposed_patch; a escrita é do aplicador transacional.
"""
from __future__ import annotations

import json
import os
import re
import threading
from typing import Any, Optional

import agent_usage
import ai
import campaign_guard_gate
import db
import engine
import grok
import leads
import vertex

ALLOWED_FIELDS = frozenset({"assunto", "preheader", "cta", "cta_url", "texto_plano", "inner_html"})
CAMPAIGN_SCALAR_FIELDS = frozenset({"objetivo", "contexto"})
PATH_RE = re.compile(
    r"^steps(?:\[(\d+)\]|\.(\d+))\.(assunto|preheader|cta|cta_url|texto_plano|inner_html)$"
)
SCALAR_PATH_RE = re.compile(r"^(objetivo|contexto)$")
GUARDRAIL_CATEGORIES = frozenset({
    "segmentacao", "oferta", "canal_timing", "compliance", "landing_page",
})
ACTIVE_RUN_STATUSES = frozenset({
    "queued", "planning", "running", "awaiting_approval", "applying",
    "regenerating_templates",
})
# Statuses that require a live daemon worker (die on uvicorn --reload).
WORKER_RUN_STATUSES = frozenset({
    "queued", "planning", "running", "applying", "regenerating_templates",
})


def improvements_enabled() -> bool:
    env = (vertex.env_get("IMPROVEMENTS_ENABLED", "") or os.environ.get("IMPROVEMENTS_ENABLED")
           or "").strip().lower()
    if env in ("0", "false", "off", "no"):
        return False
    if env in ("1", "true", "on", "yes"):
        return True
    meta = (db.meta_get("IMPROVEMENTS_ENABLED", "1") or "1").strip().lower()
    return meta not in ("0", "false", "off", "no")


def _j(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)


_LOADS_MISSING = object()


def _loads(s: Any, default: Any = _LOADS_MISSING):
    """JSON parse. default=None is honored (não vira {})."""
    if default is _LOADS_MISSING:
        default = {}
    if s is None or s == "":
        return default
    if isinstance(s, (dict, list)):
        return s
    try:
        return json.loads(s)
    except Exception:
        return default


def _patch_has_changes(patch: Any) -> bool:
    """True só se houver ao menos um change com from ≠ to (evita diff fantasma)."""
    if not isinstance(patch, dict):
        return False
    ch = patch.get("changes")
    if not isinstance(ch, list) or not ch:
        return False
    for item in ch:
        if not isinstance(item, dict):
            continue
        if str(item.get("from") or "") != str(item.get("to") or ""):
            return True
    return False


def _normalize_proposed_patch(raw: Any) -> Optional[dict]:
    """Evita `{}` fantasma na UI quando ainda não há alteração."""
    if raw is None or raw == "":
        return None
    patch = _loads(raw, None)
    if not isinstance(patch, dict):
        return None
    if not patch:
        return None
    if not _patch_has_changes(patch) and patch.get("status") != "insufficient_context":
        return None
    return patch


def _task_still_owned(tid: int, rid: int) -> bool:
    """Worker órfão não pode ressuscitar task após cancel/replace."""
    c = db.conn()
    run = c.execute("SELECT status FROM improvement_runs WHERE id=?", (rid,)).fetchone()
    task = c.execute(
        "SELECT status, improvement_run_id FROM improvement_tasks WHERE id=?",
        (tid,)).fetchone()
    c.close()
    if not run or (run["status"] or "") in ("cancelled", "failed", "completed"):
        return False
    if not task or int(task["improvement_run_id"]) != int(rid):
        return False
    if (task["status"] or "") in ("skipped", "rejected", "applied", "cancelled"):
        return False
    return True


def snapshot_campaign(cid: int) -> tuple[dict, list]:
    """Estado completo da campanha + templates dos steps para rollback."""
    c = db.conn()
    camp = c.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
    if not camp:
        c.close()
        raise ValueError("campanha não encontrada")
    payload = dict(camp)
    steps = []
    templates = []
    for s in c.execute(
        """SELECT cs.ordem, cs.template_ref, cs.offset_dias,
                  t.id AS tid, t.nome, t.assunto, t.preheader, t.cta, t.cta_url,
                  t.inner_html, t.texto_plano, t.status, t.assuntos_alt, t.versao_campanha
           FROM campaign_steps cs
           LEFT JOIN templates_ai t ON cs.template_ref='ai:'||t.id
           WHERE cs.campaign_id=? ORDER BY cs.ordem""", (cid,)
    ).fetchall():
        sd = {"ordem": s["ordem"], "template_ref": s["template_ref"],
              "offset_dias": s["offset_dias"]}
        steps.append(sd)
        if s["tid"]:
            templates.append({
                "id": s["tid"], "ordem": s["ordem"], "nome": s["nome"] or "",
                "assunto": s["assunto"] or "", "preheader": s["preheader"] or "",
                "cta": s["cta"] or "", "cta_url": s["cta_url"] or "",
                "inner_html": s["inner_html"] or "", "texto_plano": s["texto_plano"] or "",
                "status": s["status"] or "", "assuntos_alt": s["assuntos_alt"] or "",
                "versao_campanha": s["versao_campanha"],
                "template_ref": s["template_ref"],
            })
    c.close()
    payload["steps"] = steps
    briefing = ai.briefing_lancamento(cid)
    payload["briefing"] = briefing
    return payload, templates


def save_campaign_version(cid: int, *, improvement_run_id: Optional[int] = None,
                          created_by: str = "human", note: str = "") -> int:
    payload, templates = snapshot_campaign(cid)
    c = db.conn()
    row = c.execute("SELECT versao FROM campaigns WHERE id=?", (cid,)).fetchone()
    ver = int((row["versao"] if row else None) or 1)
    cur = c.execute(
        """INSERT INTO campaign_versions(
             campaign_id, improvement_run_id, payload, templates_snapshot,
             version_number, created_by, note)
           VALUES(?,?,?,?,?,?,?)""",
        (cid, improvement_run_id, _j(payload), _j(templates), ver, created_by, note[:300]))
    vid = cur.lastrowid
    c.commit()
    c.close()
    return int(vid or 0)


def _is_acknowledge_only(title: str, rationale: str = "") -> bool:
    """Riscos de negócio sem patch de texto — humano só registra ciência."""
    text = f"{title} {rationale}".lower()
    return any(k in text for k in (
        "amostra pequena", "amostra de ", "validade estatística", "validade estatistica",
        "estatístic", "estatistic", "atribuído à sorte", "atribuido a sorte",
        "atribuído a sorte", "alta variância", "alta variancia", "não para validar copy",
        "nao para validar copy", "conclusão estatística", "conclusao estatistica",
    ))


def collect_suggestions(cid: int) -> list[dict]:
    """Congela riscos / checklist / recomenda ajustar como sugestões estruturadas."""
    briefing = ai.briefing_lancamento(cid) or {}
    out = []
    for i, risco in enumerate(briefing.get("riscos") or []):
        text = str(risco).strip()
        if not text:
            continue
        cat = _classify_category(text[:120], text, "outro")
        out.append({
            "id": f"risco_{i}",
            "source": "risco",
            "title": text[:120],
            "rationale": text,
            "category_hint": cat,
        })
    cl = briefing.get("checklist") or {}
    items = cl.get("items") if isinstance(cl, dict) else None
    if not items:
        for i, label in enumerate(briefing.get("checklist_pre_lancamento") or []):
            text = str(label).strip()
            if not text:
                continue
            out.append({
                "id": f"checklist_raw_{i}",
                "source": "checklist",
                "title": text[:120],
                "rationale": text,
                "category_hint": "outro",
            })
    else:
        for it in items:
            st = it.get("status") or "pending"
            if st in ("done", "skipped"):
                continue
            if it.get("kind") == "auto" and st == "pending":
                # checks auto ainda pendentes — sugerir correção operacional
                pass
            text = (it.get("label") or it.get("id") or "").strip()
            if not text:
                continue
            cat = _classify_category(text[:120], text, "outro")
            # Checklist humano nunca vira fila de copywriter por padrão.
            if it.get("kind") == "human" and cat in ("copy", "criativo"):
                # Só mantém copy se o item pedir explicitamente alteração de e-mail.
                if not any(k in text.lower() for k in (
                        "jargão", "jargon", "assunto", "e-mail", "email", "copy",
                        "preheader", "template")):
                    cat = "outro"
            sug = {
                "id": f"checklist_{it.get('id', text[:40])}",
                "source": "checklist",
                "title": text[:120],
                "rationale": f"{text}. status={st}. evidence={it.get('evidence') or ''}",
                "category_hint": cat,
            }
            sug["human_action"] = human_action_for(
                category=cat, title=sug["title"], rationale=sug["rationale"],
                source_suggestion_id=sug["id"])
            out.append(sug)
    if (briefing.get("recomendacao") or "").strip() == "ajustar":
        just = (briefing.get("justificativa") or "").strip() or "Briefing recomenda ajustar antes de lançar"
        cat = _classify_category(just[:120], just, "outro")
        # Não forçar copy — senão a justificativa de oferta/comercial trava o agente.
        sug = {
            "id": "recomendacao_ajustar",
            "source": "recomendacao",
            "title": "Ajustar campanha (recomendação do briefing)",
            "rationale": just,
            "category_hint": cat,
        }
        sug["human_action"] = human_action_for(
            category=cat, title=sug["title"], rationale=sug["rationale"],
            source_suggestion_id=sug["id"])
        out.append(sug)
    for s in out:
        if "human_action" not in s:
            s["human_action"] = human_action_for(
                category=s.get("category_hint") or "outro",
                title=s.get("title") or "",
                rationale=s.get("rationale") or "",
                source_suggestion_id=s.get("id") or "")
    return out


def human_action_for(*, category: str = "", title: str = "", rationale: str = "",
                     source_suggestion_id: str = "") -> dict:
    """Deep-link + flags para o humano agir no dashboard."""
    text = f"{title} {rationale} {source_suggestion_id}".lower()
    cat = (category or "").lower()

    def _pack(goto: str, label: str, hint: str, can_convert_copy: bool = False) -> dict:
        return {"goto": goto, "label": label, "hint": hint,
                "can_convert_copy": bool(can_convert_copy)}

    if _is_acknowledge_only(title, rationale):
        return _pack(
            "briefing", "Ir para briefing (ciente)",
            "Risco de negócio — não há patch de e-mail. Marque como feito quando estiver ciente.",
            can_convert_copy=False)
    if any(k in text for k in ("utm", "calendly", "tracking", "ga4")) or cat == "dados_tracking":
        return _pack("cta_url", "Ir para CTA / UTM",
                     "Abra o preview do e-mail e edite o cta_url com UTM + Calendly.")
    if cat == "oferta" or any(k in text for k in (
            "oferta", "60 dias", "objetivo", "resumo", "icp", "posicionamento")):
        return _pack("edit_objetivo", "Editar objetivo / resumo",
                     "Atualize o Resumo da campanha (objetivo) e, se preciso, o Contexto IA.",
                     can_convert_copy=True)
    if ("template" in text and any(k in text for k in ("aprovad", "aprovar", "pendente"))) or (
            "templates da sequência" in text):
        return _pack("approve_templates", "Ir para aprovar templates",
                     "Aprove cada template na sequência ou use Aprovar templates no briefing.")
    if any(k in text for k in ("descadastro", "unsubscribe", "lgpd")) or cat == "compliance":
        return _pack("preview", "Ir para preview (descadastro)",
                     "Abra o preview HTML e teste o link de descadastro.")
    if any(k in text for k in ("crm", "tag", "contato", "cohort", "demo-realizada", "cliente")):
        return _pack("contatos", "Ir para contatos da campanha",
                     "Valide tags/segmento na tabela de contatos desta campanha.")
    if cat == "canal_timing" or any(k in text for k in ("follow-up", "telefone", "whatsapp", "comercial")):
        return _pack("briefing", "Ir para briefing / checklist",
                     "Defina o plano com o time e marque o item no checklist.")
    if cat in ("copy", "criativo") or any(k in text for k in (
            "copy", "jargão", "jargon", "assunto", "preheader", "cta", "roi",
            "ângulo", "angulo", "e-mail", "email", "texto")):
        return _pack("preview", "Ir para preview dos e-mails",
                     "Revise E1…En no preview; ou peça um patch de copy/resumo ao agente.",
                     can_convert_copy=True)
    return _pack("briefing", "Ir para briefing / checklist",
                 "Marque o item no checklist quando a ação humana estiver feita.",
                 can_convert_copy=False)


def _classify_category(title: str, rationale: str, hint: str) -> str:
    text = f"{title} {rationale}".lower()
    # Acknowledge-only antes de keywords de copy (ex.: "otimizar a copy" em amostra).
    if _is_acknowledge_only(title, rationale):
        return "outro"
    if any(k in text for k in ("lgpd", "dado pessoal", "claim", "promessa", "compliance", "legal")):
        return "compliance"
    if any(k in text for k in ("público", "segment", "audiência", "cohort", "temperatura")):
        return "segmentacao"
    if any(k in text for k in (
            "preço", "oferta", "desconto", "proposta comercial", "60 dias",
            "aprovação comercial", "aprovacao comercial", "documentad",
            "não está documentad", "nao esta documentad", " knowledge base",
            " no kb", "nos kb", "escopo exato da oferta")):
        return "oferta"
    # CRM / canal operacional — humano, não patch de texto (antes do catch-all "e-mail").
    if any(k in text for k in (
            "canal único", "canal unico", "sem telefone", "telefones cadastrados",
            "whatsapp", "follow-up comercial", "tag ", "tags ", "crm",
            "demo-realizada", "contatos no crm")):
        return "canal_timing"
    if any(k in text for k in ("agend", "cadência", "cadencia", "timing", "disparo", "quando enviar")):
        return "canal_timing"
    if any(k in text for k in ("utm", "ga4", "tracking", "medição", "medir")):
        return "dados_tracking"
    if any(k in text for k in ("jargão", "jargon", "assunto", "preheader", "cta ", "gancho",
                               "ângulo", "angulo", "texto plano", "inner_html", "densidade")):
        return "copy"
    if any(k in text for k in ("resumo", "objetivo", "icp", "posicionamento", "por que agora",
                               "proposta de valor")):
        return "copy"
    if any(k in text for k in ("e-mail", "email", "template", "mensagem", "copy ", " a copy",
                               "da copy", "na copy")):
        return "copy"
    if hint and hint in agent_usage.category_map() and hint != "outro":
        return hint
    return hint if hint in agent_usage.category_map() else "outro"


def _parse_path(path: str) -> Optional[dict]:
    """Retorna {"kind":"campaign","field":...} ou {"kind":"step","ordem":N,"field":...}."""
    raw = (path or "").strip()
    m = SCALAR_PATH_RE.match(raw)
    if m:
        return {"kind": "campaign", "field": m.group(1)}
    m = PATH_RE.match(raw)
    if not m:
        return None
    return {"kind": "step", "ordem": int(m.group(1) or m.group(2)), "field": m.group(3)}


def current_field_value(cid: int, path_or_ordem, field: str = "") -> str:
    """Lê valor atual: current_field_value(cid, 'objetivo') ou (cid, ordem, field)."""
    if isinstance(path_or_ordem, str) and not field:
        parsed = _parse_path(path_or_ordem)
        if not parsed:
            return ""
        if parsed["kind"] == "campaign":
            c = db.conn()
            row = c.execute(
                f"SELECT {parsed['field']} AS v FROM campaigns WHERE id=?", (cid,)
            ).fetchone()
            c.close()
            return (row["v"] if row else "") or ""
        path_or_ordem, field = parsed["ordem"], parsed["field"]
    c = db.conn()
    row = c.execute(
        """SELECT t.assunto, t.preheader, t.cta, t.cta_url, t.texto_plano, t.inner_html
           FROM campaign_steps cs
           LEFT JOIN templates_ai t ON cs.template_ref='ai:'||t.id
           WHERE cs.campaign_id=? AND cs.ordem=?""", (cid, int(path_or_ordem))).fetchone()
    c.close()
    if not row:
        return ""
    return (row[field] if field in row.keys() else "") or ""


def programmatic_validate_patch(cid: int, patch: dict) -> list[str]:
    issues = []
    if not isinstance(patch, dict):
        return ["patch não é objeto JSON"]
    if patch.get("status") == "insufficient_context":
        return [f"contexto insuficiente: {patch.get('missing')}"]
    changes = patch.get("changes")
    if not isinstance(changes, list) or not changes:
        issues.append("changes vazio ou inválido")
        return issues
    seen = set()
    for ch in changes:
        if not isinstance(ch, dict):
            issues.append("change inválida")
            continue
        path = ch.get("path") or ""
        parsed = _parse_path(path)
        if not parsed:
            issues.append(f"path não permitido: {path}")
            continue
        if parsed["kind"] == "campaign":
            field = parsed["field"]
            if field not in CAMPAIGN_SCALAR_FIELDS:
                issues.append(f"campo bloqueado: {field}")
            key = ("campaign", field)
            actual = current_field_value(cid, path)
        else:
            field = parsed["field"]
            if field not in ALLOWED_FIELDS:
                issues.append(f"campo bloqueado: {field}")
            key = ("step", parsed["ordem"], field)
            actual = current_field_value(cid, parsed["ordem"], field)
        if key in seen:
            issues.append(f"path duplicado no patch: {path}")
        seen.add(key)
        from_val = ch.get("from")
        to_val = ch.get("to")
        if from_val is not None and str(from_val) != str(actual):
            issues.append(f"`from` não bate com valor atual em {path}")
        if to_val is None:
            issues.append(f"`to` ausente em {path}")
        elif str(from_val or "") == str(to_val):
            issues.append(f"`from` e `to` são idênticos em {path} — alteração sem efeito")
    if not patch.get("verification"):
        issues.append("verification ausente")
    # Evita "alucinação de escopo": verification fala de e-mails mas o patch só mexe no resumo.
    ver = str(patch.get("verification") or "").lower()
    kinds = []
    for ch in changes:
        if not isinstance(ch, dict):
            continue
        parsed = _parse_path(ch.get("path") or "")
        if parsed:
            kinds.append(parsed.get("kind"))
    only_scalar = bool(kinds) and all(k == "campaign" for k in kinds)
    if only_scalar and any(k in ver for k in ("e1", "e2", "e3", "e4", "e-mail", "email", "steps.")):
        issues.append(
            "verification cita e-mails/steps, mas o patch só altera objetivo/contexto — "
            "ajuste os steps.* ou corrija a verification"
        )
    return issues


def run_get(rid: int) -> Optional[dict]:
    c = db.conn()
    row = c.execute("SELECT * FROM improvement_runs WHERE id=?", (rid,)).fetchone()
    if not row:
        c.close()
        return None
    tasks = [dict(t) for t in c.execute(
        "SELECT * FROM improvement_tasks WHERE improvement_run_id=? ORDER BY id",
        (rid,)).fetchall()]
    c.close()
    d = dict(row)
    d["suggestions_snapshot"] = _loads(d.get("suggestions_snapshot"), [])
    d["totals"] = _loads(d.get("totals"), {})
    for t in tasks:
        t["proposed_patch"] = _normalize_proposed_patch(t.get("proposed_patch"))
        t["validator_verdict"] = _loads(t.get("validator_verdict"), None)
        t["has_diff"] = _patch_has_changes(t["proposed_patch"])
        # Revalida na leitura: from velho / from==to / escopo inventado.
        t["patch_issues"] = (
            programmatic_validate_patch(int(t["campaign_id"]), t["proposed_patch"])
            if t["proposed_patch"] and t["has_diff"] else []
        )
        t["human_action"] = human_action_for(
            category=t.get("category") or "",
            title=t.get("title") or "",
            rationale=t.get("rationale") or "",
            source_suggestion_id=t.get("source_suggestion_id") or "")
    d["tasks"] = tasks
    return d


def list_runs_for_campaign(cid: int, limit: int = 10) -> list[dict]:
    c = db.conn()
    rows = [dict(r) for r in c.execute(
        """SELECT id, campaign_id, mode, status, totals, started_at, finished_at, error, criado_em
           FROM improvement_runs WHERE campaign_id=? ORDER BY id DESC LIMIT ?""",
        (cid, int(limit))).fetchall()]
    c.close()
    for r in rows:
        r["totals"] = _loads(r.get("totals"), {})
    return rows


def active_run_for_campaign(cid: int) -> Optional[dict]:
    c = db.conn()
    row = c.execute(
        f"""SELECT id FROM improvement_runs
            WHERE campaign_id=? AND status IN ({','.join('?' * len(ACTIVE_RUN_STATUSES))})
            ORDER BY id DESC LIMIT 1""",
        (cid, *ACTIVE_RUN_STATUSES)).fetchone()
    c.close()
    if not row:
        return None
    return run_get(row["id"])


def _update_run(rid: int, **kw):
    if not kw:
        return
    cols = []
    vals = []
    for k, v in kw.items():
        if k in ("suggestions_snapshot", "totals") and not isinstance(v, str):
            v = _j(v)
        cols.append(f"{k}=?")
        vals.append(v)
    vals.append(rid)
    c = db.conn()
    c.execute(f"UPDATE improvement_runs SET {', '.join(cols)} WHERE id=?", vals)
    c.commit()
    c.close()


def _update_task(tid: int, **kw):
    if not kw:
        return
    cols = []
    vals = []
    for k, v in kw.items():
        if k in ("proposed_patch", "validator_verdict") and not isinstance(v, str):
            v = _j(v) if v is not None else ""
        cols.append(f"{k}=?")
        vals.append(v)
    vals.append(tid)
    c = db.conn()
    c.execute(f"UPDATE improvement_tasks SET {', '.join(cols)} WHERE id=?", vals)
    c.commit()
    c.close()


def _task_row(tid: int) -> Optional[dict]:
    c = db.conn()
    row = c.execute("SELECT * FROM improvement_tasks WHERE id=?", (tid,)).fetchone()
    c.close()
    return dict(row) if row else None


def _recompute_totals(rid: int) -> dict:
    c = db.conn()
    rows = c.execute(
        "SELECT status FROM improvement_tasks WHERE improvement_run_id=?", (rid,)
    ).fetchall()
    run_row = c.execute(
        "SELECT campaign_id FROM improvement_runs WHERE id=?", (rid,)
    ).fetchone()
    cid = run_row["campaign_id"] if run_row else None
    # tokens from agent_runs linked via meta (com/sem espaço após :)
    tok = c.execute(
        """SELECT COALESCE(SUM(total_tokens),0) tok, COALESCE(SUM(cost_usd),0) cost
           FROM agent_runs
           WHERE meta_json LIKE ? OR meta_json LIKE ?
              OR (campaign_id=? AND agent IN ('planner','copywriter','validador','editor')
                  AND criado_em >= (SELECT criado_em FROM improvement_runs WHERE id=?))""",
        (f'%"improvement_run_id": {rid}%', f'%"improvement_run_id":{rid}%',
         cid, rid)
    ).fetchone()
    c.close()
    totals = {
        "tasks_total": len(rows),
        "applied": sum(1 for r in rows if r["status"] == "applied"),
        "rejected": sum(1 for r in rows if r["status"] == "rejected"),
        "failed": sum(1 for r in rows if r["status"] == "failed"),
        "skipped": sum(1 for r in rows if r["status"] == "skipped"),
        "awaiting_approval": sum(1 for r in rows if r["status"] == "awaiting_approval"),
        "tokens": int(tok["tok"] or 0),
        "cost_usd": float(tok["cost"] or 0),
        "cost_brl": agent_usage.brl_from_usd(float(tok["cost"] or 0)),
    }
    _update_run(rid, totals=totals)
    return totals


def _close_run_if_done(rid: int):
    run = run_get(rid)
    if not run or run["status"] in ("cancelled", "failed", "completed"):
        return
    open_statuses = {"pending", "queued", "running"}
    awaiting = False
    for t in run["tasks"]:
        if t["status"] in open_statuses:
            return
        if t["status"] == "awaiting_approval":
            awaiting = True
    if awaiting:
        _update_run(rid, status="awaiting_approval")
    else:
        _update_run(rid, status="completed", finished_at=db.now())
    _recompute_totals(rid)


def quarantine_stale_improvement_runs(*, max_age_sec: Optional[int] = None,
                                      force_all_workers: bool = False) -> list[int]:
    """Cancela runs órfãos (reload) ou travados há tempo demais.

    - force_all_workers=True: cancela qualquer run em status de worker (legado; evitar no startup).
    - max_age_sec: só runs mais velhos que isso (default 180s). Preferir no startup (ex.: 90).
    """
    age = int(max_age_sec if max_age_sec is not None else 180)
    if age < 1:
        age = 1
    c = db.conn()
    if force_all_workers:
        rows = c.execute(
            f"""SELECT id FROM improvement_runs
                WHERE status IN ({','.join('?' * len(WORKER_RUN_STATUSES))})""",
            tuple(WORKER_RUN_STATUSES),
        ).fetchall()
    else:
        rows = c.execute(
            f"""SELECT id FROM improvement_runs
                WHERE status IN ({','.join('?' * len(WORKER_RUN_STATUSES))})
                  AND datetime(COALESCE(NULLIF(started_at,''), criado_em))
                      <= datetime('now', 'localtime', ?)""",
            (*WORKER_RUN_STATUSES, f"-{age} seconds"),
        ).fetchall()
    c.close()
    cleared = []
    reason = (
        "worker interrompido (restart/reload) — inicie um novo run ou aprove o que ficou pronto"
        if force_all_workers else
        f"run travado >{age}s sem progresso — cancele e tente de novo, ou aprove patches prontos"
    )
    for r in rows:
        rid = int(r["id"])
        cancel_run(rid, reason=reason, abandon_approvals=False)
        cleared.append(rid)
    return cleared


def rescue_stuck_improvement_runs(max_age_sec: int = 180) -> list[int]:
    """Chamado pela API ao listar runs — destrava zumbis sem precisar restart."""
    return quarantine_stale_improvement_runs(max_age_sec=max_age_sec, force_all_workers=False)


def start_run(cid: int, mode: str = "review",
              suggestion_ids: Optional[list[str]] = None,
              triggered_by: str = "dashboard",
              replace: bool = False) -> dict:
    if not improvements_enabled():
        raise RuntimeError("melhorias desabilitadas (IMPROVEMENTS_ENABLED=0)")
    mode = (mode or "review").strip()
    if mode not in ("auto", "review", "selective"):
        raise ValueError("mode inválido")
    allow, reason = campaign_guard_gate.allow_campaign_llm(cid)
    if not allow:
        raise RuntimeError(f"campaign_guard bloqueou: {reason}")
    active = active_run_for_campaign(cid)
    if active:
        if replace:
            # Substitui tudo (inclusive patches ainda não aprovados).
            cancel_run(
                active["id"],
                reason="substituído por novo run",
                abandon_approvals=True,
            )
        elif active.get("status") in WORKER_RUN_STATUSES:
            # Worker morto: libera fila; mantém patches já prontos para aprovar.
            cancel_run(
                active["id"],
                reason="run órfão (worker morto) — aprove o que ficou pronto ou use replace",
                abandon_approvals=False,
            )
            active = active_run_for_campaign(cid)
            if active:
                raise RuntimeError(
                    "há patches aguardando aprovação nesta campanha "
                    "(aprove na lista ou confirme substituir o run)"
                )
        else:
            raise RuntimeError(
                "já existe um run de melhorias ativo nesta campanha "
                "(cancele, aprove as tasks, ou confirme substituir)"
            )
    prog = ai.build_progress(cid) or {}
    if prog.get("status") == "gerando":
        raise RuntimeError("aguarde a geração/reescrita da campanha terminar")

    suggestions = collect_suggestions(cid)
    if suggestion_ids:
        wanted = set(suggestion_ids)
        suggestions = [s for s in suggestions if s["id"] in wanted]
        if mode != "selective":
            mode = "selective"
    if not suggestions:
        raise ValueError("nenhuma sugestão disponível (gere o briefing com riscos/checklist)")

    c = db.conn()
    cur = c.execute(
        """INSERT INTO improvement_runs(
             campaign_id, triggered_by, mode, status, suggestions_snapshot, totals, started_at)
           VALUES(?,?,?,?,?,?,?)""",
        (cid, triggered_by, mode, "queued", _j(suggestions), _j({}), db.now()))
    rid = int(cur.lastrowid)
    for s in suggestions:
        c.execute(
            """INSERT INTO improvement_tasks(
                 improvement_run_id, campaign_id, source_suggestion_id, category,
                 title, rationale, status, max_iterations)
               VALUES(?,?,?,?,?,?,?,?)""",
            (rid, cid, s["id"], s.get("category_hint") or "outro",
             s["title"][:200], s["rationale"][:2000], "pending", 3))
    c.commit()
    c.close()
    save_campaign_version(cid, improvement_run_id=rid, created_by="human",
                          note=f"rollback point before improvement_run #{rid}")
    threading.Thread(target=_orchestrator_worker, args=(rid,), daemon=True).start()
    return run_get(rid)


def cancel_run(rid: int, reason: str = "cancelado pelo usuário",
               *, abandon_approvals: bool = False) -> dict:
    run = run_get(rid)
    if not run:
        raise ValueError("run não encontrado")
    if run["status"] in ("completed", "cancelled", "failed"):
        return run
    msg = (reason or "cancelado")[:400]
    c = db.conn()
    # Libera tasks ainda em voo. Mantém awaiting_approval (a menos que abandon_approvals).
    c.execute(
        """UPDATE improvement_tasks SET status='skipped',
               rejection_reason=CASE
                 WHEN rejection_reason IS NULL OR rejection_reason='' THEN ?
                 ELSE rejection_reason END
           WHERE improvement_run_id=? AND status IN ('pending','queued','running')""",
        (msg, rid))
    if abandon_approvals:
        c.execute(
            """UPDATE improvement_tasks SET status='skipped',
                   rejection_reason=CASE
                     WHEN rejection_reason IS NULL OR rejection_reason='' THEN ?
                     ELSE rejection_reason END
               WHERE improvement_run_id=? AND status='awaiting_approval'""",
            (msg, rid))
    awaiting = c.execute(
        """SELECT COUNT(*) n FROM improvement_tasks
           WHERE improvement_run_id=? AND status='awaiting_approval'""",
        (rid,)).fetchone()["n"]
    c.commit()
    c.close()
    if awaiting and not abandon_approvals:
        # Mantém o run visível na UI para aprovar patches já gerados.
        _update_run(rid, status="awaiting_approval", error=msg, finished_at="")
    else:
        _update_run(rid, status="cancelled", finished_at=db.now(), error=msg)
    _recompute_totals(rid)
    return run_get(rid)


def _apply_plan_decisions(rid: int, planned_tasks: list[dict], conflicts: list[dict]):
    """Aplica decisões do planner + guardrails programáticos (fonte da verdade)."""
    cats = agent_usage.category_map()
    run = run_get(rid)
    if not run:
        return []
    by_id = {int(t["id"]): t for t in run["tasks"]}
    skip_ids: set[int] = set()
    for conf in conflicts or []:
        for sid in conf.get("skip_task_ids") or []:
            try:
                skip_ids.add(int(sid))
            except (TypeError, ValueError):
                pass

    # index decisions by task id
    decisions: dict[int, dict] = {}
    for d in planned_tasks or []:
        try:
            tid = int(d.get("id"))
        except (TypeError, ValueError):
            continue
        if tid in by_id:
            decisions[tid] = d

    order_ids: list[int] = []
    depends: dict[int, list[int]] = {}
    for tid, t in by_id.items():
        d = decisions.get(tid) or {}
        cat = (d.get("category") or t.get("category")
               or _classify_category(t["title"], t["rationale"], t.get("category") or "outro"))
        cat = cat if cat in cats else _classify_category(t["title"], t["rationale"], "outro")
        # Fonte da verdade: acknowledge-only nunca vai para Vertex (mesmo se planner disser copy).
        if _is_acknowledge_only(t.get("title") or "", t.get("rationale") or ""):
            cat = "outro"
            d = {**d, "status": "skipped",
                 "skip_reason": "risco de negócio (ciente) — sem patch automático; marque como feito"}
        cfg = cats.get(cat) or cats.get("outro") or {}
        agent_key = (d.get("agent_key") or cfg.get("agent_key") or "") or ""
        max_it = int(cfg.get("max_iterations") or 0)
        guardrails = list(cfg.get("guardrails") or [])
        want_status = (d.get("status") or "").strip() or "queued"
        skip_reason = d.get("skip_reason") or ""

        if tid in skip_ids:
            _update_task(tid, category=cat, assigned_agent_key=agent_key,
                         status="skipped",
                         rejection_reason="conflito de path com outra task do run",
                         max_iterations=max_it or 0)
            continue

        if cat in GUARDRAIL_CATEGORIES or "always_human" in guardrails:
            _update_task(tid, category=cat, assigned_agent_key=agent_key or "",
                         status="skipped",
                         rejection_reason=skip_reason
                         or "guardrail: exige revisão humana / sem auto-aplicação",
                         max_iterations=max_it or 0)
            continue

        if cat in ("copy", "criativo"):
            agent_key = agent_key or "copywriter"
            max_it = max(3, max_it or 3)
            status = "queued"
        elif not agent_key or max_it <= 0 or want_status == "skipped":
            _update_task(tid, category=cat, assigned_agent_key=agent_key or "",
                         status="skipped",
                         rejection_reason=skip_reason
                         or "risco/ação humana — sem patch automático; use Marcar como feito",
                         max_iterations=max_it or 0)
            continue
        else:
            # Categorias com agente mas sem executor de patch → skip (não enfileirar Vertex).
            _update_task(tid, category=cat, assigned_agent_key=agent_key or "",
                         status="skipped",
                         rejection_reason=skip_reason
                         or f"categoria '{cat}' sem auto-patch — ação humana",
                         max_iterations=max_it or 0)
            continue

        _update_task(tid, category=cat, assigned_agent_key=agent_key,
                     status=status, max_iterations=max_it or 3)
        if status == "queued":
            order_ids.append(tid)
            deps = []
            for dep in d.get("depends_on") or []:
                try:
                    deps.append(int(dep))
                except (TypeError, ValueError):
                    pass
            depends[tid] = deps

    # stable order: planner order field, then id
    def _sort_key(tid: int):
        d = decisions.get(tid) or {}
        try:
            return (int(d.get("order") or 9999), tid)
        except (TypeError, ValueError):
            return (9999, tid)

    order_ids = sorted(set(order_ids), key=_sort_key)
    plan = {"order": order_ids, "depends_on": {str(k): v for k, v in depends.items()}}
    db.meta_set(f"improvement_plan_{rid}", _j(plan))
    return order_ids


def _planner_heuristic(rid: int):
    """Fallback determinístico se o Vertex falhar — degradação, não caminho feliz."""
    run = run_get(rid)
    if not run:
        return
    planned = []
    for i, t in enumerate(run["tasks"], start=1):
        cat = _classify_category(t["title"], t["rationale"], t.get("category") or "outro")
        cfg = agent_usage.category_map().get(cat) or {}
        is_copy = cat in ("copy", "criativo") and not _is_acknowledge_only(
            t.get("title") or "", t.get("rationale") or "")
        planned.append({
            "id": t["id"], "category": cat if is_copy else (cat or "outro"),
            "agent_key": (cfg.get("agent_key") or "") if is_copy else "",
            "order": i, "depends_on": [],
            "status": "queued" if is_copy else "skipped",
            "skip_reason": (None if is_copy else
                            "risco de negócio (ciente) — sem patch automático; marque como feito"
                            if _is_acknowledge_only(t.get("title") or "", t.get("rationale") or "")
                            else "ação humana — sem patch automático"),
        })
    _apply_plan_decisions(rid, planned, [])


def _planner_prompt(run: dict) -> str:
    cid = run["campaign_id"]
    payload, templates = snapshot_campaign(cid)
    cats = agent_usage.category_map()
    slim = {
        "id": cid,
        "nome": payload.get("nome"),
        "objetivo": (payload.get("objetivo") or "")[:500],
        "versao": payload.get("versao"),
        "steps": [{"ordem": t["ordem"], "assunto": t.get("assunto"),
                   "cta": t.get("cta")} for t in templates],
    }
    tasks_in = [{
        "id": t["id"],
        "source_suggestion_id": t.get("source_suggestion_id"),
        "title": t.get("title"),
        "rationale": (t.get("rationale") or "")[:500],
        "category_hint": t.get("category"),
    } for t in run["tasks"]]
    cat_desc = {k: {"agent_key": v.get("agent_key"),
                    "max_iterations": v.get("max_iterations"),
                    "guardrails": v.get("guardrails")}
                for k, v in cats.items()}
    return f"""Você é o planner multiagente de melhorias de campanha da Yoobe.

TAREFA: classificar cada sugestão, escolher o agente, definir ordem de execução
e dependências, e detectar conflitos (duas tasks que alterariam o mesmo path).

CATEGORIAS e agentes disponíveis:
{_j(cat_desc)}

REGRAS
- Responda APENAS JSON válido, sem markdown.
- Preferir category=copy e agent_key=copywriter quando a sugestão for resolúvel
  alterando assunto/preheader/cta/corpo de e-mail OU o Resumo da campanha
  (paths objetivo / contexto — ICP, posicionamento, por que agora, riscos de copy).
- segmentacao, oferta, canal_timing, compliance, landing_page → status skipped
  (sempre exigem humano / sem ferramenta).
- Em conflito de path, keep_task_id fica queued e skip_task_ids vão skipped.
- order começa em 1; depends_on lista ids que devem terminar antes.

CAMPANHA:
{_j(slim)}

TASKS:
{_j(tasks_in)}

SAÍDA
{{"tasks":[{{"id":1,"category":"copy","agent_key":"copywriter","order":1,
 "depends_on":[],"status":"queued","skip_reason":null}}],
 "conflicts":[{{"paths":["steps.1.assunto"],"keep_task_id":1,"skip_task_ids":[2]}}]}}
"""


def _planner(rid: int):
    run = run_get(rid)
    if not run or run["status"] == "cancelled":
        return
    _update_run(rid, status="planning")
    cid = run["campaign_id"]
    agent_usage.set_run_context(campaign_id=cid, agent="planner",
                                task_type="plan_improvements")
    allow, reason = campaign_guard_gate.allow_campaign_llm(cid)
    if not allow:
        agent_usage.log_run(
            agent="planner", task_type="plan_improvements", campaign_id=cid,
            ok=False, erro=f"campaign_guard: {reason}",
            detalhe="fallback heurístico por guard",
            meta={"improvement_run_id": rid, "fallback": True})
        _planner_heuristic(rid)
        _update_run(rid, status="running")
        return

    try:
        plan = vertex.generate_json(
            _planner_prompt(run),
            model=vertex.MODEL_FLASH, timeout=120, thinking_budget=0,
            agent="planner", task_type="plan_improvements")
        if not isinstance(plan, dict) or not isinstance(plan.get("tasks"), list):
            raise RuntimeError("planner não devolveu tasks[]")
        _apply_plan_decisions(rid, plan.get("tasks") or [], plan.get("conflicts") or [])
        agent_usage.log_run(
            agent="planner", task_type="plan_improvements", campaign_id=cid,
            ok=True, detalhe=f"vertex planned {len(plan.get('tasks') or [])} tasks",
            meta={"improvement_run_id": rid, "conflicts": plan.get("conflicts") or []})
    except Exception as ex:
        agent_usage.log_run(
            agent="planner", task_type="plan_improvements", campaign_id=cid,
            ok=False, erro=str(ex)[:300],
            detalhe="fallback heurístico após falha Vertex",
            meta={"improvement_run_id": rid, "fallback": True})
        _planner_heuristic(rid)
    _update_run(rid, status="running")


def _executor_prompt(cid: int, task: dict, feedback: str = "") -> str:
    payload, templates = snapshot_campaign(cid)
    camp_slim = {
        "id": cid,
        "nome": payload.get("nome"),
        "objetivo": payload.get("objetivo"),
        "contexto": (payload.get("contexto") or "")[:1500],
        "versao": payload.get("versao"),
        "steps": [{
            "ordem": t["ordem"], "nome": t.get("nome"),
            "assunto": t.get("assunto") or "",
            "preheader": t.get("preheader") or "",
            "cta": t.get("cta") or "",
            "cta_url": t.get("cta_url") or "",
            # corpo completo (até 4k) — o agente precisa do texto EXATO para `from`
            "texto_plano": (t.get("texto_plano") or "")[:4000],
            "inner_html": (t.get("inner_html") or "")[:4000],
        } for t in templates],
    }
    return f"""Você é o agente copy de otimização de campanha da Yoobe.

ENTRADA (JSON): campanha atual, sugestão a resolver, templates afetados,
feedback da iteração anterior (se houver).

TAREFA: produzir a menor alteração possível que resolva a sugestão.

REGRAS
- Responda APENAS com o JSON do patch. Sem markdown, sem explicação fora do JSON.
- Só altere os campos estritamente necessários. Nunca toque em orçamento, público,
  agendamento ou texto legal.
- Paths permitidos:
  (a) steps.N.assunto|preheader|cta|cta_url|texto_plano|inner_html
      (N = ordem do e-mail, começando em 1);
  (b) objetivo | contexto — Resumo da campanha (prosa com labels Resumo:/ICP:/etc.).
- Se a sugestão for de ICP/posicionamento/resumo puro, use `objetivo`.
- Se a sugestão for de copy/jargão/preço/assunto/corpo de e-mail:
  1) procure o jargão em CAMPANHA.steps (assunto/texto_plano/inner_html);
  2) se estiver nos e-mails, altere `steps.N.*` (não o objetivo inteiro);
  3) se o jargão estiver SÓ no `objetivo` (briefing) e NÃO nos steps, aí sim
     edite `objetivo` — e a `verification` deve falar só do campo alterado
     (não invente E1/E2/E3 se você não mexeu neles).
- NUNCA diga que um step contém OAuth/API/webhooks se o texto em CAMPANHA.steps
  não contém esses termos. Confirme com busca literal antes de patchar.
- `from` = valor atual EXATO copiado de CAMPANHA (objetivo ou steps).
- `to` DEVE ser diferente de `from`. Patch com from==to é inválido.
- Se a sugestão já estiver resolvida no texto atual, ainda assim proponha um
  micro-ajuste de qualidade (assunto, CTA ou resumo) alinhado à sugestão — NÃO
  retorne insufficient_context só porque o problema parcial já melhorou.
- Só use {{"status":"insufficient_context","missing":[...]}} se o campo necessário
  estiver ausente na CAMPANHA.
- `verification` deve ser um critério objetivo e referir apenas os paths alterados.
- `confidence` 0–1; abaixo de 0.8 irá para revisão humana.
- Sem claim sem lastro, sem dado pessoal em copy, sem promessa de resultado.

SUGESTÃO:
{_j({"id": task.get("source_suggestion_id"), "title": task.get("title"),
     "rationale": task.get("rationale"), "category": task.get("category")})}

CAMPANHA:
{_j(camp_slim)}

FEEDBACK ANTERIOR: {feedback or "—"}

SAÍDA (exemplo e-mail)
{{"target":"template","target_id":"steps.N","changes":[{{"path":"steps.N.assunto","from":"...","to":"..."}}],
 "expected_effect":"...","verification":"...","confidence":0.0,"risk_level":"low|medium|high"}}

SAÍDA (exemplo resumo)
{{"target":"campaign","target_id":"objetivo","changes":[{{"path":"objetivo","from":"...","to":"..."}}],
 "expected_effect":"...","verification":"...","confidence":0.0,"risk_level":"low|medium|high"}}
"""


def _validator_prompt(task: dict, patch: dict) -> str:
    return f"""Você é o revisor crítico. Recebe a sugestão original e o patch proposto.
Reprove por padrão se houver dúvida.

Cheque: (1) o patch resolve a sugestão? (2) `from` bate com o valor atual?
(3) alterou algo além do necessário? (4) o `verification` é objetivo?
(5) há risco de compliance (LGPD, claim sem lastro, dado pessoal, promessa)?
(6) a qualidade do texto está à altura da marca Yoobe?
(7) se path for objetivo/contexto, o Resumo ficou mais claro (labels + prosa)?
(8) REPROVE se algum change tiver from==to (sem efeito real).
(9) REPROVE se verification citar E1–E4/e-mails/steps mas changes só tiverem
    objetivo/contexto (escopo inventado).

Responda apenas JSON:
{{"approved": true|false, "issues": ["..."], "required_changes": ["..."],
 "risk_level": "low|medium|high", "confidence_adjustment": -0.3}}

SUGESTÃO: {_j({"title": task.get("title"), "rationale": task.get("rationale")})}
PATCH: {_j(patch)}
"""


def _run_copy_task(task: dict):
    tid = task["id"]
    cid = task["campaign_id"]
    rid = task["improvement_run_id"]
    if not _task_still_owned(tid, rid):
        return
    allow, reason = campaign_guard_gate.allow_campaign_llm(cid)
    if not allow:
        if _task_still_owned(tid, rid):
            _update_task(tid, status="failed", rejection_reason=f"campaign_guard: {reason}")
        return

    max_it = int(task.get("max_iterations") or 3)
    feedback = task.get("human_feedback") or ""
    best_patch = None
    best_verdict = None

    for iteration in range(1, max_it + 1):
        if not _task_still_owned(tid, rid):
            return
        _update_task(tid, status="running", iteration=iteration,
                     assigned_agent_key="copywriter")
        agent_usage.set_run_context(campaign_id=cid, agent="copywriter",
                                    task_type=f"improve_copy_t{tid}")
        try:
            patch = vertex.generate_json(
                _executor_prompt(cid, task, feedback),
                model=vertex.MODEL_PRO, timeout=180,
                agent="copywriter", task_type=f"improve_copy")
        except Exception as ex:
            agent_usage.log_run(
                agent="copywriter", task_type="improve_copy", campaign_id=cid,
                ok=False, erro=str(ex)[:300],
                meta={"improvement_run_id": rid, "improvement_task_id": tid,
                      "iteration": iteration})
            if not _task_still_owned(tid, rid):
                return
            if iteration >= max_it:
                _update_task(tid, status="failed",
                             rejection_reason=f"JSON/erro inválido {max_it}x: {ex}"[:400])
                return
            feedback = f"erro na execução: {ex}"
            continue

        if not _task_still_owned(tid, rid):
            return

        agent_usage.log_run(
            agent="copywriter", task_type="improve_copy", campaign_id=cid, ok=True,
            detalhe=f"patch iter {iteration}",
            meta={"improvement_run_id": rid, "improvement_task_id": tid,
                  "iteration": iteration})

        if not isinstance(patch, dict):
            patch = {}
        if patch.get("status") == "insufficient_context":
            missing = patch.get("missing") or []
            feedback = f"contexto insuficiente: {missing}"
            if iteration >= max_it:
                _update_task(tid, status="failed",
                             proposed_patch=_j(patch) if patch else "",
                             confidence=0, risk_level="high",
                             rejection_reason=feedback[:400],
                             validator_verdict={"approved": False, "issues": [feedback]})
                return
            continue

        issues = programmatic_validate_patch(cid, patch)
        if issues:
            feedback = "validação programática: " + "; ".join(issues)
            best_patch = patch
            if iteration >= max_it:
                if not _task_still_owned(tid, rid):
                    return
                if not _patch_has_changes(patch):
                    _update_task(tid, status="failed",
                                 proposed_patch="",
                                 confidence=0, risk_level="high",
                                 rejection_reason=feedback[:400],
                                 validator_verdict={"approved": False, "issues": issues})
                else:
                    _update_task(tid, status="awaiting_approval",
                                 proposed_patch=best_patch,
                                 confidence=float(patch.get("confidence") or 0),
                                 risk_level=patch.get("risk_level") or "high",
                                 validator_verdict={"approved": False, "issues": issues})
                return
            continue

        agent_usage.set_run_context(campaign_id=cid, agent="validador",
                                    task_type="validate_patch")
        vprompt = _validator_prompt(task, patch)
        try:
            # Grok como crítico do patch; Gemini Flash se Grok off/indisponível.
            if grok.enabled():
                try:
                    verdict = grok.generate_json(
                        vprompt, timeout=90, agent="grok_critic",
                        task_type="validate_patch", detalhe="improvement_validator")
                    verdict.pop("_via", None)
                    verdict.pop("_model", None)
                except Exception as grok_ex:
                    verdict = vertex.generate_json(
                        vprompt, model=vertex.MODEL_FLASH, timeout=90,
                        thinking_budget=0, agent="validador",
                        task_type="validate_patch",
                        detalhe=f"fallback_gemini após Grok: {str(grok_ex)[:80]}")
            else:
                verdict = vertex.generate_json(
                    vprompt, model=vertex.MODEL_FLASH, timeout=90,
                    thinking_budget=0, agent="validador",
                    task_type="validate_patch")
        except Exception as ex:
            verdict = {"approved": False, "issues": [f"validador falhou: {ex}"],
                       "required_changes": [], "risk_level": "high",
                       "confidence_adjustment": -0.3}

        if not _task_still_owned(tid, rid):
            return

        agent_usage.log_run(
            agent="validador", task_type="validate_patch", campaign_id=cid,
            ok=bool(verdict.get("approved")),
            detalhe="validator",
            meta={"improvement_run_id": rid, "improvement_task_id": tid,
                  "iteration": iteration})

        conf = float(patch.get("confidence") or 0)
        adj = float(verdict.get("confidence_adjustment") or 0)
        conf = max(0.0, min(1.0, conf + adj))
        risk = verdict.get("risk_level") or patch.get("risk_level") or "medium"
        best_patch = patch
        best_verdict = verdict
        if not _patch_has_changes(patch):
            feedback = "patch sem changes[] — reescreva com path steps.N.* ou objetivo"
            if iteration >= max_it:
                _update_task(tid, status="failed", proposed_patch="",
                             rejection_reason=feedback[:400], confidence=0)
                return
            continue
        _update_task(tid, proposed_patch=patch, confidence=conf, risk_level=risk,
                     validator_verdict=verdict)

        if verdict.get("approved"):
            run = run_get(rid)
            mode = ((run or {}).get("mode") or "review")
            auto_ok = (
                mode == "auto"
                and conf >= 0.8
                and risk == "low"
                and (task.get("category") or "") not in GUARDRAIL_CATEGORIES
            )
            if auto_ok:
                apply_task(tid, approved_by="auto", edited_patch=None)
            else:
                _update_task(tid, status="awaiting_approval")
            return

        feedback = "validador: " + "; ".join(
            (verdict.get("issues") or []) + (verdict.get("required_changes") or []))
        if iteration >= max_it:
            if not _task_still_owned(tid, rid):
                return
            _update_task(tid, status="awaiting_approval",
                         proposed_patch=best_patch, validator_verdict=best_verdict,
                         confidence=conf, risk_level=risk)
            return


def _task_deps_satisfied(tid: int, depends: dict, tasks_by_id: dict) -> bool:
    for dep in depends.get(tid) or depends.get(str(tid)) or []:
        try:
            dep_id = int(dep)
        except (TypeError, ValueError):
            continue
        other = tasks_by_id.get(dep_id)
        if not other:
            continue
        if other.get("status") in ("pending", "queued", "running"):
            return False
    return True


def _orchestrator_worker(rid: int):
    try:
        if not improvements_enabled():
            _update_run(rid, status="failed", error="IMPROVEMENTS_ENABLED=0",
                        finished_at=db.now())
            return
        _planner(rid)
        run = run_get(rid)
        if not run or run["status"] == "cancelled":
            return
        plan = _loads(db.meta_get(f"improvement_plan_{rid}", "{}"), {})
        order = plan.get("order") or [t["id"] for t in run["tasks"]]
        depends = plan.get("depends_on") or {}
        # topological-ish: walk order, skip if deps not done, retry pass
        pending = list(order)
        guard = 0
        while pending and guard < len(order) * 3 + 5:
            guard += 1
            run = run_get(rid)
            if not run or run["status"] == "cancelled":
                return
            tasks_by_id = {int(t["id"]): t for t in run["tasks"]}
            progressed = False
            still = []
            for tid in pending:
                t = tasks_by_id.get(int(tid))
                if not t:
                    progressed = True
                    continue
                if t["status"] != "queued":
                    progressed = True
                    continue
                if not _task_deps_satisfied(int(tid), depends, tasks_by_id):
                    still.append(tid)
                    continue
                cat = t.get("category") or ""
                if _is_acknowledge_only(t.get("title") or "", t.get("rationale") or ""):
                    _update_task(
                        t["id"], status="skipped", category="outro",
                        rejection_reason=(
                            "risco de negócio (ciente) — sem patch automático; "
                            "marque como feito"
                        )[:400],
                    )
                elif cat in ("copy", "criativo"):
                    _run_copy_task(t)
                else:
                    _update_task(
                        t["id"], status="skipped",
                        rejection_reason=(
                            f"categoria '{cat}' sem auto-patch — "
                            "ação humana; use Marcar como feito"
                        )[:400],
                    )
                progressed = True
            pending = still
            if not progressed:
                for tid in still:
                    t = tasks_by_id.get(int(tid))
                    if t and t["status"] == "queued":
                        if (t.get("category") or "") in ("copy", "criativo") and not (
                                _is_acknowledge_only(t.get("title") or "",
                                                     t.get("rationale") or "")):
                            _run_copy_task(t)
                        else:
                            _update_task(
                                t["id"], status="skipped",
                                rejection_reason=(
                                    "ação humana — sem patch automático; "
                                    "marque como feito"
                                )[:400],
                            )
                break
        # Nunca deixar tasks em running se o worker saiu do loop sem fechar
        run = run_get(rid)
        if run and run["status"] not in ("cancelled", "failed", "completed"):
            for t in run.get("tasks") or []:
                if t.get("status") == "running":
                    _update_task(
                        t["id"], status="failed",
                        rejection_reason=(
                            "execução interrompida sem patch — "
                            "tente Resolver esta de novo"
                        ),
                    )
        _close_run_if_done(rid)
        _recompute_totals(rid)
    except Exception as ex:
        # Terminal claro: não deixar running eterno
        try:
            c = db.conn()
            c.execute(
                """UPDATE improvement_tasks SET status='failed',
                       rejection_reason=?
                   WHERE improvement_run_id=? AND status IN ('running','queued','pending')""",
                (f"erro no orquestrador: {str(ex)[:200]}", rid))
            c.commit()
            c.close()
        except Exception:
            pass
        _update_run(rid, status="failed", error=str(ex)[:400], finished_at=db.now())
        print(f"[improvement_run {rid}] ERRO: {ex}", flush=True)
    finally:
        agent_usage.clear_run_context()


def apply_patch_to_campaign(cid: int, patch: dict, *, note: str = "",
                            improvement_run_id: Optional[int] = None) -> dict:
    """Aplica proposed_patch em objetivo/contexto e/ou novos templates — sem LLM."""
    if not isinstance(patch, dict):
        raise ValueError("patch inválido")
    issues = programmatic_validate_patch(cid, patch)
    if issues:
        raise ValueError("patch inválido: " + "; ".join(issues))

    by_ordem: dict[int, dict[str, Any]] = {}
    scalars: dict[str, Any] = {}
    for ch in patch["changes"]:
        parsed = _parse_path(ch["path"])
        if not parsed:
            continue
        if parsed["kind"] == "campaign":
            scalars[parsed["field"]] = ch["to"]
        else:
            by_ordem.setdefault(parsed["ordem"], {})[parsed["field"]] = ch["to"]

    c = db.conn()
    camp = c.execute("SELECT status, versao FROM campaigns WHERE id=?", (cid,)).fetchone()
    if not camp:
        c.close()
        raise ValueError("campanha não encontrada")
    if camp["status"] == "ativa":
        c.close()
        raise ValueError("pause a campanha antes de aplicar melhorias")
    c.close()

    nova_versao = db.campaign_bump_versao(
        cid, diretrizes=note or (patch.get("expected_effect") or "improvement patch"),
        ordens=sorted(by_ordem.keys()), origem="improvement")
    templates_novos = []
    c = db.conn()
    try:
        if scalars:
            sets = []
            args = []
            for field in ("objetivo", "contexto"):
                if field in scalars:
                    sets.append(f"{field}=?")
                    args.append(scalars[field] if scalars[field] is not None else "")
            if sets:
                args.append(cid)
                c.execute(
                    f"UPDATE campaigns SET {', '.join(sets)}, atualizada_em=? WHERE id=?",
                    (*args[:-1], db.now(), args[-1]))
        for ordem, fields in sorted(by_ordem.items()):
            row = c.execute(
                """SELECT cs.template_ref, t.id AS tid, t.nome, t.assunto, t.preheader,
                          t.cta, t.cta_url, t.inner_html, t.texto_plano, t.assuntos_alt,
                          t.origem
                   FROM campaign_steps cs
                   LEFT JOIN templates_ai t ON cs.template_ref='ai:'||t.id
                   WHERE cs.campaign_id=? AND cs.ordem=?""", (cid, ordem)).fetchone()
            if not row or not row["tid"]:
                raise ValueError(f"step {ordem} sem template IA")
            data = {
                "nome": row["nome"] or f"Email {ordem}",
                "assunto": row["assunto"] or "",
                "preheader": row["preheader"] or "",
                "cta": row["cta"] or "",
                "cta_url": row["cta_url"] or "",
                "inner_html": row["inner_html"] or "",
                "texto_plano": row["texto_plano"] or "",
                "assuntos_alt": row["assuntos_alt"] or "",
            }
            for f, v in fields.items():
                data[f] = v if v is not None else ""
            tid = int(row["tid"])
            # Snapshot histórico + UPDATE in-place no mesmo ai:ID da sequência.
            # Assim Preview/Editar e Melhorias sempre abrem o template vivo da campanha.
            c.execute("""INSERT INTO templates_ai(nome,assunto,preheader,cta,cta_url,inner_html,
                                                  texto_plano,status,origem,assuntos_alt,
                                                  campaign_id,versao_campanha,substituido_por)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (row["nome"], row["assunto"], row["preheader"], row["cta"], row["cta_url"],
                       row["inner_html"], row["texto_plano"], "arquivado",
                       row["origem"] or "ia", row["assuntos_alt"] or "[]",
                       cid, nova_versao, tid))
            snap_id = c.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
            c.execute("""UPDATE templates_ai
                         SET nome=?, assunto=?, preheader=?, cta=?, cta_url=?, inner_html=?,
                             texto_plano=?, status='pendente', assuntos_alt=?,
                             campaign_id=?, versao_campanha=?, substituido_por=NULL
                         WHERE id=?""",
                      (data["nome"], data["assunto"], data["preheader"], data["cta"],
                       data["cta_url"], data["inner_html"], data["texto_plano"],
                       data["assuntos_alt"], cid, nova_versao, tid))
            c.execute("UPDATE campaign_steps SET template_ref=? WHERE campaign_id=? AND ordem=?",
                      (f"ai:{tid}", cid, ordem))
            templates_novos.append({"ordem": ordem, "template_id": tid, "antigo_id": snap_id,
                                    "template_ref": f"ai:{tid}"})
        c.commit()
    finally:
        c.close()

    vid = save_campaign_version(
        cid, improvement_run_id=improvement_run_id, created_by="agent",
        note=note or patch.get("expected_effect") or "applied improvement")
    return {"versao": nova_versao, "campaign_version_id": vid,
            "templates_novos": templates_novos, "scalars": list(scalars.keys()),
            "stale_templates": bool(templates_novos)}


def apply_task(tid: int, *, approved_by: str = "dashboard",
               edited_patch: Optional[dict] = None) -> dict:
    task = _task_row(tid)
    if not task:
        raise ValueError("task não encontrada")
    if task["status"] not in ("awaiting_approval", "approved", "running"):
        if task["status"] == "applied":
            return run_get(task["improvement_run_id"])
        raise ValueError(f"task não aprovável no status {task['status']}")
    patch = (edited_patch if edited_patch is not None
             else _normalize_proposed_patch(task.get("proposed_patch")))
    if not patch or not _patch_has_changes(patch):
        raise ValueError("sem proposed_patch com changes[] — nada para aplicar")
    issues = programmatic_validate_patch(int(task["campaign_id"]), patch)
    if issues:
        raise ValueError("patch inválido/desatualizado: " + "; ".join(issues[:4]))
    rid = task["improvement_run_id"]
    _update_run(rid, status="applying")
    _update_task(tid, status="approved", approved_by=approved_by,
                 proposed_patch=patch)
    try:
        result = apply_patch_to_campaign(
            task["campaign_id"], patch,
            note=f"task #{tid}: {task.get('title') or ''}",
            improvement_run_id=rid)
        _update_task(tid, status="applied", applied_at=db.now(),
                     approved_by=approved_by)
        _update_run(rid, status="regenerating_templates")
        # briefing refresh best-effort
        try:
            ai.gerar_briefing_lancamento(task["campaign_id"])
        except Exception as ex:
            print(f"[improvement apply] briefing: {ex}", flush=True)
        _close_run_if_done(rid)
        _recompute_totals(rid)
        out = run_get(rid)
        out["apply_result"] = result
        return out
    except Exception as ex:
        _update_task(tid, status="failed", rejection_reason=str(ex)[:400])
        _close_run_if_done(rid)
        raise


def reject_task(tid: int, reason: str = "") -> dict:
    task = _task_row(tid)
    if not task:
        raise ValueError("task não encontrada")
    _update_task(tid, status="rejected", rejection_reason=(reason or "")[:500])
    _close_run_if_done(task["improvement_run_id"])
    _recompute_totals(task["improvement_run_id"])
    return run_get(task["improvement_run_id"])


def mark_human_done(tid: int, note: str = "") -> dict:
    """Humano concluiu a ação operacional — marca applied com evidência."""
    task = _task_row(tid)
    if not task:
        raise ValueError("task não encontrada")
    if task["status"] == "applied" and (task.get("approved_by") or "") == "human":
        return run_get(task["improvement_run_id"])
    if task["status"] not in ("skipped", "rejected", "failed", "awaiting_approval"):
        raise ValueError(f"não dá para marcar feito no status {task['status']}")
    # Se há patch aguardando, preferir approve — evitar burlar o diff
    if task["status"] == "awaiting_approval" and _normalize_proposed_patch(task.get("proposed_patch")):
        raise ValueError("esta task tem patch — use Aprovar e aplicar (não Marcar como feito)")
    evidence = (note or "").strip()[:1000] or f"marcado como feito em {db.now()}"
    _update_task(
        tid, status="applied", approved_by="human", applied_at=db.now(),
        human_feedback=evidence,
        rejection_reason=(task.get("rejection_reason") or "ação humana")[:400],
    )
    sid = (task.get("source_suggestion_id") or "").strip()
    if sid.startswith("checklist_"):
        item_id = sid[len("checklist_"):]
        try:
            ai.patch_checklist_item(task["campaign_id"], item_id, "done", evidence=evidence)
        except Exception as ex:
            print(f"[human_done] checklist sync: {ex}", flush=True)
    _close_run_if_done(task["improvement_run_id"])
    _recompute_totals(task["improvement_run_id"])
    return run_get(task["improvement_run_id"])


def convert_to_copy_task(tid: int, feedback: str = "") -> dict:
    """Reenfileira task pulada como copy para o agente gerar patch de texto/resumo."""
    task = _task_row(tid)
    if not task:
        raise ValueError("task não encontrada")
    if task["status"] not in ("skipped", "rejected", "failed"):
        raise ValueError(f"só converte tasks puladas/falhas (agora: {task['status']})")
    action = human_action_for(
        category=task.get("category") or "",
        title=task.get("title") or "",
        rationale=task.get("rationale") or "",
        source_suggestion_id=task.get("source_suggestion_id") or "")
    if not action.get("can_convert_copy"):
        raise ValueError(
            "esta sugestão não vira patch automático — use Ir para… e Marcar como feito")
    if not improvements_enabled():
        raise RuntimeError("melhorias desabilitadas")
    allow, reason = campaign_guard_gate.allow_campaign_llm(task["campaign_id"])
    if not allow:
        raise RuntimeError(f"campaign_guard: {reason}")
    note = (feedback or "").strip()[:1000] or "convertido para patch de copy/resumo"
    _update_task(
        tid, status="queued", category="copy", assigned_agent_key="copywriter",
        max_iterations=max(3, int(task.get("max_iterations") or 3)),
        iteration=0, rejection_reason="", human_feedback=note,
        proposed_patch="", validator_verdict="")
    _update_run(task["improvement_run_id"], status="running", finished_at="", error="")
    threading.Thread(target=_retry_worker, args=(tid,), daemon=True).start()
    return run_get(task["improvement_run_id"])


def retry_task(tid: int, feedback: str = "") -> dict:
    task = _task_row(tid)
    if not task:
        raise ValueError("task não encontrada")
    if not improvements_enabled():
        raise RuntimeError("melhorias desabilitadas")
    allow, reason = campaign_guard_gate.allow_campaign_llm(task["campaign_id"])
    if not allow:
        raise RuntimeError(f"campaign_guard: {reason}")
    _update_task(tid, status="queued", human_feedback=(feedback or "")[:1000],
                 iteration=0, rejection_reason="")
    _update_run(task["improvement_run_id"], status="running", finished_at="")
    threading.Thread(target=_retry_worker, args=(tid,), daemon=True).start()
    return run_get(task["improvement_run_id"])


def _retry_worker(tid: int):
    try:
        task = _task_row(tid)
        if not task:
            return
        if (task.get("category") or "") in ("copy", "criativo"):
            _run_copy_task(task)
        else:
            _update_task(tid, status="awaiting_approval")
        _close_run_if_done(task["improvement_run_id"])
        _recompute_totals(task["improvement_run_id"])
    finally:
        agent_usage.clear_run_context()


def rollback_campaign(cid: int, campaign_version_id: int) -> dict:
    c = db.conn()
    ver = c.execute(
        "SELECT * FROM campaign_versions WHERE id=? AND campaign_id=?",
        (campaign_version_id, cid)).fetchone()
    if not ver:
        c.close()
        raise ValueError("versão não encontrada")
    payload = _loads(ver["payload"], {})
    templates = _loads(ver["templates_snapshot"], [])
    # restore campaign scalar fields
    for col in ("nome", "objetivo", "contexto", "audience_profile", "ads_briefing"):
        if col in payload:
            c.execute(f"UPDATE campaigns SET {col}=? WHERE id=?", (payload.get(col) or "", cid))
    c.execute("UPDATE campaigns SET versao=?, atualizada_em=? WHERE id=?",
              (int(ver["version_number"] or payload.get("versao") or 1), db.now(), cid))
    # restore step template refs pointing to historical template rows if still present;
    # otherwise re-insert templates from snapshot
    for t in templates:
        ordem = t.get("ordem")
        tid = t.get("id")
        if not ordem:
            continue
        exists = c.execute("SELECT id FROM templates_ai WHERE id=?", (tid,)).fetchone() if tid else None
        if exists:
            # restore content of that template id
            c.execute(
                """UPDATE templates_ai SET nome=?, assunto=?, preheader=?, cta=?, cta_url=?,
                                   inner_html=?, texto_plano=?, status=?, assuntos_alt=?,
                                   versao_campanha=?
                   WHERE id=?""",
                (t.get("nome") or "", t.get("assunto") or "", t.get("preheader") or "",
                 t.get("cta") or "", t.get("cta_url") or "", t.get("inner_html") or "",
                 t.get("texto_plano") or "", t.get("status") or "pendente",
                 t.get("assuntos_alt") or "", t.get("versao_campanha"), tid))
            c.execute("UPDATE campaign_steps SET template_ref=? WHERE campaign_id=? AND ordem=?",
                      (f"ai:{tid}", cid, ordem))
        else:
            c.execute(
                """INSERT INTO templates_ai(nome,assunto,preheader,cta,cta_url,inner_html,
                                            texto_plano,status,origem,assuntos_alt,
                                            campaign_id,versao_campanha)
                   VALUES(?,?,?,?,?,?,?,?, 'ia',?,?,?)""",
                (t.get("nome") or "", t.get("assunto") or "", t.get("preheader") or "",
                 t.get("cta") or "", t.get("cta_url") or "", t.get("inner_html") or "",
                 t.get("texto_plano") or "", t.get("status") or "pendente",
                 t.get("assuntos_alt") or "", cid, t.get("versao_campanha")))
            new_id = c.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
            c.execute("UPDATE campaign_steps SET template_ref=? WHERE campaign_id=? AND ordem=?",
                      (f"ai:{new_id}", cid, ordem))
    # restore briefing if present (mesma conexão — evita database is locked)
    if payload.get("briefing"):
        c.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"briefing_{cid}", _j(payload["briefing"])))
    c.commit()
    c.close()
    save_campaign_version(cid, created_by="human",
                          note=f"rollback to campaign_versions.id={campaign_version_id}")
    return {"ok": True, "campaign_id": cid, "restored_version_id": campaign_version_id,
            "version_number": ver["version_number"]}


def list_versions(cid: int, limit: int = 20) -> list[dict]:
    c = db.conn()
    rows = [dict(r) for r in c.execute(
        """SELECT id, campaign_id, improvement_run_id, version_number, created_by,
                  note, criado_em
           FROM campaign_versions WHERE campaign_id=? ORDER BY id DESC LIMIT ?""",
        (cid, int(limit))).fetchall()]
    c.close()
    return rows


def abandon_stale_approvals(cid: int) -> dict:
    """Descarta awaiting_approval de runs mortos ou com patch inválido — libera o caminho."""
    c = db.conn()
    rows = c.execute(
        """SELECT t.id, t.improvement_run_id, t.proposed_patch, r.status AS run_status
           FROM improvement_tasks t
           JOIN improvement_runs r ON r.id=t.improvement_run_id
           WHERE t.campaign_id=? AND t.status='awaiting_approval'""",
        (cid,)).fetchall()
    skipped = []
    for row in rows:
        patch = _normalize_proposed_patch(row["proposed_patch"])
        if patch and _patch_has_changes(patch):
            issues = programmatic_validate_patch(cid, patch)
        else:
            issues = ["sem alteração concreta"]
        # Só descarta inválidos/desatualizados — patches válidos continuam aprováveis.
        if issues:
            c.execute(
                """UPDATE improvement_tasks SET status='skipped',
                       rejection_reason=? WHERE id=?""",
                (("patch antigo/desatualizado descartado: " + "; ".join(issues[:2]))[:400],
                 row["id"]))
            skipped.append(int(row["id"]))
    c.commit()
    c.close()
    return {"skipped_task_ids": skipped, "n": len(skipped)}


def activation_guide(cid: int) -> dict:
    """Passo a passo até Ativar: o que falta, onde ir, o que bloqueia."""
    c = db.conn()
    camp = c.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
    if not camp:
        c.close()
        raise ValueError("campanha não encontrada")
    camp = dict(camp)
    steps_rows = c.execute(
        """SELECT cs.ordem, cs.template_ref, t.status AS status_template, t.assunto
           FROM campaign_steps cs
           LEFT JOIN templates_ai t ON cs.template_ref='ai:'||t.id
           WHERE cs.campaign_id=? ORDER BY cs.ordem""", (cid,)).fetchall()
    n_contacts = c.execute(
        "SELECT COUNT(*) n FROM campaign_contacts WHERE campaign_id=?",
        (cid,)).fetchone()["n"]
    awaiting = [dict(r) for r in c.execute(
        """SELECT t.id, t.title, t.confidence, t.improvement_run_id, r.status AS run_status
           FROM improvement_tasks t
           JOIN improvement_runs r ON r.id=t.improvement_run_id
           WHERE t.campaign_id=? AND t.status='awaiting_approval'
           ORDER BY t.id DESC""", (cid,)).fetchall()]
    active = None
    for r in c.execute(
        """SELECT id, status, mode, error FROM improvement_runs
           WHERE campaign_id=? AND status IN ('pending','planning','running',
             'awaiting_approval','applying','queued')
           ORDER BY id DESC LIMIT 1""", (cid,)):
        active = dict(r)
    c.close()

    build = _loads(db.meta_get(f"build_{cid}", ""), None) or {}
    gerando = (build.get("status") or "") == "gerando"
    # Build zumbi (worker morto no reload): não bloquear a ativação para sempre.
    if gerando:
        iniciado = str(build.get("iniciado_em") or "")
        stuck = False
        try:
            from datetime import datetime
            t0 = datetime.strptime(iniciado[:19], "%Y-%m-%d %H:%M:%S")
            stuck = (datetime.now() - t0).total_seconds() > 900  # 15 min
        except Exception:
            stuck = True
        if stuck:
            build["status"] = "erro"
            build["erro"] = "build interrompido (sem progresso) — Retomar criação se precisar"
            build["erro_tipo"] = "restart"
            db.meta_set(f"build_{cid}", _j(build))
            gerando = False

    try:
        ai.rodar_checklist(cid)
    except Exception:
        pass
    try:
        cl = ai.get_checklist(cid)
    except Exception:
        cl = {"items": [], "ready": True}
    cl_items = cl.get("items") or []
    cl_pend = [it for it in cl_items
               if it.get("required") is not False
               and (it.get("status") or "pending") not in ("done", "skipped")]

    tpl_total = len(steps_rows)
    tpl_ok = sum(1 for s in steps_rows if (s["status_template"] or "") == "aprovado")
    templates_ok = tpl_total > 0 and tpl_ok == tpl_total

    sender = db.sender_get(camp.get("sender_id") or 1) or db.sender_default()
    sender_ok = bool(sender and engine.sender_auth_status(sender).get("conectado"))

    try:
        report = leads.campaign_validation_report(cid)
        validos = int(report.get("validos") or 0)
    except Exception:
        report, validos = {}, n_contacts

    # Patches aguardando: marcar se estão válidos
    patches = []
    for t in awaiting:
        raw = None
        c2 = db.conn()
        row = c2.execute(
            "SELECT proposed_patch FROM improvement_tasks WHERE id=?",
            (t["id"],)).fetchone()
        c2.close()
        patch = _normalize_proposed_patch(row["proposed_patch"] if row else None)
        issues = (programmatic_validate_patch(cid, patch)
                  if patch and _patch_has_changes(patch) else ["sem alteração concreta"])
        patches.append({
            "task_id": t["id"], "title": t["title"],
            "run_id": t["improvement_run_id"], "run_status": t["run_status"],
            "ok": not issues, "issues": issues[:3],
            "goto": "melhorias",
        })
    patches_ok = [p for p in patches if p["ok"]]
    patches_stale = [p for p in patches if not p["ok"]]

    # Agent-resolvable checklist hints (copy/criativo)
    cl_agent = {}
    try:
        for sug in collect_suggestions(cid):
            sid = str(sug.get("id") or "")
            if not sid.startswith("checklist_"):
                continue
            item_id = sid[len("checklist_"):]
            ha = sug.get("human_action") or {}
            cl_agent[item_id] = {
                "suggestion_id": sid,
                "can_convert_copy": bool(ha.get("can_convert_copy")),
                "category": sug.get("category_hint") or "",
                "hint": ha.get("hint") or "",
            }
    except Exception:
        pass

    tpl_pending_items = [
        {"ordem": s["ordem"], "template_ref": s["template_ref"],
         "assunto": s["assunto"] or "", "status": s["status_template"] or "pendente"}
        for s in steps_rows
        if (s["status_template"] or "") != "aprovado"
    ]
    cl_pend_items = []
    for it in cl_pend:
        iid = it.get("id")
        agent = cl_agent.get(iid) or {}
        resolvable = "manual"
        if it.get("kind") == "auto":
            resolvable = "auto"
        elif agent.get("can_convert_copy"):
            resolvable = "agent"
        cl_pend_items.append({
            "id": iid,
            "label": it.get("label"),
            "kind": it.get("kind"),
            "status": it.get("status") or "pending",
            "evidence": it.get("evidence") or "",
            "required_for_send": bool(it.get("required_for_send")),
            "resolvable": resolvable,
            "suggestion_id": agent.get("suggestion_id") or "",
            "agent_hint": agent.get("hint") or "",
        })

    run_busy = bool(active and active.get("status") in (
        "pending", "planning", "running", "queued", "applying"))

    steps = []

    def add(key, title, done, *, severity="soft", resolvable="none",
            where="", action="", detail="", items=None):
        hard = severity == "hard"
        steps.append({
            "key": key, "title": title, "done": bool(done),
            "severity": severity,
            "resolvable": resolvable,
            "blocks_activation": bool(hard and not done),
            "where": where, "action": action, "detail": detail,
            "items": items or [],
        })

    add("build", "Geração/reescrita concluída",
        not gerando, severity="hard", resolvable="none",
        where="topo da campanha",
        action="Aguarde o build ou use Retomar criação",
        detail="IA ainda escrevendo e-mails" if gerando else "ok")

    add("templates", f"Templates aprovados ({tpl_ok}/{tpl_total})",
        templates_ok, severity="soft", resolvable="manual",
        where="Sequência de e-mails / guia → Aprovar templates",
        action="Aprove no preview ou use Aprovar todos; no disparo com ressalvas os pendentes são carimbados",
        detail=("Pendentes: " + ", ".join(f"E{x['ordem']}" for x in tpl_pending_items)
                if tpl_pending_items else "ok"),
        items=tpl_pending_items)

    add("run_ativo", "Nenhum run de melhorias travado",
        not run_busy, severity="soft", resolvable="manual",
        where="Melhorias com agentes",
        action="Cancelar run ativo (descarta fila)",
        detail=(f"Run #{active['id']} · {active.get('status')}"
                if active and run_busy else "ok"))

    add("patches", "Decidir patches de agentes",
        not patches_ok and not patches_stale,
        severity="soft", resolvable="manual",
        where="Melhorias com agentes",
        action=("Aprovar ou rejeitar cada diff" if patches_ok
                else ("Descartar patches antigos" if patches_stale else "—")),
        detail=(f"{len(patches_ok)} pronto(s) · {len(patches_stale)} antigo(s)"
                if patches else "nenhum patch pendente"),
        items=patches)

    add("checklist", f"Checklist pré-ativação ({len(cl_items) - len(cl_pend)}/{len(cl_items)})",
        not cl_pend, severity="soft",
        resolvable="manual" if cl_pend else "none",
        where="Passo a passo / briefing → Antes de ativar",
        action="Marque como feito, rode checks auto ou peça patch aos agentes",
        detail="; ".join((it.get("label") or it.get("id") or "")[:60] for it in cl_pend[:4])
               + ("…" if len(cl_pend) > 4 else ""),
        items=cl_pend_items)

    add("remetente", "Remetente conectado",
        sender_ok, severity="hard", resolvable="none",
        where="Aba Remetentes",
        action=f"Conecte {(sender or {}).get('email') or 'a conta'}",
        detail="ok" if sender_ok else "desconectado")

    add("contatos", f"Contatos válidos para envio ({validos})",
        validos > 0, severity="hard", resolvable="none",
        where="Contatos da campanha / Qualidade",
        action="Revise audiência ou rode Validar leads",
        detail=f"{validos} válidos · {n_contacts} vinculados")

    add("status", "Campanha ainda em rascunho/pausada",
        (camp.get("status") or "") in ("rascunho", "pausada"),
        severity="soft", resolvable="none",
        where="barra superior",
        action="▶ Ativar campanha",
        detail=camp.get("status") or "")

    blocking = [s for s in steps if s["blocks_activation"]]
    soft_pending = []
    if not templates_ok and tpl_total > 0:
        soft_pending.append({
            "key": "templates",
            "title": f"Templates não aprovados ({tpl_ok}/{tpl_total})",
            "detail": ", ".join(f"E{x['ordem']}" for x in tpl_pending_items),
        })
    if cl_pend:
        soft_pending.append({
            "key": "checklist",
            "title": f"Checklist incompleto ({len(cl_pend)} item(ns))",
            "detail": "; ".join((it.get("label") or it.get("id") or "")[:50] for it in cl_pend[:5]),
        })
    if run_busy:
        soft_pending.append({
            "key": "run_ativo",
            "title": f"Run de melhorias ativo #{active['id']}",
            "detail": active.get("status") or "",
        })
    if patches_ok:
        soft_pending.append({
            "key": "patches",
            "title": f"{len(patches_ok)} patch(es) aguardando aprovação",
            "detail": "; ".join((p.get("title") or "")[:40] for p in patches_ok[:3]),
        })

    next_step = next((s for s in steps if not s["done"] and s["blocks_activation"]), None)
    if not next_step and (camp.get("status") or "") in ("rascunho", "pausada"):
        if soft_pending:
            next_step = {
                "key": "soft", "title": "Pode ativar com ressalvas — ou resolva o recomendado",
                "done": False, "blocks_activation": False, "severity": "soft",
                "where": "Passo a passo / barra superior",
                "action": "Resolva no guia ou use Ativar / Disparar mesmo assim",
                "detail": f"{len(soft_pending)} pendência(s) recomendada(s)",
            }
        else:
            next_step = {
                "key": "ativar", "title": "Pronto — Ativar campanha",
                "done": False, "blocks_activation": False, "severity": "soft",
                "where": "barra superior → ▶ Ativar campanha",
                "action": "Clique em Ativar campanha",
                "detail": "Todos os passos recomendados estão ok",
            }

    status_ok = (camp.get("status") or "") in ("rascunho", "pausada")
    ready_to_activate = not blocking and status_ok
    ready_strict = ready_to_activate and not soft_pending

    return {
        "campaign_id": cid,
        "status": camp.get("status"),
        "ready_to_activate": ready_to_activate,
        "ready_strict": ready_strict,
        "soft_pending": soft_pending,
        "next_step": next_step,
        "steps": steps,
        "checklist_pending": cl_pend_items,
        "patches_awaiting": patches,
        "active_run": active,
        "templates": {"aprovados": tpl_ok, "total": tpl_total,
                      "pendentes": tpl_pending_items},
        "validacao": {"validos": validos, "total_vinculados": n_contacts,
                      "duvidosos": report.get("duvidosos"), "stale": report.get("stale")},
    }
