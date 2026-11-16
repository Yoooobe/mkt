#!/usr/bin/env python3
"""Consumidor de eventos do Catálogo: assinatura, idempotência, ordem e privacidade.

Banco SQLite temporário; nada é enviado. Rode: cd dashboard && ../.venv/bin/python ../scripts/test_integrations.py
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

DASH = Path(__file__).resolve().parents[1] / "dashboard"
sys.path.insert(0, str(DASH))
os.chdir(DASH)
_fd, _db = tempfile.mkstemp(suffix=".db")
os.close(_fd)

import db  # noqa: E402

db.DB_PATH = Path(_db)
db.init()
import integrations  # noqa: E402

SECRET = "s3cr3t"
FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "catalog-event-signed.json").read_text())


def sign(body: bytes, t=None):
    import hashlib
    import hmac
    t = int(time.time()) if t is None else t
    return f"t={t},v1=" + hmac.new(SECRET.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()


def event(**over):
    base = {
        "event_id": "catalogo:budget:7:quote_requested", "schema_version": 1,
        "type": "catalog.quote_requested", "source": "catalogo", "source_entity": "budget",
        "source_entity_id": "7", "business_unit": "catalogo",
        "occurred_at": "2026-11-16T10:00:00.000Z",
        "buyer": {"email": "Ana@Cliente-A.test", "name": "Ana", "company": "Cliente A"},
        "budget_id": 7, "signals": {"amount": 5400.5, "currency": "BRL", "kits": 50},
        "attribution": {"first": None, "last": {"source": "yoobe-mail", "medium": "email"}},
    }
    base.update(over)
    return json.dumps(base).encode()


def send(body, secret=SECRET, sig=None):
    c = db.conn()
    try:
        return integrations.ingest(c, body, sig if sig is not None else sign(body), secret, db.tags_str)
    finally:
        c.close()


def q(sql, *a):
    c = db.conn()
    try:
        return [dict(r) for r in c.execute(sql, a).fetchall()]
    finally:
        c.close()


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        c = db.conn()
        for t in ("integration_events", "opportunities", "contacts"):
            c.execute(f"DELETE FROM {t}")
        c.commit()
        c.close()

    def test_fixture_assinado_pelo_catalogo_ts(self):
        body = FIXTURE["body"].encode()
        c = db.conn()
        res = integrations.ingest(c, body, FIXTURE["signature"], "fixture-secret", db.tags_str,
                                  now=FIXTURE["now"])
        c.close()
        self.assertFalse(res["duplicate"])
        opp = q("SELECT * FROM opportunities")[0]
        self.assertEqual((opp["stage"], opp["amount"], opp["business_unit"]), ("orcamento", 5400.5, "catalogo"))

    def test_cria_comprador_e_oportunidade_e_dez_reentregas_nao_duplicam(self):
        body = event()
        first = send(body)
        self.assertEqual((first["duplicate"], first["contato"]), (False, "criado"))
        for _ in range(10):
            self.assertTrue(send(body)["duplicate"])
        self.assertEqual(len(q("SELECT * FROM integration_events")), 1)
        self.assertEqual(len(q("SELECT * FROM opportunities")), 1)
        contact = q("SELECT * FROM contacts")[0]
        self.assertEqual((contact["email"], contact["origem"], contact["tags"]),
                         ("ana@cliente-a.test", "catalogo", "catalogo_comprador"))

    def test_assinatura_invalida_vencida_e_integracao_desligada(self):
        body = event()
        for sig, secret, code in [("t=1,v1=00", SECRET, "assinatura_invalida"),
                                  (sign(body, int(time.time()) - 3600), SECRET, "assinatura_invalida"),
                                  (sign(event(event_id="x")), SECRET, "assinatura_invalida"),
                                  (sign(body), "", "integracao_desligada")]:
            with self.assertRaises(integrations.Rejected) as e:
                send(body, secret=secret, sig=sig)
            self.assertEqual(e.exception.code, code)
        self.assertEqual(q("SELECT * FROM integration_events"), [])

    def test_contrato_invalido(self):
        for over, code in [({"type": "catalog.x"}, "tipo_desconhecido"),
                           ({"schema_version": 2}, "versao_nao_suportada"),
                           ({"buyer": {"email": "sem"}}, "comprador_sem_email"),
                           ({"source": "outro"}, "origem_desconhecida")]:
            with self.assertRaises(integrations.Rejected) as e:
                send(event(**over))
            self.assertEqual(e.exception.code, code)

    def test_fora_de_ordem_nao_regride_e_venda_nao_vira_perda(self):
        send(event(event_id="e-approved", type="catalog.delivery_approved", source_entity="delivery_plan",
                   source_entity_id="p1", occurred_at="2026-11-16T12:00:00Z"))
        send(event())  # orçamento chega atrasado
        self.assertEqual(q("SELECT stage FROM opportunities")[0]["stage"], "entrega_aprovada")
        send(event(event_id="e-sale", type="catalog.sale_confirmed", signals={"amount": 6000, "currency": "BRL"}))
        send(event(event_id="e-lost", type="catalog.quote_lost", signals={}))
        opp = q("SELECT * FROM opportunities")
        self.assertEqual(len(opp), 1)
        self.assertEqual((opp[0]["stage"], opp[0]["amount"]), ("venda_confirmada", 6000))

    def test_interesse_corporate_e_oportunidade_separada(self):
        send(event())
        send(event(event_id="e-corp", type="corporate.interest_signaled", business_unit="corporate_platform",
                   source_entity="delivery_plan", source_entity_id="p1", signals={"delivery_mode": "multiple"}))
        units = sorted((o["business_unit"], o["stage"]) for o in q("SELECT * FROM opportunities"))
        self.assertEqual(units, [("catalogo", "orcamento"), ("corporate_platform", "interesse")])
        self.assertEqual(q("SELECT tags FROM contacts")[0]["tags"], "catalogo_comprador,interesse_corporate")

    def test_contato_existente_preserva_supressao_e_tags_protegidas(self):
        c = db.conn()
        c.execute("INSERT INTO contacts(email, nome, empresa, tags, origem) VALUES(?,?,?,?,?)",
                  ("ana@cliente-a.test", "Ana Souza", "", "cliente_ativo_4unik,descadastrado", "rd"))
        c.commit()
        c.close()
        self.assertEqual(send(event())["contato"], "atualizado")
        row = q("SELECT * FROM contacts")[0]
        self.assertEqual((row["nome"], row["empresa"], row["origem"]), ("Ana Souza", "Cliente A", "rd"))
        self.assertEqual(row["tags"], "cliente_ativo_4unik,descadastrado,catalogo_comprador")

    def test_campos_extras_como_destinatarios_nao_sao_gravados(self):
        send(event(recipients=[{"name": "Maria Destinatária", "address1": "Rua das Flores 123"}]))
        dump = json.dumps([q("SELECT * FROM integration_events"), q("SELECT * FROM opportunities"),
                           q("SELECT * FROM contacts")], ensure_ascii=False)
        self.assertNotIn("Maria", dump)
        self.assertNotIn("Rua das Flores", dump)

    def test_nao_toca_campanhas_fila_nem_envios(self):
        before = [len(q(f"SELECT * FROM {t}")) for t in ("campaigns", "campaign_contacts", "queue", "sends")]
        send(event())
        after = [len(q(f"SELECT * FROM {t}")) for t in ("campaigns", "campaign_contacts", "queue", "sends")]
        self.assertEqual(before, after)

    def test_busca_aplica_confirma_e_rejeita_com_erro(self):
        good = event()
        bad = event(event_id="catalogo:budget:8:x", type="catalog.desconhecido")
        calls = []

        def http(method, url, body=None):
            calls.append((method, url, json.loads(body) if body else None))
            if method == "GET":
                return {"events": [
                    {"event_id": "catalogo:budget:7:quote_requested", "body": good.decode(), "signature": sign(good)},
                    {"event_id": "catalogo:budget:7:quote_requested", "body": good.decode(), "signature": sign(good)},
                    {"event_id": "catalogo:budget:8:x", "body": bad.decode(), "signature": sign(bad)},
                    {"event_id": "catalogo:budget:9:y", "body": good.decode(), "signature": "t=1,v1=00"},
                ]}
            return {"sent": 2, "failed": 2}

        res = integrations.pull_once(db.conn, "https://cat.test/api/integrations/marketing/events/",
                                     SECRET, db.tags_str, http=http)
        self.assertEqual((res["recebidos"], res["novos"], res["duplicados"], res["rejeitados"]), (4, 1, 1, 2))
        self.assertEqual(calls[0][1], "https://cat.test/api/integrations/marketing/events?limit=50")
        self.assertEqual(calls[1][1], "https://cat.test/api/integrations/marketing/events/ack")
        acks = calls[1][2]["results"]
        self.assertEqual([a["ok"] for a in acks], [True, True, False, False])
        self.assertEqual(acks[2]["error"], "tipo_desconhecido")
        self.assertEqual(acks[3]["error"], "assinatura_invalida")
        self.assertEqual(len(q("SELECT * FROM opportunities")), 1)

    def test_busca_vazia_nao_confirma(self):
        calls = []
        integrations.pull_once(db.conn, "https://cat.test/e", SECRET, db.tags_str,
                               http=lambda m, u, b=None: calls.append(m) or {"events": []})
        self.assertEqual(calls, ["GET"])


if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        os.unlink(_db)
