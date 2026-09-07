#!/usr/bin/env python3
"""Testes de integração do loop de melhorias (sem chamar Vertex).

Happy path: review → approve → apply
Rejeição: reject não altera templates
Rollback: restaura versão anterior

Rode a partir de dashboard/:
  cd dashboard && ../.venv/bin/python ../scripts/test_improvements.py
ou:
  cd dashboard && python3 ../scripts/test_improvements.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

DASH = Path(__file__).resolve().parents[1] / "dashboard"
sys.path.insert(0, str(DASH))
os.chdir(DASH)

# Banco temporário isolado
_fd, _db = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["IMPROVEMENTS_ENABLED"] = "1"

import db  # noqa: E402

db.DB_PATH = Path(_db)
db.init()

import improvements  # noqa: E402
import agent_usage  # noqa: E402
import campaign_guard_gate  # noqa: E402

# Gate externo aponta para campanhas reais — libera nos testes.
campaign_guard_gate.allow_campaign_llm = lambda cid: (True, "test-allow")
improvements.campaign_guard_gate.allow_campaign_llm = lambda cid: (True, "test-allow")
# Nunca dispara worker/LLM de verdade nestes testes.
improvements._orchestrator_worker = lambda rid: None
improvements._retry_worker = lambda tid: None


def _fake_vertex_plan(prompt, **kwargs):
    """Simula planner Vertex a partir das tasks já no prompt (ou falha → heurística)."""
    agent = kwargs.get("agent") or ""
    if agent == "planner" or "planner multiagente" in (prompt or ""):
        run_hint = []
        # Devolve plano genérico; o teste depois filtra copy queued
        return {
            "tasks": [],
            "conflicts": [],
        }
    raise RuntimeError("vertex mock: só planner permitido nos testes unitários")


# Planner unitário usa heurística (Vertex fake devolve tasks vazias → ainda aplica decisões;
# forçamos fallback levantando erro).
def _planner_no_vertex(rid):
    with mock.patch.object(improvements.vertex, "generate_json",
                           side_effect=RuntimeError("no vertex in unit test")):
        improvements._planner(rid)


def _seed_campaign():
    c = db.conn()
    c.execute(
        """INSERT INTO campaigns(nome, objetivo, status, contexto, versao, atualizada_em)
           VALUES(?,?,?,?,?,?)""",
        ("Campanha Teste Melhorias", "Validar orquestrador", "rascunho",
         "contexto teste", 1, db.now()))
    cid = c.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
    c.execute(
        """INSERT INTO templates_ai(nome,assunto,preheader,cta,cta_url,inner_html,texto_plano,
                                    status,origem,campaign_id,versao_campanha)
           VALUES(?,?,?,?,?,?,?,'pendente','ia',?,?)""",
        ("E1", "Assunto original fraco", "pre", "Agende", "https://4unik.com.br",
         "<p>corpo</p>", "corpo plano", cid, 1))
    tid = c.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
    c.execute("INSERT INTO campaign_steps(campaign_id,ordem,template_ref,offset_dias) VALUES(?,?,?,?)",
              (cid, 1, f"ai:{tid}", 0))
    c.commit()
    c.close()
    briefing = {
        "resumo": "teste",
        "riscos": [
            "Assunto do E1 é fraco e genérico",
            "CTA pouco específico",
            "Prova social ausente no corpo",
            "Preheader não complementa o assunto",
            "Ângulo do E1 não conecta com a dor do ICP",
        ],
        "checklist_pre_lancamento": ["Revisar assunto E1"],
        "recomendacao": "ajustar",
        "justificativa": "Copy precisa de gancho mais concreto antes de lançar",
        "checklist": {"items": [], "ready": False},
    }
    db.meta_set(f"briefing_{cid}", json.dumps(briefing, ensure_ascii=False))
    return cid, tid


def test_happy_path_review_approve():
    cid, tid = _seed_campaign()
    sug = improvements.collect_suggestions(cid)
    assert len(sug) >= 5, f"esperado >=5 sugestões, got {len(sug)}"

    # Evita LLM: worker já é no-op; injeta patch manual após planner
    run = improvements.start_run(cid, mode="review")
    assert run["id"]
    assert len(run["tasks"]) >= 5
    # campanha intacta
    c = db.conn()
    assunto = c.execute("SELECT assunto FROM templates_ai WHERE id=?", (tid,)).fetchone()["assunto"]
    c.close()
    assert assunto == "Assunto original fraco"

    # Classifica e injeta patch na primeira task copy
    rid = run["id"]
    # worker é no-op — planner sync sem Vertex (fallback heurístico)
    _planner_no_vertex(rid)
    run = improvements.run_get(rid)
    copy_tasks = [t for t in run["tasks"] if t["category"] == "copy" and t["status"] == "queued"]
    assert copy_tasks, "planner deveria enfileirar ao menos 1 task copy"
    task = copy_tasks[0]
    patch = {
        "target": "template",
        "target_id": "steps.1",
        "changes": [{
            "path": "steps.1.assunto",
            "from": "Assunto original fraco",
            "to": "Assunto revisado com gancho concreto",
        }],
        "expected_effect": "melhorar abertura",
        "verification": "assunto do E1 mudou",
        "confidence": 0.9,
        "risk_level": "low",
    }
    improvements._update_task(task["id"], status="awaiting_approval",
                              proposed_patch=patch, confidence=0.9, risk_level="low",
                              assigned_agent_key="copywriter")
    with mock.patch.object(improvements.ai, "gerar_briefing_lancamento", return_value={}):
        out = improvements.apply_task(task["id"], approved_by="test")
    assert any(t["status"] == "applied" for t in out["tasks"])

    c = db.conn()
    step = c.execute(
        "SELECT template_ref FROM campaign_steps WHERE campaign_id=? AND ordem=1",
        (cid,)).fetchone()
    new_tid = int(step["template_ref"].split(":")[1])
    novo = c.execute("SELECT assunto, status FROM templates_ai WHERE id=?",
                     (new_tid,)).fetchone()
    versao = c.execute("SELECT versao FROM campaigns WHERE id=?", (cid,)).fetchone()["versao"]
    c.close()
    assert novo["assunto"] == "Assunto revisado com gancho concreto"
    assert novo["status"] == "pendente"
    assert int(versao) >= 2
    versions = improvements.list_versions(cid)
    assert len(versions) >= 2
    print("OK happy_path_review_approve")


def test_reject_no_change():
    cid, tid = _seed_campaign()
    run = improvements.start_run(cid, mode="review")
    # encerra run ativo para não conflitar com outros testes no mesmo processo
    _planner_no_vertex(run["id"])
    tid_task = run["tasks"][0]["id"]
    improvements._update_task(tid_task, status="awaiting_approval",
                              proposed_patch={"changes": []}, assigned_agent_key="copywriter")
    improvements.reject_task(tid_task, reason="não faz sentido")
    improvements.cancel_run(run["id"])
    c = db.conn()
    assunto = c.execute("SELECT assunto FROM templates_ai WHERE id=?", (tid,)).fetchone()["assunto"]
    c.close()
    assert assunto == "Assunto original fraco"
    run = improvements.run_get(run["id"])
    assert any(t["status"] == "rejected" for t in run["tasks"])
    print("OK reject_no_change")


def test_rollback():
    cid, tid = _seed_campaign()
    v0 = improvements.save_campaign_version(cid, created_by="human", note="base")
    patch = {
        "target": "template",
        "target_id": "steps.1",
        "changes": [{
            "path": "steps.1.assunto",
            "from": "Assunto original fraco",
            "to": "Assunto que será revertido",
        }],
        "expected_effect": "tmp",
        "verification": "tmp",
        "confidence": 0.9,
        "risk_level": "low",
    }
    improvements.apply_patch_to_campaign(cid, patch, note="temp")
    c = db.conn()
    step = c.execute(
        "SELECT template_ref FROM campaign_steps WHERE campaign_id=? AND ordem=1",
        (cid,)).fetchone()
    new_tid = int(step["template_ref"].split(":")[1])
    assert c.execute("SELECT assunto FROM templates_ai WHERE id=?",
                     (new_tid,)).fetchone()["assunto"] == "Assunto que será revertido"
    c.close()
    improvements.rollback_campaign(cid, v0)
    c = db.conn()
    step = c.execute(
        "SELECT template_ref FROM campaign_steps WHERE campaign_id=? AND ordem=1",
        (cid,)).fetchone()
    cur_tid = int(step["template_ref"].split(":")[1])
    assunto = c.execute("SELECT assunto FROM templates_ai WHERE id=?",
                        (cur_tid,)).fetchone()["assunto"]
    c.close()
    assert assunto == "Assunto original fraco"
    print("OK rollback")


def test_catalog_and_guard():
    cat = agent_usage.catalog_with_status()
    assert any(a["id"] == "copywriter" for a in cat)
    assert any(a["id"] == "planner" for a in cat)
    os.environ["IMPROVEMENTS_ENABLED"] = "0"
    assert improvements.improvements_enabled() is False
    os.environ["IMPROVEMENTS_ENABLED"] = "1"
    print("OK catalog_and_guard")


if __name__ == "__main__":
    test_catalog_and_guard()
    test_happy_path_review_approve()
    test_reject_no_change()
    test_rollback()
    print("\nTodos os testes passaram.")
    try:
        os.unlink(_db)
    except Exception:
        pass
