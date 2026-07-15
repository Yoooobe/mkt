#!/usr/bin/env python3
"""4unik.mail — camada de dados (SQLite).

Tudo que importa fica em dashboard.db: contatos, envios, respostas, templates
de IA, campanhas e a fila persistente (guardrail de offline/bateria).
"""
import base64
import hashlib
import hmac
import json
import re
import sqlite3
import unicodedata
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
GWS_DIR = BASE.parent
PROPOSALS = GWS_DIR / "proposals"
DB_PATH = BASE / "dashboard.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
  email TEXT PRIMARY KEY,
  nome TEXT DEFAULT '',
  empresa TEXT DEFAULT '',
  modulos TEXT DEFAULT '',
  mensalidades TEXT DEFAULT '',
  setup TEXT DEFAULT '',
  validade TEXT DEFAULT '',
  primeiro_envio TEXT DEFAULT '',
  ultimo_envio TEXT DEFAULT '',
  envios INTEGER DEFAULT 0,
  respondeu TEXT DEFAULT 'não',
  classe_monitor TEXT DEFAULT '',
  segmento TEXT DEFAULT '',
  origem TEXT DEFAULT 'manual',
  tags TEXT DEFAULT '',
  msgid TEXT DEFAULT '',
  thread TEXT DEFAULT '',
  criado_em TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS sends (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  data TEXT,
  msgid TEXT UNIQUE,
  email TEXT,
  etapa TEXT,
  thread TEXT,
  campaign_id INTEGER,
  track_id TEXT DEFAULT ''
);

-- Espelho local dos eventos coletados pela API pública e persistidos no Sanity.
-- worker_id = id do evento na borda (cursor de sincronização, sem perda).
CREATE TABLE IF NOT EXISTS events (
  worker_id INTEGER PRIMARY KEY,
  type TEXT,                               -- open | click
  track_id TEXT,
  email TEXT DEFAULT '',
  campaign_id INTEGER,
  etapa TEXT DEFAULT '',
  url TEXT DEFAULT '',
  ts TEXT DEFAULT '',
  ua TEXT DEFAULT '',
  cf_country TEXT DEFAULT '',
  prefetch INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_track ON events(track_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);

CREATE TABLE IF NOT EXISTS replies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  msgid TEXT UNIQUE,
  thread TEXT,
  email TEXT,
  data TEXT,
  classe TEXT DEFAULT '',
  confianca REAL DEFAULT 0,
  resumo TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS templates_ai (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  nome TEXT,
  assunto TEXT,
  preheader TEXT DEFAULT '',
  cta TEXT DEFAULT '',
  cta_url TEXT DEFAULT '',
  inner_html TEXT,
  texto_plano TEXT DEFAULT '',
  status TEXT DEFAULT 'pendente',          -- pendente | aprovado
  origem TEXT DEFAULT 'ia',                -- ia | seed
  criado_em TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS campaigns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  nome TEXT,
  objetivo TEXT DEFAULT '',
  tipo TEXT DEFAULT 'finita',              -- finita | infinita
  status TEXT DEFAULT 'rascunho',          -- rascunho | ativa | pausada | encerrada
  cad_min_dias INTEGER,                    -- cadência aleatória: sorteio entre min e max
  cad_max_dias INTEGER,                    -- NULL nos dois = usa offset fixo dos steps
  contexto TEXT DEFAULT '',
  sender_id INTEGER DEFAULT 1,             -- remetente que envia a campanha (senders.id)
  inicio_em TEXT DEFAULT '',               -- data/hora do 1º envio ('' = assim que ativar)
  audience_profile TEXT DEFAULT '',        -- segmento inteligente usado na seleção da audiência
  criada_em TEXT DEFAULT (datetime('now','localtime'))
);

-- Remetentes (multi-conta gws). Cada um é um perfil isolado em gws/accounts/<slug>/.
CREATE TABLE IF NOT EXISTS senders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT UNIQUE,
  nome TEXT DEFAULT '',
  proposito TEXT DEFAULT '',
  slug TEXT DEFAULT '',                     -- nome do perfil em gws/accounts/<slug>/ ('' = default/frank)
  signature_url TEXT DEFAULT '',
  ativo INTEGER DEFAULT 1,
  auto_reply TEXT DEFAULT 'off',           -- on | off (por remetente)
  auto_reply_classes TEXT DEFAULT 'agendar,interessado,pergunta_simples,recusa_encerrar',
  criado_em TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS campaign_steps (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  campaign_id INTEGER,
  ordem INTEGER,
  template_ref TEXT,                       -- 'builtin:trilha-1' | 'ai:<id>'
  offset_dias INTEGER                      -- NULL = cadência aleatória da campanha
);

CREATE TABLE IF NOT EXISTS campaign_contacts (
  campaign_id INTEGER,
  email TEXT,
  etapa_atual INTEGER DEFAULT 0,           -- última etapa já enviada (0 = nenhuma)
  status TEXT DEFAULT 'pendente',          -- pendente | ativo | pausado_resposta | concluido
  next_send_at TEXT,
  PRIMARY KEY (campaign_id, email)
);

CREATE TABLE IF NOT EXISTS queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tipo TEXT,                               -- check_thread | campaign_send | whatsapp | sync
  payload TEXT DEFAULT '{}',
  status TEXT DEFAULT 'pending',           -- pending | processing | done | failed
  attempts INTEGER DEFAULT 0,
  run_at TEXT DEFAULT (datetime('now','localtime')),
  created_at TEXT DEFAULT (datetime('now','localtime')),
  updated_at TEXT DEFAULT (datetime('now','localtime')),
  error TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);

-- Agendamentos reais detectados nas notificações do Calendly.
CREATE TABLE IF NOT EXISTS bookings (
  msgid TEXT PRIMARY KEY,
  email TEXT,
  nome TEXT DEFAULT '',
  quando TEXT DEFAULT '',
  status TEXT DEFAULT 'agendado',          -- agendado | cancelado
  criado_em TEXT DEFAULT (datetime('now','localtime'))
);

-- Propostas de correção de nome (qualidade de dados, revisadas pelo Genal antes de aplicar).
CREATE TABLE IF NOT EXISTS name_fixes (
  email TEXT PRIMARY KEY,
  nome_atual TEXT DEFAULT '',
  nome_sugerido TEXT DEFAULT '',
  acao TEXT DEFAULT 'manter',              -- manter | corrigir | limpar
  confianca REAL DEFAULT 0,
  motivo TEXT DEFAULT '',
  status TEXT DEFAULT 'pendente',          -- pendente | aprovado | rejeitado | aplicado
  lote TEXT DEFAULT '',
  criado_em TEXT DEFAULT (datetime('now','localtime'))
);

-- Devoluções (bounces) detectadas nas caixas dos remetentes. msgid = id da notificação
-- do mailer-daemon (dedup). tipo = hard (e-mail não existe → suprime) | soft (temporário).
CREATE TABLE IF NOT EXISTS bounces (
  msgid TEXT PRIMARY KEY,
  email TEXT,
  tipo TEXT DEFAULT 'hard',                -- hard | soft
  codigo TEXT DEFAULT '',                  -- ex.: 5.1.1
  diagnostico TEXT DEFAULT '',
  sender_id INTEGER,
  criado_em TEXT DEFAULT (datetime('now','localtime'))
);

-- Cache de relatórios da GA4 Data API (TTL controlado em ga4.py — protege a cota).
CREATE TABLE IF NOT EXISTS ga4_cache (
  key TEXT PRIMARY KEY,
  payload TEXT,
  fetched_at TEXT
);
"""


def conn():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def slugify(nome):
    s = unicodedata.normalize("NFKD", nome or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:40].rstrip("-")


def campaign_slug(campaign_id):
    if not campaign_id:
        return ""
    c = conn()
    r = c.execute("SELECT slug, nome FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    if not r:
        c.close()
        return f"c{campaign_id}"
    slug = r["slug"] or slugify(r["nome"]) or f"c{campaign_id}"
    if not r["slug"]:
        c.execute("UPDATE campaigns SET slug=? WHERE id=?", (slug, campaign_id))
        c.commit()
    c.close()
    return slug


def init():
    c = conn()
    c.executescript(SCHEMA)
    # migração idempotente: bancos antigos não têm sends.track_id / contacts.tags
    cols = {r["name"] for r in c.execute("PRAGMA table_info(sends)")}
    if "track_id" not in cols:
        c.execute("ALTER TABLE sends ADD COLUMN track_id TEXT DEFAULT ''")
    if "enviado_em" not in cols:
        # timestamp real do envio em UTC ISO (sends.data é só a data). Usado para detectar
        # cliques-fantasma de scanner (clique em <N s do envio). Ver click_bot_trackids.
        c.execute("ALTER TABLE sends ADD COLUMN enviado_em TEXT DEFAULT ''")
    ccols = {r["name"] for r in c.execute("PRAGMA table_info(contacts)")}
    if "tags" not in ccols:
        c.execute("ALTER TABLE contacts ADD COLUMN tags TEXT DEFAULT ''")
    rcols = {r["name"] for r in c.execute("PRAGMA table_info(replies)")}
    if "acao" not in rcols:
        c.execute("ALTER TABLE replies ADD COLUMN acao TEXT DEFAULT ''")
    pcols = {r["name"] for r in c.execute("PRAGMA table_info(campaigns)")}
    if "sender_id" not in pcols:
        c.execute("ALTER TABLE campaigns ADD COLUMN sender_id INTEGER DEFAULT 1")
    if "agendado_em" not in ccols:
        c.execute("ALTER TABLE contacts ADD COLUMN agendado_em TEXT DEFAULT ''")
    if "inicio_em" not in pcols:
        c.execute("ALTER TABLE campaigns ADD COLUMN inicio_em TEXT DEFAULT ''")
    if "clientes_confirmados" not in pcols:
        # guardrail: 1 só após o Genal digitar QUERO_ENVIAR na ativação (campanha que toca cliente)
        c.execute("ALTER TABLE campaigns ADD COLUMN clientes_confirmados INTEGER DEFAULT 0")
    if "audience_profile" not in pcols:
        c.execute("ALTER TABLE campaigns ADD COLUMN audience_profile TEXT DEFAULT ''")
    if "slug" not in pcols:
        # slug estável usado como utm_campaign nos links dos emails (casa GA4 ↔ campanha local)
        c.execute("ALTER TABLE campaigns ADD COLUMN slug TEXT DEFAULT ''")
    if "ads_briefing" not in pcols:
        c.execute("ALTER TABLE campaigns ADD COLUMN ads_briefing TEXT DEFAULT ''")
    for r in c.execute("SELECT id, nome FROM campaigns WHERE slug=''").fetchall():
        c.execute("UPDATE campaigns SET slug=? WHERE id=?",
                  (slugify(r["nome"]) or f"c{r['id']}", r["id"]))
    # enriquecimento de contatos (RD Station inbound): campos para cohort e personalização
    for col in ("cargo", "porte_assinantes", "porte_funcionarios", "erp", "etapa_funil",
                "estado_negocio", "motivo_perda", "qualificacao", "telefone", "enriquecimento"):
        if col not in ccols:
            c.execute(f"ALTER TABLE contacts ADD COLUMN {col} TEXT DEFAULT ''")
    c.commit()
    c.close()


# ---------------- tags (lista leve, normalizada, separada por vírgula) ----------------

def tag_norm(t):
    t = (t or "").strip().lower()
    t = re.sub(r"\s+", "-", t)
    return re.sub(r"[^0-9a-zà-ÿ_-]", "", t)


def tags_list(s):
    return [t for t in (s or "").split(",") if t.strip()]


def tags_str(lst):
    out = []
    for t in lst:
        n = tag_norm(t)
        if n and n not in out:
            out.append(n)
    return ",".join(out)


def aplicar_tags_resposta(c, email, classe=""):
    """Tags automáticas a partir da resposta (conexão aberta):
    sempre +'respondeu' -'sem-resposta'; classe 'agendar' → +'agendou' (-'descartado');
    classe 'recusa_encerrar' → +'descartado' (-'agendou'). agendou/descartado são exclusivos."""
    row = c.execute("SELECT tags FROM contacts WHERE email=?", (email,)).fetchone()
    if not row:
        return
    tags = [t for t in tags_list(row["tags"]) if t != "sem-resposta"]
    if "respondeu" not in tags:
        tags.append("respondeu")
    if classe == "agendar":
        # intenção de agendar (não é reserva real — essa vem do Calendly)
        tags = [t for t in tags if t != "descartado"]
        if "pediu_agendamento" not in tags:
            tags.append("pediu_agendamento")
    elif classe == "recusa_encerrar":
        tags = [t for t in tags if t not in ("agendou", "pediu_agendamento")]
        if "descartado" not in tags:
            tags.append("descartado")
    c.execute("UPDATE contacts SET tags=? WHERE email=?", (tags_str(tags), email))


def marcar_agendou(c, email, quando=""):
    """Reserva REAL do Calendly: +'agendou' -'pediu_agendamento', grava agendado_em."""
    row = c.execute("SELECT tags FROM contacts WHERE email=?", (email,)).fetchone()
    if not row:
        return False
    tags = [t for t in tags_list(row["tags"]) if t not in ("pediu_agendamento", "descartado")]
    if "respondeu" not in tags:
        tags.append("respondeu")
    if "sem-resposta" in tags:
        tags = [t for t in tags if t != "sem-resposta"]
    if "agendou" not in tags:
        tags.append("agendou")
    c.execute("UPDATE contacts SET tags=?, respondeu='sim', agendado_em=COALESCE(NULLIF(?, ''), agendado_em) "
              "WHERE email=?", (tags_str(tags), quando, email))
    return True


def desmarcar_agendou(c, email):
    """Cancelamento no Calendly: -'agendou' (volta a 'pediu_agendamento')."""
    row = c.execute("SELECT tags FROM contacts WHERE email=?", (email,)).fetchone()
    if not row:
        return
    tags = [t for t in tags_list(row["tags"]) if t != "agendou"]
    if "pediu_agendamento" not in tags:
        tags.append("pediu_agendamento")
    c.execute("UPDATE contacts SET tags=?, agendado_em='' WHERE email=?", (tags_str(tags), email))


def migrar_tags_agendamento():
    """Uma vez: os 'agendou' antigos eram só intenção → viram 'pediu_agendamento'.
    Reservas reais voltam a 'agendou' via scan_calendly()."""
    if meta_get("migr_agendou_v2"):
        return 0
    c = conn()
    n = 0
    for r in c.execute("SELECT email, tags, classe_monitor FROM contacts "
                       "WHERE ','||tags||',' LIKE '%,agendou,%'").fetchall():
        tags = [t for t in tags_list(r["tags"]) if t != "agendou"]
        if (r["classe_monitor"] or "") == "agendar" and "pediu_agendamento" not in tags:
            tags.append("pediu_agendamento")
        c.execute("UPDATE contacts SET tags=?, agendado_em='' WHERE email=?", (tags_str(tags), r["email"]))
        n += 1
    c.commit()
    c.close()
    meta_set("migr_agendou_v2", "1")
    return n


def backfill_respondeu_tags():
    """Sincroniza as tags com respondeu + classe_monitor (auto-cura: startup e sob demanda)."""
    c = conn()
    n = 0
    for r in c.execute("SELECT email, tags, classe_monitor FROM contacts WHERE LOWER(respondeu)='sim'").fetchall():
        antes = r["tags"] or ""
        aplicar_tags_resposta(c, r["email"], r["classe_monitor"] or "")
        depois = c.execute("SELECT tags FROM contacts WHERE email=?", (r["email"],)).fetchone()["tags"]
        if depois != antes:
            n += 1
    c.commit()
    c.close()
    return n


# ---------------- token de rastreio (HMAC, stateless) ----------------
# token = base64url(payload_json) + "." + base64url(HMAC_SHA256(secret, payload_json))
# O mesmo segredo e algoritmo vivem na API pública (leads-ingest-api/lib/emailTracking.ts).

def _b64u(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64u_decode(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign_token(payload: dict, secret: str) -> str:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    sig = hmac.new(secret.encode(), raw, hashlib.sha256).digest()
    return f"{_b64u(raw)}.{_b64u(sig)}"


def verify_token(token: str, secret: str):
    try:
        body_b64, sig_b64 = token.split(".", 1)
        raw = _b64u_decode(body_b64)
        expected = hmac.new(secret.encode(), raw, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64u_decode(sig_b64)):
            return None
        return json.loads(raw)
    except Exception:
        return None


def meta_get(key, default=""):
    c = conn()
    row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    c.close()
    return row["value"] if row else default


def meta_set(key, value):
    c = conn()
    c.execute("INSERT INTO meta(key,value) VALUES(?,?) "
              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
    c.commit()
    c.close()


def funil(contato):
    """novo → contatado → follow-up → último-toque → respondeu → pediu-agendamento → agendou | encerrado."""
    cols = contato.keys()
    classe = (contato["classe_monitor"] or "").strip()
    tags = tags_list(contato["tags"]) if "tags" in cols else []
    agendado = contato["agendado_em"] if "agendado_em" in cols else ""
    if "agendou" in tags or agendado:           # reserva REAL (Calendly)
        return "agendou"
    if classe == "recusa_encerrar" or "descartado" in tags:
        return "encerrado"
    if classe == "agendar" or "pediu_agendamento" in tags:
        return "pediu-agendamento"
    if (contato["respondeu"] or "").strip().lower() in ("sim", "yes", "true", "1"):
        return "respondeu"
    envios = contato["envios"] or 0
    if envios <= 0:
        return "novo"
    if envios == 1:
        return "contatado"
    if envios == 2:
        return "follow-up"
    return "último-toque"


def segmento_por_data(primeiro_envio):
    """Temperatura conforme abordagens-email.md: 2026 quente, 2025 morno, ≤2024 frio."""
    ano = (primeiro_envio or "")[:4]
    if ano >= "2026":
        return "quente"
    if ano == "2025":
        return "morno"
    return "frio" if ano else ""


def contato_dict(row):
    d = dict(row)
    d["funil"] = funil(row)
    d["tags"] = tags_list(d.get("tags", ""))
    d["lead_score"] = lead_score(d)
    d["perfis_lead"] = [k for k in PERFIS_LEAD if _perfil_match(d, k)]
    # Resumo seguro para cards do CRM; os detalhes continuam na linha do tempo.
    partes = []
    if d.get("ultimo_envio"):
        partes.append("último e-mail " + str(d["ultimo_envio"])[:10])
    if (d.get("respondeu") or "").lower() == "sim":
        partes.append("respondeu")
    if d.get("agendado_em"):
        partes.append("agendamento " + str(d["agendado_em"])[:10])
    d["resumo_historico"] = " · ".join(partes) or "sem interação registrada"
    return d


def sender_get(sid):
    """Remetente por id (dict) ou None."""
    c = conn()
    r = c.execute("SELECT * FROM senders WHERE id=?", (sid,)).fetchone()
    c.close()
    return dict(r) if r else None


def sender_default():
    c = conn()
    r = c.execute("SELECT * FROM senders ORDER BY id LIMIT 1").fetchone()
    c.close()
    return dict(r) if r else None


PERFIS_LEAD = {
    "com_telefone": "Com telefone",
    "telefone_alta_intencao": "Alta intenção + telefone",
    "telefone_decisor": "Decisores + telefone",
    "telefone_reativacao": "Reativação + telefone",
    "dados_ricos": "Dados ricos para campanha",
    "prontos_campanha": "Prontos para campanha",
}


def lead_score(row):
    """Nota explicável de utilidade comercial (0–100); não decide envio sozinho."""
    d = dict(row)
    raw_tags = d.get("tags", "")
    tags = set(raw_tags if isinstance(raw_tags, list) else tags_list(raw_tags))
    score = 0
    score += 20 if (d.get("telefone") or "").strip() else 0
    score += 12 if (d.get("cargo") or "").strip() else 0
    score += 10 if (d.get("empresa") or "").strip() else 0
    score += 10 if (d.get("qualificacao") or "").strip() else 0
    score += 8 if (d.get("etapa_funil") or d.get("estado_negocio") or "").strip() else 0
    score += 8 if (d.get("enriquecimento") or "").strip() else 0
    score += 6 if (d.get("erp") or "").strip() else 0
    score += 6 if (d.get("porte_assinantes") or d.get("porte_funcionarios") or "").strip() else 0
    score += 10 if tags & {"fez-demo", "pediu-agendamento", "interessado", "agendou"} else 0
    score += 5 if (d.get("envios") or 0) > 0 else 0
    score += 5 if (d.get("respondeu") or "").lower() == "sim" else 0
    return min(score, 100)


def _perfil_match(row, perfil):
    if not perfil:
        return True
    d = dict(row)
    phone = bool((d.get("telefone") or "").strip())
    raw_tags = d.get("tags", "")
    tags = set(raw_tags if isinstance(raw_tags, list) else tags_list(raw_tags))
    cargo = (d.get("cargo") or "").lower()
    decisor = bool(re.search(r"diretor|diretora|head|gerente|ceo|founder|fundador|s[oó]ci|vp|coordenador", cargo))
    alta_intencao = bool(tags & {"fez-demo", "pediu-agendamento", "interessado", "agendou"}) or bool(
        (d.get("qualificacao") or d.get("etapa_funil") or "").strip())
    if perfil == "com_telefone":
        return phone
    if perfil == "telefone_alta_intencao":
        return phone and alta_intencao
    if perfil == "telefone_decisor":
        return phone and decisor
    if perfil == "telefone_reativacao":
        return phone and (d.get("envios") or 0) > 0 and (d.get("respondeu") or "").lower() != "sim"
    if perfil == "dados_ricos":
        return lead_score(d) >= 50
    if perfil == "prontos_campanha":
        return lead_score(d) >= 40 and bool((d.get("empresa") or d.get("cargo") or "").strip())
    return True


def resolve_audience(segmentos=None, tags=None, apenas_sem_resposta=True, excluir_cliente=True, perfil=""):
    """Resolve a audiência de uma campanha pelos filtros do CRM.
    segmentos vazio = todos os segmentos; tags = inclui quem tiver ALGUMA delas (OR).
    excluir_cliente=True (padrão): remove quem tem tag de cliente protegida — A NÃO SER que
    essa tag de cliente seja explicitamente um dos alvos em `tags` (aí o Genal quer mirar cliente
    de propósito, e o gate QUERO_ENVIAR cuida da confirmação na ativação)."""
    c = conn()
    rows = c.execute("SELECT * FROM contacts").fetchall()
    c.close()
    segset = set(s for s in (segmentos or []) if s)
    tagset = set(tag_norm(t) for t in (tags or []) if t and tag_norm(t))
    prot = set(TAGS_CLIENTE_PROTEGIDAS)
    out = []
    for r in rows:
        ctags = set(tags_list(r["tags"]))
        # quem está suprimido (descadastro/unsubscribe ou e-mail inválido) NUNCA entra — sem exceção
        if ctags & set(TAGS_SUPRIMIDAS):
            continue
        # segmento vazio = curinga (não é excluído por filtro de segmento)
        if segset and r["segmento"] and r["segmento"] not in segset:
            continue
        if apenas_sem_resposta and (r["respondeu"] or "").lower() == "sim":
            continue
        if tagset and not (tagset & ctags):
            continue
        if not _perfil_match(r, perfil):
            continue
        if excluir_cliente:
            prot_c = ctags & prot
            if prot_c and not (prot_c & tagset):    # é cliente e não foi mirado de propósito
                continue
        out.append(r["email"])
    return out


def segmentos_leads():
    """Contagens e recomendações dos segmentos inteligentes, sempre após supressões/clientes."""
    objetivos = {
        "com_telefone": "Relacionamento multicanal com leads que possuem telefone confirmado",
        "telefone_alta_intencao": "Converter leads de alta intenção em reuniões comerciais",
        "telefone_decisor": "Abordar decisores com proposta de valor orientada a ROI",
        "telefone_reativacao": "Reativar leads já contatados que ainda não responderam",
        "dados_ricos": "Criar campanha altamente personalizada usando o perfil agregado da base",
        "prontos_campanha": "Iniciar prospecção com os contatos mais completos e acionáveis",
    }
    return [{"id": k, "nome": nome, "contatos": len(resolve_audience(perfil=k)),
             "objetivo_sugerido": objetivos[k]} for k, nome in PERFIS_LEAD.items()]


def resumo_audiencia(emails):
    """Perfil agregado do público resolvido (para o briefing de cohort da IA).
    Conta perfil comercial e histórico agregado, sem levar PII ao prompt da IA."""
    from collections import Counter
    emails = list(emails or [])
    if not emails:
        return {"total": 0}
    c = conn()
    qs = ",".join("?" * len(emails))
    rows = c.execute(
        f"""SELECT porte_assinantes, porte_funcionarios, erp, cargo, estado_negocio,
                   motivo_perda, qualificacao, origem, tags, envios, respondeu, ultimo_envio,
                   telefone, enriquecimento, etapa_funil
            FROM contacts WHERE email IN ({qs})""", emails).fetchall()
    c.close()

    def top(campo, n=4):
        cnt = Counter((r[campo] or "").strip() for r in rows if (r[campo] or "").strip())
        return [{"valor": v, "n": q} for v, q in cnt.most_common(n)]

    fez_demo = sum(1 for r in rows if "fez-demo" in tags_list(r["tags"]))
    ja_contatados = sum(1 for r in rows if (r["envios"] or 0) > 0)
    responderam = sum(1 for r in rows if (r["respondeu"] or "").lower() == "sim")
    ultimo_contato = max((r["ultimo_envio"] or "" for r in rows), default="")
    com_telefone = sum(1 for r in rows if (r["telefone"] or "").strip())
    com_historico = sum(1 for r in rows if (r["enriquecimento"] or "").strip())
    em_etapa = sum(1 for r in rows if (r["etapa_funil"] or "").strip())
    return {
        "total": len(rows),
        "porte": top("porte_assinantes") or top("porte_funcionarios"),
        "erp": top("erp"),
        "cargo": top("cargo"),
        "estado": top("estado_negocio", 3),
        "motivo_perda": top("motivo_perda"),
        "qualificacao": top("qualificacao"),
        "origem": top("origem"),
        "fez_demo": fez_demo,
        "ja_contatados": ja_contatados,
        "responderam": responderam,
        "ultimo_contato": ultimo_contato,
        # Apenas contagens agregadas: telefone e observações individuais não entram no prompt.
        "com_telefone": com_telefone,
        "com_historico": com_historico,
        "com_etapa_funil": em_etapa,
    }


def _ts_epoch(s):
    """'2026-07-03T20:34:28.604Z' | '...Z' | 'YYYY-MM-DD HH:MM:SS' → epoch (s). None se inválido.
    events.ts e sends.enviado_em são ambos UTC; como só usamos deltas/ordenação, o resultado é
    consistente independente do fuso."""
    if not s:
        return None
    from datetime import datetime, timezone
    s = str(s).replace("T", " ").replace("Z", "")[:19].strip()
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        return None


def click_bot_trackids(c, janela_seg):
    """track_ids de clique que são de SCANNER (bot), não humanos → dict {track_id: motivo}.
    S1: clique ANTES de qualquer abertura do mesmo track (humano precisa abrir p/ clicar) — só
        dispara se há abertura (humano com imagens off, sem nenhuma abertura, não é marcado).
    S2: clique em < janela_seg do envio (sends.enviado_em). Ver plano cliques-fantasma."""
    rows = c.execute(
        """SELECT cl.track_id,
                  MIN(cl.ts) AS click_ts,
                  (SELECT MIN(o.ts) FROM events o WHERE o.track_id=cl.track_id AND o.type='open') AS first_open,
                  (SELECT s.enviado_em FROM sends s WHERE s.track_id=cl.track_id) AS enviado_em
           FROM events cl WHERE cl.type='click' AND cl.track_id!='' GROUP BY cl.track_id""").fetchall()
    bot = {}
    for r in rows:
        ct = _ts_epoch(r["click_ts"])
        if ct is None:
            continue
        fo = _ts_epoch(r["first_open"])
        if fo is not None and ct < fo:
            bot[r["track_id"]] = "clicou antes de abrir"
            continue
        en = _ts_epoch(r["enviado_em"])
        if en is not None and (ct - en) < janela_seg:
            bot[r["track_id"]] = f"clicou +{int(ct - en)}s do envio"
    return bot


# ---------------- guardrail de clientes (nunca prospectar quem já é cliente) ----------------
# Tags que marcam um contato como cliente 4Unik (ativo/inativo) ou suspeito de ser:
#  *_asaas        → veio da planilha-fonte de clientes (Asaas)
#  *_4unik        → contato da base que casou por DOMÍNIO ou revisão manual
#  possivel_*     → casou por NOME (fuzzy) — suspeita, também protegido
# Campanha que contenha QUALQUER um destes só envia após confirmação explícita (app.py).
TAGS_CLIENTE_PROTEGIDAS = (
    "cliente_ativo_4unik", "cliente_inativo_4unik", "possivel_cliente_4unik",
)


def contato_eh_cliente(tags):
    """True se a lista/str de tags inclui alguma tag de cliente protegida."""
    s = set(tags_list(tags) if isinstance(tags, str) else (tags or []))
    return bool(s & set(TAGS_CLIENTE_PROTEGIDAS))


# Tags de SUPRESSÃO — nunca voltam para audiência e nunca recebem envio:
#  descadastrado         → clicou em "descadastrar" (unsubscribe), via worker de rastreio.
#  email_invalido        → e-mail não existe (hard bounce detectado em scan_bounces).
#  soft_bounce_suprimido → devolveu soft (temporário) N vezes seguidas (reincidência) e foi
#                          removido — o endereço pode existir, mas nunca entrega, então vira lixo.
TAG_DESCADASTRO = "descadastrado"
TAG_EMAIL_INVALIDO = "email_invalido"
TAG_SOFT_SUPRIMIDO = "soft_bounce_suprimido"
TAGS_SUPRIMIDAS = (TAG_DESCADASTRO, TAG_EMAIL_INVALIDO, TAG_SOFT_SUPRIMIDO, "nao-contatar")


def _marcar_suprimido(c, email, tag, status_cc):
    """Adiciona uma tag de supressão e pausa a cadência ativa do contato. Idempotente.
    `c` é uma conexão aberta (commit fica a cargo de quem chama)."""
    email = (email or "").lower()
    row = c.execute("SELECT tags FROM contacts WHERE email=?", (email,)).fetchone()
    if not row:
        return False
    tags = tags_list(row["tags"])
    if tag not in tags:
        tags.append(tag)
        c.execute("UPDATE contacts SET tags=? WHERE email=?", (tags_str(tags), email))
    c.execute("UPDATE campaign_contacts SET status=? WHERE email=? AND status IN ('pendente','ativo')",
              (status_cc, email))
    return True


def marcar_descadastro(c, email):
    """Marca o contato como descadastrado (unsubscribe) e pausa a cadência. Idempotente."""
    return _marcar_suprimido(c, email, TAG_DESCADASTRO, "descadastrado")


def marcar_email_invalido(c, email):
    """Marca o contato como e-mail inválido (hard bounce) e pausa a cadência. Idempotente."""
    return _marcar_suprimido(c, email, TAG_EMAIL_INVALIDO, "email_invalido")


def marcar_soft_suprimido(c, email):
    """Remove o contato por soft bounce reincidente (N devoluções temporárias) e pausa a
    cadência. Idempotente. Tag separada da hard para distinguir o motivo no dashboard."""
    return _marcar_suprimido(c, email, TAG_SOFT_SUPRIMIDO, "soft_suprimido")


def contatos_cliente_na_campanha(cid):
    """Quantos contatos da campanha são clientes protegidos (para o gate de ativação).
    Devolve total + quebra por ativo/inativo/possível + uma amostra (email, empresa, tag)."""
    c = conn()
    rows = c.execute(
        """SELECT ct.email, ct.empresa, ct.tags FROM campaign_contacts cc
           JOIN contacts ct ON ct.email=cc.email WHERE cc.campaign_id=?""", (cid,)).fetchall()
    c.close()
    total = ativos = inativos = possiveis = 0
    amostra = []
    for r in rows:
        tags = set(tags_list(r["tags"]))
        protegidas = tags & set(TAGS_CLIENTE_PROTEGIDAS)
        if not protegidas:
            continue
        total += 1
        if "cliente_ativo_4unik" in tags:
            ativos += 1
        elif "cliente_inativo_4unik" in tags:
            inativos += 1
        elif "possivel_cliente_4unik" in tags:
            possiveis += 1
        if len(amostra) < 8:
            amostra.append({"email": r["email"], "empresa": r["empresa"] or "",
                            "tag": sorted(protegidas)[0]})
    return {"total": total, "ativos": ativos, "inativos": inativos,
            "possiveis": possiveis, "amostra": amostra}


def enqueue(tipo, payload=None, run_at=None):
    c = conn()
    c.execute("INSERT INTO queue(tipo,payload,run_at) VALUES(?,?,COALESCE(?,datetime('now','localtime')))",
              (tipo, json.dumps(payload or {}, ensure_ascii=False), run_at))
    c.commit()
    qid = c.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
    c.close()
    return qid


def enqueue_once(tipo, payload=None, run_at=None):
    """Enfileira uma tarefa somente se ela ainda não estiver ativa ou esgotada.

    O BEGIN IMMEDIATE torna a checagem + inserção atômicas mesmo quando o ciclo
    periódico e o gatilho rápido encontram o mesmo envio ao mesmo tempo.
    Tarefas failed também bloqueiam uma nova inserção automática: elas precisam
    de intervenção explícita depois de esgotarem MAX_ATTEMPTS.
    """
    serialized = json.dumps(payload or {}, ensure_ascii=False)
    c = conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        existing = c.execute(
            "SELECT id FROM queue WHERE tipo=? AND payload=? "
            "AND status IN ('pending','processing','failed') LIMIT 1",
            (tipo, serialized),
        ).fetchone()
        if existing:
            c.commit()
            return None
        c.execute(
            "INSERT INTO queue(tipo,payload,run_at) "
            "VALUES(?,?,COALESCE(?,datetime('now','localtime')))",
            (tipo, serialized, run_at),
        )
        qid = c.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
        c.commit()
        return qid
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def quarantine_interrupted_queue():
    """No startup, bloqueia tarefas que ficaram reservadas durante uma queda."""
    c = conn()
    n = c.execute(
        "UPDATE queue SET status='failed', updated_at=datetime('now','localtime'), "
        "error=CASE WHEN error='' THEN "
        "'processo interrompido durante execução; reenvio automático bloqueado' "
        "ELSE error END WHERE status='processing'"
    ).rowcount
    c.commit()
    c.close()
    return n


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


if __name__ == "__main__":
    init()
    print(f"schema ok em {DB_PATH}")
