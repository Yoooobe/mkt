#!/usr/bin/env python3
"""4unik.mail — servidor local (FastAPI + SQLite) em http://localhost:8377.

API REST documentada em /docs (autentique com o header X-API-Key do arquivo
dashboard/.env). O ciclo da fila roda numa thread a cada 30min e também no
boot (catch-up pós offline/bateria, junto com o LaunchAgent RunAtLoad).
"""
import json
import csv
import io
import re
import secrets
import threading
import time
import unicodedata
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import ai
import db
import engine
import ga4
import nomes
import seed
import sync
import strategy
import google_media

ENV_FILE = db.BASE / ".env"


def _api_key():
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if line.startswith("API_KEY="):
                return line.split("=", 1)[1].strip()
    key = secrets.token_hex(16)
    ENV_FILE.write_text(f"API_KEY={key}\n")
    return key


API_KEY = _api_key()
app = FastAPI(title="4unik.mail", version="1.0",
              description="Dashboard local de email marketing com IA da 4Unik")


@app.middleware("http")
async def auth(request: Request, call_next):
    if request.url.path.startswith("/api/") and request.headers.get("X-API-Key") != API_KEY:
        return JSONResponse({"erro": "X-API-Key inválida (veja dashboard/.env)"}, status_code=401)
    return await call_next(request)


# ---------------- bootstrap ----------------

def notificar_leads_novos():
    """Lead novo do formulário do site (Sanity leadSubmission) → WhatsApp (Z-API, número
    configurado em ZAPI_PHONE) + email para LEADS_NOTIFY_EMAIL. Cursor em meta evita
    renotificar; a primeira rodada só posiciona o cursor (não notifica histórico)."""
    r = ga4.site_leads(limit=20)
    leads = [l for l in r.get("leads", []) if l.get("quando") and l.get("email")
             and "smoke" not in l["email"] and not l["email"].endswith("example.com")]
    if not leads:
        return
    mais_novo = max(l["quando"] for l in leads)
    cursor = db.meta_get("leads_notify_cursor", "")
    if not cursor:
        db.meta_set("leads_notify_cursor", mais_novo)
        return
    novos = [l for l in leads if l["quando"] > cursor]
    if not novos:
        return
    db.meta_set("leads_notify_cursor", mais_novo)
    email_dest = engine.ENV.get("LEADS_NOTIFY_EMAIL", "comercial@4unik.com.br")
    for l in novos:
        linhas = [f"{l['nome']} — {l['empresa'] or 'sem empresa'}",
                  f"{l['email']} · {l['telefone'] or 'sem telefone'}",
                  f"Origem: {l['origem'] or '—'} · {l['quando']}"]
        if l.get("mensagem"):
            linhas.append(f"Mensagem: {l['mensagem']}")
        corpo = "\n".join(linhas)
        texto = f"🧲 *Novo lead do site 4unik*\n\n{corpo}\n\nResponda rápido — lead quente."
        if not engine.whatsapp(texto) and engine.ENV.get("ZAPI_URL"):
            db.enqueue("whatsapp", {"texto": texto})
        try:
            html = ("<h3>🧲 Novo lead do site 4unik</h3><p>" +
                    corpo.replace("\n", "<br>") + "</p>"
                    "<p>Lead salvo no Sanity (Studio → Leads e audiência) e disponível "
                    "na aba Site &amp; Ads do 4unik.mail para importar ao CRM.</p>")
            engine.enviar_email({"email": email_dest, "nome": "Comercial 4Unik"},
                                f"🧲 Novo lead do site: {l['nome']} ({l['empresa'] or l['origem'] or 'site'})",
                                html, texto)
        except Exception as e:
            engine.log(f"notificação de lead por email falhou: {e}")
        engine.log(f"lead do site notificado: {l['email']} ({l['origem']})")


# ---------------- loops de runtime (ligados sob demanda) ----------------
# Para reduzir consumo, as 4 threads de background só nascem quando existe
# campanha ativa (ensure_runtime_loops) e adormecem sozinhas quando não há
# mais trabalho: no topo de cada iteração o loop checa sua condição e, se
# falsa, espera no _wake (timeout de reavaliação) em vez de girar no
# intervalo curto. _wake.set() na ativação acorda todo mundo na hora.

_loops_lock = threading.Lock()
_loops_started = False
_wake = threading.Event()
_IDLE_WAIT_S = 300


def _dormir_ate_ativar():
    """Espera um _wake.set() (ativação de campanha) ou o timeout de reavaliação."""
    if _wake.wait(_IDLE_WAIT_S):
        _wake.clear()


def _ciclo_periodico():
    while True:
        if not engine.has_active_campaigns():
            _dormir_ate_ativar()
            continue
        try:
            engine.process_cycle()
            strategy.run("periodic", perform_sync=False)
        except Exception as e:
            engine.log(f"ciclo com erro inesperado: {e}")
        time.sleep(1800)


def _poll_tracking_loop():
    # pull leve de aberturas/cliques a cada 60s → painel quase em tempo real
    while True:
        if not engine.has_active_campaigns():
            _dormir_ate_ativar()
            continue
        time.sleep(60)
        try:
            engine.poll_tracking()
        except Exception:
            pass


def _replies_tem_trabalho():
    # Período de graça: mesmo sem campanha ativa, threads de envios recentes
    # ainda podem receber resposta — continua varrendo por ~14 dias.
    return engine.has_active_campaigns() or engine.has_recent_sends(days=14)


def _poll_replies_loop():
    # respostas de campanha + agendamentos do Calendly a cada ~3 min
    while True:
        if not _replies_tem_trabalho():
            _dormir_ate_ativar()
            continue
        time.sleep(180)
        try:
            engine.poll_replies()
        except Exception:
            pass
        try:
            engine.scan_calendly()
        except Exception:
            pass
        try:
            engine.scan_bounces()   # auto-supressão de e-mails inexistentes (hard bounce)
        except Exception:
            pass
        try:
            notificar_leads_novos()  # lead novo do site → WhatsApp + email comercial
        except Exception:
            pass


def _campaign_dispatch_loop():
    # O ciclo completo continua a cada 30 min; este gatilho leve garante que
    # um horário de campanha vencido seja disparado em poucos segundos.
    while True:
        if not engine.has_active_campaigns():
            _dormir_ate_ativar()
            continue
        time.sleep(10)
        try:
            engine.process_due_campaign_sends()
        except Exception as e:
            engine.log(f"gatilho instantâneo de campanha falhou: {e}")


def ensure_runtime_loops():
    """Liga as 4 threads de background uma única vez (idempotente e thread-safe).

    Não existe "stop": ao pausar/apagar a última campanha ativa, cada loop
    percebe na iteração seguinte e adormece no _wake até a próxima ativação."""
    global _loops_started
    with _loops_lock:
        if _loops_started:
            return
        _loops_started = True
    for alvo in (_ciclo_periodico, _poll_tracking_loop, _poll_replies_loop, _campaign_dispatch_loop):
        threading.Thread(target=alvo, daemon=True).start()
    engine.log("loops de runtime iniciados")


def _startup_catchup():
    """Trabalho pesado DEPOIS que a porta já está ouvindo.

    No remoto, strategy.run + Calendly/Gmail + backfills de 60k contacts
    atrasavam o primeiro response em dezenas de segundos após o restore R2.
    """
    try:
        strategy.run("startup", perform_sync=True)
        db.backfill_respondeu_tags()
        engine.backfill_threads()
        try:
            engine.scan_calendly()
        except Exception as e:
            engine.log(f"scan_calendly no startup falhou: {e}")
        if engine.has_active_campaigns() or engine.has_recent_sends(days=14):
            ensure_runtime_loops()
        engine.log("startup catch-up concluído")
    except Exception as e:
        engine.log(f"startup catch-up falhou: {e}")


@app.on_event("startup")
def startup():
    # Só o mínimo para a API responder — catch-up pesado vai em background.
    db.init()
    interrompidos = db.quarantine_interrupted_queue()
    if interrompidos:
        engine.log(f"startup: {interrompidos} envio(s) interrompido(s) isolado(s); reenvio automático bloqueado")
    seed.seed_senders()
    seed.seed_campanha_reativacao()
    seed.seed_tags()
    db.migrar_tags_agendamento()  # 'agendou' antigo (intenção) → 'pediu_agendamento'
    threading.Thread(target=_startup_catchup, daemon=True).start()


# ---------------- contatos / CRM ----------------

TAGS_INATIVOS = ("cliente_inativo_4unik", "origem-base-inativa")


def _segmento_contato(d):
    """Temperatura efetiva: coluna preenchida ou derivação por data (db.segmento_por_data)."""
    return (d.get("segmento") or "").strip().lower() or db.segmento_por_data(d.get("primeiro_envio") or "")


@app.get("/api/contacts")
def contacts(funil: str = "", respondeu: str = "", campanha: int = 0, q: str = "", tag: str = "",
             perfil: str = "", com_telefone: bool = False, score_min: int = 0,
             segmento: str = "", erp: str = "", tem_erp: bool = False,
             page: int = 1, page_size: int = 50, ordenar: str = "prioridade"):
    c = db.conn()
    if campanha:
        rows = c.execute("""SELECT ct.*, cc.status AS status_campanha, cc.etapa_atual, cc.next_send_at
                            FROM contacts ct JOIN campaign_contacts cc ON cc.email=ct.email
                            WHERE cc.campaign_id=? ORDER BY ct.ultimo_envio DESC""", (campanha,)).fetchall()
    else:
        rows = c.execute("SELECT * FROM contacts ORDER BY ultimo_envio DESC").fetchall()
    camps = {r["email"]: r["nome"] for r in c.execute(
        """SELECT cc.email, cp.nome FROM campaign_contacts cc
           JOIN campaigns cp ON cp.id=cc.campaign_id""").fetchall()}
    c.close()
    out = []
    for r in rows:
        d = db.contato_dict(r)
        d["campanha"] = camps.get(d["email"], "")
        if funil and d["funil"] != funil:
            continue
        if respondeu and (d["respondeu"] or "").lower() != respondeu.lower():
            continue
        if tag and tag not in d["tags"]:
            continue
        if segmento and _segmento_contato(d) != segmento.strip().lower():
            continue
        if erp and erp.strip().lower() not in (d.get("erp") or "").lower():
            continue
        if tem_erp and not (d.get("erp") or "").strip():
            continue
        if (com_telefone or perfil == "com_telefone") and not (d.get("telefone") or "").strip():
            continue
        if perfil and perfil != "com_telefone" and perfil not in d.get("perfis_lead", []):
            continue
        if d.get("lead_score", 0) < max(0, min(score_min, 100)):
            continue
        if q and q.lower() not in json.dumps(d, ensure_ascii=False, default=str).lower():
            continue
        out.append(d)
    if ordenar == "prioridade":
        out.sort(key=lambda x: (
            x.get("lead_score", 0), bool(x.get("telefone")),
            x.get("segmento") == "quente", bool(x.get("qualificacao")),
            bool(x.get("enriquecimento"))), reverse=True)
    elif ordenar == "nome":
        out.sort(key=lambda x: (x.get("nome") or x.get("email") or "").lower())
    elif ordenar == "empresa":
        out.sort(key=lambda x: (x.get("empresa") or "").lower())
    elif ordenar == "recentes":
        out.sort(key=lambda x: x.get("criado_em") or "", reverse=True)
    total = len(out)
    page_size = max(20, min(int(page_size or 50), 200))
    page = max(1, int(page or 1))
    inicio = (page - 1) * page_size
    pagina = out[inicio:inicio + page_size]
    resumo = {"telefones": sum(bool(x.get("telefone")) for x in out),
              "historicos": sum(bool(x.get("enriquecimento")) for x in out),
              "respondedores": sum((x.get("respondeu") or "").lower() == "sim" for x in out),
              "quentes": sum(_segmento_contato(x) == "quente" for x in out),
              "frios": sum(_segmento_contato(x) == "frio" for x in out),
              "inativos": sum(any(t in (x.get("tags") or []) for t in TAGS_INATIVOS) for x in out)}
    return {"total": total, "contatos": pagina, "page": page, "page_size": page_size,
            "pages": max(1, (total + page_size - 1) // page_size), "resumo": resumo}


@app.get("/api/contacts/export.csv")
def contacts_export(q: str = "", funil: str = "", respondeu: str = "", tag: str = "",
                    perfil: str = "", com_telefone: bool = False, score_min: int = 0,
                    segmento: str = "", erp: str = "", tem_erp: bool = False,
                    ordenar: str = "prioridade"):
    data = contacts(funil=funil, respondeu=respondeu, q=q, tag=tag, perfil=perfil,
                    com_telefone=com_telefone, score_min=score_min, segmento=segmento,
                    erp=erp, tem_erp=tem_erp, page=1, page_size=200, ordenar=ordenar)
    # A exportação deve conter todos os resultados filtrados, não só a página visível.
    paginas = [data["contatos"]]
    for pagina in range(2, data["pages"] + 1):
        paginas.append(contacts(funil=funil, respondeu=respondeu, q=q, tag=tag, perfil=perfil,
                                com_telefone=com_telefone, score_min=score_min, segmento=segmento,
                                erp=erp, tem_erp=tem_erp, page=pagina,
                                page_size=200, ordenar=ordenar)["contatos"])
    output = io.StringIO()
    campos = ["nome", "empresa", "email", "telefone", "cargo", "porte_funcionarios", "erp",
              "segmento", "funil", "lead_score", "qualificacao", "etapa_funil", "tags",
              "resumo_historico", "enriquecimento"]
    writer = csv.DictWriter(output, fieldnames=campos, extrasaction="ignore")
    writer.writeheader()
    for contato in (x for pg in paginas for x in pg):
        row = dict(contato); row["tags"] = ",".join(row.get("tags") or [])
        writer.writerow(row)
    nome = f"leads-4unik-{datetime.now().strftime('%Y%m%d-%H%M')}.csv"
    return StreamingResponse(iter(["\ufeff" + output.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{nome}"'})


@app.get("/api/lead-segments")
def lead_segments():
    segmentos = db.segmentos_leads()
    return {"segmentos": segmentos,
            "recomendado": next((s["id"] for s in segmentos if s["contatos"] > 0), "dados_ricos")}


@app.get("/api/tags")
def tags_list_endpoint():
    """Todas as tags existentes com contagem (para filtro e sugestões)."""
    c = db.conn()
    counts = {}
    for r in c.execute("SELECT tags FROM contacts WHERE tags != ''").fetchall():
        for t in db.tags_list(r["tags"]):
            counts[t] = counts.get(t, 0) + 1
    c.close()
    tags = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return {"tags": [{"tag": t, "contatos": n} for t, n in tags]}


class TagsBody(BaseModel):
    tags: List[str]


@app.post("/api/contacts/{email}/tags")
def set_tags(email: str, body: TagsBody):
    """Define a lista completa de tags do contato (normalizadas e deduplicadas)."""
    email = email.lower()
    c = db.conn()
    if not c.execute("SELECT 1 FROM contacts WHERE email=?", (email,)).fetchone():
        c.close()
        return JSONResponse({"erro": "contato não encontrado"}, status_code=404)
    norm = db.tags_str(body.tags)
    c.execute("UPDATE contacts SET tags=? WHERE email=?", (norm, email))
    c.commit()
    c.close()
    return {"email": email, "tags": db.tags_list(norm)}


class EditarContato(BaseModel):
    nome: Optional[str] = None
    empresa: Optional[str] = None
    segmento: Optional[str] = None
    telefone: Optional[str] = None
    cargo: Optional[str] = None
    porte_assinantes: Optional[str] = None
    porte_funcionarios: Optional[str] = None
    erp: Optional[str] = None
    etapa_funil: Optional[str] = None
    estado_negocio: Optional[str] = None
    qualificacao: Optional[str] = None
    motivo_perda: Optional[str] = None
    modulos: Optional[str] = None
    mensalidades: Optional[str] = None
    setup: Optional[str] = None
    validade: Optional[str] = None
    enriquecimento: Optional[str] = None


class EnriquecerContatoBody(BaseModel):
    confirmar_pesquisa_publica: bool = False


class VincularCampanhaBody(BaseModel):
    campaign_id: int


class DisparoImediatoBody(BaseModel):
    campaign_id: int
    confirmacao: str = ""                 # QUERO_ENVIAR se a campanha inclui cliente protegido


class CampanhaIndividualBody(BaseModel):
    objetivo: str
    n_emails: int = 3
    sender_id: int = 1
    inicio_em: Optional[str] = None


@app.post("/api/contacts/{email}/edit")
def edit_contact(email: str, body: EditarContato):
    """Correção manual de dados de cadastro (nome/empresa) — ex.: erros vindos da importação.
    Edita SOMENTE campos de exibição; nunca toca em respondeu/tags/status (nada de envio)."""
    email = email.lower()
    c = db.conn()
    if not c.execute("SELECT 1 FROM contacts WHERE email=?", (email,)).fetchone():
        c.close()
        return JSONResponse({"erro": "contato não encontrado"}, status_code=404)
    campos, vals = [], []
    if body.nome is not None:
        campos.append("nome=?"); vals.append(body.nome.strip())
    if body.empresa is not None:
        campos.append("empresa=?"); vals.append(body.empresa.strip())
    if body.segmento is not None:
        segmento = body.segmento.strip().lower()
        if segmento not in {"", "quente", "morno", "frio"}:
            c.close()
            return JSONResponse({"erro": "segmento inválido"}, status_code=422)
        campos.append("segmento=?"); vals.append(segmento)
    for campo in ("telefone", "cargo", "porte_assinantes", "porte_funcionarios", "erp",
                  "etapa_funil", "estado_negocio", "qualificacao", "motivo_perda", "modulos",
                  "mensalidades", "setup", "validade", "enriquecimento"):
        valor = getattr(body, campo)
        if valor is not None:
            campos.append(f"{campo}=?"); vals.append(valor.strip())
    if not campos:
        c.close()
        return JSONResponse({"erro": "nada para atualizar"}, status_code=400)
    c.execute(f"UPDATE contacts SET {', '.join(campos)} WHERE email=?", (*vals, email))
    c.commit()
    row = c.execute("SELECT * FROM contacts WHERE email=?", (email,)).fetchone()
    c.close()
    return db.contato_dict(row)


@app.get("/api/contacts/{email}/validation")
def contact_validation(email: str):
    """Validação explicável de cadastro; não afirma que telefone/e-mail existem sem verificação externa."""
    email = email.lower()
    c = db.conn()
    row = c.execute("SELECT * FROM contacts WHERE email=?", (email,)).fetchone()
    c.close()
    if not row:
        return JSONResponse({"erro": "contato não encontrado"}, status_code=404)
    d = dict(row)
    digits = re.sub(r"\D", "", d.get("telefone") or "")
    checks = [
        {"id": "email", "label": "Formato do e-mail", "ok": bool(sync.EMAIL_RE.match(email)),
         "detail": "formato válido" if sync.EMAIL_RE.match(email) else "formato inválido"},
        {"id": "telefone", "label": "Telefone", "ok": 10 <= len(digits) <= 15,
         "detail": "formato plausível; ainda não confirmado" if 10 <= len(digits) <= 15 else "ausente ou incompleto"},
        {"id": "identidade", "label": "Identidade profissional", "ok": bool(d.get("nome") and d.get("empresa")),
         "detail": "nome e empresa preenchidos" if d.get("nome") and d.get("empresa") else "nome ou empresa ausente"},
        {"id": "perfil", "label": "Perfil comercial", "ok": bool(d.get("cargo") or d.get("qualificacao")),
         "detail": "cargo ou qualificação preenchido" if d.get("cargo") or d.get("qualificacao") else "sem cargo/qualificação"},
    ]
    campos = ("nome", "empresa", "telefone", "cargo", "porte_funcionarios", "erp",
              "etapa_funil", "qualificacao", "enriquecimento")
    preenchidos = sum(bool((d.get(k) or "").strip()) for k in campos)
    return {"score": round(preenchidos / len(campos) * 100), "lead_score": db.lead_score(row),
            "checks": checks, "validado": all(x["ok"] for x in checks),
            "nota": "Validação de consistência. Entregabilidade e titularidade exigem confirmação externa."}


@app.post("/api/contacts/{email}/enrich")
def contact_enrich(email: str, body: EnriquecerContatoBody):
    """Pesquisa assistida por IA, somente leitura: devolve sugestões para revisão manual."""
    if not body.confirmar_pesquisa_publica:
        return JSONResponse({"erro": "confirme a pesquisa em fontes públicas"}, status_code=422)
    email = email.lower()
    c = db.conn()
    row = c.execute("SELECT * FROM contacts WHERE email=?", (email,)).fetchone()
    c.close()
    if not row:
        return JSONResponse({"erro": "contato não encontrado"}, status_code=404)
    try:
        resultado = ai.sugerir_enriquecimento_contato(dict(row))
    except Exception as e:
        return JSONResponse({"erro": f"enriquecimento não concluído: {str(e)[:260]}"}, status_code=502)
    permitidos = set(EditarContato.model_fields)
    sugestoes = {k: str(v or "").strip() for k, v in (resultado.get("sugestoes") or {}).items()
                 if k in permitidos and str(v or "").strip()}
    fontes = [f for f in (resultado.get("fontes") or [])
              if isinstance(f, dict) and str(f.get("url") or "").startswith(("https://", "http://"))]
    return {"sugestoes": sugestoes, "fontes": fontes[:10],
            "resumo": resultado.get("resumo", ""), "confianca": resultado.get("confianca", 0),
            "nao_encontrado": resultado.get("nao_encontrado", []),
            "aplicado": False, "nota": "Revise as sugestões; nenhum dado foi alterado."}


@app.post("/api/contacts/{email}/campaigns")
def contact_add_campaign(email: str, body: VincularCampanhaBody):
    """Inclui contato em campanha editável. Nunca ativa ou dispara a campanha."""
    email = email.lower()
    c = db.conn()
    contato = c.execute("SELECT 1 FROM contacts WHERE email=?", (email,)).fetchone()
    camp = c.execute("SELECT id,nome,status FROM campaigns WHERE id=?", (body.campaign_id,)).fetchone()
    if not contato or not camp:
        c.close()
        return JSONResponse({"erro": "contato ou campanha não encontrado"}, status_code=404)
    if camp["status"] not in {"rascunho", "pausada"}:
        c.close()
        return JSONResponse({"erro": "só é possível incluir contatos em campanha rascunho ou pausada"}, status_code=409)
    exists = c.execute("SELECT 1 FROM campaign_contacts WHERE campaign_id=? AND email=?",
                       (body.campaign_id, email)).fetchone()
    if not exists:
        c.execute("INSERT INTO campaign_contacts(campaign_id,email,status) VALUES(?,?,'pendente')",
                  (body.campaign_id, email))
        c.commit()
    c.close()
    return {"ok": True, "campaign_id": body.campaign_id, "email": email,
            "ja_vinculado": bool(exists), "nota": "Contato incluído; campanha não foi ativada."}


@app.post("/api/contacts/{email}/campaigns/launch-now")
def contact_launch_campaign_now(email: str, body: DisparoImediatoBody):
    """Inclui o lead e envia a primeira etapa imediatamente, mediante ação explícita.

    A campanha é ativada quando necessário e seus demais contatos seguem a cadência
    normal. O envio manual só dispensa a janela comercial; nunca dispensa os outros
    guardrails de entrega, consentimento e proteção de clientes.
    """
    email = email.lower()
    c = db.conn()
    contato = c.execute("SELECT * FROM contacts WHERE email=?", (email,)).fetchone()
    camp = c.execute("SELECT * FROM campaigns WHERE id=?", (body.campaign_id,)).fetchone()
    if not contato or not camp:
        c.close()
        return JSONResponse({"erro": "contato ou campanha não encontrado"}, status_code=404)
    if camp["status"] not in {"rascunho", "pausada", "ativa"}:
        c.close()
        return JSONResponse({"erro": "a campanha está encerrada e não pode ser disparada"}, status_code=409)
    ativando = camp["status"] != "ativa"
    ja_vinculado = c.execute("SELECT 1 FROM campaign_contacts WHERE campaign_id=? AND email=?",
                             (body.campaign_id, email)).fetchone()
    if ja_vinculado:
        c.close()
        return JSONResponse({"erro": "este lead já participa da campanha; abra a campanha para acompanhar a próxima etapa"}, status_code=409)
    steps = c.execute("SELECT template_ref FROM campaign_steps WHERE campaign_id=? ORDER BY ordem",
                      (body.campaign_id,)).fetchall()
    if not steps:
        c.close()
        return JSONResponse({"erro": "a campanha não possui e-mails para enviar"}, status_code=409)
    nao_aprovados = [s["template_ref"] for s in steps
                      if (engine.template_get(s["template_ref"]) or {}).get("status") != "aprovado"]
    if nao_aprovados:
        c.close()
        return JSONResponse({"erro": "todos os templates precisam estar aprovados antes do disparo",
                             "pendentes": nao_aprovados}, status_code=409)
    sender = db.sender_get(camp["sender_id"] or 1) or db.sender_default()
    if not sender or not engine.sender_auth_status(sender)["conectado"]:
        c.close()
        return JSONResponse({"erro": "o remetente da campanha não está conectado"}, status_code=409)

    c.execute("INSERT INTO campaign_contacts(campaign_id,email,status) VALUES(?,?,'pendente')",
              (body.campaign_id, email))
    c.commit()
    c.close()

    # Mesmo fluxo de proteção da ativação normal, agora incluindo este lead.
    prot = db.contatos_cliente_na_campanha(body.campaign_id)
    confirmacao = body.confirmacao.strip()
    if prot["total"] > 0 and not camp["clientes_confirmados"] and confirmacao != "QUERO_ENVIAR":
        c = db.conn()
        c.execute("DELETE FROM campaign_contacts WHERE campaign_id=? AND email=?",
                  (body.campaign_id, email))
        c.commit()
        c.close()
        return JSONResponse({"erro": "esta campanha inclui cliente(s) 4Unik e exige confirmação QUERO_ENVIAR",
                             "requer_confirmacao": True, "token": "QUERO_ENVIAR",
                             "clientes": prot["total"], "ativos": prot["ativos"],
                             "inativos": prot["inativos"], "possiveis": prot["possiveis"]}, status_code=409)

    c = db.conn()
    if prot["total"] > 0:
        c.execute("UPDATE campaigns SET clientes_confirmados=1 WHERE id=?", (body.campaign_id,))
    c.execute("UPDATE campaigns SET status='ativa' WHERE id=?", (body.campaign_id,))
    c.commit()
    c.close()
    ensure_runtime_loops()
    _wake.set()  # acorda os loops adormecidos sem esperar o timeout
    # Ao incluir alguém numa campanha já ativa, não reagendamos a audiência inteira:
    # somente este lead recebe a etapa 1 agora. Campanha recém-ativada mantém o
    # agendamento normal dos demais contatos.
    agendados = engine.agendar_campanha(body.campaign_id) if ativando else 0

    # Substitui qualquer agendamento futuro deste lead por uma fila manual agora.
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c = db.conn()
    c.execute("DELETE FROM queue WHERE tipo='campaign_send' AND status='pending' "
              "AND payload LIKE ? AND payload LIKE ?",
              (f'%\"campaign_id\": {body.campaign_id},%', f'%\"email\": \"{email}\"%'))
    c.execute("UPDATE campaign_contacts SET status='ativo', etapa_atual=0, next_send_at=? "
              "WHERE campaign_id=? AND email=?", (agora, body.campaign_id, email))
    c.commit()
    c.close()
    qid = db.enqueue_once("campaign_send", {"campaign_id": body.campaign_id, "email": email,
                                               "etapa": 1, "immediate": True}, agora)
    enviado, erro = False, ""
    if engine.online() and qid is not None:
        c = db.conn()
        item = c.execute("SELECT * FROM queue WHERE id=?", (qid,)).fetchone()
        c.close()
        if item:
            engine._process_queue_item(item)
            c = db.conn()
            estado = c.execute("SELECT status,error FROM queue WHERE id=?", (qid,)).fetchone()
            c.close()
            nota_fila = (estado["error"] if estado else "") or ""
            # A fila usa status=done também para bloqueios deliberados. Só um
            # retorno "msg <gmail-id>" prova que o Gmail aceitou o envio.
            enviado = bool(estado and estado["status"] == "done"
                           and nota_fila.startswith("msg "))
            erro = "" if enviado else (nota_fila or "disparo não concluído")
    engine.log(f"DISPARO MANUAL campanha {body.campaign_id} → {email} ({'enviado' if enviado else 'na fila'})")
    return {"ok": True, "campaign_id": body.campaign_id, "email": email, "agendados": agendados,
            "enviado": enviado, "aguardando_conexao": not engine.online(), "erro": erro,
            "nota": ("Primeiro e-mail enviado agora." if enviado else
                     "O e-mail não foi enviado; revise o bloqueio informado antes de tentar novamente.")}


@app.post("/api/contacts/{email}/campaign-ai")
def contact_campaign_ai(email: str, body: CampanhaIndividualBody):
    """Cria campanha individual em rascunho; geração não envia nem aprova templates."""
    objetivo = body.objetivo.strip()
    if not objetivo:
        return JSONResponse({"erro": "objetivo é obrigatório"}, status_code=422)
    sender = db.sender_get(body.sender_id)
    if not sender or not engine.sender_auth_status(sender)["conectado"]:
        return JSONResponse({"erro": "escolha um remetente conectado"}, status_code=409)
    try:
        cid = ai.criar_campanha_contato(email.lower(), objetivo, body.n_emails, body.sender_id,
                                        _normaliza_dt(body.inicio_em))
    except ValueError as e:
        return JSONResponse({"erro": str(e)}, status_code=404)
    return {"id": cid, "status": "gerando", "contatos": 1,
            "nota": "Campanha individual criada em rascunho; nenhum e-mail foi enviado."}


@app.delete("/api/contacts/{email}")
def delete_contact(email: str):
    """Remove o contato e suas referências (vínculos de campanha, histórico de envios/respostas,
    eventos, agendamentos) e limpa qualquer item PENDENTE na fila que o referencie — assim um
    envio enfileirado nunca dispara para um contato apagado."""
    email = email.lower()
    c = db.conn()
    if not c.execute("SELECT 1 FROM contacts WHERE email=?", (email,)).fetchone():
        c.close()
        return JSONResponse({"erro": "contato não encontrado"}, status_code=404)
    track_ids = [r["track_id"] for r in c.execute(
        "SELECT track_id FROM sends WHERE email=? AND track_id!=''", (email,)).fetchall()]
    c.execute("DELETE FROM campaign_contacts WHERE email=?", (email,))
    c.execute("DELETE FROM replies WHERE email=?", (email,))
    c.execute("DELETE FROM bookings WHERE email=?", (email,))
    c.execute("DELETE FROM events WHERE email=?", (email,))
    for tid in track_ids:
        c.execute("DELETE FROM events WHERE track_id=?", (tid,))
    c.execute("DELETE FROM sends WHERE email=?", (email,))
    c.execute("DELETE FROM queue WHERE status='pending' AND payload LIKE ?", (f"%{email}%",))
    c.execute("DELETE FROM contacts WHERE email=?", (email,))
    c.commit()
    c.close()
    return {"email": email, "excluido": True}


@app.get("/api/contacts/{email}/timeline")
def timeline(email: str):
    email = email.lower()
    c = db.conn()
    contato = c.execute("SELECT * FROM contacts WHERE email=?", (email,)).fetchone()
    if not contato:
        c.close()
        return JSONResponse({"erro": "contato não encontrado"}, status_code=404)
    eventos = []
    for s in c.execute("SELECT * FROM sends WHERE email=? ORDER BY data", (email,)).fetchall():
        eventos.append({"tipo": "envio", "data": s["data"], "etapa": s["etapa"],
                        "msgid": s["msgid"], "campaign_id": s["campaign_id"],
                        "gmail": f"https://mail.google.com/mail/u/0/#all/{s['msgid']}"})
    for r in c.execute("SELECT * FROM replies WHERE email=? ORDER BY data", (email,)).fetchall():
        eventos.append({"tipo": "resposta", "data": r["data"], "classe": r["classe"],
                        "confianca": r["confianca"], "resumo": r["resumo"],
                        "gmail": f"https://mail.google.com/mail/u/0/#all/{r['msgid']}"})
    campanhas = [dict(r) for r in c.execute(
        """SELECT cp.id, cp.nome, cp.status AS status_campanha, cc.etapa_atual, cc.status, cc.next_send_at
           FROM campaign_contacts cc JOIN campaigns cp ON cp.id=cc.campaign_id
           WHERE cc.email=?""", (email,)).fetchall()]
    c.close()
    eventos.sort(key=lambda e: e["data"] or "")
    rascunhos = [f.name for f in (db.PROPOSALS / "drafts").glob("*.md")
                 if email.split("@")[0][:10] in f.read_text(errors="replace").lower()]
    d = db.contato_dict(contato)
    d["gmail_thread"] = (f"https://mail.google.com/mail/u/0/#all/{contato['msgid']}"
                         if contato["msgid"] else "")
    return {"contato": d, "eventos": eventos, "campanhas": campanhas, "rascunhos": rascunhos}


@app.post("/api/contacts/upload")
async def upload_contacts(file: UploadFile = File(...)):
    return sync.import_contacts_upload(await file.read(), file.filename or "upload.csv")


class NovoContato(BaseModel):
    email: str
    nome: Optional[str] = ""
    empresa: Optional[str] = ""
    modulos: Optional[str] = ""
    mensalidades: Optional[str] = ""
    setup: Optional[str] = ""
    validade: Optional[str] = ""
    segmento: Optional[str] = ""          # quente | morno | frio | ''
    tags: Optional[str] = ""              # separadas por vírgula
    respondeu: Optional[str] = "não"      # não | sim
    origem: Optional[str] = "manual"
    telefone: Optional[str] = ""
    cargo: Optional[str] = ""
    porte_funcionarios: Optional[str] = ""
    erp: Optional[str] = ""
    etapa_funil: Optional[str] = ""
    estado_negocio: Optional[str] = ""
    qualificacao: Optional[str] = ""
    motivo_perda: Optional[str] = ""
    enriquecimento: Optional[str] = ""


@app.post("/api/contacts")
def contact_create(b: NovoContato):
    """Cria (ou atualiza, dedup por email) um contato individual com os campos do CRM."""
    email = (b.email or "").strip().lower()
    if not sync.EMAIL_RE.match(email):
        return JSONResponse({"erro": "e-mail inválido"}, status_code=400)
    tags = db.tags_str([t for t in (b.tags or "").split(",")])
    seg = (b.segmento or "").strip().lower()
    if seg not in ("quente", "morno", "frio"):
        seg = ""
    resp = "sim" if (b.respondeu or "").strip().lower() == "sim" else "não"
    c = db.conn()
    existe = c.execute("SELECT 1 FROM contacts WHERE email=?", (email,)).fetchone()
    c.execute("""INSERT INTO contacts(email,nome,empresa,modulos,mensalidades,setup,validade,
                   segmento,tags,respondeu,origem,telefone,cargo,porte_funcionarios,erp,etapa_funil,
                   estado_negocio,qualificacao,motivo_perda,enriquecimento)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                 ON CONFLICT(email) DO UPDATE SET
                   nome=excluded.nome, empresa=excluded.empresa, modulos=excluded.modulos,
                   mensalidades=excluded.mensalidades, setup=excluded.setup, validade=excluded.validade,
                   segmento=excluded.segmento, tags=excluded.tags, respondeu=excluded.respondeu,
                   telefone=excluded.telefone, cargo=excluded.cargo,
                   porte_funcionarios=excluded.porte_funcionarios, erp=excluded.erp,
                   etapa_funil=excluded.etapa_funil, estado_negocio=excluded.estado_negocio,
                   qualificacao=excluded.qualificacao, motivo_perda=excluded.motivo_perda,
                   enriquecimento=excluded.enriquecimento""",
              (email, b.nome or "", b.empresa or "", b.modulos or "", b.mensalidades or "",
               b.setup or "", b.validade or "", seg, tags, resp, b.origem or "manual",
               b.telefone or "", b.cargo or "", b.porte_funcionarios or "", b.erp or "",
               b.etapa_funil or "", b.estado_negocio or "", b.qualificacao or "",
               b.motivo_perda or "", b.enriquecimento or ""))
    c.commit()
    c.close()
    return {"email": email, "criado": not existe, "atualizado": bool(existe),
            "tags": db.tags_list(tags)}


# ---------------- qualidade de dados: normalização de nomes ----------------

def _nomes_set(**kw):
    p = {}
    raw = db.meta_get("nomes_analise", "")
    if raw:
        try:
            p = json.loads(raw)
        except Exception:
            p = {}
    p.update(kw)
    db.meta_set("nomes_analise", json.dumps(p, ensure_ascii=False))


def _nomes_progresso():
    raw = db.meta_get("nomes_analise", "")
    if not raw:
        return {"status": "idle"}
    try:
        return json.loads(raw)
    except Exception:
        return {"status": "idle"}


def _nomes_worker(origem=None):
    """Varre a base, filtra suspeitos (heurística) e pede à IA as correções; grava só
    as propostas que MUDAM algo (corrigir/limpar) em name_fixes como 'pendente'."""
    try:
        c = db.conn()
        if origem:
            rows = c.execute("SELECT email,nome,empresa FROM contacts WHERE origem=?", (origem,)).fetchall()
        else:
            rows = c.execute("SELECT email,nome,empresa FROM contacts").fetchall()
        c.close()
        suspeitos = []
        for r in rows:
            ok, motivo = nomes.suspeito(r["nome"], r["email"], r["empresa"])
            if ok:
                suspeitos.append({"email": r["email"], "nome": r["nome"] or "",
                                  "empresa": r["empresa"] or "", "_motivo": motivo})
        _nomes_set(status="analisando", total=len(suspeitos), feitos=0,
                   etapa="consultando a IA…", erro="")
        lote_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        feitos, BATCH = 0, 25
        c = db.conn()
        for i in range(0, len(suspeitos), BATCH):
            chunk = suspeitos[i:i + BATCH]
            dmap = {d["email"]: d for d in nomes.analisar_lote(chunk, batch=BATCH)}
            for r in chunk:
                d = dmap.get(r["email"], {"acao": "manter"})
                if d.get("acao") not in ("corrigir", "limpar"):
                    continue   # só estaciona o que muda algo
                ex = c.execute("SELECT status FROM name_fixes WHERE email=?", (r["email"],)).fetchone()
                if ex and ex["status"] in ("aprovado", "rejeitado", "aplicado"):
                    continue   # não atropela decisão já tomada pelo Genal
                c.execute("""INSERT INTO name_fixes(email,nome_atual,nome_sugerido,acao,confianca,motivo,status,lote)
                             VALUES(?,?,?,?,?,?, 'pendente', ?)
                             ON CONFLICT(email) DO UPDATE SET
                               nome_atual=excluded.nome_atual, nome_sugerido=excluded.nome_sugerido,
                               acao=excluded.acao, confianca=excluded.confianca, motivo=excluded.motivo,
                               status='pendente', lote=excluded.lote""",
                          (r["email"], r["nome"], d.get("nome_sugerido", ""), d["acao"],
                           d.get("confianca", 0), d.get("motivo") or r["_motivo"], lote_id))
            c.commit()
            feitos += len(chunk)
            _nomes_set(feitos=feitos, etapa=f"analisados {feitos}/{len(suspeitos)}")
        propostas = c.execute("SELECT COUNT(*) n FROM name_fixes WHERE status='pendente'").fetchone()["n"]
        c.close()
        _nomes_set(status="pronto", etapa="concluído", feitos=len(suspeitos), propostas=propostas)
    except Exception as ex:
        _nomes_set(status="erro", erro=str(ex)[:300])
        print(f"[nomes] ERRO: {ex}", flush=True)


class AnalisarNomes(BaseModel):
    origem: Optional[str] = None


@app.post("/api/nomes/analisar")
def nomes_analisar(b: Optional[AnalisarNomes] = None):
    if _nomes_progresso().get("status") == "analisando":
        return JSONResponse({"erro": "já existe uma análise em andamento"}, status_code=409)
    _nomes_set(status="analisando", total=0, feitos=0, etapa="iniciando…", erro="", propostas=0)
    threading.Thread(target=_nomes_worker, args=((b.origem if b else None),), daemon=True).start()
    return {"status": "analisando"}


@app.get("/api/nomes/analise")
def nomes_analise():
    return _nomes_progresso()


@app.get("/api/nomes/propostas")
def nomes_propostas(status: str = "pendente", min_conf: float = 0.0, acao: str = ""):
    c = db.conn()
    q = """SELECT nf.email, nf.nome_atual, nf.nome_sugerido, nf.acao, nf.confianca, nf.motivo,
                  nf.status, ct.empresa
           FROM name_fixes nf LEFT JOIN contacts ct ON ct.email=nf.email WHERE 1=1"""
    args = []
    if status:
        q += " AND nf.status=?"; args.append(status)
    if acao:
        q += " AND nf.acao=?"; args.append(acao)
    if min_conf:
        q += " AND nf.confianca>=?"; args.append(min_conf)
    q += " ORDER BY nf.acao, nf.confianca DESC"
    rows = [dict(r) for r in c.execute(q, args).fetchall()]
    counts = {r["status"]: r["n"] for r in c.execute(
        "SELECT status, COUNT(*) n FROM name_fixes GROUP BY status").fetchall()}
    c.close()
    return {"total": len(rows), "propostas": rows, "counts": counts}


class EditarProposta(BaseModel):
    nome_sugerido: Optional[str] = None
    acao: Optional[str] = None
    status: Optional[str] = None


@app.post("/api/nomes/propostas/{email}")
def nomes_editar_proposta(email: str, b: EditarProposta):
    email = email.lower()
    c = db.conn()
    if not c.execute("SELECT 1 FROM name_fixes WHERE email=?", (email,)).fetchone():
        c.close()
        return JSONResponse({"erro": "proposta não encontrada"}, status_code=404)
    campos, vals = [], []
    if b.nome_sugerido is not None:
        campos.append("nome_sugerido=?"); vals.append(b.nome_sugerido.strip())
    if b.acao in ("manter", "corrigir", "limpar"):
        campos.append("acao=?"); vals.append(b.acao)
    if b.status in ("pendente", "aprovado", "rejeitado"):
        campos.append("status=?"); vals.append(b.status)
    if not campos:
        c.close()
        return JSONResponse({"erro": "nada para atualizar"}, status_code=400)
    c.execute(f"UPDATE name_fixes SET {', '.join(campos)} WHERE email=?", (*vals, email))
    c.commit()
    c.close()
    return {"email": email, "ok": True}


class AprovarLote(BaseModel):
    min_conf: float = 0.9


@app.post("/api/nomes/aprovar-lote")
def nomes_aprovar_lote(b: AprovarLote):
    c = db.conn()
    cur = c.execute("UPDATE name_fixes SET status='aprovado' WHERE status='pendente' AND confianca>=?",
                    (b.min_conf,))
    c.commit()
    n = cur.rowcount
    c.close()
    return {"aprovados": n}


@app.post("/api/nomes/aplicar")
def nomes_aplicar():
    """Aplica em contacts.nome todas as propostas 'aprovado'. corrigir→nome_sugerido, limpar→''.
    Só altera o campo nome — nada é enviado."""
    c = db.conn()
    aprovadas = c.execute(
        "SELECT email, nome_sugerido, acao FROM name_fixes WHERE status='aprovado'").fetchall()
    aplicados = 0
    for r in aprovadas:
        novo = "" if r["acao"] == "limpar" else (r["nome_sugerido"] or "").strip()
        c.execute("UPDATE contacts SET nome=? WHERE email=?", (novo, r["email"]))
        c.execute("UPDATE name_fixes SET status='aplicado' WHERE email=?", (r["email"],))
        aplicados += 1
    c.commit()
    c.close()
    return {"aplicados": aplicados}


# ---------------- templates ----------------

@app.get("/api/templates")
def templates():
    out = []
    for e in __import__("email_templates").EMAILS:
        out.append({"ref": f"builtin:{e['id']}", "nome": e["assunto"], "assunto": e["assunto"],
                    "grupo": e["grupo"], "cadencia": e.get("cadencia", ""), "status": "aprovado",
                    "origem": "builtin", "cta": e["cta"]})
    c = db.conn()
    for r in c.execute("SELECT * FROM templates_ai ORDER BY id").fetchall():
        out.append({"ref": f"ai:{r['id']}", "nome": r["nome"], "assunto": r["assunto"],
                    "grupo": "Campanha Reativação (seed)" if r["origem"] == "seed" else "Criados com IA",
                    "cadencia": "", "status": r["status"], "origem": r["origem"], "cta": r["cta"]})
    c.close()
    return {"total": len(out), "templates": out}


@app.get("/api/templates/preview")
def template_preview(ref: str, email: str = ""):
    contato = None
    if email:
        c = db.conn()
        row = c.execute("SELECT * FROM contacts WHERE email=?", (email.lower(),)).fetchone()
        c.close()
        contato = dict(row) if row else None
    assunto, html = engine.render_html(ref, contato)
    if html is None:
        return JSONResponse({"erro": f"template {ref} não encontrado"}, status_code=404)
    return HTMLResponse(html, headers={"X-Assunto": assunto.encode("ascii", "ignore").decode()})


class Briefing(BaseModel):
    briefing: str


@app.post("/api/templates/ai-create")
def template_ai_create(b: Briefing):
    tid = ai.criar_template(b.briefing)
    return {"id": tid, "ref": f"ai:{tid}", "status": "pendente",
            "nota": "Template criado pela IA — revise o preview e aprove antes de usar em envios."}


@app.post("/api/templates/{tid}/approve")
def template_approve(tid: int):
    c = db.conn()
    c.execute("UPDATE templates_ai SET status='aprovado' WHERE id=?", (tid,))
    c.commit()
    c.close()
    return {"id": tid, "status": "aprovado"}


@app.get("/api/templates/{tid}/raw")
def template_raw(tid: int):
    """Campos crus de um template de IA (para o editor manual no preview)."""
    c = db.conn()
    r = c.execute("""SELECT id,nome,assunto,preheader,cta,cta_url,inner_html,texto_plano,status
                     FROM templates_ai WHERE id=?""", (tid,)).fetchone()
    c.close()
    if not r:
        return JSONResponse({"erro": "template não encontrado"}, status_code=404)
    return dict(r)


class EditarTemplate(BaseModel):
    nome: Optional[str] = None
    assunto: Optional[str] = None
    preheader: Optional[str] = None
    cta: Optional[str] = None
    cta_url: Optional[str] = None
    inner_html: Optional[str] = None
    texto_plano: Optional[str] = None


@app.post("/api/templates/{tid}")
def template_edit(tid: int, body: EditarTemplate):
    """Edição manual simples de um template de IA. Atualiza só os campos enviados.
    Guardrail: se o template estava 'aprovado', volta para 'pendente' (reconferência antes de ativar).
    Só mexe em dado do template — nada é enviado."""
    campos, vals = [], []
    for col in ("nome", "assunto", "preheader", "cta", "cta_url", "inner_html", "texto_plano"):
        v = getattr(body, col)
        if v is not None:
            campos.append(f"{col}=?")
            vals.append(v)
    if not campos:
        return JSONResponse({"erro": "nada para atualizar"}, status_code=400)
    c = db.conn()
    row = c.execute("SELECT status FROM templates_ai WHERE id=?", (tid,)).fetchone()
    if not row:
        c.close()
        return JSONResponse({"erro": "template não encontrado"}, status_code=404)
    ativa = c.execute("""SELECT cp.id, cp.nome FROM campaign_steps cs
                         JOIN campaigns cp ON cp.id=cs.campaign_id
                         WHERE cs.template_ref=? AND cp.status='ativa' LIMIT 1""",
                      (f"ai:{tid}",)).fetchone()
    if ativa:
        c.close()
        return JSONResponse({"erro": f"pause a campanha ativa '{ativa['nome']}' antes de editar este e-mail"},
                            status_code=409)
    novo_status = "pendente" if row["status"] == "aprovado" else row["status"]
    campos.append("status=?")
    vals.append(novo_status)
    c.execute(f"UPDATE templates_ai SET {', '.join(campos)} WHERE id=?", (*vals, tid))
    c.commit()
    c.close()
    return {"id": tid, "status": novo_status}


@app.post("/api/campaigns/{cid}/steps/{ordem}/make-editable")
def campaign_step_make_editable(cid: int, ordem: int):
    """Converte um template fixo em cópia editável, exclusiva da campanha.

    Nunca sobrescreve a biblioteca base: a cópia nasce pendente e deve ser
    revisada antes de (re)ativar a campanha.
    """
    c = db.conn()
    camp = c.execute("SELECT status FROM campaigns WHERE id=?", (cid,)).fetchone()
    step = c.execute("SELECT * FROM campaign_steps WHERE campaign_id=? AND ordem=?", (cid, ordem)).fetchone()
    if not camp or not step:
        c.close()
        return JSONResponse({"erro": "campanha ou etapa não encontrada"}, status_code=404)
    if camp["status"] == "ativa":
        c.close()
        return JSONResponse({"erro": "pause a campanha antes de alterar o e-mail"}, status_code=409)
    if step["template_ref"].startswith("ai:"):
        c.close()
        return {"ref": step["template_ref"], "ja_editavel": True}
    t = engine.template_get(step["template_ref"])
    if not t:
        c.close()
        return JSONResponse({"erro": "template não encontrado"}, status_code=404)
    c.execute("""INSERT INTO templates_ai(nome,assunto,preheader,cta,cta_url,inner_html,texto_plano,status,origem)
                 VALUES(?,?,?,?,?,?,?,'pendente','campanha')""",
              (t.get("nome") or f"E-mail {ordem}", t.get("assunto", ""), t.get("preheader", ""),
               t.get("cta", ""), t.get("cta_url", ""), t.get("inner_html", ""), t.get("texto_plano", "")))
    tid = c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    c.execute("UPDATE campaign_steps SET template_ref=? WHERE campaign_id=? AND ordem=?",
              (f"ai:{tid}", cid, ordem))
    c.commit(); c.close()
    return {"ref": f"ai:{tid}", "ja_editavel": False,
            "nota": "Cópia criada para esta campanha. Edite e aprove antes de reativar."}


# ---------------- campanhas ----------------

def _campaign_payload(c, camp):
    steps = []
    for s in c.execute("SELECT * FROM campaign_steps WHERE campaign_id=? ORDER BY ordem",
                       (camp["id"],)).fetchall():
        t = engine.template_get(s["template_ref"]) or {}
        steps.append({"ordem": s["ordem"], "template_ref": s["template_ref"],
                      "offset_dias": s["offset_dias"], "nome": t.get("nome", "?"),
                      "assunto": t.get("assunto", ""), "status_template": t.get("status", "?")})
    stats = {r["status"]: r["n"] for r in c.execute(
        """SELECT status, COUNT(*) AS n FROM campaign_contacts WHERE campaign_id=?
           GROUP BY status""", (camp["id"],)).fetchall()}
    etapas = {str(r["etapa_atual"]): r["n"] for r in c.execute(
        """SELECT etapa_atual, COUNT(*) AS n FROM campaign_contacts WHERE campaign_id=?
           GROUP BY etapa_atual""", (camp["id"],)).fetchall()}
    enviados = c.execute("SELECT COUNT(*) AS n FROM sends WHERE campaign_id=?",
                         (camp["id"],)).fetchone()["n"]
    # métricas de engajamento desta campanha (events traz campaign_id; dedup por track_id/email)
    cid = camp["id"]
    abertos = len(db.effective_open_trackids(c, engine.BOT_CLICK_JANELA_SEG,
                                             campaign_id=cid))
    cliques = c.execute("SELECT COUNT(DISTINCT track_id) AS n FROM events "
                        "WHERE campaign_id=? AND type='click'", (cid,)).fetchone()["n"]
    descadastros = c.execute("SELECT COUNT(DISTINCT email) AS n FROM events "
                             "WHERE campaign_id=? AND type='unsubscribe'", (cid,)).fetchone()["n"]
    # respostas REAIS de leads (a tabela replies não contém bounces — scan_replies ignora
    # mailer-daemon); ligadas à campanha pela thread dos envios.
    respostas = c.execute("""SELECT COUNT(DISTINCT r.msgid) AS n FROM replies r
                             JOIN sends s ON s.thread=r.thread
                             WHERE s.campaign_id=? AND r.thread!=''""", (cid,)).fetchone()["n"]
    agendados = c.execute("""SELECT COUNT(DISTINCT b.email) AS n FROM bookings b
                              JOIN campaign_contacts cc ON cc.email=b.email
                              WHERE cc.campaign_id=? AND b.status='agendado'""", (cid,)).fetchone()["n"]
    pct = lambda x: round(100.0 * x / enviados, 1) if enviados else 0.0
    proximos = [dict(r) for r in c.execute(
        """SELECT email, etapa_atual+1 AS proxima_etapa, next_send_at FROM campaign_contacts
           WHERE campaign_id=? AND status='ativo' AND next_send_at IS NOT NULL
           ORDER BY next_send_at LIMIT 10""", (camp["id"],)).fetchall()]
    d = dict(camp)
    sender = db.sender_get(camp["sender_id"] or 1) or db.sender_default() or {}
    d.update({"steps": steps, "contatos_por_status": stats, "contatos_por_etapa": etapas,
              "total_contatos": sum(stats.values()), "enviados": enviados,
              "metricas": {"enviados": enviados, "abertos": abertos, "taxa_abertura": pct(abertos),
                           "cliques": cliques, "taxa_clique": pct(cliques),
                           "respostas": respostas, "taxa_resposta": pct(respostas),
                           "agendados": agendados, "taxa_agendamento": pct(agendados),
                           "descadastros": descadastros, "taxa_descadastro": pct(descadastros),
                           "invalidos": stats.get("email_invalido", 0)},
              "funil_disparo": [
                  {"etapa": "Selecionados", "n": sum(stats.values())},
                  {"etapa": "Enviados", "n": enviados},
                  {"etapa": "Abertos", "n": abertos},
                  {"etapa": "Clicaram", "n": cliques},
                  {"etapa": "Responderam", "n": respostas},
                  {"etapa": "Agendaram", "n": agendados},
              ],
              "proximos_envios": proximos, "build": ai.build_progress(camp["id"]),
              "sender": {"id": sender.get("id"), "email": sender.get("email"),
                         "nome": sender.get("nome"), "proposito": sender.get("proposito")}})
    return d


@app.get("/api/campaigns")
def campaigns(q: str = "", status: str = "", tipo: str = "",
              sender_id: Optional[int] = None, ordenar: str = "recentes"):
    """Lista campanhas com busca/filtro opcional: q (nome/objetivo, case-insensitive),
    status (rascunho|ativa|pausada|encerrada), tipo, sender_id e ordenar
    (recentes|nome|status; default recentes)."""
    where, params = [], []
    if q.strip():
        like = f"%{q.strip().lower()}%"
        where.append("(LOWER(nome) LIKE ? OR LOWER(objetivo) LIKE ?)")
        params += [like, like]
    if status:
        where.append("status=?")
        params.append(status)
    if tipo:
        where.append("tipo=?")
        params.append(tipo)
    if sender_id is not None:
        where.append("sender_id=?")
        params.append(sender_id)
    ordem = {"recentes": "id DESC", "nome": "nome COLLATE NOCASE ASC",
             "status": "status ASC, id DESC"}.get(ordenar, "id DESC")
    sql = "SELECT * FROM campaigns"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY {ordem}"
    c = db.conn()
    out = []
    for r in c.execute(sql, params).fetchall():
        p = _campaign_payload(c, r)
        if p.get("build"):
            # a lista só precisa do resumo do build (log/plano ficam no detalhe)
            p["build"] = {k: p["build"].get(k) for k in
                          ("status", "etapa", "feitos", "total", "percent", "eta_s", "eta_estimada")}
        out.append(p)
    c.close()
    return {"total": len(out), "campanhas": out}


# ---------------- loop de sugestão de campanhas (portfólio IA) ----------------
# Registrado ANTES de /api/campaigns/{cid} para "suggest" não ser interpretado
# como um id de campanha.

class SugestaoBody(BaseModel):
    max_campanhas: int = 4
    auto_gerar: bool = False            # True: gera os rascunhos logo após sugerir
    sender_id: int = 1


@app.post("/api/campaigns/suggest")
def campaigns_suggest(b: SugestaoBody):
    """Dispara o loop estrategista GTM + crítica do copywriter em background.
    Tudo termina em rascunho; nada é ativado ou enviado automaticamente."""
    return ai.iniciar_sugestao(b.max_campanhas, b.auto_gerar, b.sender_id)


@app.get("/api/campaigns/suggest")
def campaigns_suggest_status():
    return ai.suggest_status() or {"status": "vazio",
                                   "nota": "nenhum portfólio sugerido ainda"}


@app.post("/api/campaigns/suggest/generate")
def campaigns_suggest_generate(b: SugestaoBody):
    """Gera os rascunhos do portfólio já sugerido (sequencial, em background)."""
    try:
        return ai.iniciar_geracao_portfolio(b.sender_id)
    except RuntimeError as ex:
        return JSONResponse({"erro": str(ex)}, status_code=400)


@app.get("/api/campaigns/{cid}")
def campaign_get(cid: int):
    c = db.conn()
    camp = c.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
    if not camp:
        c.close()
        return JSONResponse({"erro": "campanha não encontrada"}, status_code=404)
    d = _campaign_payload(c, camp)
    d["contatos"] = [dict(r) for r in c.execute(
        """SELECT ct.email, ct.nome, ct.empresa, ct.segmento, ct.respondeu, ct.telefone,
                  ct.cargo, ct.qualificacao, ct.etapa_funil,
                  cc.etapa_atual, cc.status, cc.next_send_at
           FROM campaign_contacts cc JOIN contacts ct ON ct.email=cc.email
           WHERE cc.campaign_id=? ORDER BY ct.segmento, ct.empresa""", (cid,)).fetchall()]
    c.close()
    return d


class NovaCampanha(BaseModel):
    objetivo: str
    tipo: str = "finita"                 # finita | infinita
    cad_min: Optional[int] = None        # cadência aleatória: sorteio entre min e max dias
    cad_max: Optional[int] = None
    n_emails: int = 3
    segmentos: Optional[List[str]] = None
    apenas_sem_resposta: bool = True
    tags: Optional[List[str]] = None     # restringe a quem tiver alguma destas tags
    sender_id: int = 1                   # remetente que envia a campanha
    inicio_em: Optional[str] = None      # data/hora do 1º envio ('' = ao ativar)
    audience_profile: Optional[str] = ""


class EditarCampanha(BaseModel):
    nome: str
    objetivo: str
    sender_id: int
    tipo: str = "finita"
    cad_min: Optional[int] = None
    cad_max: Optional[int] = None
    inicio_em: Optional[str] = None


@app.patch("/api/campaigns/{cid}")
def campaign_edit(cid: int, body: EditarCampanha):
    """Edita o planejamento de uma campanha sem alterar sua audiência ou sequência.

    Campanhas ativas precisam ser pausadas primeiro: evita mudar cadência/remetente no
    meio de uma fila já agendada. Alterações de segmento são uma ação separada, visível.
    """
    nome, objetivo = body.nome.strip(), body.objetivo.strip()
    if not nome or not objetivo:
        return JSONResponse({"erro": "nome e objetivo são obrigatórios"}, status_code=422)
    if body.tipo not in {"finita", "infinita"}:
        return JSONResponse({"erro": "tipo de campanha inválido"}, status_code=422)
    cad_min, cad_max = body.cad_min, body.cad_max
    if body.tipo == "infinita" and cad_min is None:
        cad_min, cad_max = 7, 14
    if (cad_min is None) != (cad_max is None) or (cad_min and cad_max and cad_min > cad_max):
        return JSONResponse({"erro": "informe uma cadência válida (mínimo menor ou igual ao máximo)"}, status_code=422)
    c = db.conn()
    camp = c.execute("SELECT status FROM campaigns WHERE id=?", (cid,)).fetchone()
    if not camp:
        c.close()
        return JSONResponse({"erro": "campanha não encontrada"}, status_code=404)
    if camp["status"] == "ativa":
        c.close()
        return JSONResponse({"erro": "pause a campanha antes de editar o planejamento"}, status_code=409)
    sender = db.sender_get(body.sender_id)
    if not sender:
        c.close()
        return JSONResponse({"erro": "remetente não encontrado"}, status_code=422)
    c.execute("""UPDATE campaigns
                 SET nome=?, objetivo=?, sender_id=?, tipo=?, cad_min_dias=?, cad_max_dias=?, inicio_em=?
                 WHERE id=?""",
              (nome, objetivo, body.sender_id, body.tipo, cad_min, cad_max,
               _normaliza_dt(body.inicio_em), cid))
    c.commit()
    c.close()
    return {"id": cid, "status": "atualizada",
            "nota": "Planejamento atualizado. A audiência e os e-mails permanecem iguais."}


@app.get("/api/audience")
def audience(segmentos: str = "", tags: str = "", apenas_sem_resposta: bool = True,
             perfil: str = ""):
    """Prévia da audiência (contagem ao vivo na criação de campanha).
    Clientes são auto-excluídos; reporta quantos saíram para transparência."""
    segs = [s for s in segmentos.split(",") if s]
    tg = [t for t in tags.split(",") if t]
    emails = db.resolve_audience(segs, tg, apenas_sem_resposta, perfil=perfil)
    com_cliente = db.resolve_audience(segs, tg, apenas_sem_resposta, excluir_cliente=False, perfil=perfil)
    return {"total": len(emails), "clientes_excluidos": len(com_cliente) - len(emails),
            "perfil": perfil, "resumo": db.resumo_audiencia(emails)}


def _normaliza_dt(s):
    """datetime-local 'YYYY-MM-DDTHH:MM' → 'YYYY-MM-DD HH:MM:SS' (ou '' se vazio/ inválido)."""
    s = (s or "").strip().replace("T", " ")
    if not s:
        return ""
    if len(s) == 16:        # sem segundos
        s += ":00"
    try:
        datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        return s
    except Exception:
        return ""


@app.post("/api/campaigns/ai-create")
def campaign_ai_create(nc: NovaCampanha):
    if nc.tipo == "infinita" and nc.cad_min is None:
        nc.cad_min, nc.cad_max = 7, 14
    cid = ai.criar_campanha(nc.objetivo, nc.tipo, nc.cad_min, nc.cad_max,
                            nc.n_emails, nc.segmentos, nc.apenas_sem_resposta, nc.tags, nc.sender_id,
                            _normaliza_dt(nc.inicio_em), nc.audience_profile or "")
    return {"id": cid, "status": "gerando",
            "nota": "Campanha criada em rascunho. A IA está redigindo os e-mails em segundo plano — "
                    "acompanhe o progresso na tela da campanha."}


class CampanhaTemplate(BaseModel):
    template_ref: str
    nome: Optional[str] = None
    sender_id: int = 1
    tipo: str = "finita"
    cad_min: Optional[int] = None
    cad_max: Optional[int] = None
    segmentos: Optional[List[str]] = None
    tags: Optional[List[str]] = None
    apenas_sem_resposta: bool = True
    inicio_em: Optional[str] = None
    audience_profile: Optional[str] = ""


@app.post("/api/campaigns/from-template")
def campaign_from_template(b: CampanhaTemplate):
    """Cria uma campanha a partir de um template existente (sem IA) — 1 etapa = o template."""
    t = engine.template_get(b.template_ref)
    if not t:
        return JSONResponse({"erro": "template não encontrado"}, status_code=404)
    cad_min, cad_max = b.cad_min, b.cad_max
    if b.tipo == "infinita" and cad_min is None:
        cad_min, cad_max = 7, 14
    nome = (b.nome or "").strip() or f"Campanha — {(t.get('nome') or t.get('assunto') or 'template')[:50]}"
    c = db.conn()
    c.execute("""INSERT INTO campaigns(nome,objetivo,tipo,status,cad_min_dias,cad_max_dias,contexto,sender_id,inicio_em,audience_profile)
                 VALUES(?,?,?,'rascunho',?,?,?,?,?,?)""",
              (nome, f"Campanha a partir do template '{t.get('nome') or t.get('assunto')}'", b.tipo,
               cad_min, cad_max, "Criada a partir de template existente", b.sender_id or 1,
               _normaliza_dt(b.inicio_em), b.audience_profile or ""))
    cid = c.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
    # finita de 1 toque = D+0; infinita usa a cadência (offset NULL)
    offset = 0 if b.tipo == "finita" else None
    c.execute("INSERT INTO campaign_steps(campaign_id,ordem,template_ref,offset_dias) VALUES(?,1,?,?)",
              (cid, b.template_ref, offset))
    segs = b.segmentos if b.segmentos else ["quente", "morno", "frio"]
    n = 0
    for email in db.resolve_audience(segs, b.tags, b.apenas_sem_resposta, perfil=b.audience_profile or ""):
        c.execute("INSERT OR IGNORE INTO campaign_contacts(campaign_id,email) VALUES(?,?)", (cid, email))
        n += 1
    c.commit()
    c.close()
    return {"id": cid, "status": "rascunho", "contatos": n,
            "aprovado": t.get("status") == "aprovado",
            "nota": "Campanha criada a partir do template. Revise e ative quando quiser."}


class AtivarBody(BaseModel):
    confirmacao: str = ""                 # "QUERO_ENVIAR" libera campanha que toca clientes


class TesteCampanhaBody(BaseModel):
    email: str
    etapa: int = 1
    rastrear: bool = False


@app.post("/api/campaigns/{cid}/test-send")
def campaign_test_send(cid: int, body: TesteCampanhaBody):
    """Envia uma amostra isolada sem ativar ou agendar contatos.

    Quando ``rastrear`` é explicitamente solicitado, associa o envio e os
    eventos à campanha para permitir um teste ponta a ponta do funil. A amostra
    continua fora de ``campaign_contacts`` e nunca inicia a cadência.
    """
    email = (body.email or "").strip().lower()
    if not sync.EMAIL_RE.match(email):
        return JSONResponse({"erro": "e-mail de teste inválido"}, status_code=422)
    c = db.conn()
    camp = c.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
    step = c.execute("SELECT * FROM campaign_steps WHERE campaign_id=? AND ordem=?",
                     (cid, max(1, body.etapa))).fetchone()
    c.close()
    if not camp or not step:
        return JSONResponse({"erro": "campanha ou etapa não encontrada"}, status_code=404)
    sender = db.sender_get(camp["sender_id"] or 1) or db.sender_default()
    st = engine.sender_auth_status(sender) if sender else {"conectado": False, "motivo": "sem remetente"}
    if not st["conectado"]:
        return JSONResponse({"erro": f"remetente {sender['email'] if sender else '?'} não conectado "
                             f"({st['motivo']}) — conecte na aba Remetentes"}, status_code=409)
    tpl = engine.template_get(step["template_ref"])
    contato = {"email": email, "nome": "Genau", "empresa": "4Unik", "telefone": "",
               "cargo": "Gestão", "porte_assinantes": "", "erp": "", "tags": ""}
    # Testes não recebem link de descadastro: o destinatário pode ser uma caixa
    # interna e a amostra não faz parte da base/cadência. O tracking, quando
    # pedido, é injetado abaixo com campaign_id para atribuição no funil.
    assunto, html = engine.render_html(step["template_ref"], contato, sender, None)
    plain = engine.render_vars((tpl or {}).get("texto_plano") or "", contato)
    tracking_cid = cid if body.rastrear else None
    msgid, thread_id, track_id = engine.enviar_email(
        contato, f"[TESTE 4UNIK] {assunto}", html, plain,
        campaign_id=tracking_cid, etapa=f"teste-{body.etapa}", sender=sender)
    if not msgid:
        return JSONResponse({"erro": "o Gmail não retornou id para o envio de teste"}, status_code=502)
    if body.rastrear:
        from datetime import datetime
        hoje = datetime.now().strftime("%Y-%m-%d")
        enviado_em = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        c = db.conn()
        c.execute("""INSERT OR IGNORE INTO sends
                     (data,msgid,email,etapa,thread,campaign_id,track_id,enviado_em)
                     VALUES(?,?,?,?,?,?,?,?)""",
                  (hoje, msgid, email, f"camp{cid}-teste-{body.etapa}",
                   thread_id, cid, track_id, enviado_em))
        c.commit()
        c.close()
    engine.log(f"TESTE campanha {cid} etapa {body.etapa} → {email} (msg {msgid})")
    return {"ok": True, "email": email, "msgid": msgid, "assunto": assunto,
            "rastreado": bool(body.rastrear), "campaign_id": cid if body.rastrear else None,
            "track_id": track_id if body.rastrear else ""}


@app.post("/api/campaigns/{cid}/activate")
def campaign_activate(cid: int, body: Optional[AtivarBody] = None):
    c = db.conn()
    camp = c.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
    if not camp:
        c.close()
        return JSONResponse({"erro": "campanha não encontrada"}, status_code=404)
    nao_aprovados = [s["template_ref"] for s in c.execute(
        "SELECT template_ref FROM campaign_steps WHERE campaign_id=?", (cid,)).fetchall()
        if (engine.template_get(s["template_ref"]) or {}).get("status") != "aprovado"]
    if nao_aprovados:
        c.close()
        return JSONResponse({"erro": "todos os templates precisam estar aprovados antes de ativar",
                             "pendentes": nao_aprovados}, status_code=409)
    # à prova de erros: o remetente da campanha precisa estar conectado
    sender = db.sender_get(camp["sender_id"] or 1) or db.sender_default()
    if sender and not engine.sender_auth_status(sender)["conectado"]:
        c.close()
        return JSONResponse({"erro": f"o remetente {sender['email']} não está conectado — "
                             "conecte a conta na aba Remetentes antes de ativar."}, status_code=409)
    # GUARDRAIL: campanha que toca CLIENTE (ativo/inativo/possível) só ativa com QUERO_ENVIAR
    confirmacao = (body.confirmacao if body else "").strip()
    prot = db.contatos_cliente_na_campanha(cid)
    if prot["total"] > 0 and confirmacao != "QUERO_ENVIAR":
        c.close()
        return JSONResponse({
            "erro": (f"Esta campanha inclui {prot['total']} contato(s) que são CLIENTES 4unik "
                     f"({prot['ativos']} ativos, {prot['inativos']} inativos, {prot['possiveis']} possíveis). "
                     "Para enviar mesmo assim, confirme digitando QUERO_ENVIAR."),
            "requer_confirmacao": True, "token": "QUERO_ENVIAR",
            "clientes": prot["total"], "ativos": prot["ativos"], "inativos": prot["inativos"],
            "possiveis": prot["possiveis"], "amostra": prot["amostra"]}, status_code=409)
    if prot["total"] > 0:
        c.execute("UPDATE campaigns SET clientes_confirmados=1 WHERE id=?", (cid,))
        engine.log(f"campanha {cid}: QUERO_ENVIAR confirmado pelo Genal — {prot['total']} cliente(s) "
                   f"liberado(s) ({prot['ativos']} ativos / {prot['inativos']} inativos / {prot['possiveis']} possíveis)")
    c.execute("UPDATE campaigns SET status='ativa' WHERE id=?", (cid,))
    c.commit()
    c.close()
    ensure_runtime_loops()
    _wake.set()  # acorda os loops adormecidos sem esperar o timeout
    agendados = engine.agendar_campanha(cid)
    engine.log(f"campanha {cid} ATIVADA pelo Genal — {agendados} contato(s) agendados")
    # Se o horário já estiver liberado, não espera nem o próximo tick de 10 s.
    # Roda fora da request para a interface responder imediatamente.
    threading.Thread(target=engine.process_due_campaign_sends, args=(cid,), daemon=True).start()
    slug = db.campaign_slug(cid)
    ga4.mp_send_event("email_campaign_activated",
                      {"campaign_id": cid, "campaign_name": camp["nome"], "campaign_slug": slug})
    if not (camp["ads_briefing"] or "").strip():
        def _briefing_bg():
            try:
                c2, s2 = _campanha_e_steps(cid)
                ai.gerar_briefing_ads(c2, s2)
                engine.log(f"campanha {cid}: briefing Google Ads gerado na ativação")
            except Exception as e:
                engine.log(f"campanha {cid}: briefing Ads falhou (gere manualmente): {e}")
        threading.Thread(target=_briefing_bg, daemon=True).start()
    return {"id": cid, "status": "ativa", "agendados": agendados,
            "utm": {"utm_source": "4unik-mail", "utm_medium": "email", "utm_campaign": slug}}


@app.post("/api/campaigns/{cid}/pause")
def campaign_pause(cid: int):
    c = db.conn()
    camp = c.execute("SELECT id FROM campaigns WHERE id=?", (cid,)).fetchone()
    if not camp:
        c.close()
        return JSONResponse({"erro": "campanha não encontrada"}, status_code=404)
    c.execute("UPDATE campaigns SET status='pausada' WHERE id=?", (cid,))
    # Nada a matar aqui: os loops de runtime adormecem sozinhos na próxima
    # iteração quando não restar campanha ativa (ver ensure_runtime_loops).
    # Tarefas ainda não iniciadas não devem sobreviver ao comando de pausa.
    c.execute("UPDATE queue SET status='done', updated_at=?, "
              "error='cancelado: campanha pausada' "
              "WHERE tipo='campaign_send' AND status='pending' AND payload LIKE ?",
              (db.now(), f'%\"campaign_id\": {cid},%'))
    c.commit()
    c.close()
    build = ai.build_progress(cid)
    geracao_cancelada = bool(build and build.get("status") == "gerando")
    if geracao_cancelada:
        build.update({"status": "erro", "erro": "Geração interrompida ao pausar a campanha.",
                      "erro_tipo": "outro", "etapa": "pausada manualmente"})
        db.meta_set(f"build_{cid}", json.dumps(build, ensure_ascii=False))
    return {"id": cid, "status": "pausada", "geracao_cancelada": geracao_cancelada}


@app.post("/api/campaigns/{cid}/resume-build")
def campaign_resume_build(cid: int):
    """Retoma a geração de uma campanha que travou no meio (ex.: cota da IA), continuando de onde
    parou. Só cria templates 'pendente' + steps — a campanha permanece rascunho (não ativa/envia)."""
    c = db.conn()
    camp = c.execute("SELECT id FROM campaigns WHERE id=?", (cid,)).fetchone()
    c.close()
    if not camp:
        return JSONResponse({"erro": "campanha não encontrada"}, status_code=404)
    prog = ai.build_progress(cid)
    if not prog or prog.get("status") != "erro":
        return JSONResponse({"erro": "a campanha não está com geração interrompida"}, status_code=409)
    ai.retomar_campanha(cid)
    return {"id": cid, "status": "gerando",
            "nota": "Retomando a redação dos e-mails de onde parou — acompanhe na tela da campanha."}


class ReescreverBody(BaseModel):
    diretrizes: str                      # novas orientações do dono da campanha
    ordens: Optional[List[int]] = None   # e-mails a reescrever (vazio/None = todos)


@app.post("/api/campaigns/{cid}/rewrite")
def campaign_rewrite(cid: int, b: ReescreverBody):
    """Reescreve os e-mails da campanha com novas diretrizes + análise (métricas reais)
    + briefing de lançamento. Guardrails: só campanhas NÃO ativas; as novas versões
    nascem 'pendente' (exigem aprovação) e as antigas ficam preservadas como histórico."""
    try:
        r = ai.reescrever_campanha(cid, b.diretrizes, b.ordens)
    except ValueError as ex:
        return JSONResponse({"erro": str(ex)}, status_code=409)
    return {**r, "status": "gerando",
            "nota": "Reescrevendo os e-mails com as novas diretrizes — acompanhe na tela da campanha."}


def _campaign_delete_safe(c, camp):
    """Rotina segura de exclusão de uma campanha (sem commit): se estiver ativa marca
    'pausada' primeiro, cancela a fila pendente de envio e só então apaga etapas,
    vínculos, a campanha e os templates de IA nunca enviados. Preserva o histórico
    (sends/events/replies) e os contatos do CRM. Retorna nº de tarefas canceladas."""
    cid = camp["id"]
    if camp["status"] == "ativa":
        c.execute("UPDATE campaigns SET status='pausada' WHERE id=?", (cid,))
    canceladas = c.execute(
        "UPDATE queue SET status='done', updated_at=?, "
        "error='cancelado: campanha excluída' "
        "WHERE tipo='campaign_send' AND status='pending' AND payload LIKE ?",
        (db.now(), f'%\"campaign_id\": {cid},%')).rowcount
    # remove campanha + etapas + vínculos; templates de IA da campanha (não usados em envios) também
    refs = [r["template_ref"] for r in c.execute(
        "SELECT template_ref FROM campaign_steps WHERE campaign_id=?", (cid,)).fetchall()]
    c.execute("DELETE FROM campaign_steps WHERE campaign_id=?", (cid,))
    c.execute("DELETE FROM campaign_contacts WHERE campaign_id=?", (cid,))
    c.execute("DELETE FROM campaigns WHERE id=?", (cid,))
    for ref in refs:
        if ref.startswith("ai:"):
            tid = ref.split(":", 1)[1]
            usado = c.execute("SELECT 1 FROM sends WHERE etapa LIKE ? LIMIT 1",
                              (f"%camp{cid}-%",)).fetchone()
            if not usado:
                c.execute("DELETE FROM templates_ai WHERE id=? AND origem='ia'", (tid,))
    return canceladas


@app.delete("/api/campaigns/{cid}")
def campaign_delete(cid: int):
    c = db.conn()
    camp = c.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
    if not camp:
        c.close()
        return JSONResponse({"erro": "campanha não encontrada"}, status_code=404)
    fila_canceladas = _campaign_delete_safe(c, camp)
    c.commit()
    c.close()
    return {"id": cid, "deletado": True, "fila_canceladas": fila_canceladas}


class BulkDeleteCampanhas(BaseModel):
    ids: Optional[List[int]] = None       # sem ids/status = todas as campanhas
    status: Optional[List[str]] = None    # rascunho | ativa | pausada | encerrada
    dry_run: bool = False


@app.post("/api/campaigns/bulk-delete")
def campaigns_bulk_delete(body: BulkDeleteCampanhas):
    """Exclusão em massa com a mesma rotina segura por campanha (pausa ativas e cancela
    a fila antes de apagar). dry_run=true só retorna as contagens, sem apagar nada."""
    where, params = [], []
    if body.ids:
        where.append(f"id IN ({','.join('?' * len(body.ids))})")
        params += body.ids
    if body.status:
        where.append(f"status IN ({','.join('?' * len(body.status))})")
        params += body.status
    sql = "SELECT * FROM campaigns" + (" WHERE " + " AND ".join(where) if where else "")
    c = db.conn()
    camps = c.execute(sql, params).fetchall()
    if body.dry_run:
        steps = contatos = fila = 0
        for camp in camps:
            cid = camp["id"]
            steps += c.execute("SELECT COUNT(*) AS n FROM campaign_steps "
                               "WHERE campaign_id=?", (cid,)).fetchone()["n"]
            contatos += c.execute("SELECT COUNT(*) AS n FROM campaign_contacts "
                                  "WHERE campaign_id=?", (cid,)).fetchone()["n"]
            fila += c.execute("SELECT COUNT(*) AS n FROM queue WHERE tipo='campaign_send' "
                              "AND status='pending' AND payload LIKE ?",
                              (f'%\"campaign_id\": {cid},%',)).fetchone()["n"]
        c.close()
        return {"dry_run": True, "campanhas": len(camps), "steps": steps,
                "contatos_campanha": contatos, "fila_cancelar": fila,
                "ids": [camp["id"] for camp in camps]}
    excluidas, fila_canceladas = [], 0
    for camp in camps:
        fila_canceladas += _campaign_delete_safe(c, camp)
        excluidas.append(camp["id"])
    c.commit()
    c.close()
    return {"dry_run": False, "campanhas_excluidas": len(excluidas),
            "fila_canceladas": fila_canceladas, "ids": excluidas}


# ---------------- remetentes (multi-conta gws) ----------------

def _sender_stats(c, sid):
    enviados = c.execute("""SELECT COUNT(*) AS n FROM sends s JOIN campaigns cp ON cp.id=s.campaign_id
                            WHERE cp.sender_id=?""", (sid,)).fetchone()["n"]
    abertos = len(db.effective_open_trackids(c, engine.BOT_CLICK_JANELA_SEG,
                                             sender_id=sid))
    cliques = c.execute("""SELECT COUNT(DISTINCT e.track_id) AS n FROM events e
                           JOIN sends s ON s.track_id=e.track_id JOIN campaigns cp ON cp.id=s.campaign_id
                           WHERE e.type='click' AND cp.sender_id=?""", (sid,)).fetchone()["n"]
    respostas = c.execute("""SELECT COUNT(*) AS n FROM replies r WHERE r.thread IN (
                              SELECT s.thread FROM sends s JOIN campaigns cp ON cp.id=s.campaign_id
                              WHERE cp.sender_id=?)""", (sid,)).fetchone()["n"]
    taxa = lambda x: round(x / enviados * 100, 1) if enviados else 0
    return {"enviados": enviados, "abertos": abertos, "taxa_abertura": taxa(abertos),
            "cliques": cliques, "taxa_clique": taxa(cliques),
            "respostas": respostas, "taxa_resposta": taxa(respostas)}


@app.get("/api/senders")
def senders_list():
    c = db.conn()
    rows = [dict(r) for r in c.execute("SELECT * FROM senders ORDER BY id").fetchall()]
    for s in rows:
        st = engine.sender_auth_status(s)
        s["conectado"], s["status_motivo"] = st["conectado"], st["motivo"]
        s["auto_reply_on"] = s["auto_reply"] == "on"
        s["classes_ativas"] = [x for x in (s["auto_reply_classes"] or "").split(",") if x]
        s["stats"] = _sender_stats(c, s["id"])
        s["campanhas"] = c.execute("SELECT COUNT(*) AS n FROM campaigns WHERE sender_id=?",
                                   (s["id"],)).fetchone()["n"]
    c.close()
    return {"senders": rows, "classes_seguras": engine.CLASSES_SEGURAS}


@app.get("/api/senders/{sid}")
def sender_detail(sid: int):
    c = db.conn()
    row = c.execute("SELECT * FROM senders WHERE id=?", (sid,)).fetchone()
    if not row:
        c.close()
        return JSONResponse({"erro": "remetente não encontrado"}, status_code=404)
    s = dict(row)
    s["stats"] = _sender_stats(c, sid)
    s["campanhas"] = [dict(r) for r in c.execute(
        "SELECT id,nome,status,tipo FROM campaigns WHERE sender_id=? ORDER BY id DESC", (sid,)).fetchall()]
    s["respostas"] = [dict(r) for r in c.execute(
        """SELECT r.email,r.classe,r.confianca,r.data,r.resumo,r.acao,r.msgid FROM replies r
           WHERE r.thread IN (SELECT s2.thread FROM sends s2 JOIN campaigns cp ON cp.id=s2.campaign_id
                              WHERE cp.sender_id=?) ORDER BY r.id DESC""", (sid,)).fetchall()]
    c.close()
    s["conectado"] = engine.sender_auth_status(s)["conectado"]
    s["auto_reply_on"] = s["auto_reply"] == "on"
    s["classes_ativas"] = [x for x in (s["auto_reply_classes"] or "").split(",") if x]
    for r in s["respostas"]:
        r["gmail"] = f"https://mail.google.com/mail/u/0/#all/{r['msgid']}"
    return s


@app.get("/api/senders/{sid}/status")
def sender_status(sid: int):
    s = db.sender_get(sid)
    if not s:
        return JSONResponse({"erro": "não encontrado"}, status_code=404)
    return engine.sender_auth_status(s)


@app.post("/api/senders/{sid}/setup")
def sender_setup(sid: int):
    return engine.preparar_perfil(sid)


@app.post("/api/senders/{sid}/connect")
def sender_connect(sid: int):
    """Inicia o login OAuth do perfil gws e devolve a URL de autorização para o
    usuário clicar/copiar. O processo fica aguardando o callback local; ao concluir
    no navegador, use GET /api/senders/{sid}/status para confirmar."""
    r = engine.iniciar_login(sid)
    if not r.get("ok"):
        return JSONResponse({"erro": r.get("erro", "falha ao iniciar login")}, status_code=502)
    return r


class SenderUpdate(BaseModel):
    nome: Optional[str] = None
    proposito: Optional[str] = None
    signature_url: Optional[str] = None
    ativo: Optional[bool] = None


class SenderCreate(BaseModel):
    email: str
    nome: str = ""
    proposito: str = ""


@app.post("/api/senders")
def sender_create(payload: SenderCreate):
    """Cadastra um remetente novo, sempre desconectado e com perfil GWS isolado."""
    email = payload.email.strip().lower()
    if not re.fullmatch(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", email):
        return JSONResponse({"erro": "informe um e-mail válido"}, status_code=422)

    raw_slug = unicodedata.normalize("NFKD", email.split("@", 1)[0])
    raw_slug = "".join(c for c in raw_slug if not unicodedata.combining(c))
    base_slug = re.sub(r"[^a-z0-9]+", "-", raw_slug.lower()).strip("-") or "remetente"

    c = db.conn()
    if c.execute("SELECT 1 FROM senders WHERE email=?", (email,)).fetchone():
        c.close()
        return JSONResponse({"erro": "este e-mail já está cadastrado"}, status_code=409)
    slug, n = base_slug, 2
    while c.execute("SELECT 1 FROM senders WHERE slug=?", (slug,)).fetchone():
        slug = f"{base_slug}-{n}"
        n += 1
    c.execute("""INSERT INTO senders(email,nome,proposito,slug,signature_url,ativo,auto_reply)
                 VALUES(?,?,?,?,?,1,'off')""",
              (email, payload.nome.strip(), payload.proposito.strip(), slug, ""))
    sid = c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    c.commit()
    c.close()
    return db.sender_get(sid)


@app.post("/api/senders/{sid}")
def sender_update(sid: int, u: SenderUpdate):
    c = db.conn()
    for field, val in (("nome", u.nome), ("proposito", u.proposito), ("signature_url", u.signature_url)):
        if val is not None:
            c.execute(f"UPDATE senders SET {field}=? WHERE id=?", (val, sid))
    if u.ativo is not None:
        c.execute("UPDATE senders SET ativo=? WHERE id=?", (1 if u.ativo else 0, sid))
    c.commit()
    c.close()
    return db.sender_get(sid)


class SenderAutoReply(BaseModel):
    auto_reply: Optional[bool] = None
    classes: Optional[List[str]] = None


@app.post("/api/senders/{sid}/autoreply")
def sender_autoreply(sid: int, b: SenderAutoReply):
    return engine.set_sender_autoreply(sid, b.auto_reply, b.classes)


# ---------------- métricas ----------------

def _tracking_stats(c):
    """Abertura/clique deduplicados; proxy Gmail conta e prefetch puro não."""
    rastreaveis = c.execute("SELECT COUNT(*) AS n FROM sends WHERE track_id != ''").fetchone()["n"]
    abertos_ids = db.effective_open_trackids(c, engine.BOT_CLICK_JANELA_SEG)
    abertos = len(abertos_ids)
    maquina_ids = {r["track_id"] for r in c.execute(
        "SELECT DISTINCT track_id FROM events WHERE type='open' AND prefetch=1 AND track_id!=''"
    ).fetchall()} - abertos_ids
    abertos_maquina = len(maquina_ids)
    # cliques só de HUMANO — desconta os de scanner de segurança (ver db.click_bot_trackids)
    bots = set(db.click_bot_trackids(c, engine.BOT_CLICK_JANELA_SEG))
    todos_clicks = {r["track_id"] for r in c.execute(
        "SELECT DISTINCT track_id FROM events WHERE type='click' AND track_id!=''").fetchall()}
    cliques = len(todos_clicks - bots)
    cliques_bot = len(todos_clicks & bots)
    taxa = lambda x: round(x / rastreaveis * 100, 1) if rastreaveis else 0
    return {"rastreaveis": rastreaveis, "abertos": abertos, "abertos_maquina": abertos_maquina,
            "taxa_abertura": taxa(abertos), "cliques": cliques, "cliques_bot": cliques_bot,
            "taxa_clique": taxa(cliques), "tracking_ativo": engine.TRACKING_ON}


def _bounce_stats(c):
    """Devoluções a partir da tabela bounces (dedup por endereço). hard = e-mail inexistente,
    soft = temporário (caixa cheia, quota, política). taxa_bounce = endereços distintos que
    bouncearam (qualquer tipo) sobre o total de e-mails enviados — 'bounce rate' de entrega."""
    enviados = c.execute("SELECT COUNT(*) AS n FROM sends").fetchone()["n"]
    hard = c.execute("SELECT COUNT(DISTINCT email) AS n FROM bounces WHERE tipo='hard'").fetchone()["n"]
    soft = c.execute("SELECT COUNT(DISTINCT email) AS n FROM bounces WHERE tipo='soft'").fetchone()["n"]
    distintos = c.execute("SELECT COUNT(DISTINCT email) AS n FROM bounces").fetchone()["n"]
    return {"hard": hard, "soft": soft, "bounces_distintos": distintos,
            "taxa_bounce": round(distintos / enviados * 100, 1) if enviados else 0}


@app.get("/api/metrics/overview")
def metrics_overview():
    """Payload único da tela inicial (Visão Geral)."""
    c = db.conn()
    # próximo envio de qualquer campanha ativa
    prox = c.execute("""SELECT cc.next_send_at, cc.etapa_atual, ct.empresa, ct.nome, cp.nome AS campanha,
                               (SELECT COUNT(*) FROM campaign_steps WHERE campaign_id=cp.id) AS total_etapas
                        FROM campaign_contacts cc
                        JOIN contacts ct ON ct.email=cc.email
                        JOIN campaigns cp ON cp.id=cc.campaign_id
                        WHERE cp.status='ativa' AND cc.status='ativo' AND cc.next_send_at IS NOT NULL
                        ORDER BY cc.next_send_at ASC LIMIT 1""").fetchone()
    proximo = dict(prox) if prox else None

    campanhas_ativas = c.execute("SELECT COUNT(*) AS n FROM campaigns WHERE status='ativa'").fetchone()["n"]
    campanhas_total = c.execute("SELECT COUNT(*) AS n FROM campaigns").fetchone()["n"]
    contatos_ativos = c.execute("SELECT COUNT(DISTINCT email) AS n FROM campaign_contacts "
                               "WHERE status='ativo'").fetchone()["n"]
    contatos_pendentes = c.execute("SELECT COUNT(DISTINCT email) AS n FROM campaign_contacts "
                                  "WHERE status='pendente'").fetchone()["n"]
    enviados = c.execute("SELECT COUNT(*) AS n FROM sends").fetchone()["n"]
    enviados_30d = c.execute("SELECT COUNT(*) AS n FROM sends WHERE data >= date('now','-30 days')").fetchone()["n"]
    respondidos = c.execute("SELECT COUNT(*) AS n FROM contacts WHERE LOWER(respondeu)='sim'").fetchone()["n"]
    total_contatos = c.execute("SELECT COUNT(*) AS n FROM contacts").fetchone()["n"]
    telefones = c.execute("SELECT COUNT(*) AS n FROM contacts WHERE TRIM(COALESCE(telefone,''))!=''").fetchone()["n"]
    historicos = c.execute("SELECT COUNT(*) AS n FROM contacts WHERE TRIM(COALESCE(enriquecimento,''))!=''").fetchone()["n"]
    dados_ricos = sum(1 for r in c.execute("SELECT * FROM contacts").fetchall() if db.lead_score(r) >= 50)
    descadastros = c.execute("SELECT COUNT(*) AS n FROM contacts "
                             "WHERE instr(','||tags||',', ',descadastrado,')>0").fetchone()["n"]
    invalidos = c.execute("SELECT COUNT(*) AS n FROM contacts "
                          "WHERE instr(','||tags||',', ',email_invalido,')>0").fetchone()["n"]
    soft_suprimidos = c.execute("SELECT COUNT(*) AS n FROM contacts "
                                "WHERE instr(','||tags||',', ',soft_bounce_suprimido,')>0").fetchone()["n"]
    removidos_bounce = c.execute(
        "SELECT COUNT(*) AS n FROM contacts WHERE instr(','||tags||',', ',email_invalido,')>0 "
        "OR instr(','||tags||',', ',soft_bounce_suprimido,')>0").fetchone()["n"]
    trk = _tracking_stats(c)
    bnc = _bounce_stats(c)

    # tendência 14 dias: enviados / abertos / cliques / respostas por dia
    dias = [(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(13, -1, -1)]
    env_d = {r["d"]: r["n"] for r in c.execute(
        "SELECT data AS d, COUNT(*) AS n FROM sends WHERE data >= ? GROUP BY data", (dias[0],)).fetchall()}
    resp_d = {r["d"]: r["n"] for r in c.execute(
        "SELECT data AS d, COUNT(*) AS n FROM replies WHERE data >= ? GROUP BY data", (dias[0],)).fetchall()}
    abertos_ids = db.effective_open_trackids(c, engine.BOT_CLICK_JANELA_SEG)
    op_d = {}
    for r in c.execute(
        """SELECT track_id, substr(MIN(ts),1,10) AS d FROM events
             WHERE type IN ('open','click') AND substr(ts,1,10) >= ? GROUP BY track_id""",
        (dias[0],),
    ).fetchall():
        if r["track_id"] in abertos_ids:
            op_d[r["d"]] = op_d.get(r["d"], 0) + 1
    # cliques por dia: distintos por track_id, excluindo os de scanner (bot)
    _bots = set(db.click_bot_trackids(c, engine.BOT_CLICK_JANELA_SEG))
    cl_d, _vistos = {}, set()
    for r in c.execute("SELECT substr(ts,1,10) AS d, track_id FROM events "
                       "WHERE type='click' AND track_id!='' AND substr(ts,1,10) >= ?",
                       (dias[0],)).fetchall():
        if r["track_id"] in _bots or (r["d"], r["track_id"]) in _vistos:
            continue
        _vistos.add((r["d"], r["track_id"]))
        cl_d[r["d"]] = cl_d.get(r["d"], 0) + 1
    tendencia = [{"dia": d, "enviados": env_d.get(d, 0), "abertos": op_d.get(d, 0),
                  "cliques": cl_d.get(d, 0), "respostas": resp_d.get(d, 0)} for d in dias]
    c.close()

    return {
        "proximo_envio": proximo,
        "campanhas_ativas": campanhas_ativas, "campanhas_total": campanhas_total,
        "contatos_ativos": contatos_ativos, "contatos_pendentes": contatos_pendentes,
        "enviados": enviados, "enviados_30d": enviados_30d,
        "respondidos": respondidos, "total_contatos": total_contatos,
        "telefones": telefones, "historicos": historicos, "dados_ricos": dados_ricos,
        "descadastros": descadastros, "invalidos": invalidos,
        "soft_bounces": bnc["soft"], "hard_bounces": bnc["hard"],
        "bounces_total": bnc["bounces_distintos"], "taxa_bounce": bnc["taxa_bounce"],
        "soft_suprimidos": soft_suprimidos, "removidos_bounce": removidos_bounce,
        "taxa_removidos": round(removidos_bounce / total_contatos * 100, 1) if total_contatos else 0,
        "taxa_resposta": round(respondidos / total_contatos * 100, 1) if total_contatos else 0,
        "abertos": trk["abertos"], "abertos_maquina": trk["abertos_maquina"],
        "taxa_abertura": trk["taxa_abertura"], "cliques": trk["cliques"],
        "cliques_bot": trk["cliques_bot"],
        "taxa_clique": trk["taxa_clique"], "rastreaveis": trk["rastreaveis"],
        "tracking_ativo": trk["tracking_ativo"], "tendencia_14d": tendencia,
    }


@app.get("/api/metrics/summary")
def metrics_summary():
    c = db.conn()
    contatos = [db.contato_dict(r) for r in c.execute("SELECT * FROM contacts").fetchall()]
    enviados = c.execute("SELECT COUNT(*) AS n FROM sends").fetchone()["n"]
    respostas = c.execute("SELECT COUNT(*) AS n FROM replies").fetchone()["n"]
    respondidos = sum(1 for x in contatos if (x["respondeu"] or "").lower() == "sim")
    por_classe = {r["classe"] or "sem classe": r["n"] for r in c.execute(
        "SELECT classe, COUNT(*) AS n FROM replies GROUP BY classe").fetchall()}
    por_campanha = [dict(r) for r in c.execute(
        """SELECT cp.nome, cp.status, COUNT(s.id) AS enviados
           FROM campaigns cp LEFT JOIN sends s ON s.campaign_id=cp.id GROUP BY cp.id""").fetchall()]
    serie = [dict(r) for r in c.execute(
        """SELECT substr(data,1,7) AS mes, COUNT(*) AS envios FROM sends
           GROUP BY mes ORDER BY mes""").fetchall()]
    por_segmento = {r["segmento"] or "—": r["n"] for r in c.execute(
        "SELECT segmento, COUNT(*) AS n FROM contacts GROUP BY segmento").fetchall()}
    trk = _tracking_stats(c)
    c.close()
    return {"total_contatos": len(contatos), "total_enviados": enviados,
            "total_respostas": respostas, "contatos_que_responderam": respondidos,
            "taxa_resposta": round(respondidos / len(contatos) * 100, 1) if contatos else 0,
            "respostas_por_classe": por_classe, "envios_por_campanha": por_campanha,
            "serie_mensal": serie, "contatos_por_segmento": por_segmento,
            "abertos": trk["abertos"], "taxa_abertura": trk["taxa_abertura"],
            "cliques": trk["cliques"], "taxa_clique": trk["taxa_clique"],
            "tracking_ativo": trk["tracking_ativo"]}


@app.get("/api/metrics/funnel")
def metrics_funnel():
    c = db.conn()
    contatos = [db.contato_dict(r) for r in c.execute("SELECT * FROM contacts").fetchall()]
    c.close()
    ordem = ["novo", "contatado", "follow-up", "último-toque", "respondeu",
             "pediu-agendamento", "agendou", "encerrado"]
    counts = {k: 0 for k in ordem}
    for x in contatos:
        counts[x["funil"]] = counts.get(x["funil"], 0) + 1
    return {"funil": [{"estagio": k, "contatos": counts[k]} for k in ordem]}


# Drill-down: lista os contatos/registros por trás de cada número do dash (somente leitura).
# Cliques/aberturas são agrupados por track_id para casar EXATAMENTE com o número do card
# (dedup por track_id). Os demais são por contato/endereço.
_BREAKDOWN = {
    "cliques":      {"titulo": "Quem clicou (humano)", "col": "Quando · campanha"},
    "cliques_bot":  {"titulo": "Cliques de scanner (filtrados)", "col": "Motivo · quando"},
    "aberturas":    {"titulo": "Quem abriu", "col": "Quando · campanha"},
    "respostas":    {"titulo": "Quem respondeu", "col": "Última resposta"},
    "descadastros": {"titulo": "Descadastros", "col": "—"},
    "invalidos":    {"titulo": "E-mails inválidos (hard bounce)", "col": "Código · quando"},
    "soft_bounce":  {"titulo": "Soft bounce (devolução temporária)", "col": "Código · quando"},
    "hard_bounce":  {"titulo": "Hard bounce (endereço inexistente)", "col": "Código · quando"},
    "soft_suprimido": {"titulo": "Removidos por soft reincidente", "col": "Toques · quando"},
    "removidos":    {"titulo": "Removidos da base por bounce", "col": "Motivo · quando"},
}


def _camp_nomes(c):
    return {r["id"]: r["nome"] for r in c.execute("SELECT id, nome FROM campaigns").fetchall()}


@app.get("/api/metrics/breakdown/{kind}")
def metrics_breakdown(kind: str):
    if kind not in _BREAKDOWN:
        return JSONResponse({"erro": "tipo inválido"}, status_code=404)
    c = db.conn()
    nomes_emp = {r["email"]: (r["nome"], r["empresa"])
                 for r in c.execute("SELECT email, nome, empresa FROM contacts").fetchall()}

    def base(email):
        nome, empresa = nomes_emp.get(email, (None, None))
        return {"email": email, "nome": nome or "", "empresa": empresa or "",
                "is_contato": email in nomes_emp}

    itens = []
    if kind in ("cliques", "cliques_bot", "aberturas"):
        tipo = "open" if kind == "aberturas" else "click"
        effective_opens = (db.effective_open_trackids(c, engine.BOT_CLICK_JANELA_SEG)
                           if kind == "aberturas" else set())
        extra = ""
        bots = db.click_bot_trackids(c, engine.BOT_CLICK_JANELA_SEG) if tipo == "click" else {}
        camp = _camp_nomes(c)
        rows = c.execute(
            f"""SELECT track_id, MAX(email) email, MAX(ts) quando, MAX(campaign_id) campaign_id,
                       MAX(etapa) etapa, MAX(url) url, COUNT(*) toques
                FROM events WHERE type=?{extra}
                GROUP BY track_id ORDER BY quando DESC""", (tipo,)).fetchall()
        for r in rows:
            if kind == "aberturas" and r["track_id"] not in effective_opens:
                continue
            eh_bot = r["track_id"] in bots
            if kind == "cliques" and eh_bot:        # humanos: pula scanner
                continue
            if kind == "cliques_bot" and not eh_bot:  # scanner: só os filtrados
                continue
            it = base(r["email"] or "")
            if kind == "cliques_bot":
                det = f"{bots[r['track_id']]} · {(r['quando'] or '')[:16]}"
            else:
                cn = camp.get(r["campaign_id"], f"#{r['campaign_id']}") if r["campaign_id"] else "—"
                det = f"{(r['quando'] or '')[:16]} · {cn}"
                if r["etapa"]:
                    det += f" · {r['etapa']}"
                if r["toques"] and r["toques"] > 1:
                    det += f" ({r['toques']}×)"
            it.update({"detalhe": det, "quando": r["quando"] or "", "url": r["url"] or ""})
            itens.append(it)
    elif kind == "respostas":
        rows = c.execute("SELECT email FROM contacts WHERE LOWER(respondeu)='sim'").fetchall()
        for r in rows:
            it = base(r["email"])
            ult = c.execute("SELECT data, classe, resumo FROM replies WHERE email=? "
                            "ORDER BY data DESC LIMIT 1", (r["email"],)).fetchone()
            it.update({"detalhe": (f"{(ult['data'] or '')[:16]} · {ult['classe'] or ''}".strip(" ·")
                                   if ult else ""), "quando": (ult["data"] if ult else "")})
            itens.append(it)
    elif kind in ("descadastros", "invalidos", "soft_suprimido", "removidos"):
        # contatos removidos/suprimidos por tag. 'removidos' = os que o sistema tirou por bounce
        # (hard inválido OU soft reincidente); os outros são por tag específica.
        if kind == "descadastros":
            cond, args = "instr(','||tags||',', ',descadastrado,')>0", ()
        elif kind == "invalidos":
            cond, args = "instr(','||tags||',', ',email_invalido,')>0", ()
        elif kind == "soft_suprimido":
            cond, args = "instr(','||tags||',', ',soft_bounce_suprimido,')>0", ()
        else:  # removidos
            cond, args = ("instr(','||tags||',', ',email_invalido,')>0 OR "
                          "instr(','||tags||',', ',soft_bounce_suprimido,')>0"), ()
        rows = c.execute(f"SELECT email, tags FROM contacts WHERE {cond} ORDER BY email", args).fetchall()
        for r in rows:
            it = base(r["email"])
            tags = set(db.tags_list(r["tags"]))
            eh_soft = db.TAG_SOFT_SUPRIMIDO in tags
            tipo_b = "soft" if (kind == "soft_suprimido" or (kind == "removidos" and eh_soft
                     and db.TAG_EMAIL_INVALIDO not in tags)) else "hard"
            bo = c.execute("SELECT codigo, MAX(criado_em) quando, COUNT(*) toques FROM bounces "
                           "WHERE email=? AND tipo=?", (r["email"], tipo_b)).fetchone()
            motivo = ("hard bounce" if db.TAG_EMAIL_INVALIDO in tags else
                      "soft reincidente" if eh_soft else "")
            quando = (bo["quando"] if bo and bo["quando"] else "")
            if kind == "removidos":
                det = f"{motivo} · {quando[:16]}".strip(" ·")
            elif kind == "descadastros":
                det = ""
            else:
                det = f"{(bo['codigo'] if bo else '') or '—'} · {quando[:16]}".strip(" ·")
                if bo and bo["toques"] and bo["toques"] > 1:
                    det += f" ({bo['toques']}×)"
            it.update({"detalhe": det, "quando": quando})
            itens.append(it)
    else:  # soft_bounce | hard_bounce
        tipo = "soft" if kind == "soft_bounce" else "hard"
        rows = c.execute(
            """SELECT email, MAX(criado_em) quando, MAX(codigo) codigo, COUNT(*) toques
               FROM bounces WHERE tipo=? GROUP BY email ORDER BY quando DESC""", (tipo,)).fetchall()
        for r in rows:
            it = base(r["email"])
            det = f"{r['codigo'] or '—'} · {(r['quando'] or '')[:16]}"
            if r["toques"] and r["toques"] > 1:
                det += f" ({r['toques']}×)"
            it.update({"detalhe": det, "quando": r["quando"] or ""})
            itens.append(it)
    c.close()
    meta = _BREAKDOWN[kind]
    return {"titulo": meta["titulo"], "total": len(itens), "itens": itens}


# ---------------- fila / sync / contexto ----------------

@app.get("/api/queue/status")
def queue_status():
    c = db.conn()
    por_status = {r["status"]: r["n"] for r in c.execute(
        "SELECT status, COUNT(*) AS n FROM queue GROUP BY status").fetchall()}
    ultimos = [dict(r) for r in c.execute(
        "SELECT id,tipo,status,attempts,run_at,updated_at,error FROM queue "
        "ORDER BY id DESC LIMIT 15").fetchall()]
    c.close()
    return {"online": engine.online(), "fila": por_status, "ultimos_itens": ultimos,
            "last_successful_run": db.meta_get("last_successful_run"),
            "last_sync": db.meta_get("last_sync")}


@app.post("/api/sync")
def api_sync(deep: bool = False):
    res = sync.sync_all()
    if deep:
        c = db.conn()
        n = 0
        for r in c.execute("""SELECT email, thread FROM contacts
                              WHERE thread != '' AND LOWER(respondeu) != 'sim'""").fetchall():
            db.enqueue("check_thread", {"thread": r["thread"], "email": r["email"]})
            n += 1
        c.close()
        res["threads_enfileiradas"] = n
    return res


@app.post("/api/queue/run")
def queue_run():
    return engine.process_cycle()


@app.get("/api/strategy/status")
def strategy_status():
    return strategy.status()


@app.post("/api/strategy/run")
def strategy_run():
    return strategy.run("manual", perform_sync=True)


@app.get("/api/seo-geo/status")
def seo_geo_status():
    return strategy.seo_geo_status()


@app.post("/api/seo-geo/run")
def seo_geo_run():
    return strategy.run("seo-geo-manual", perform_sync=False)["seo_geo"]


# ---------------- mídia Google (somente leitura) ----------------

class GoogleMediaConfig(BaseModel):
    GOOGLE_GA4_ACCOUNT_ID: str = ""
    GOOGLE_GA4_PROPERTY_ID: str = ""
    GOOGLE_GA4_MEASUREMENT_ID: str = ""
    GOOGLE_GA4_SITE_MEASUREMENT_ID: str = ""
    GOOGLE_TAG_ID: str = ""
    GOOGLE_ADS_TAG_ID: str = ""
    GOOGLE_GTM_ACCOUNT_ID: str = ""
    GOOGLE_GTM_CONTAINER_ID: str = ""
    GOOGLE_ADS_CUSTOMER_ID: str = ""
    GOOGLE_ADS_LOGIN_CUSTOMER_ID: str = ""
    GOOGLE_ADS_DEVELOPER_TOKEN: str = ""


@app.get("/api/google-media/status")
def google_media_status():
    return google_media.status()


@app.post("/api/google-media/config")
def google_media_config(cfg: GoogleMediaConfig):
    return google_media.save_config(cfg.model_dump())


@app.post("/api/google-media/sync")
def google_media_sync(days: int = 30):
    return google_media.sync(days)


@app.get("/api/google-media/ga4-resources")
def google_media_ga4_resources():
    try:
        return google_media.ga4_resources()
    except Exception as exc:
        return JSONResponse({"erro": str(exc)}, status_code=409)


@app.get("/api/google-media/gtm-resources")
def google_media_gtm_resources():
    try:
        return google_media.gtm_resources()
    except Exception as exc:
        return JSONResponse({"erro": str(exc)}, status_code=409)


@app.post("/api/google-media/ga4-contact-goals")
def google_media_ga4_contact_goals():
    """Configura os eventos-chave oficiais de aquisição no GA4 sem duplicá-los."""
    try:
        return google_media.ensure_ga4_key_events()
    except Exception as exc:
        return JSONResponse({"erro": str(exc)}, status_code=409)


@app.get("/api/google-media/utm")
def google_media_utm(url: str, campaign: str, source: str = "google", medium: str = "cpc"):
    if not url.startswith(("http://", "https://")):
        return JSONResponse({"erro": "URL deve começar com http:// ou https://"}, status_code=422)
    return {"url": google_media.utm_url(url, campaign, source, medium)}


# ---------------- site & performance (GA4 Data API, cache em ga4_cache) ----------------

@app.get("/api/site/overview")
def site_overview(days: int = 28, site: str = "plataforma"):
    if site not in ("plataforma", "site", "loja", "todos"):
        return JSONResponse({"erro": "site deve ser 'plataforma', 'site' ou 'todos'"}, status_code=422)
    return ga4.site_overview(days=max(1, min(days, 365)), site=site)


@app.get("/api/site/campanhas")
def site_campanhas(days: int = 28):
    """Funil ponta-a-ponta por campanha: enviados → abertos → cliques humanos (local)
    → sessões → key events (GA4, casadas por utm_campaign = campaigns.slug)."""
    ga = ga4.utm_campaign_report(days=max(1, min(days, 365)))
    por_slug = {x["utm_campaign"]: x for x in ga.get("campanhas", [])}
    c = db.conn()
    bots = set(db.click_bot_trackids(c, engine.BOT_CLICK_JANELA_SEG))
    out = []
    for camp in c.execute("SELECT id, nome, slug, status FROM campaigns "
                          "WHERE status!='rascunho' ORDER BY id DESC").fetchall():
        slug = camp["slug"] or db.slugify(camp["nome"]) or f"c{camp['id']}"
        enviados = c.execute("SELECT COUNT(*) AS n FROM sends WHERE campaign_id=?",
                             (camp["id"],)).fetchone()["n"]
        abertos = len(db.effective_open_trackids(c, engine.BOT_CLICK_JANELA_SEG,
                                                 campaign_id=camp["id"]))
        cliques = [r["track_id"] for r in c.execute(
            "SELECT DISTINCT track_id FROM events WHERE campaign_id=? AND type='click'",
            (camp["id"],)).fetchall() if r["track_id"] not in bots]
        g = por_slug.get(slug, {})
        out.append({"id": camp["id"], "nome": camp["nome"], "slug": slug,
                    "status": camp["status"], "enviados": enviados, "abertos": abertos,
                    "cliques": len(cliques), "sessoes": g.get("sessoes", 0),
                    "usuarios": g.get("usuarios", 0), "key_events": g.get("key_events", 0)})
    c.close()
    fora = [x for s, x in por_slug.items()
            if s not in {o["slug"] for o in out}]
    return {"configurado": ga.get("configurado", False), "erro": ga.get("erro"),
            "campanhas": out, "ga4_sem_campanha_local": fora}


@app.get("/api/site/drilldown")
def site_drilldown(kind: str, days: int = 28, site: str = "plataforma"):
    if site not in ("plataforma", "site", "loja", "todos"):
        return JSONResponse({"erro": "site inválido"}, status_code=422)
    try:
        return ga4.site_drilldown(kind, days=max(1, min(days, 365)), site=site)
    except ValueError as exc:
        return JSONResponse({"erro": str(exc)}, status_code=422)


@app.get("/api/site/leads")
def site_leads_identificados(limit: int = 50):
    """Leads com identidade (formulário do site → Sanity leadSubmission), com flag
    de quem já está no CRM local para importar com 1 clique."""
    r = ga4.site_leads(limit=limit)
    c = db.conn()
    emails_crm = {x["email"] for x in c.execute("SELECT email FROM contacts").fetchall()}
    c.close()
    for l in r.get("leads", []):
        l["no_crm"] = l["email"] in emails_crm
    return r


# ---------------- briefing Google Ads (espelho da campanha; nada é criado na conta) ----------------

def _campanha_e_steps(cid):
    c = db.conn()
    camp = c.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
    steps = c.execute("SELECT ordem, template_ref FROM campaign_steps WHERE campaign_id=? "
                      "ORDER BY ordem", (cid,)).fetchall()
    c.close()
    return (dict(camp) if camp else None), [dict(s) for s in steps]


@app.post("/api/campaigns/{cid}/ads-briefing")
def campaign_ads_briefing_create(cid: int):
    camp, steps = _campanha_e_steps(cid)
    if not camp:
        return JSONResponse({"erro": "campanha não encontrada"}, status_code=404)
    try:
        return {"id": cid, "briefing": ai.gerar_briefing_ads(camp, steps)}
    except Exception as e:
        code = 429 if ai._erro_eh_cota(str(e)) else 502
        return JSONResponse({"erro": str(e)}, status_code=code)


@app.get("/api/campaigns/{cid}/briefing")
def campaign_briefing(cid: int):
    """Briefing de lançamento (os porquês) já gerado — recomendação go/no-go."""
    b = ai.briefing_lancamento(cid)
    return {"id": cid, "briefing": b}


@app.post("/api/campaigns/{cid}/briefing")
def campaign_briefing_create(cid: int):
    """(Re)gera o briefing de lançamento com IA. Nada é ativado — só recomendação."""
    try:
        return {"id": cid, "briefing": ai.gerar_briefing_lancamento(cid)}
    except ValueError as e:
        return JSONResponse({"erro": str(e)}, status_code=404)
    except Exception as e:
        code = 429 if ai._erro_eh_cota(str(e)) else 502
        return JSONResponse({"erro": str(e)}, status_code=code)


@app.get("/api/campaigns/{cid}/ads-briefing")
def campaign_ads_briefing(cid: int):
    camp, _ = _campanha_e_steps(cid)
    if not camp:
        return JSONResponse({"erro": "campanha não encontrada"}, status_code=404)
    b = camp.get("ads_briefing") or ""
    return {"id": cid, "briefing": json.loads(b) if b else None,
            "utm": {"utm_source": "4unik-mail", "utm_medium": "email",
                    "utm_campaign": camp.get("slug") or db.campaign_slug(cid)}}


@app.get("/api/campaigns/{cid}/ads-briefing.csv")
def campaign_ads_briefing_csv(cid: int):
    """CSV importável no Google Ads Editor (Conta > Importar): linhas de RSA + keywords."""
    camp, _ = _campanha_e_steps(cid)
    if not camp or not camp.get("ads_briefing"):
        return JSONResponse({"erro": "gere o briefing antes de exportar"}, status_code=404)
    b = json.loads(camp["ads_briefing"])
    heads = [f"Headline {i}" for i in range(1, 16)]
    descs = [f"Description {i}" for i in range(1, 5)]
    cols = (["Campaign", "Ad group", "Ad type"] + heads + descs +
            ["Final URL", "Keyword", "Criterion Type"])
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    nome_camp = b.get("campaign_name", camp.get("slug") or f"c{cid}")
    for g in b.get("ad_groups", []):
        row = {"Campaign": nome_camp, "Ad group": g.get("nome", "Grupo 1"),
               "Ad type": "Responsive search ad", "Final URL": g.get("final_url", "")}
        for i, h in enumerate(g.get("headlines", [])[:15], 1):
            row[f"Headline {i}"] = h
        for i, d in enumerate(g.get("descriptions", [])[:4], 1):
            row[f"Description {i}"] = d
        w.writerow(row)
    grupo1 = (b.get("ad_groups") or [{}])[0].get("nome", "Grupo 1")
    tipo = {"exact": "Exact", "phrase": "Phrase", "broad": "Broad"}
    for k in b.get("keywords", []):
        w.writerow({"Campaign": nome_camp, "Ad group": grupo1, "Keyword": k.get("kw", ""),
                    "Criterion Type": tipo.get((k.get("match") or "phrase").lower(), "Phrase")})
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition":
                                      f'attachment; filename="ads-{nome_camp}.csv"'})


@app.post("/api/tracking/sync")
def tracking_sync():
    """Pull imediato de aberturas/cliques (usado ao abrir a Visão Geral)."""
    return engine.poll_tracking()


@app.post("/api/replies/scan")
def replies_scan(thread: str = ""):
    """Varre as threads de campanha atrás de respostas novas (opcional: só uma thread)."""
    return engine.scan_replies(only_thread=thread)


@app.post("/api/calendly/scan")
def calendly_scan():
    """Detecta agendamentos reais nas notificações do Calendly e atualiza as tags."""
    return engine.scan_calendly()


@app.get("/api/replies")
def replies_list():
    c = db.conn()
    rows = [dict(r) for r in c.execute(
        """SELECT r.email, r.classe, r.confianca, r.data, r.resumo, r.msgid, r.acao,
                  ct.nome, ct.empresa
           FROM replies r LEFT JOIN contacts ct ON ct.email=r.email
           ORDER BY r.data DESC, r.id DESC""").fetchall()]
    c.close()
    for r in rows:
        r["gmail"] = f"https://mail.google.com/mail/u/0/#all/{r['msgid']}"
    por_classe = {}
    for r in rows:
        por_classe[r["classe"]] = por_classe.get(r["classe"], 0) + 1
    cfg = engine.reply_config()
    return {
        "total": len(rows),
        "config": {
            "auto_reply": cfg["auto_reply"],
            "classes_ativas": cfg["classes"],
            "classes_seguras": engine.CLASSES_SEGURAS,
            "conf_min": cfg["conf_min"],
            "max_dia": cfg["max_dia"],
            "max_thread": cfg["max_thread"],
        },
        "stats": {
            "auto_respondidas": sum(1 for r in rows if r["acao"] == "auto-respondida"),
            "escaladas": sum(1 for r in rows if r["acao"] != "auto-respondida"),
            "por_classe": por_classe,
        },
        "respostas": rows,
    }


class ReplyConfig(BaseModel):
    auto_reply: Optional[bool] = None
    classes: Optional[List[str]] = None


@app.post("/api/replies/config")
def replies_config(cfg: ReplyConfig):
    return engine.set_reply_config(auto_reply=cfg.auto_reply, classes=cfg.classes)


@app.get("/api/context")
def context_list():
    return ai.listar_contexto()


class NovoKB(BaseModel):
    nome: str
    conteudo: str


@app.post("/api/context/kb")
def context_kb(kb: NovoKB):
    nome = kb.nome if kb.nome.endswith(".md") else kb.nome + ".md"
    nome = nome.replace("/", "-")
    (ai.KB_DIR / nome).write_text(kb.conteudo, encoding="utf-8")
    return {"ok": True, "arquivo": nome}


@app.post("/api/context/images")
async def context_image(file: UploadFile = File(...)):
    nome = (file.filename or "imagem.png").replace("/", "-")
    (ai.IMG_DIR / nome).write_bytes(await file.read())
    return {"ok": True, "arquivo": nome, "url": f"/context/images/{nome}"}


# ---------------- frontend ----------------

app.mount("/context/images", StaticFiles(directory=ai.IMG_DIR), name="imagens")
app.mount("/static", StaticFiles(directory=db.BASE / "static"), name="static")


@app.get("/", response_class=HTMLResponse)
def index():
    html = (db.BASE / "static" / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html.replace("__API_KEY__", API_KEY))


@app.get("/favicon.ico")
def favicon():
    return FileResponse(db.BASE / "static" / "favicon.svg", media_type="image/svg+xml")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8377, log_level="warning")
