#!/usr/bin/env python3
"""Pipeline real de campanha com contexto comercial (LLM simulado, sem threads nem envios).

criar_campanha → plano usa o contexto do negócio/idioma e grava snapshot;
retomar reusa o snapshot mesmo se o contexto mudar; reescrever usa o contexto vigente e
grava novo snapshot. Campanha continua rascunho; fila e envios intocados.
Rode: cd dashboard && ../.venv/bin/python ../scripts/test_campaign_pipeline.py
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

DASH = Path(__file__).resolve().parents[1] / "dashboard"
sys.path.insert(0, str(DASH))
os.chdir(DASH)
_tmp = Path(tempfile.mkdtemp())

import db  # noqa: E402

db.DB_PATH = _tmp / "pipeline.db"
db.init()
import ai  # noqa: E402
import business_context  # noqa: E402

_ctx = _tmp / "business-contexts.json"
shutil.copy(business_context.SOURCE, _ctx)
business_context.SOURCE = _ctx

PROMPTS, LOOP_KB, REWRITE_KB = [], [], []


class SyncThread:
    def __init__(self, target, args=(), kwargs=None, daemon=None):
        self.target, self.args, self.kwargs = target, args, kwargs or {}

    def start(self):
        self.target(*self.args, **self.kwargs)


def fake_json(*args, **kw):
    prompt = args[1] if len(args) > 1 and isinstance(args[1], str) else args[0]
    PROMPTS.append(prompt)
    return {"nome": "Campanha teste", "resumo": "r", "insight_gtm": {},
            "emails": [{"nome": "E1", "angulo": "a"}, {"nome": "E2", "angulo": "b"}]}


ai.threading.Thread = SyncThread
ai.sync_skill_kbs = lambda: []
ai._ai_json = fake_json
ai._ai_json_with_heartbeat = fake_json
ai._gerar_loop = lambda cid, objetivo, emails, cad_min, kb, imgs, brief, temperatura="": LOOP_KB.append(kb)


def fake_rewrite_plan(cid, diretrizes, analise, objetivo, contexto_atual, steps, brief, kb):
    REWRITE_KB.append(kb)
    return {}


ai._plano_reescrever = fake_rewrite_plan
_cancel = ai._geracao_cancelada
ai._geracao_cancelada = lambda cid: bool(REWRITE_KB) or _cancel(cid)

c = db.conn()
c.execute("INSERT INTO senders(email, nome) VALUES('genau@yoobe.co','Genau')")
for i, seg in enumerate(["quente", "morno", "frio"]):
    c.execute("INSERT INTO contacts(email, nome, segmento) VALUES(?,?,?)", (f"p{i}@empresa.test", f"P{i}", seg))
c.commit()
c.close()


def counts():
    c = db.conn()
    out = [c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("queue", "sends")]
    c.close()
    return out


class PipelineTests(unittest.TestCase):
    def setUp(self):
        PROMPTS.clear()
        LOOP_KB.clear()
        REWRITE_KB.clear()

    def test_catalogo_ptbr_planeja_com_contexto_do_catalogo_e_grava_snapshot(self):
        before = counts()
        cid = ai.criar_campanha("Welcome kits para onboarding de times", business_unit="catalogo",
                                market="br", language="pt-BR")
        self.assertTrue(PROMPTS, "planejador chamado")
        plan = PROMPTS[0]
        self.assertIn("Negócio: catalogo; mercado: br; idioma de saída: pt-BR", plan)
        self.assertIn("Catálogo vende brindes e kits personalizados", plan)
        self.assertEqual(LOOP_KB[0], json.loads(db.meta_get(f"commercial_context_{cid}"))["text"])
        c = db.conn()
        status = c.execute("SELECT status FROM campaigns WHERE id=?", (cid,)).fetchone()[0]
        c.close()
        self.assertEqual(status, "rascunho")
        self.assertEqual(counts(), before)

    def test_api_internacional_em_ingles(self):
        cid = ai.criar_campanha("API integration for SaaS with teams in Brazil", business_unit="corporate_api",
                                market="international_with_brazil_team", language="en")
        self.assertIn("Negócio: corporate_api; mercado: international_with_brazil_team; idioma de saída: en",
                      PROMPTS[0])
        sel = json.loads(db.meta_get(f"commercial_selection_{cid}"))
        self.assertEqual(sel["business_unit"], "corporate_api")

    def test_retomar_reusa_snapshot_e_reescrever_usa_contexto_vigente(self):
        cid = ai.criar_campanha("Kits para eventos", business_unit="catalogo", market="br", language="pt-BR")
        snap = json.loads(db.meta_get(f"commercial_context_{cid}"))
        data = json.loads(_ctx.read_text())
        data["catalogo"] += "\nREGRA NOVA DEPOIS DA CRIAÇÃO"
        _ctx.write_text(json.dumps(data, ensure_ascii=False))
        try:
            LOOP_KB.clear()
            ai.retomar_campanha(cid)
            self.assertEqual(LOOP_KB[-1], snap["text"])
            self.assertNotIn("REGRA NOVA", LOOP_KB[-1])

            c = db.conn()
            c.execute("INSERT INTO templates_ai(nome, assunto, inner_html, campaign_id) VALUES('E1','Assunto','<p>x</p>',?)", (cid,))
            tid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.execute("INSERT INTO campaign_steps(campaign_id, ordem, template_ref) VALUES(?,?,?)", (cid, 1, f"ai:{tid}"))
            c.commit()
            c.close()
            ai._set_progress(cid, status="pronta")
            ai.reescrever_campanha(cid, "foco em prazo de produção")
            self.assertIn("REGRA NOVA", REWRITE_KB[-1])
            novo = json.loads(db.meta_get(f"commercial_context_{cid}"))
            self.assertNotEqual(novo["version"], snap["version"])
            self.assertTrue(db.meta_get(f"commercial_context_snapshot_{cid}_{snap['version']}"))
        finally:
            shutil.copy(DASH / "context" / "business-contexts.json", _ctx)


if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        shutil.rmtree(_tmp, ignore_errors=True)
