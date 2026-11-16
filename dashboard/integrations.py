"""Consumidor dos eventos comerciais do Catálogo (outbox transacional → POST assinado).

Idempotência: integration_events.event_id é a chave; reentrega do mesmo evento devolve
`duplicate` sem alterar nada. Cada evento atualiza uma oportunidade por orçamento/plano e
o contato do COMPRADOR. Destinatários das planilhas nunca chegam aqui (o envelope não os
traz) e nada é enviado, ativado ou colocado em campanha por este módulo.
"""
import hashlib
import hmac
import json
import re
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS integration_events (
  event_id TEXT PRIMARY KEY,
  type TEXT NOT NULL,
  schema_version INTEGER NOT NULL,
  source TEXT NOT NULL,
  source_entity TEXT NOT NULL,
  source_entity_id TEXT NOT NULL,
  business_unit TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  received_at TEXT DEFAULT (datetime('now')),
  email TEXT DEFAULT '',
  opportunity_id INTEGER,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS opportunities (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  opportunity_key TEXT NOT NULL,
  business_unit TEXT NOT NULL,
  email TEXT NOT NULL,
  empresa TEXT DEFAULT '',
  budget_id INTEGER,
  stage TEXT NOT NULL,
  stage_at TEXT NOT NULL,
  amount REAL,
  currency TEXT DEFAULT '',
  signals TEXT DEFAULT '{}',
  attribution TEXT DEFAULT '{}',
  owner TEXT DEFAULT '',
  next_step TEXT DEFAULT '',
  created_at TEXT DEFAULT (datetime('now')),
  updated_at TEXT DEFAULT (datetime('now')),
  UNIQUE(source, opportunity_key)
);
CREATE INDEX IF NOT EXISTS idx_opportunities_email ON opportunities(email);
"""

SOURCES = {"catalogo"}
SCHEMA_VERSIONS = {1}
# Ordem das etapas: só avança (eventos atrasados ou fora de ordem não regridem).
STAGES = {
    "catalog.quote_requested": ("orcamento", 1),
    "catalog.delivery_submitted": ("entrega_em_analise", 2),
    "catalog.delivery_approved": ("entrega_aprovada", 3),
    "catalog.sale_confirmed": ("venda_confirmada", 4),
    "catalog.quote_lost": ("perdida", 5),
    "corporate.interest_signaled": ("interesse", 1),
}
RANK = {stage: rank for stage, rank in STAGES.values()}
TYPES = set(STAGES)
UNITS = {"catalogo", "corporate_platform"}
TAG_BY_UNIT = {"catalogo": "catalogo_comprador", "corporate_platform": "interesse_corporate"}
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
TOLERANCE_SEC = 300


class Rejected(Exception):
    def __init__(self, status, code):
        super().__init__(code)
        self.status = status
        self.code = code


def ensure_schema(c):
    c.executescript(SCHEMA)


def verify_signature(body: bytes, header: str, secret: str, now=None) -> bool:
    parts = dict(p.strip().split("=", 1) for p in (header or "").split(",") if "=" in p)
    try:
        t = int(parts.get("t", ""))
    except ValueError:
        return False
    if abs((now if now is not None else time.time()) - t) > TOLERANCE_SEC:
        return False
    expected = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, parts.get("v1", ""))


def _text(v, n=200):
    return v.strip()[:n] if isinstance(v, str) else ""


def parse(body: bytes) -> dict:
    try:
        env = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise Rejected(400, "json_invalido")
    if not isinstance(env, dict):
        raise Rejected(400, "json_invalido")
    if env.get("source") not in SOURCES:
        raise Rejected(422, "origem_desconhecida")
    if env.get("schema_version") not in SCHEMA_VERSIONS:
        raise Rejected(422, "versao_nao_suportada")
    if env.get("type") not in TYPES:
        raise Rejected(422, "tipo_desconhecido")
    if env.get("business_unit") not in UNITS:
        raise Rejected(422, "negocio_desconhecido")
    for k in ("event_id", "source_entity", "source_entity_id", "occurred_at"):
        if not _text(env.get(k)):
            raise Rejected(422, f"campo_obrigatorio:{k}")
    buyer = env.get("buyer") if isinstance(env.get("buyer"), dict) else {}
    email = _text(buyer.get("email"), 254).lower()
    if not EMAIL_RE.match(email):
        raise Rejected(422, "comprador_sem_email")
    env["buyer"] = {"email": email, "name": _text(buyer.get("name"), 120),
                    "company": _text(buyer.get("company"), 160)}
    env["signals"] = env.get("signals") if isinstance(env.get("signals"), dict) else {}
    env["attribution"] = env.get("attribution") if isinstance(env.get("attribution"), dict) else {}
    return env


def _opportunity_key(env):
    # Uma oportunidade por negócio: Catálogo (orçamento → venda) e Corporate (interesse)
    # ficam separadas mesmo vindo do mesmo orçamento. Plano com orçamento = o orçamento.
    base = (f"budget:{env['budget_id']}" if env.get("budget_id") is not None
            else f"{env['source_entity']}:{env['source_entity_id']}")
    return f"{env['business_unit']}:{base}"


def advances(current, new):
    """Etapa só avança; venda confirmada não vira perda, e venda após perda reabre."""
    if current == "venda_confirmada" and new == "perdida":
        return False
    if current == "perdida" and new == "venda_confirmada":
        return True
    return RANK[new] > RANK.get(current, 0)


def _upsert_buyer(c, env, tags_str):
    b = env["buyer"]
    tag = TAG_BY_UNIT[env["business_unit"]]
    row = c.execute("SELECT nome, empresa, tags FROM contacts WHERE email=?", (b["email"],)).fetchone()
    if not row:
        c.execute("INSERT INTO contacts(email, nome, empresa, origem, tags) VALUES(?,?,?,?,?)",
                  (b["email"], b["name"], b["company"], "catalogo", tags_str([tag])))
        return "criado"
    # Só completa vazios e acrescenta a tag: não mexe em tags protegidas nem em supressão.
    tags = [t for t in (row["tags"] or "").split(",") if t.strip()]
    c.execute("UPDATE contacts SET nome=?, empresa=?, tags=? WHERE email=?",
              (row["nome"] or b["name"], row["empresa"] or b["company"],
               tags_str(tags + [tag]), b["email"]))
    return "atualizado"


def _apply(c, env):
    key = _opportunity_key(env)
    signals = env["signals"]
    stage_info = STAGES.get(env["type"])
    cur = c.execute("SELECT * FROM opportunities WHERE source=? AND opportunity_key=?",
                    (env["source"], key)).fetchone()
    amount = signals.get("amount") if isinstance(signals.get("amount"), (int, float)) else None
    currency = _text(signals.get("currency"), 3)
    if not cur:
        stage = stage_info[0]
        c.execute("""INSERT INTO opportunities(source, opportunity_key, business_unit, email, empresa,
                       budget_id, stage, stage_at, amount, currency, signals,
                       attribution) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (env["source"], key, env["business_unit"], env["buyer"]["email"],
                   env["buyer"]["company"], env.get("budget_id"), stage, env["occurred_at"],
                   amount, currency,
                   json.dumps(signals, ensure_ascii=False),
                   json.dumps(env["attribution"], ensure_ascii=False)))
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]
    merged = {**json.loads(cur["signals"] or "{}"), **signals}
    stage, stage_at = cur["stage"], cur["stage_at"]
    if advances(stage, stage_info[0]):
        stage, stage_at = stage_info[0], env["occurred_at"]
    c.execute("""UPDATE opportunities SET stage=?, stage_at=?, amount=COALESCE(?, amount),
                   currency=CASE WHEN ?<>'' THEN ? ELSE currency END,
                   signals=?,
                   empresa=CASE WHEN empresa='' THEN ? ELSE empresa END,
                   budget_id=COALESCE(budget_id, ?), updated_at=datetime('now') WHERE id=?""",
              (stage, stage_at, amount, currency, currency,
               json.dumps(merged, ensure_ascii=False), env["buyer"]["company"],
               env.get("budget_id"), cur["id"]))
    return cur["id"]


def ingest(c, body: bytes, signature: str, secret: str, tags_str, now=None) -> dict:
    """Valida, registra e aplica um evento numa única transação SQLite."""
    if not secret:
        raise Rejected(503, "integracao_desligada")
    if not verify_signature(body, signature, secret, now):
        raise Rejected(401, "assinatura_invalida")
    env = parse(body)
    ensure_schema(c)
    c.execute("BEGIN IMMEDIATE")
    try:
        done = c.execute("SELECT opportunity_id FROM integration_events WHERE event_id=?",
                         (env["event_id"],)).fetchone()
        if done:
            c.execute("ROLLBACK")
            return {"ok": True, "duplicate": True, "opportunity_id": done[0]}
        contact = _upsert_buyer(c, env, tags_str)
        opp = _apply(c, env)
        c.execute("""INSERT INTO integration_events(event_id, type, schema_version, source,
                       source_entity, source_entity_id, business_unit, occurred_at, email,
                       opportunity_id, payload) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                  (env["event_id"], env["type"], env["schema_version"], env["source"],
                   env["source_entity"], env["source_entity_id"], env["business_unit"],
                   env["occurred_at"], env["buyer"]["email"], opp,
                   json.dumps({"signals": env["signals"], "attribution": env["attribution"]},
                              ensure_ascii=False)))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    return {"ok": True, "duplicate": False, "opportunity_id": opp, "contato": contact}


def pull_once(conn_factory, url, secret, tags_str, http=None, limit=50):
    """Modo de busca: pede ao Catálogo os eventos prontos, aplica cada um com `ingest` e
    confirma em /ack. Rejeição de contrato (4xx) também confirma com erro, para o Catálogo
    reentregar com backoff e desistir depois; falha de rede não confirma (volta à fila)."""
    if http is None:
        import requests  # certifi: o Python.org do macOS não usa a cadeia do sistema

        def http(method, u, body=None):
            r = requests.request(method, u, data=body, timeout=20, headers={
                "authorization": f"Bearer {secret}", "content-type": "application/json",
                "user-agent": "yoobe-mail-integration/1"})
            r.raise_for_status()
            return r.json() if r.content else {}
    base = url.rstrip("/")
    got = http("GET", f"{base}?limit={int(limit)}")
    results, out = [], {"recebidos": 0, "novos": 0, "duplicados": 0, "rejeitados": 0}
    for ev in got.get("events") or []:
        out["recebidos"] += 1
        c = conn_factory()
        try:
            res = ingest(c, ev.get("body", "").encode("utf-8"), ev.get("signature", ""),
                         secret, tags_str)
            out["duplicados" if res["duplicate"] else "novos"] += 1
            results.append({"event_id": ev.get("event_id"), "ok": True})
        except Rejected as e:
            out["rejeitados"] += 1
            results.append({"event_id": ev.get("event_id"), "ok": False, "error": e.code})
        finally:
            c.close()
    if results:
        out["ack"] = http("POST", f"{base}/ack", json.dumps({"results": results}).encode())
    return out
