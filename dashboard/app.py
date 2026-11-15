#!/usr/bin/env python3
"""Yoobe.mail — servidor local (FastAPI + SQLite) em http://localhost:8377.

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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import agent_usage
import ai
import campaign_guard_gate
import db
import engine
import export_campanha
import firecrawl
import ga4
import grok
import improvements
import leads
import nomes
import seed
import sync
import strategy
import google_media
import vertex
import gmail_oauth

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
app = FastAPI(title="Yoobe Mail", version="1.0",
              description="Dashboard local de campanhas e relacionamento da Yoobe")


def _campaign_origem(camp) -> str:
    if camp is None:
        return "local"
    try:
        v = camp["origem_ambiente"]
    except (KeyError, IndexError, TypeError):
        v = None
    return (v or "local").strip().lower() or "local"


def _somente_acompanhamento(origem: str) -> bool:
    """No remoto, campanhas criadas no local são só leitura."""
    return db.dashboard_env() == "remoto" and _campaign_origem({"origem_ambiente": origem}) == "local"


def _precisa_decisao_import(origem: str) -> bool:
    """No local, campanhas criadas no remoto pedem importar-como-local antes de mutar."""
    return db.dashboard_env() == "local" and _campaign_origem({"origem_ambiente": origem}) == "remoto"


def _guard_campaign_create():
    if db.dashboard_env() == "remoto":
        return JSONResponse(
            {"erro": "Criação de campanhas só no ambiente local (Mac / localhost:8377).",
             "dashboard_env": "remoto"},
            status_code=403,
        )
    return None


def _campaign_sender(sender_id=None):
    sender = db.sender_default()
    if not sender:
        return JSONResponse({"erro": "Cadastre e conecte genau@yoobe.co antes de criar campanhas."},
                            status_code=409)
    if sender_id is not None and int(sender_id) != int(sender["id"]):
        return JSONResponse({"erro": "Novas campanhas só podem usar o remetente genau@yoobe.co."},
                            status_code=409)
    return sender


def _campaign_sender_guard(camp):
    sender = db.sender_get(camp["sender_id"] or 0) if camp else None
    if not sender or (sender.get("email") or "").strip().lower() != "genau@yoobe.co":
        return JSONResponse({"erro": "Campanhas antigas com remetente legado não podem voltar a enviar. "
                             "Crie uma nova campanha com genau@yoobe.co."}, status_code=409)
    return None


def _guard_campaign_mutation(cid: int):
    """Bloqueia mutações conforme origem × ambiente. None = liberado."""
    c = db.conn()
    camp = c.execute("SELECT id, origem_ambiente FROM campaigns WHERE id=?", (cid,)).fetchone()
    c.close()
    if not camp:
        return JSONResponse({"erro": "campanha não encontrada"}, status_code=404)
    origem = _campaign_origem(camp)
    if _somente_acompanhamento(origem):
        return JSONResponse({
            "erro": ("Campanha criada no ambiente local — só acompanhamento no remoto. "
                     "Altere no Mac (localhost:8377)."),
            "somente_acompanhamento": True,
            "origem_ambiente": origem,
            "dashboard_env": db.dashboard_env(),
        }, status_code=403)
    if _precisa_decisao_import(origem):
        return JSONResponse({
            "erro": ("Campanha criada no remoto. Importe como local "
                     "(POST /api/campaigns/{id}/claim-local) antes de alterar."),
            "precisa_decisao": True,
            "origem_ambiente": origem,
            "dashboard_env": db.dashboard_env(),
        }, status_code=403)
    return None


def _guard_improvement_run_mutation(rid: int):
    c = db.conn()
    row = c.execute("SELECT campaign_id FROM improvement_runs WHERE id=?", (rid,)).fetchone()
    c.close()
    if not row:
        return JSONResponse({"erro": "run não encontrado"}, status_code=404)
    return _guard_campaign_mutation(int(row["campaign_id"]))


def _guard_improvement_task_mutation(tid: int):
    c = db.conn()
    row = c.execute("SELECT campaign_id FROM improvement_tasks WHERE id=?", (tid,)).fetchone()
    c.close()
    if not row:
        return JSONResponse({"erro": "task não encontrada"}, status_code=404)
    return _guard_campaign_mutation(int(row["campaign_id"]))


@app.middleware("http")
async def auth(request: Request, call_next):
    if request.url.path.startswith("/api/") and request.headers.get("X-API-Key") != API_KEY:
        return JSONResponse({"erro": "X-API-Key inválida (veja dashboard/.env)"}, status_code=401)
    return await call_next(request)


@app.get("/api/health")
def api_health():
    """Identidade do ambiente + flags para a UI (operacional ou espelho)."""
    env = db.dashboard_env()
    dono = env in ("local", "operacional")
    return {
        "ok": True,
        "dashboard_env": env,
        "dono_campanhas": dono,
        "espelho": env == "remoto",
        "nota": ("Painel remoto: autoridade de criação, edição e envio de campanhas."
                 if env == "operacional" else
                 "Remoto acompanha campanhas de origem local; o envio fica desligado."
                 if env == "remoto" else
                 "Painel local: autoridade de criação e alteração de campanhas."),
    }


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
    email_dest = engine.ENV.get("LEADS_NOTIFY_EMAIL", "corporate@yoobe.co")
    for l in novos:
        linhas = [f"{l['nome']} — {l['empresa'] or 'sem empresa'}",
                  f"{l['email']} · {l['telefone'] or 'sem telefone'}",
                  f"Origem: {l['origem'] or '—'} · {l['quando']}"]
        if l.get("mensagem"):
            linhas.append(f"Mensagem: {l['mensagem']}")
        corpo = "\n".join(linhas)
        texto = f"🧲 *Novo lead do site Yoobe*\n\n{corpo}\n\nResponda rápido — lead quente."
        if not engine.whatsapp(texto) and engine.ENV.get("ZAPI_URL"):
            db.enqueue("whatsapp", {"texto": texto})
        try:
            html = ("<h3>🧲 Novo lead do site Yoobe</h3><p>" +
                    corpo.replace("\n", "<br>") + "</p>"
                    "<p>Lead salvo no Sanity (Studio → Leads e audiência) e disponível "
                    "na aba Site &amp; Ads do Yoobe Mail para importar ao CRM.</p>")
            engine.enviar_email({"email": email_dest, "nome": "Comercial Yoobe"},
                                f"🧲 Novo lead do site: {l['nome']} ({l['empresa'] or l['origem'] or 'site'})",
                                html, texto)
        except Exception as e:
            engine.log(f"notificação de lead por email falhou: {e}")
        engine.log(f"lead do site notificado: {l['email']} ({l['origem']})")


# ---------------- loops de runtime (ligados sob demanda) ----------------
# Para reduzir consumo, as threads de campanha só nascem quando existe
# campanha ativa / envio recente (ensure_runtime_loops) e adormecem sozinhas
# quando não há mais trabalho. Já o loop de estratégia + SEO/GEO é contínuo:
# mede o site mesmo sem campanha de e-mail ativa.

_loops_lock = threading.Lock()
_loops_started = False
_strategy_loop_started = False
_wake = threading.Event()
_IDLE_WAIT_S = 300
_STRATEGY_INTERVAL_S = 1800  # 30 min — ciclo contínuo SEO/GEO + estratégia


def _dormir_ate_ativar():
    """Espera um _wake.set() (ativação de campanha) ou o timeout de reavaliação."""
    if _wake.wait(_IDLE_WAIT_S):
        _wake.clear()


def _strategy_seo_geo_loop():
    """Loop contínuo de estratégia + SEO/GEO — independente de campanhas ativas."""
    while True:
        time.sleep(_STRATEGY_INTERVAL_S)
        try:
            strategy.run("periodic", perform_sync=False)
            engine.log("ciclo contínuo estratégia/SEO-GEO concluído")
        except Exception as e:
            engine.log(f"ciclo estratégia/SEO-GEO com erro: {e}")


def _ciclo_periodico():
    while True:
        if not engine.has_active_campaigns():
            _dormir_ate_ativar()
            continue
        try:
            engine.process_cycle()
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
        # Bounces ANTES de replies: NDR na thread não pode virar auto-resposta.
        try:
            engine.scan_bounces()
        except Exception:
            pass
        try:
            engine.poll_replies()
        except Exception:
            pass
        try:
            engine.scan_calendly()
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
    """Liga as threads de campanha uma única vez (idempotente e thread-safe).

    Não existe "stop": ao pausar/apagar a última campanha ativa, cada loop
    percebe na iteração seguinte e adormece no _wake até a próxima ativação.

    No remoto (espelho), NÃO dispara envio — só tracking/respostas para acompanhar.
    O LaunchAgent local continua como remetente único."""
    global _loops_started
    with _loops_lock:
        if _loops_started:
            return
        _loops_started = True
    if db.dashboard_env() == "remoto":
        for alvo in (_poll_tracking_loop, _poll_replies_loop):
            threading.Thread(target=alvo, daemon=True).start()
        engine.log("loops de runtime (espelho remoto: tracking/replies, sem envio) iniciados")
        return
    for alvo in (_ciclo_periodico, _poll_tracking_loop, _poll_replies_loop, _campaign_dispatch_loop):
        threading.Thread(target=alvo, daemon=True).start()
    engine.log("loops de runtime iniciados")


def ensure_strategy_loop():
    """Liga o loop contínuo de estratégia + SEO/GEO (sempre ativo, sem campanha)."""
    global _strategy_loop_started
    with _loops_lock:
        if _strategy_loop_started:
            return
        _strategy_loop_started = True
    threading.Thread(target=_strategy_seo_geo_loop, daemon=True).start()
    engine.log("loop contínuo estratégia/SEO-GEO iniciado")


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
        # SEO/GEO e estratégia rodam mesmo com 0 campanhas ativas.
        ensure_strategy_loop()
        if engine.has_active_campaigns() or engine.has_recent_sends(days=14):
            ensure_runtime_loops()
        engine.log("startup catch-up concluído")
    except Exception as e:
        engine.log(f"startup catch-up falhou: {e}")


def _quarantine_stale_bg_jobs():
    """Jobs em meta (threads daemon) morrem com --reload; não deixar lock zumbi."""
    cleared = []
    for key, label in (("nomes_analise", "análise de nomes"),
                       ("leads_validacao", "validação de leads")):
        raw = db.meta_get(key, "")
        if not raw:
            continue
        try:
            p = json.loads(raw)
        except Exception:
            continue
        if (p.get("status") or "") != "analisando":
            continue
        feitos = int(p.get("feitos") or 0)
        total = int(p.get("total") or 0)
        p.update({
            "status": "erro",
            "cancel": False,
            "etapa": "interrompido (restart)",
            "erro": (f"worker morto no restart do servidor — {label} em "
                     f"{feitos}/{total or '?'}. Clique em Retomar ou Analisar de novo."),
        })
        db.meta_set(key, json.dumps(p, ensure_ascii=False))
        cleared.append(f"{label} ({feitos}/{total or '?'})")
    return cleared


@app.on_event("startup")
def startup():
    # Só o mínimo para a API responder — catch-up pesado vai em background.
    db.init()
    interrompidos = db.quarantine_interrupted_queue()
    if interrompidos:
        engine.log(f"startup: {interrompidos} envio(s) interrompido(s) isolado(s); reenvio automático bloqueado")
    stale = _quarantine_stale_bg_jobs()
    if stale:
        engine.log("startup: jobs em background interrompidos: " + "; ".join(stale))
    try:
        # Só zumbis antigos — force_all mata runs legítimos a cada --reload.
        imp_cleared = improvements.quarantine_stale_improvement_runs(
            max_age_sec=90, force_all_workers=False)
        if imp_cleared:
            engine.log(
                "startup: runs de melhorias órfãos cancelados: "
                + ", ".join(str(x) for x in imp_cleared)
            )
    except Exception as ex:
        engine.log(f"startup: quarantine melhorias falhou: {ex}")
    seed.seed_senders()
    seed.seed_campanha_reativacao()
    seed.seed_tags()
    db.migrar_tags_agendamento()  # 'agendou' antigo (intenção) → 'pediu_agendamento'
    threading.Thread(target=_startup_catchup, daemon=True).start()


# ---------------- contatos / CRM ----------------

TAGS_INATIVOS = ("cliente_inativo_4unik", "cliente_inativo_yoobe", "origem-base-inativa")


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
    nome = f"leads-yoobe-{datetime.now().strftime('%Y%m%d-%H%M')}.csv"
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
    sender_id: Optional[int] = None
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
    blocked = _guard_campaign_mutation(body.campaign_id)
    if blocked:
        return blocked
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
    blocked = _guard_campaign_mutation(body.campaign_id)
    if blocked:
        return blocked
    email = email.lower()
    c = db.conn()
    contato = c.execute("SELECT * FROM contacts WHERE email=?", (email,)).fetchone()
    camp = c.execute("SELECT * FROM campaigns WHERE id=?", (body.campaign_id,)).fetchone()
    if not contato or not camp:
        c.close()
        return JSONResponse({"erro": "contato ou campanha não encontrado"}, status_code=404)
    sender_blocked = _campaign_sender_guard(camp)
    if sender_blocked:
        c.close()
        return sender_blocked
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
        return JSONResponse({"erro": "esta campanha inclui cliente(s) Yoobe e exige confirmação QUERO_ENVIAR",
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
    blocked = _guard_campaign_create()
    if blocked:
        return blocked
    objetivo = body.objetivo.strip()
    if not objetivo:
        return JSONResponse({"erro": "objetivo é obrigatório"}, status_code=422)
    sender = _campaign_sender(body.sender_id)
    if isinstance(sender, JSONResponse):
        return sender
    if not sender or not engine.sender_auth_status(sender)["conectado"]:
        return JSONResponse({"erro": "escolha um remetente conectado"}, status_code=409)
    try:
        cid = ai.criar_campanha_contato(email.lower(), objetivo, body.n_emails, sender["id"],
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


def _nomes_aplicar_contato(c, email, decisao, contato):
    """Aplica sugestões da decisão em contacts. Retorna True se algo mudou."""
    mudou = False
    acao = decisao.get("acao")
    if acao == "limpar":
        if (contato.get("nome") or "").strip():
            c.execute("UPDATE contacts SET nome='' WHERE email=?", (email,))
            mudou = True
    else:
        nome_novo = (decisao.get("nome_sugerido") or "").strip()
        nome_atual = (contato.get("nome") or "").strip()
        if nome_novo and nome_novo != nome_atual and acao in ("corrigir", "enriquecer"):
            c.execute("UPDATE contacts SET nome=? WHERE email=?", (nome_novo, email))
            mudou = True
    for col, key in (("empresa", "empresa_sugerida"), ("cargo", "cargo_sugerido"),
                     ("telefone", "telefone_sugerido")):
        sug = (decisao.get(key) or "").strip()
        atual = (contato.get(col) or "").strip()
        if sug and sug != atual:
            c.execute(f"UPDATE contacts SET {col}=? WHERE email=?", (sug, email))
            mudou = True
    return mudou


def _nomes_cancel_requested() -> bool:
    return bool(_nomes_progresso().get("cancel"))


def _nomes_worker(origem=None, tags=None, segmento=None, emails=None, campaign_id=None):
    """Varre a base, coleta evidência (Firecrawl), pede à IA (name_analyzer) e
    grava propostas. Auto-aplica no CRM quando confianca >= AUTO_APPLY_MIN_CONF.

    Retoma sem reprocessar e-mails que já têm name_fixes em
    pendente|aprovado|rejeitado|aplicado."""
    try:
        agent_usage.set_run_context(agent="name_analyzer", task_type="analisar_nomes",
                                    campaign_id=campaign_id)
        rows = leads.resolve_contacts(origem=origem, tags=tags, segmento=segmento,
                                      emails=emails, campaign_id=campaign_id)
        c = db.conn()
        ja = {r["email"] for r in c.execute(
            "SELECT email FROM name_fixes WHERE status IN "
            "('pendente','aprovado','rejeitado','aplicado')").fetchall()}
        c.close()
        candidatos = []
        ja_resolvidos = 0
        for r in rows:
            ok, motivo = nomes.candidato(r)
            if not ok:
                continue
            if r["email"] in ja:
                ja_resolvidos += 1
                continue
            candidatos.append({
                "email": r["email"],
                "nome": r.get("nome") or "",
                "empresa": r.get("empresa") or "",
                "cargo": r.get("cargo") or "",
                "telefone": r.get("telefone") or "",
                "_motivo": motivo,
            })
        total = ja_resolvidos + len(candidatos)
        _nomes_set(status="analisando", total=total, feitos=ja_resolvidos, cancel=False,
                   etapa=(f"retomando — {ja_resolvidos} já resolvidos, "
                          f"{len(candidatos)} restantes…" if ja_resolvidos
                          else "coletando evidência (site/e-mail)…"),
                   erro="", auto_aplicados=0)
        if not candidatos:
            c = db.conn()
            propostas = c.execute(
                "SELECT COUNT(*) n FROM name_fixes WHERE status='pendente'").fetchone()["n"]
            c.close()
            _nomes_set(status="pronto", etapa="concluído (nada pendente)",
                       feitos=total, propostas=propostas)
            return
        lote_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        feitos, auto_n, BATCH = ja_resolvidos, 0, 10
        c = db.conn()
        for i in range(0, len(candidatos), BATCH):
            if _nomes_cancel_requested():
                propostas = c.execute(
                    "SELECT COUNT(*) n FROM name_fixes WHERE status='pendente'").fetchone()["n"]
                c.close()
                _nomes_set(status="erro", cancel=False, feitos=feitos,
                           propostas=propostas, auto_aplicados=auto_n,
                           etapa="cancelado",
                           erro="análise cancelada — clique em Retomar para continuar")
                return
            chunk = candidatos[i:i + BATCH]
            _nomes_set(etapa=f"evidência {feitos + 1}–{min(feitos + BATCH, total)}/{total}…")
            for r in chunk:
                try:
                    evidence = leads._resolve_evidence(r, escalate_search=True)
                    r["evidence_text"] = firecrawl.evidence_text_for_agent(evidence)
                except Exception as ex:
                    r["evidence_text"] = f"(erro ao coletar evidência: {str(ex)[:80]})"
            _nomes_set(etapa=f"consultando Vertex name_analyzer ({feitos}/{total})…")
            dmap = {d["email"]: d for d in nomes.analisar_lote(chunk, batch=BATCH)}
            for r in chunk:
                d = dmap.get(r["email"], {"acao": "manter", "confianca": 0})
                if not nomes.tem_mudanca(d, r):
                    continue
                ex = c.execute("SELECT status FROM name_fixes WHERE email=?", (r["email"],)).fetchone()
                if ex and ex["status"] in ("aprovado", "rejeitado", "aplicado"):
                    continue   # não atropela decisão já tomada pelo Genal
                campos_json = json.dumps({
                    "campos_vazios": d.get("campos_vazios") or [],
                    "fontes": d.get("fontes") or [],
                }, ensure_ascii=False)
                conf = float(d.get("confianca") or 0)
                auto = 0
                status = "pendente"
                # Auto-aplicar só com confiança alta e mudança segura
                if conf >= nomes.AUTO_APPLY_MIN_CONF:
                    if _nomes_aplicar_contato(c, r["email"], d, r):
                        status = "aplicado"
                        auto = 1
                        auto_n += 1
                c.execute("""INSERT INTO name_fixes(
                               email,nome_atual,nome_sugerido,empresa_sugerida,cargo_sugerido,
                               telefone_sugerido,campos_json,acao,confianca,motivo,status,auto,lote)
                             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                             ON CONFLICT(email) DO UPDATE SET
                               nome_atual=excluded.nome_atual,
                               nome_sugerido=excluded.nome_sugerido,
                               empresa_sugerida=excluded.empresa_sugerida,
                               cargo_sugerido=excluded.cargo_sugerido,
                               telefone_sugerido=excluded.telefone_sugerido,
                               campos_json=excluded.campos_json,
                               acao=excluded.acao, confianca=excluded.confianca,
                               motivo=excluded.motivo, status=excluded.status,
                               auto=excluded.auto, lote=excluded.lote""",
                          (r["email"], r["nome"], d.get("nome_sugerido", ""),
                           d.get("empresa_sugerida", ""), d.get("cargo_sugerido", ""),
                           d.get("telefone_sugerido", ""), campos_json,
                           d.get("acao", "manter"), conf,
                           d.get("motivo") or r["_motivo"], status, auto, lote_id))
            c.commit()
            feitos += len(chunk)
            _nomes_set(feitos=feitos, auto_aplicados=auto_n,
                       etapa=f"analisados {feitos}/{total}")
        propostas = c.execute("SELECT COUNT(*) n FROM name_fixes WHERE status='pendente'").fetchone()["n"]
        c.close()
        _nomes_set(status="pronto", etapa="concluído", feitos=total,
                   propostas=propostas, auto_aplicados=auto_n, cancel=False)
    except Exception as ex:
        _nomes_set(status="erro", erro=str(ex)[:300], cancel=False)
        print(f"[nomes] ERRO: {ex}", flush=True)
    finally:
        agent_usage.clear_run_context()


class AnalisarNomes(BaseModel):
    origem: Optional[str] = None
    tags: Optional[str] = None
    segmento: Optional[str] = None
    emails: Optional[List[str]] = None
    campaign_id: Optional[int] = None


@app.post("/api/nomes/analisar")
def nomes_analisar(b: Optional[AnalisarNomes] = None):
    if _nomes_progresso().get("status") == "analisando":
        return JSONResponse({"erro": "já existe uma análise em andamento"}, status_code=409)
    _nomes_set(status="analisando", total=0, feitos=0, etapa="iniciando…",
               erro="", propostas=0, cancel=False)
    body = b or AnalisarNomes()
    threading.Thread(
        target=_nomes_worker,
        kwargs={"origem": body.origem, "tags": body.tags, "segmento": body.segmento,
                "emails": body.emails, "campaign_id": body.campaign_id},
        daemon=True).start()
    return {"status": "analisando"}


@app.post("/api/nomes/cancel")
def nomes_cancel():
    """Destrava lock zumbi ou pede cancelamento ao worker vivo."""
    p = _nomes_progresso()
    if p.get("status") != "analisando":
        return {"status": p.get("status") or "idle", "ok": True,
                "msg": "nenhuma análise em andamento"}
    _nomes_set(
        cancel=True,
        status="erro",
        etapa="cancelado",
        erro=("análise cancelada/destravada — clique em Retomar para continuar "
              f"a partir de {p.get('feitos') or 0}/{p.get('total') or '?'}"),
    )
    return {"status": "erro", "ok": True, "feitos": p.get("feitos") or 0,
            "total": p.get("total") or 0}


@app.get("/api/nomes/analise")
def nomes_analise():
    return _nomes_progresso()


def _name_fixes_counts(c):
    return {r["status"]: r["n"] for r in c.execute(
        "SELECT status, COUNT(*) n FROM name_fixes GROUP BY status").fetchall()}


def _name_fixes_pendente_stats(c):
    """Distribuição de confiança das propostas ainda pendentes (para UI de lote)."""
    rows = c.execute(
        """SELECT ROUND(confianca, 2) AS conf, COUNT(*) AS n
           FROM name_fixes WHERE status='pendente'
           GROUP BY ROUND(confianca, 2)
           ORDER BY conf DESC"""
    ).fetchall()
    por_conf = {float(r["conf"]): int(r["n"]) for r in rows}
    max_conf = max(por_conf) if por_conf else None
    return {"por_conf_pendente": por_conf, "max_conf_pendente": max_conf}


@app.get("/api/nomes/propostas")
def nomes_propostas(status: str = "pendente", min_conf: float = 0.0, acao: str = ""):
    c = db.conn()
    q = """SELECT nf.email, nf.nome_atual, nf.nome_sugerido, nf.empresa_sugerida,
                  nf.cargo_sugerido, nf.telefone_sugerido, nf.campos_json,
                  nf.acao, nf.confianca, nf.motivo, nf.status, nf.auto, ct.empresa
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
    counts = _name_fixes_counts(c)
    stats = _name_fixes_pendente_stats(c)
    c.close()
    return {"total": len(rows), "propostas": rows, "counts": counts, "stats": stats}


class EditarProposta(BaseModel):
    nome_sugerido: Optional[str] = None
    empresa_sugerida: Optional[str] = None
    cargo_sugerido: Optional[str] = None
    telefone_sugerido: Optional[str] = None
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
    if b.empresa_sugerida is not None:
        campos.append("empresa_sugerida=?"); vals.append(b.empresa_sugerida.strip())
    if b.cargo_sugerido is not None:
        campos.append("cargo_sugerido=?"); vals.append(b.cargo_sugerido.strip())
    if b.telefone_sugerido is not None:
        campos.append("telefone_sugerido=?"); vals.append(b.telefone_sugerido.strip())
    if b.acao in ("manter", "corrigir", "limpar", "enriquecer"):
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
    min_conf = float(b.min_conf)
    stats = _name_fixes_pendente_stats(c)
    elegiveis = c.execute(
        "SELECT COUNT(*) FROM name_fixes WHERE status='pendente' AND confianca>=?",
        (min_conf,)).fetchone()[0]
    if elegiveis:
        c.execute(
            "UPDATE name_fixes SET status='aprovado' WHERE status='pendente' AND confianca>=?",
            (min_conf,))
        c.commit()
    counts = _name_fixes_counts(c)
    c.close()
    return {
        "aprovados": int(elegiveis),
        "min_conf": min_conf,
        "max_conf_pendente": stats.get("max_conf_pendente"),
        "counts": counts,
    }


@app.post("/api/nomes/aplicar")
def nomes_aplicar():
    """Aplica propostas 'aprovado' em contacts (nome/empresa/cargo/telefone). Nada é enviado."""
    c = db.conn()
    aprovadas = c.execute(
        """SELECT email, nome_sugerido, empresa_sugerida, cargo_sugerido,
                  telefone_sugerido, acao FROM name_fixes WHERE status='aprovado'""").fetchall()
    aplicados = 0
    for r in aprovadas:
        email = r["email"]
        row = c.execute(
            "SELECT nome,empresa,cargo,telefone FROM contacts WHERE email=?",
            (email,)).fetchone()
        contato = dict(row) if row else {}
        decisao = {
            "acao": r["acao"],
            "nome_sugerido": r["nome_sugerido"] or "",
            "empresa_sugerida": r["empresa_sugerida"] or "",
            "cargo_sugerido": r["cargo_sugerido"] or "",
            "telefone_sugerido": r["telefone_sugerido"] or "",
        }
        # limpar zera nome; corrigir/enriquecer grava sugestões não vazias
        if r["acao"] == "limpar":
            c.execute("UPDATE contacts SET nome='' WHERE email=?", (email,))
        else:
            novo = (r["nome_sugerido"] or "").strip()
            if novo and novo != (contato.get("nome") or "").strip():
                c.execute("UPDATE contacts SET nome=? WHERE email=?", (novo, email))
        for col, key in (("empresa", "empresa_sugerida"), ("cargo", "cargo_sugerido"),
                         ("telefone", "telefone_sugerido")):
            sug = (decisao.get(key) or "").strip()
            atual = (contato.get(col) or "").strip()
            if sug and sug != atual:
                c.execute(f"UPDATE contacts SET {col}=? WHERE email=?", (sug, email))
        c.execute("UPDATE name_fixes SET status='aplicado', auto=0 WHERE email=?", (email,))
        aplicados += 1
    c.commit()
    counts = _name_fixes_counts(c)
    c.close()
    return {"aplicados": aplicados, "counts": counts}


# ---------------- qualidade: validação / evidência de leads ----------------

class ValidarLeadsBody(BaseModel):
    origem: Optional[str] = None
    tags: Optional[str] = None
    segmento: Optional[str] = None
    emails: Optional[List[str]] = None
    campaign_id: Optional[int] = None


class LeadEvidenceBody(BaseModel):
    email: str
    payload: dict
    job_id: Optional[str] = ""


@app.post("/api/leads/validar")
def leads_validar(b: Optional[ValidarLeadsBody] = None):
    """Inicia validação Firecrawl+Vertex em background. HTTP 202 + job_id."""
    body = b or ValidarLeadsBody()
    if body.campaign_id:
        ok, msg = campaign_guard_gate.allow_campaign_llm(body.campaign_id)
        if not ok:
            return JSONResponse({"erro": f"campaign-guard bloqueou: {msg}"}, status_code=429)
    res = leads.start_validacao(
        origem=body.origem, tags=body.tags, segmento=body.segmento,
        emails=body.emails, campaign_id=body.campaign_id)
    if res.get("status") == 409 or res.get("erro"):
        return JSONResponse({"erro": res.get("erro", "conflito")}, status_code=409)
    return JSONResponse(res, status_code=202)


@app.get("/api/leads/validacao")
def leads_validacao_status():
    prog = leads.progresso_validacao()
    c = db.conn()
    rows = c.execute(
        """SELECT validacao_status AS s, COUNT(*) n FROM contacts
           WHERE TRIM(COALESCE(validacao_status,''))!='' GROUP BY validacao_status"""
    ).fetchall()
    c.close()
    contagens = {r["s"]: r["n"] for r in rows}
    return {**prog, "contagens_crm": contagens}


@app.post("/api/leads/evidence")
def leads_evidence(b: LeadEvidenceBody, request: Request):
    """Interno: Worker/lead-quality grava evidência Firecrawl e dispara lead_validator."""
    sync = (request.headers.get("X-Sync-Key") or "").strip()
    expected = (
        vertex.env_get("LEAD_QUALITY_SYNC_KEY", "")
        or vertex.env_get("TRACKING_SYNC_KEY", "")
        or API_KEY
    )
    # Middleware já exige X-API-Key; Sync-Key reforça origem Worker quando configurada.
    if expected and sync and sync != expected and request.headers.get("X-API-Key") != API_KEY:
        return JSONResponse({"erro": "X-Sync-Key inválida"}, status_code=401)
    email = (b.email or "").strip().lower()
    if not email or not isinstance(b.payload, dict):
        return JSONResponse({"erro": "email e payload obrigatórios"}, status_code=400)
    try:
        res = leads.apply_evidence_and_validate(email, b.payload, job_id=b.job_id or "")
    except Exception as e:
        return JSONResponse({"erro": str(e)[:260]}, status_code=502)
    return {"ok": True, "email": email, "status": res.get("status"),
            "motivo": res.get("motivo", "")}


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


def _preview_headers(assunto: str = "") -> dict:
    assunto_hdr = (assunto or "").encode("ascii", "ignore").decode()
    return {
        "X-Assunto": assunto_hdr,
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
    }


@app.get("/api/templates/preview")
def template_preview(ref: str, email: str = "", bh: str = "", _: str = ""):
    """bh/_ são só cache-busters do front (hash do corpo / timestamp)."""
    contato = None
    if email:
        c = db.conn()
        row = c.execute("SELECT * FROM contacts WHERE email=?", (email.lower(),)).fetchone()
        c.close()
        contato = dict(row) if row else None
    assunto, html = engine.render_html(ref, contato)
    if html is None:
        return JSONResponse({"erro": f"template {ref} não encontrado"}, status_code=404)
    return HTMLResponse(html, headers=_preview_headers(assunto))


class PreviewDraft(BaseModel):
    preheader: Optional[str] = ""
    cta: Optional[str] = ""
    cta_url: Optional[str] = ""
    inner_html: str = ""
    email: Optional[str] = ""


@app.post("/api/templates/preview-draft")
def template_preview_draft(body: PreviewDraft):
    """Renderiza um miolo ainda não salvo (prévia ao vivo no editor)."""
    contato = None
    if body.email:
        c = db.conn()
        row = c.execute("SELECT * FROM contacts WHERE email=?", (body.email.lower(),)).fetchone()
        c.close()
        contato = dict(row) if row else None
    html = engine.render_draft_html(
        preheader=body.preheader or "",
        inner_html=body.inner_html or "",
        cta=body.cta or "",
        cta_url=body.cta_url or "",
        contato=contato,
    )
    return HTMLResponse(html, headers=_preview_headers(""))


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
    row = c.execute("SELECT campaign_id FROM templates_ai WHERE id=?", (tid,)).fetchone()
    if row and row["campaign_id"]:
        blocked = _guard_campaign_mutation(int(row["campaign_id"]))
        if blocked:
            c.close()
            return blocked
    c.execute("UPDATE templates_ai SET status='aprovado' WHERE id=?", (tid,))
    c.commit()
    c.close()
    return {"id": tid, "status": "aprovado"}


@app.post("/api/campaigns/{cid}/approve-templates")
def campaign_approve_templates(cid: int):
    """Aprova em lote todos os templates AI ainda pendentes da sequência da campanha."""
    blocked = _guard_campaign_mutation(cid)
    if blocked:
        return blocked
    c = db.conn()
    camp = c.execute("SELECT id FROM campaigns WHERE id=?", (cid,)).fetchone()
    if not camp:
        c.close()
        return JSONResponse({"erro": "campanha não encontrada"}, status_code=404)
    refs = [r["template_ref"] for r in c.execute(
        "SELECT template_ref FROM campaign_steps WHERE campaign_id=? ORDER BY ordem",
        (cid,)).fetchall()]
    aprovados, ja_ok, ignorados = [], [], []
    for ref in refs:
        if not str(ref).startswith("ai:"):
            ignorados.append(ref)
            continue
        try:
            tid = int(str(ref).split(":", 1)[1])
        except ValueError:
            ignorados.append(ref)
            continue
        row = c.execute("SELECT status FROM templates_ai WHERE id=?", (tid,)).fetchone()
        if not row:
            ignorados.append(ref)
            continue
        if row["status"] == "aprovado":
            ja_ok.append(ref)
            continue
        c.execute("UPDATE templates_ai SET status='aprovado' WHERE id=?", (tid,))
        aprovados.append(ref)
    c.commit()
    c.close()
    try:
        checklist = ai.rodar_checklist(cid)
    except Exception:
        checklist = None
    return {"id": cid, "aprovados": aprovados, "ja_aprovados": ja_ok,
            "ignorados": ignorados, "checklist": checklist,
            "nota": (f"{len(aprovados)} template(s) aprovado(s)"
                     + (f", {len(ja_ok)} já estavam ok" if ja_ok else ""))}


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
    row = c.execute("SELECT status, campaign_id FROM templates_ai WHERE id=?", (tid,)).fetchone()
    if not row:
        c.close()
        return JSONResponse({"erro": "template não encontrado"}, status_code=404)
    if row["campaign_id"]:
        blocked = _guard_campaign_mutation(int(row["campaign_id"]))
        if blocked:
            c.close()
            return blocked
    ref = f"ai:{tid}"
    ativa = c.execute("""SELECT cp.id, cp.nome FROM campaign_steps cs
                         JOIN campaigns cp ON cp.id=cs.campaign_id
                         WHERE cs.template_ref=? AND cp.status='ativa' LIMIT 1""",
                      (ref,)).fetchone()
    if ativa:
        c.close()
        return JSONResponse({"erro": f"pause a campanha ativa '{ativa['nome']}' antes de editar este e-mail"},
                            status_code=409)
    # Campanhas que usam este template (para travar se houver rewrite em andamento e
    # para marcar a campanha como atualizada — a UI da sequência lê daqui).
    camp_rows = c.execute("""SELECT DISTINCT cp.id, cp.versao FROM campaign_steps cs
                             JOIN campaigns cp ON cp.id=cs.campaign_id
                             WHERE cs.template_ref=?""", (ref,)).fetchall()
    for camp in camp_rows:
        prog = ai.build_progress(camp["id"]) or {}
        if prog.get("status") == "gerando":
            c.close()
            return JSONResponse({
                "erro": ("a IA ainda está regenerando os e-mails desta campanha — "
                         "aguarde terminar (ou cancele a geração) antes de editar manualmente"),
                "campaign_id": camp["id"],
            }, status_code=409)
    novo_status = "pendente" if row["status"] == "aprovado" else row["status"]
    campos.append("status=?")
    vals.append(novo_status)
    # Alinha versao_campanha com a campanha dona (ou a maior versão entre as que usam o ref).
    versoes = [int(r["versao"] or 1) for r in camp_rows]
    if versoes:
        campos.append("versao_campanha=?")
        vals.append(max(versoes))
    if camp_rows and not row["campaign_id"]:
        campos.append("campaign_id=?")
        vals.append(camp_rows[0]["id"])
    c.execute(f"UPDATE templates_ai SET {', '.join(campos)} WHERE id=?", (*vals, tid))
    agora = db.now()
    for camp in camp_rows:
        c.execute("UPDATE campaigns SET atualizada_em=? WHERE id=?", (agora, camp["id"]))
    c.commit()
    updated = c.execute("""SELECT id,nome,assunto,preheader,cta,cta_url,inner_html,texto_plano,
                                  status,versao_campanha,campaign_id
                           FROM templates_ai WHERE id=?""", (tid,)).fetchone()
    c.close()
    out = dict(updated) if updated else {"id": tid, "status": novo_status}
    out["ref"] = ref
    out["campaign_ids"] = [r["id"] for r in camp_rows]
    out["cta_shell_ignored"] = bool(engine._inner_tem_botao(out.get("inner_html") or ""))
    return out


@app.post("/api/campaigns/{cid}/steps/{ordem}/make-editable")
def campaign_step_make_editable(cid: int, ordem: int):
    """Converte um template fixo em cópia editável, exclusiva da campanha.

    Nunca sobrescreve a biblioteca base: a cópia nasce pendente e deve ser
    revisada antes de (re)ativar a campanha.
    """
    blocked = _guard_campaign_mutation(cid)
    if blocked:
        return blocked
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

def _compat_status(camp_versao, t):
    """compatível | pendente_aprovacao | desatualizado | risco_cta | desconhecido."""
    if not t:
        return "desconhecido"
    status = (t.get("status") or "").lower()
    v_tpl = t.get("versao_campanha")
    try:
        v_tpl = int(v_tpl) if v_tpl is not None else None
    except (TypeError, ValueError):
        v_tpl = None
    risco = bool(engine._inner_tem_botao(t.get("inner_html") or ""))
    if risco:
        return "risco_cta"
    if v_tpl is not None and v_tpl < int(camp_versao or 1):
        return "desatualizado"
    if status == "aprovado":
        return "compativel"
    if status == "pendente":
        return "pendente_aprovacao"
    return "desconhecido"


def _campaign_payload(c, camp):
    camp_versao = int(camp["versao"] or 1) if "versao" in camp.keys() else 1
    steps = []
    for s in c.execute("SELECT * FROM campaign_steps WHERE campaign_id=? ORDER BY ordem",
                       (camp["id"],)).fetchall():
        t = engine.template_get(s["template_ref"]) or {}
        alts = []
        raw_alts = t.get("assuntos_alt") or ""
        if raw_alts:
            try:
                alts = json.loads(raw_alts) if isinstance(raw_alts, str) else list(raw_alts)
            except Exception:
                alts = []
        compat = _compat_status(camp_versao, t)
        steps.append({"ordem": s["ordem"], "template_ref": s["template_ref"],
                      "offset_dias": s["offset_dias"], "nome": t.get("nome", "?"),
                      "assunto": t.get("assunto", ""), "assuntos_alt": alts,
                      "status_template": t.get("status", "?"),
                      "versao_template": t.get("versao_campanha"),
                      "compativel": compat == "compativel",
                      "compat_status": compat})
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
    n_comp = sum(1 for s in steps if s.get("compativel"))
    d.update({"steps": steps, "contatos_por_status": stats, "contatos_por_etapa": etapas,
              "total_contatos": sum(stats.values()), "enviados": enviados,
              "versao": camp_versao,
              "atualizada_em": (camp["atualizada_em"] if "atualizada_em" in camp.keys() else "") or "",
              "templates_compativeis": n_comp,
              "templates_total": len(steps),
              "rewrite_log": db.rewrite_log_get(camp["id"], limit=8),
              "export_docx": True,  # atualizado no endpoint /health se preciso; UI testa export
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
    d["export_docx"] = bool(export_campanha.HAS_DOCX)
    origem = _campaign_origem(camp)
    d["origem_ambiente"] = origem
    d["somente_acompanhamento"] = _somente_acompanhamento(origem)
    d["precisa_decisao"] = _precisa_decisao_import(origem)
    d["dashboard_env"] = db.dashboard_env()
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
    sender_id: Optional[int] = None


@app.post("/api/campaigns/suggest")
def campaigns_suggest(b: SugestaoBody):
    """Dispara o loop estrategista GTM + crítica do copywriter em background.
    Tudo termina em rascunho; nada é ativado ou enviado automaticamente."""
    blocked = _guard_campaign_create()
    if blocked:
        return blocked
    sender = _campaign_sender(b.sender_id)
    if isinstance(sender, JSONResponse):
        return sender
    return ai.iniciar_sugestao(b.max_campanhas, b.auto_gerar, sender["id"])


@app.get("/api/campaigns/suggest")
def campaigns_suggest_status():
    return ai.suggest_status() or {"status": "vazio",
                                   "nota": "nenhum portfólio sugerido ainda"}


@app.post("/api/campaigns/suggest/generate")
def campaigns_suggest_generate(b: SugestaoBody):
    """Gera os rascunhos do portfólio já sugerido (sequencial, em background)."""
    blocked = _guard_campaign_create()
    if blocked:
        return blocked
    sender = _campaign_sender(b.sender_id)
    if isinstance(sender, JSONResponse):
        return sender
    try:
        return ai.iniciar_geracao_portfolio(sender["id"])
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
    business_unit: str = ""
    market: str = ""
    language: str = ""
    objetivo: str
    tipo: str = "finita"                 # finita | infinita
    cad_min: Optional[int] = None        # cadência aleatória: sorteio entre min e max dias
    cad_max: Optional[int] = None
    n_emails: int = 3
    segmentos: Optional[List[str]] = None
    apenas_sem_resposta: bool = True
    tags: Optional[List[str]] = None     # restringe a quem tiver alguma destas tags
    sender_id: Optional[int] = None
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
    # Regeneração é opt-in. Mudar só horário/cadência/nome NÃO recria e-mails.
    regenerar: bool = False


@app.patch("/api/campaigns/{cid}")
def campaign_edit(cid: int, body: EditarCampanha):
    """Edita o planejamento (nome/objetivo/cadência/remetente/horário).

    Regenera e-mails só quando ``regenerar=True`` **e** o objetivo mudou.
    Alterar somente o 1º envio / cadência / nome / remetente nunca dispara IA.

    Campanhas ativas precisam ser pausadas primeiro.
    """
    blocked = _guard_campaign_mutation(cid)
    if blocked:
        return blocked
    c = db.conn()
    current = c.execute("SELECT sender_id FROM campaigns WHERE id=?", (cid,)).fetchone()
    c.close()
    if current and int(body.sender_id) != int(current["sender_id"] or 0):
        return JSONResponse({"erro": "O remetente de uma campanha existente não pode ser alterado. "
                             "Crie uma nova campanha para usar genau@yoobe.co."}, status_code=409)
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
    camp = c.execute(
        "SELECT status, objetivo FROM campaigns WHERE id=?", (cid,)
    ).fetchone()
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
    n_steps = c.execute(
        "SELECT COUNT(*) AS n FROM campaign_steps WHERE campaign_id=?", (cid,)
    ).fetchone()["n"]
    objetivo_antes = (camp["objetivo"] or "").strip()
    objetivo_alterado = objetivo != objetivo_antes
    # Segurança: regenerar só com objetivo novo. Horário/cadência sozinhos nunca disparam IA.
    deve_regenerar = bool(body.regenerar) and objetivo_alterado
    agora = db.now()
    c.execute("""UPDATE campaigns
                 SET nome=?, objetivo=?, sender_id=?, tipo=?, cad_min_dias=?, cad_max_dias=?,
                     inicio_em=?, atualizada_em=?
                 WHERE id=?""",
              (nome, objetivo, body.sender_id, body.tipo, cad_min, cad_max,
               _normaliza_dt(body.inicio_em), agora, cid))
    if deve_regenerar and n_steps:
        c.execute(
            """UPDATE campaigns SET contexto=? WHERE id=? AND (
                 contexto IS NULL OR contexto='' OR contexto LIKE 'A IA está%'
                 OR contexto LIKE 'Objetivo atualizado%')""",
            ("Objetivo atualizado — regenerando e-mails…", cid))
    c.commit()
    c.close()

    regenerando = False
    nota = "Planejamento atualizado."
    if body.regenerar and not objetivo_alterado:
        nota = ("Planejamento atualizado. E-mails mantidos — o objetivo não mudou "
                "(alterar só o horário/cadência não regenera).")
    elif deve_regenerar and n_steps:
        # Libera build travado (gerando/stale/erro) antes de disparar a reescrita.
        prog = ai.build_progress(cid) or {}
        if prog.get("status") == "gerando":
            ai.interromper_geracao(cid, "operador salvou novo objetivo — regenerando")
            time.sleep(0.4)
        try:
            diretrizes = (
                "OBJETIVO ATUALIZADO PELO OPERADOR (prioridade máxima).\n"
                "Reescreva TODOS os e-mails para aderir a este objetivo. "
                "Atualize também o Resumo/Planejamento da campanha.\n\n"
                f"{objetivo}"
            )
            ai.reescrever_campanha(cid, diretrizes, None)
            regenerando = True
            nota = ("Objetivo salvo. Regenerando os e-mails com o novo objetivo — "
                    "acompanhe na tela da campanha.")
        except ValueError as ex:
            return JSONResponse({
                "id": cid, "status": "parcial",
                "erro": str(ex),
                "nota": f"Objetivo salvo, mas a regeneração não iniciou: {ex}",
            }, status_code=409)
    elif deve_regenerar and not n_steps:
        # Sem e-mails ainda: retoma/cria build se estiver em erro; senão só salva.
        prog = ai.build_progress(cid) or {}
        if prog.get("status") in ("erro",) or (prog.get("status") == "gerando" and prog.get("stale")):
            try:
                if prog.get("status") == "gerando":
                    ai.interromper_geracao(cid, "operador salvou novo objetivo")
                ai.retomar_campanha(cid)
                regenerando = True
                nota = "Objetivo salvo. Retomando a criação dos e-mails…"
            except Exception as ex:
                nota = f"Objetivo salvo. Geração não retomada: {ex}"
        else:
            nota = "Objetivo salvo. A campanha ainda não tem e-mails — use Retomar criação se a geração parou."
    elif not body.regenerar:
        nota = "Planejamento atualizado. E-mails não foram regenerados."

    return {"id": cid, "status": "gerando" if regenerando else "atualizada",
            "regenerando": regenerando, "objetivo_alterado": objetivo_alterado,
            "nota": nota}


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
    blocked = _guard_campaign_create()
    if blocked:
        return blocked
    sender = _campaign_sender(nc.sender_id)
    if isinstance(sender, JSONResponse):
        return sender
    if nc.tipo == "infinita" and nc.cad_min is None:
        nc.cad_min, nc.cad_max = 7, 14
    try:
        ai.business_context.resolve(objective=nc.objetivo, business_unit=nc.business_unit, market=nc.market, language=nc.language)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    cid = ai.criar_campanha(nc.objetivo, nc.tipo, nc.cad_min, nc.cad_max,
                            nc.n_emails, nc.segmentos, nc.apenas_sem_resposta, nc.tags, sender["id"],
                            _normaliza_dt(nc.inicio_em), nc.audience_profile or "", nc.business_unit, nc.market, nc.language)
    return {"id": cid, "status": "gerando",
            "nota": "Campanha criada em rascunho. A IA está redigindo os e-mails em segundo plano — "
                    "acompanhe o progresso na tela da campanha."}


class CampanhaTemplate(BaseModel):
    template_ref: str
    nome: Optional[str] = None
    sender_id: Optional[int] = None
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
    """Cria uma campanha a partir de um template existente (sem IA) — 1 etapa = o template.

    Sempre clona para um `templates_ai` exclusivo da campanha. Assim editar/salvar
    nesta campanha não altera a biblioteca nem outras campanhas que usem o mesmo original.
    """
    blocked = _guard_campaign_create()
    if blocked:
        return blocked
    sender = _campaign_sender(b.sender_id)
    if isinstance(sender, JSONResponse):
        return sender
    t = engine.template_get(b.template_ref)
    if not t:
        return JSONResponse({"erro": "template não encontrado"}, status_code=404)
    cad_min, cad_max = b.cad_min, b.cad_max
    if b.tipo == "infinita" and cad_min is None:
        cad_min, cad_max = 7, 14
    nome = (b.nome or "").strip() or f"Campanha — {(t.get('nome') or t.get('assunto') or 'template')[:50]}"
    c = db.conn()
    c.execute("""INSERT INTO campaigns(nome,objetivo,tipo,status,cad_min_dias,cad_max_dias,contexto,sender_id,inicio_em,audience_profile,origem_ambiente)
                 VALUES(?,?,?,'rascunho',?,?,?,?,?,?,?)""",
              (nome, f"Campanha a partir do template '{t.get('nome') or t.get('assunto')}'", b.tipo,
               cad_min, cad_max, "Criada a partir de template existente", sender["id"],
               _normaliza_dt(b.inicio_em), b.audience_profile or "", db.campaign_origin()))
    cid = c.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
    # Clone exclusivo — nunca compartilha o ref da biblioteca/base.
    c.execute("""INSERT INTO templates_ai(nome,assunto,preheader,cta,cta_url,inner_html,texto_plano,
                                          status,origem,assuntos_alt,campaign_id,versao_campanha)
                 VALUES(?,?,?,?,?,?,?,'pendente','campanha',?,?,1)""",
              (t.get("nome") or f"E-mail 1", t.get("assunto") or "", t.get("preheader") or "",
               t.get("cta") or "", t.get("cta_url") or "", t.get("inner_html") or "",
               t.get("texto_plano") or "", t.get("assuntos_alt") or "[]", cid))
    tid = c.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
    step_ref = f"ai:{tid}"
    # finita de 1 toque = D+0; infinita usa a cadência (offset NULL)
    offset = 0 if b.tipo == "finita" else None
    c.execute("INSERT INTO campaign_steps(campaign_id,ordem,template_ref,offset_dias) VALUES(?,1,?,?)",
              (cid, step_ref, offset))
    segs = b.segmentos if b.segmentos else ["quente", "morno", "frio"]
    n = 0
    for email in db.resolve_audience(segs, b.tags, b.apenas_sem_resposta, perfil=b.audience_profile or ""):
        c.execute("INSERT OR IGNORE INTO campaign_contacts(campaign_id,email) VALUES(?,?)", (cid, email))
        n += 1
    c.commit()
    c.close()
    return {"id": cid, "status": "rascunho", "contatos": n, "template_ref": step_ref,
            "aprovado": False,
            "nota": "Campanha criada com cópia editável do template. Revise, aprove e ative quando quiser."}


class AtivarBody(BaseModel):
    # QUERO_ENVIAR = clientes protegidos;
    # ENVIAR_COM_PENDENCIAS = soft gates (templates/checklist/patches) — CHECKLIST_OK aceito como alias
    confirmacao: str = ""
    # True = 1ª etapa agora (ignora inicio_em + janela comercial via immediate=True)
    imediato: bool = False


class ChecklistItemPatch(BaseModel):
    status: str                           # pending | done | failed | skipped
    evidence: Optional[str] = None


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
    blocked = _guard_campaign_mutation(cid)
    if blocked:
        return blocked
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
    sender_blocked = _campaign_sender_guard(camp)
    if sender_blocked:
        return sender_blocked
    sender = db.sender_get(camp["sender_id"] or 1) or db.sender_default()
    st = engine.sender_auth_status(sender) if sender else {"conectado": False, "motivo": "sem remetente"}
    if not st["conectado"]:
        return JSONResponse({"erro": f"remetente {sender['email'] if sender else '?'} não conectado "
                             f"({st['motivo']}) — conecte na aba Remetentes"}, status_code=409)
    tpl = engine.template_get(step["template_ref"])
    contato = {"email": email, "nome": "Genau", "empresa": "Yoobe", "telefone": "",
               "cargo": "Gestão", "porte_assinantes": "", "erp": "", "tags": ""}
    # Testes não recebem link de descadastro: o destinatário pode ser uma caixa
    # interna e a amostra não faz parte da base/cadência. O tracking, quando
    # pedido, é injetado abaixo com campaign_id para atribuição no funil.
    assunto, html = engine.render_html(step["template_ref"], contato, sender, None)
    plain = engine.render_vars((tpl or {}).get("texto_plano") or "", contato)
    tracking_cid = cid if body.rastrear else None
    msgid, thread_id, track_id = engine.enviar_email(
        contato, f"[TESTE Yoobe] {assunto}", html, plain,
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
    blocked = _guard_campaign_mutation(cid)
    if blocked:
        return blocked
    c = db.conn()
    camp = c.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
    if not camp:
        c.close()
        return JSONResponse({"erro": "campanha não encontrada"}, status_code=404)
    sender_blocked = _campaign_sender_guard(camp)
    if sender_blocked:
        c.close()
        return sender_blocked
    confirmacao = (body.confirmacao if body else "").strip()
    soft_token = confirmacao in ("ENVIAR_COM_PENDENCIAS", "CHECKLIST_OK")
    if soft_token:
        db.meta_set(f"soft_activate_override_{cid}", db.now())
    # Meta cobre o 2º passo (QUERO_ENVIAR) após confirmar ressalvas soft
    soft_ok = soft_token or bool(db.meta_get(f"soft_activate_override_{cid}", ""))

    # Hard: build ainda gerando (não há e-mail pronto)
    build = {}
    try:
        build = json.loads(db.meta_get(f"build_{cid}", "") or "{}") or {}
    except Exception:
        build = {}
    if (build.get("status") or "") == "gerando":
        c.close()
        return JSONResponse({
            "erro": "a geração/reescrita ainda está em andamento — aguarde ou use Retomar criação",
            "build": build,
        }, status_code=409)

    # Soft pendências (templates / checklist / patches / run) — não bloqueiam sem token
    nao_aprovados = [s["template_ref"] for s in c.execute(
        "SELECT template_ref FROM campaign_steps WHERE campaign_id=?", (cid,)).fetchall()
        if (engine.template_get(s["template_ref"]) or {}).get("status") != "aprovado"]
    try:
        ai.rodar_checklist(cid)
        cl, pend_cl = ai.checklist_incomplete_items(cid)
    except ValueError:
        cl, pend_cl = {"ready": True, "items": []}, []
    except Exception:
        try:
            cl, pend_cl = ai.checklist_incomplete_items(cid)
        except ValueError:
            cl, pend_cl = {"ready": True, "items": []}, []
    soft_pending = []
    if nao_aprovados:
        soft_pending.append({
            "key": "templates",
            "title": f"{len(nao_aprovados)} template(s) não aprovado(s)",
            "detail": ", ".join(nao_aprovados[:8]),
        })
    if pend_cl:
        soft_pending.append({
            "key": "checklist",
            "title": f"Checklist incompleto ({len(pend_cl)} item(ns))",
            "detail": "; ".join((x.get("label") or x.get("id") or "")[:50] for x in pend_cl[:5]),
        })
    active_run = c.execute(
        """SELECT id, status FROM improvement_runs
           WHERE campaign_id=? AND status IN ('pending','planning','running',
             'queued','applying') ORDER BY id DESC LIMIT 1""", (cid,)).fetchone()
    if active_run:
        soft_pending.append({
            "key": "run_ativo",
            "title": f"Run de melhorias ativo #{active_run['id']}",
            "detail": active_run["status"] or "",
        })
    n_patches = c.execute(
        """SELECT COUNT(*) n FROM improvement_tasks
           WHERE campaign_id=? AND status='awaiting_approval'""", (cid,)).fetchone()["n"]
    if n_patches:
        soft_pending.append({
            "key": "patches",
            "title": f"{n_patches} patch(es) aguardando aprovação",
            "detail": "recomendado aprovar/rejeitar antes",
        })
    if soft_pending and not soft_ok:
        c.close()
        return JSONResponse({
            "erro": (f"Há {len(soft_pending)} pendência(s) recomendada(s). "
                     "Resolva no guia ou confirme com ENVIAR_COM_PENDENCIAS."),
            "requer_soft_confirm": True,
            "token": "ENVIAR_COM_PENDENCIAS",
            "soft_pending": soft_pending,
            "pendentes": [{"id": x.get("id"), "label": x.get("label"),
                           "status": x.get("status"), "evidence": x.get("evidence")}
                          for x in pend_cl],
            "templates_pendentes": nao_aprovados,
            "checklist": cl,
        }, status_code=409)

    if soft_ok and soft_pending:
        # Motor de envio exige template aprovado — carimba pendentes no bypass
        if nao_aprovados:
            for ref in nao_aprovados:
                if not str(ref).startswith("ai:"):
                    continue
                try:
                    tid = int(str(ref).split(":", 1)[1])
                except ValueError:
                    continue
                c.execute("UPDATE templates_ai SET status='aprovado' WHERE id=? AND status!='aprovado'",
                          (tid,))
            c.commit()
            engine.log(f"campanha {cid}: {len(nao_aprovados)} template(s) carimbado(s) "
                       f"via ENVIAR_COM_PENDENCIAS")
        engine.log(f"campanha {cid}: ativação com ressalvas — "
                   + "; ".join(p["title"] for p in soft_pending))

    # Hard: remetente conectado
    sender = db.sender_get(camp["sender_id"] or 1) or db.sender_default()
    if sender and not engine.sender_auth_status(sender)["conectado"]:
        c.close()
        return JSONResponse({"erro": f"o remetente {sender['email']} não está conectado — "
                             "conecte a conta na aba Remetentes antes de ativar."}, status_code=409)
    # Qualidade: exclui desatualizados/inválidos; enfileira stale residual (não bloqueia).
    report = leads.campaign_validation_report(cid)
    excluidos = report.get("emails_excluidos") or []
    if excluidos:
        for em in excluidos:
            c.execute(
                """UPDATE campaign_contacts SET status='desatualizado'
                   WHERE campaign_id=? AND email=? AND status IN ('pendente','ativo')""",
                (cid, em))
        c.commit()
        engine.log(f"campanha {cid}: {len(excluidos)} contato(s) excluído(s) na ativação "
                   f"(validação desatualizado/inválido)")
    if report.get("stale"):
        try:
            leads.enqueue_stale_for_campaign(cid)
        except Exception:
            pass
    if report["validos"] == 0:
        c.close()
        return JSONResponse({
            "erro": "nenhum contato válido para envio após validação de qualidade "
                    "(todos desatualizados, inválidos ou suprimidos). "
                    "Rode Validar leads na aba Qualidade ou revise a audiência.",
            "validacao": {
                "validos": 0, "excluidos": report["excluidos"],
                "duvidosos": report["duvidosos"], "stale": report["stale"],
                "amostra": report.get("amostra_excluidos") or [],
            },
        }, status_code=409)
    # GUARDRAIL: campanha que toca CLIENTE (ativo/inativo/possível) só ativa com QUERO_ENVIAR
    prot = db.contatos_cliente_na_campanha(cid)
    if prot["total"] > 0 and confirmacao != "QUERO_ENVIAR":
        c.close()
        return JSONResponse({
            "erro": (f"Esta campanha inclui {prot['total']} contato(s) que são CLIENTES Yoobe "
                     f"({prot['ativos']} ativos, {prot['inativos']} inativos, {prot['possiveis']} possíveis). "
                     "Para enviar mesmo assim, confirme digitando QUERO_ENVIAR."),
            "requer_confirmacao": True, "token": "QUERO_ENVIAR",
            "clientes": prot["total"], "ativos": prot["ativos"], "inativos": prot["inativos"],
            "possiveis": prot["possiveis"], "amostra": prot["amostra"],
            "validacao": {
                "validos": report["validos"], "excluidos": report["excluidos"],
                "duvidosos": report["duvidosos"], "stale": report["stale"],
            }}, status_code=409)
    if prot["total"] > 0:
        c.execute("UPDATE campaigns SET clientes_confirmados=1 WHERE id=?", (cid,))
        engine.log(f"campanha {cid}: QUERO_ENVIAR confirmado pelo Genal — {prot['total']} cliente(s) "
                   f"liberado(s) ({prot['ativos']} ativos / {prot['inativos']} inativos / {prot['possiveis']} possíveis)")
    c.execute("UPDATE campaigns SET status='ativa' WHERE id=?", (cid,))
    c.commit()
    c.close()
    try:
        db.meta_set(f"checklist_override_{cid}", "")
        db.meta_set(f"soft_activate_override_{cid}", "")
    except Exception:
        pass
    ensure_runtime_loops()
    _wake.set()  # acorda os loops adormecidos sem esperar o timeout
    imediato = bool(body and body.imediato)
    if imediato:
        # Evita agendar no inicio_em futuro e depois brigar com o disparo imediato.
        c = db.conn()
        c.execute("UPDATE campaigns SET inicio_em='' WHERE id=?", (cid,))
        c.commit()
        c.close()
    agendados = engine.agendar_campanha(cid)
    disparo = {"enfileirados": 0, "processados": 0}
    if imediato:
        disparo = engine.forcar_disparo_imediato(cid)
        agendados = disparo.get("agendados", agendados)
        engine.log(f"campanha {cid} ATIVADA + DISPARO IMEDIATO — {agendados} contato(s) "
                   f"(validação: {report['validos']} válidos / {report['excluidos']} excluídos)")
    else:
        engine.log(f"campanha {cid} ATIVADA pelo Genal — {agendados} contato(s) agendados "
                   f"(validação: {report['validos']} válidos / {report['excluidos']} excluídos)")
        # Se o horário já estiver liberado, não espera nem o próximo tick de 10 s.
        # Roda fora da request para a interface responder imediatamente.
        threading.Thread(target=engine.process_due_campaign_sends, args=(cid,), daemon=True).start()
    slug = db.campaign_slug(cid)
    ga4.mp_send_event("email_campaign_activated",
                      {"campaign_id": cid, "campaign_name": camp["nome"], "campaign_slug": slug})
    if not (camp["ads_briefing"] or "").strip():
        def _briefing_bg():
            try:
                ok_g, msg_g = campaign_guard_gate.allow_campaign_llm(cid)
                if not ok_g:
                    engine.log(f"campanha {cid}: briefing Ads bloqueado pelo campaign-guard ({msg_g})")
                    return
                c2, s2 = _campanha_e_steps(cid)
                ai.gerar_briefing_ads(c2, s2)
                engine.log(f"campanha {cid}: briefing Google Ads gerado na ativação")
            except Exception as e:
                engine.log(f"campanha {cid}: briefing Ads falhou (gere manualmente): {e}")
        threading.Thread(target=_briefing_bg, daemon=True).start()
    out = {"id": cid, "status": "ativa", "agendados": agendados,
           "validacao": {
               "validos": report["validos"], "excluidos": report["excluidos"],
               "duvidosos": report["duvidosos"], "stale": report["stale"],
               "amostra_excluidos": report.get("amostra_excluidos") or [],
           },
           "utm": {"utm_source": "yoobe-mail", "utm_medium": "email", "utm_campaign": slug}}
    if imediato:
        out.update({
            "imediato": True,
            "enfileirados": disparo.get("enfileirados", 0),
            "processados": disparo.get("processados", 0),
            "nota": (f"1ª etapa disparada agora para {agendados} contato(s). "
                     "Os próximos e-mails seguem a cadência."),
        })
    return out


@app.post("/api/campaigns/{cid}/launch-now")
def campaign_launch_now(cid: int, body: Optional[AtivarBody] = None):
    """Ativa (se necessário) e dispara a 1ª etapa imediatamente.

    Ignora ``inicio_em`` futuro e a janela comercial via ``immediate=True``.
    Demais guardrails (remetente, contatos, clientes, soft confirm) continuam iguais.
    """
    blocked = _guard_campaign_mutation(cid)
    if blocked:
        return blocked
    c = db.conn()
    camp = c.execute("SELECT id, status, nome FROM campaigns WHERE id=?", (cid,)).fetchone()
    c.close()
    if not camp:
        return JSONResponse({"erro": "campanha não encontrada"}, status_code=404)
    c = db.conn()
    campaign_row = c.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
    c.close()
    sender_blocked = _campaign_sender_guard(campaign_row)
    if sender_blocked:
        return sender_blocked
    if camp["status"] not in {"rascunho", "pausada", "ativa"}:
        return JSONResponse({"erro": "a campanha está encerrada e não pode ser disparada"},
                            status_code=409)

    confirmacao = (body.confirmacao if body else "").strip()
    ativou = False
    activate_payload = {}
    if camp["status"] in {"rascunho", "pausada"}:
        # Mesmo gate de ativação, sem thread paralela de process_due (imediato=True).
        r = campaign_activate(cid, AtivarBody(confirmacao=confirmacao, imediato=True))
        if isinstance(r, JSONResponse):
            return r
        activate_payload = r if isinstance(r, dict) else {}
        ativou = True
        return {
            "id": cid,
            "status": "ativa",
            "ativou": True,
            "agendados": activate_payload.get("agendados", 0),
            "enfileirados": activate_payload.get("enfileirados", 0),
            "processados": activate_payload.get("processados", 0),
            "validacao": activate_payload.get("validacao"),
            "nota": activate_payload.get("nota") or (
                f"1ª etapa disparada agora para {activate_payload.get('agendados', 0)} contato(s). "
                "Os próximos e-mails seguem a cadência."
            ),
        }

    # Campanha já ativa: ainda exige QUERO_ENVIAR se houver clientes sem confirmação.
    prot = db.contatos_cliente_na_campanha(cid)
    c = db.conn()
    camp_full = c.execute(
        "SELECT clientes_confirmados FROM campaigns WHERE id=?", (cid,)
    ).fetchone()
    c.close()
    if prot["total"] > 0 and not (camp_full and camp_full["clientes_confirmados"]) \
            and confirmacao != "QUERO_ENVIAR":
        return JSONResponse({
            "erro": (f"Esta campanha inclui {prot['total']} cliente(s) Yoobe. "
                     "Para disparar agora, digite QUERO_ENVIAR."),
            "requer_confirmacao": True, "token": "QUERO_ENVIAR",
            "clientes": prot["total"], "ativos": prot["ativos"],
            "inativos": prot["inativos"], "possiveis": prot["possiveis"],
        }, status_code=409)
    if prot["total"] > 0 and confirmacao == "QUERO_ENVIAR":
        c = db.conn()
        c.execute("UPDATE campaigns SET clientes_confirmados=1 WHERE id=?", (cid,))
        c.commit()
        c.close()
    ensure_runtime_loops()
    _wake.set()

    disparo = engine.forcar_disparo_imediato(cid)
    engine.log(
        f"campanha {cid} DISPARO IMEDIATO pelo Genal — "
        f"{disparo.get('agendados', 0)} contato(s) na 1ª etapa (já estava ativa)"
    )
    return {
        "id": cid,
        "status": "ativa",
        "ativou": ativou,
        "agendados": disparo.get("agendados", 0),
        "enfileirados": disparo.get("enfileirados", 0),
        "processados": disparo.get("processados", 0),
        "nota": (
            f"1ª etapa disparada agora para {disparo.get('agendados', 0)} contato(s). "
            "Os próximos e-mails seguem a cadência."
        ),
    }


@app.get("/api/campaigns/{cid}/replies")
def campaign_replies(cid: int):
    """Respostas de leads ligadas a esta campanha (via thread dos envios)."""
    c = db.conn()
    camp = c.execute("SELECT id, nome FROM campaigns WHERE id=?", (cid,)).fetchone()
    if not camp:
        c.close()
        return JSONResponse({"erro": "campanha não encontrada"}, status_code=404)
    rows = [dict(r) for r in c.execute(
        """SELECT r.msgid, r.thread, r.email, r.data, r.classe, r.confianca, r.resumo, r.acao,
                  ct.nome, ct.empresa
           FROM replies r
           JOIN sends s ON s.thread=r.thread AND s.campaign_id=?
           LEFT JOIN contacts ct ON ct.email=r.email
           WHERE r.thread!=''
             AND NOT EXISTS (SELECT 1 FROM bounces b WHERE b.msgid=r.msgid)
             AND LOWER(IFNULL(r.email,'')) NOT LIKE '%postmaster%'
             AND LOWER(IFNULL(r.email,'')) NOT LIKE '%mailer-daemon%'
           GROUP BY r.msgid
           ORDER BY r.data DESC, r.msgid DESC
           LIMIT 200""", (cid,)).fetchall()]
    c.close()
    out = []
    for r in rows:
        out.append({
            "msgid": r["msgid"], "thread": r["thread"], "email": r["email"],
            "data": r["data"], "classe": r["classe"], "confianca": r["confianca"],
            "resumo": r["resumo"], "acao": r.get("acao") or "",
            "nome": r.get("nome") or "", "empresa": r.get("empresa") or "",
            "gmail": f"https://mail.google.com/mail/u/0/#all/{r['msgid']}",
        })
    return {"campaign_id": cid, "n": len(out), "replies": out}


@app.get("/api/campaigns/{cid}/invalidos")
def campaign_invalidos(cid: int):
    """Contatos da campanha com status email_invalido / soft_suprimido (+ último bounce)."""
    c = db.conn()
    camp = c.execute("SELECT id FROM campaigns WHERE id=?", (cid,)).fetchone()
    if not camp:
        c.close()
        return JSONResponse({"erro": "campanha não encontrada"}, status_code=404)
    rows = [dict(r) for r in c.execute(
        """SELECT cc.email, cc.status, ct.nome, ct.empresa, ct.tags, ct.validacao_status,
                  (SELECT b.tipo FROM bounces b WHERE b.email=cc.email
                   ORDER BY b.criado_em DESC LIMIT 1) AS bounce_tipo,
                  (SELECT b.diagnostico FROM bounces b WHERE b.email=cc.email
                   ORDER BY b.criado_em DESC LIMIT 1) AS bounce_diag,
                  (SELECT b.criado_em FROM bounces b WHERE b.email=cc.email
                   ORDER BY b.criado_em DESC LIMIT 1) AS bounce_em
           FROM campaign_contacts cc
           LEFT JOIN contacts ct ON ct.email=cc.email
           WHERE cc.campaign_id=? AND cc.status IN ('email_invalido','soft_suprimido')
           ORDER BY cc.email""", (cid,)).fetchall()]
    c.close()
    return {"campaign_id": cid, "n": len(rows), "invalidos": rows}


class SanitizeInvalidosBody(BaseModel):
    action: str = "inativar_crm"  # inativar_crm | remover_campanha | excluir_crm
    emails: Optional[list[str]] = None
    confirmacao: str = ""


@app.post("/api/campaigns/{cid}/sanitize-invalidos")
def campaign_sanitize_invalidos(cid: int, body: SanitizeInvalidosBody):
    """Sanitiza e-mails inválidos da campanha (CRM / vínculo / exclusão)."""
    blocked = _guard_campaign_mutation(cid)
    if blocked:
        return blocked
    action = (body.action or "inativar_crm").strip()
    if action not in ("inativar_crm", "remover_campanha", "excluir_crm"):
        return JSONResponse({"erro": "action inválida"}, status_code=422)
    if action == "excluir_crm" and (body.confirmacao or "").strip() != "EXCLUIR":
        return JSONResponse({
            "erro": "Para excluir do CRM, confirme digitando EXCLUIR.",
            "requer_confirmacao": True, "token": "EXCLUIR",
        }, status_code=409)

    c = db.conn()
    camp = c.execute("SELECT id FROM campaigns WHERE id=?", (cid,)).fetchone()
    if not camp:
        c.close()
        return JSONResponse({"erro": "campanha não encontrada"}, status_code=404)

    if body.emails:
        wanted = [e.strip().lower() for e in body.emails if (e or "").strip()]
        placeholders = ",".join("?" * len(wanted)) if wanted else "''"
        rows = [dict(r) for r in c.execute(
            f"""SELECT email, status FROM campaign_contacts
                WHERE campaign_id=? AND email IN ({placeholders})
                  AND status IN ('email_invalido','soft_suprimido')""",
            (cid, *wanted)).fetchall()] if wanted else []
    else:
        rows = [dict(r) for r in c.execute(
            """SELECT email, status FROM campaign_contacts
               WHERE campaign_id=? AND status IN ('email_invalido','soft_suprimido')""",
            (cid,)).fetchall()]
    emails = [r["email"] for r in rows]
    feitos, erros = [], []

    for email in emails:
        try:
            if action == "remover_campanha":
                c.execute(
                    """UPDATE campaign_contacts SET status='email_invalido', next_send_at=NULL
                       WHERE campaign_id=? AND email=?""", (cid, email))
                feitos.append(email)
            elif action == "inativar_crm":
                db.marcar_email_invalido(c, email)
                row = c.execute("SELECT tags FROM contacts WHERE email=?", (email,)).fetchone()
                if row:
                    tags = db.tags_list(row["tags"])
                    if "nao-contatar" not in tags:
                        tags.append("nao-contatar")
                    c.execute(
                        """UPDATE contacts SET tags=?, validacao_status='invalido',
                               validacao_motivo=COALESCE(NULLIF(validacao_motivo,''),
                                                         'sanitizado: hard bounce')
                           WHERE email=?""",
                        (db.tags_str(tags), email))
                c.execute(
                    """UPDATE campaign_contacts SET status='email_invalido', next_send_at=NULL
                       WHERE email=? AND status IN ('pendente','ativo','email_invalido','soft_suprimido')""",
                    (email,))
                feitos.append(email)
            else:  # excluir_crm — hard delete (mesma lógica do DELETE /contacts)
                c.commit()
                c.close()
                r = delete_contact(email)
                if isinstance(r, JSONResponse):
                    erros.append({"email": email, "erro": "não encontrado"})
                else:
                    feitos.append(email)
                c = db.conn()
        except Exception as ex:
            erros.append({"email": email, "erro": str(ex)[:120]})

    c.commit()
    c.close()
    engine.log(f"campanha {cid}: sanitize-invalidos action={action} n={len(feitos)}")
    return {"campaign_id": cid, "action": action, "n": len(feitos), "emails": feitos, "erros": erros}


@app.post("/api/campaigns/{cid}/pause")
def campaign_pause(cid: int):
    blocked = _guard_campaign_mutation(cid)
    if blocked:
        return blocked
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
    blocked = _guard_campaign_mutation(cid)
    if blocked:
        return blocked
    c = db.conn()
    camp = c.execute("SELECT id FROM campaigns WHERE id=?", (cid,)).fetchone()
    c.close()
    if not camp:
        return JSONResponse({"erro": "campanha não encontrada"}, status_code=404)
    prog = ai.build_progress(cid)
    # Erro clássico (cota/crash) OU build stale (worker morto por reload, status ainda "gerando").
    pode_retomar = bool(prog) and (
        prog.get("status") == "erro"
        or (prog.get("status") == "gerando" and prog.get("stale"))
    )
    if not pode_retomar:
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
    blocked = _guard_campaign_mutation(cid)
    if blocked:
        return blocked
    try:
        r = ai.reescrever_campanha(cid, b.diretrizes, b.ordens)
    except ValueError as ex:
        return JSONResponse({"erro": str(ex)}, status_code=409)
    return {**r, "status": "gerando",
            "nota": "Reescrevendo os e-mails com as novas diretrizes — acompanhe na tela da campanha."}


def _campaign_meta_keys(cid):
    """Chaves meta ligadas a uma campanha (briefing / build / rewrite log)."""
    return (f"briefing_{cid}", f"build_{cid}", f"rewrite_log_{cid}")


def _campaign_meta_count(c, cid):
    n = 0
    for key in _campaign_meta_keys(cid):
        if c.execute("SELECT 1 FROM meta WHERE key=? LIMIT 1", (key,)).fetchone():
            n += 1
    return n


def _campaign_delete_safe(c, camp):
    """Rotina segura de exclusão de uma campanha (sem commit): se estiver ativa marca
    'pausada' primeiro, cancela a fila pendente de envio e só então apaga etapas,
    vínculos, a campanha, templates de IA nunca enviados e meta órfã (briefing/build/log).
    Preserva o histórico (sends/events/replies) e os contatos do CRM.
    Retorna nº de tarefas canceladas."""
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
    for key in _campaign_meta_keys(cid):
        c.execute("DELETE FROM meta WHERE key=?", (key,))
    return canceladas


@app.delete("/api/campaigns/{cid}")
def campaign_delete(cid: int):
    blocked = _guard_campaign_mutation(cid)
    if blocked:
        return blocked
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
    if db.dashboard_env() == "remoto":
        return JSONResponse(
            {"erro": "Exclusão de campanhas só no ambiente local.",
             "dashboard_env": "remoto"},
            status_code=403,
        )
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
    # No local, campanhas de origem remota exigem claim antes de apagar.
    if not body.dry_run:
        for camp in camps:
            blocked = _guard_campaign_mutation(int(camp["id"]))
            if blocked:
                c.close()
                return blocked
    if body.dry_run:
        steps = contatos = fila = meta_keys = 0
        for camp in camps:
            cid = camp["id"]
            steps += c.execute("SELECT COUNT(*) AS n FROM campaign_steps "
                               "WHERE campaign_id=?", (cid,)).fetchone()["n"]
            contatos += c.execute("SELECT COUNT(*) AS n FROM campaign_contacts "
                                  "WHERE campaign_id=?", (cid,)).fetchone()["n"]
            fila += c.execute("SELECT COUNT(*) AS n FROM queue WHERE tipo='campaign_send' "
                              "AND status='pending' AND payload LIKE ?",
                              (f'%\"campaign_id\": {cid},%',)).fetchone()["n"]
            meta_keys += _campaign_meta_count(c, cid)
        c.close()
        return {"dry_run": True, "campanhas": len(camps), "steps": steps,
                "contatos_campanha": contatos, "fila_cancelar": fila,
                "meta_keys": meta_keys,
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
        s["para_campanha_nova"] = (s.get("email") or "").strip().lower() == "genau@yoobe.co"
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
    """Inicia o OAuth do remetente no ambiente apropriado."""
    if db.dashboard_env() == "remoto":
        return JSONResponse({
            "erro": "O remoto está em modo espelho e não aceita autenticação de remetentes.",
            "somente_local": True,
        }, status_code=409)
    if db.dashboard_env() == "operacional":
        s = db.sender_get(sid)
        if not s:
            return JSONResponse({"erro": "remetente não encontrado"}, status_code=404)
        try:
            url = gmail_oauth.begin(sid)
        except ValueError as exc:
            return JSONResponse({"erro": str(exc)}, status_code=409)
        return {"ok": True, "url": url, "email": s["email"],
                "nota": f"Autorize o Gmail como {s['email']}; depois volte e clique em Verificar."}
    r = engine.iniciar_login(sid)
    if not r.get("ok"):
        return JSONResponse({"erro": r.get("erro", "falha ao iniciar login")}, status_code=502)
    return r


@app.get("/oauth/gmail/callback", include_in_schema=False)
def gmail_oauth_callback(code: str = "", state: str = "", error: str = ""):
    if error:
        return HTMLResponse(gmail_oauth.result_page(False, "A autorização do Google foi cancelada."), status_code=400)
    try:
        email = gmail_oauth.complete(code, state)
        return HTMLResponse(gmail_oauth.result_page(
            True, f"Gmail conectado como {email}. Volte ao painel e verifique o remetente."))
    except ValueError as exc:
        return HTMLResponse(gmail_oauth.result_page(False, str(exc)), status_code=400)
    except Exception:
        return HTMLResponse(gmail_oauth.result_page(
            False, "Não foi possível concluir. Gere um novo link no painel."), status_code=502)


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
    if email != "genau@yoobe.co":
        return JSONResponse({"erro": "O único remetente de campanhas é genau@yoobe.co."},
                            status_code=409)
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
    blocked = _guard_campaign_mutation(cid)
    if blocked:
        return blocked
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
    if b:
        b = dict(b)
        try:
            b["checklist"] = ai.get_checklist(cid)
        except ValueError:
            pass
    return {"id": cid, "briefing": b}


@app.post("/api/campaigns/{cid}/briefing")
def campaign_briefing_create(cid: int):
    """(Re)gera o briefing de lançamento com IA. Nada é ativado — só recomendação."""
    blocked = _guard_campaign_mutation(cid)
    if blocked:
        return blocked
    try:
        return {"id": cid, "briefing": ai.gerar_briefing_lancamento(cid)}
    except ValueError as e:
        return JSONResponse({"erro": str(e)}, status_code=404)
    except Exception as e:
        code = 429 if ai._erro_eh_cota(str(e)) else 502
        return JSONResponse({"erro": str(e)}, status_code=code)


@app.get("/api/campaigns/{cid}/checklist")
def campaign_checklist_get(cid: int):
    try:
        return {"id": cid, "checklist": ai.get_checklist(cid)}
    except ValueError as e:
        return JSONResponse({"erro": str(e)}, status_code=404)


@app.get("/api/campaigns/{cid}/activation-guide")
def campaign_activation_guide(cid: int):
    """Passo a passo até Ativar: o que falta e onde ir."""
    try:
        return improvements.activation_guide(cid)
    except ValueError as e:
        return JSONResponse({"erro": str(e)}, status_code=404)


@app.post("/api/campaigns/{cid}/improvements/abandon-stale")
def campaign_improvements_abandon_stale(cid: int):
    """Descarta patches awaiting com from desatualizado / sem diff."""
    blocked = _guard_campaign_mutation(cid)
    if blocked:
        return blocked
    try:
        return improvements.abandon_stale_approvals(cid)
    except ValueError as e:
        return JSONResponse({"erro": str(e)}, status_code=404)


@app.patch("/api/campaigns/{cid}/checklist/{item_id}")
def campaign_checklist_patch(cid: int, item_id: str, body: ChecklistItemPatch):
    blocked = _guard_campaign_mutation(cid)
    if blocked:
        return blocked
    try:
        cl = ai.patch_checklist_item(cid, item_id, body.status, body.evidence)
        return {"id": cid, "checklist": cl}
    except ValueError as e:
        msg = str(e)
        code = 404 if "não encontrad" in msg else 400
        return JSONResponse({"erro": msg}, status_code=code)


@app.post("/api/campaigns/{cid}/checklist/run")
def campaign_checklist_run(cid: int):
    """Roda checks automáticos (templates, remetente, contatos, UTM, recomendação)."""
    blocked = _guard_campaign_mutation(cid)
    if blocked:
        return blocked
    try:
        return {"id": cid, "checklist": ai.rodar_checklist(cid)}
    except ValueError as e:
        return JSONResponse({"erro": str(e)}, status_code=404)
    except Exception as e:
        return JSONResponse({"erro": str(e)}, status_code=502)


@app.get("/api/campaigns/{cid}/ads-briefing")
def campaign_ads_briefing(cid: int):
    camp, _ = _campanha_e_steps(cid)
    if not camp:
        return JSONResponse({"erro": "campanha não encontrada"}, status_code=404)
    b = camp.get("ads_briefing") or ""
    return {"id": cid, "briefing": json.loads(b) if b else None,
            "utm": {"utm_source": "yoobe-mail", "utm_medium": "email",
                    "utm_campaign": camp.get("slug") or db.campaign_slug(cid)}}


@app.get("/api/campaigns/{cid}/export")
def campaign_export(cid: int, fmt: str = "md"):
    """Pacote editorial da campanha (briefing + assuntos + porquês + textos) em md/txt/docx."""
    try:
        data, media, filename = export_campanha.exportar(cid, fmt)
    except ValueError as ex:
        msg = str(ex)
        code = 404 if "não encontrada" in msg else 422
        return JSONResponse({"erro": msg}, status_code=code)
    except RuntimeError as ex:
        return JSONResponse({"erro": str(ex), "docx_disponivel": export_campanha.HAS_DOCX},
                            status_code=501)
    return Response(content=data, media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


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


_ENGAGEMENT_TIPOS = {"todos", "abertura", "clique", "resposta", "descadastro"}


@app.get("/api/engagement")
def engagement_feed(tipo: str = "todos", campaign_id: Optional[int] = None,
                    q: str = "", limit: int = 200):
    """Feed unificado: aberturas, cliques (com destino), replies e descadastros.

    Usa os mesmos filtros de qualidade dos KPIs (aberturas efetivas, cliques sem bot).
    Monta sempre o universo completo (p/ stats dos chips) e filtra por ``tipo`` na resposta.
    """
    tipo = (tipo or "todos").strip().lower()
    if tipo not in _ENGAGEMENT_TIPOS:
        return JSONResponse({"erro": "tipo inválido"}, status_code=422)
    limit = max(1, min(int(limit or 200), 500))
    q_norm = (q or "").strip().lower()
    cid_filtro = campaign_id

    c = db.conn()
    nomes_emp = {r["email"]: (r["nome"] or "", r["empresa"] or "")
                 for r in c.execute("SELECT email, nome, empresa FROM contacts").fetchall()}
    camp = _camp_nomes(c)
    effective_opens = db.effective_open_trackids(c, engine.BOT_CLICK_JANELA_SEG,
                                                 campaign_id=cid_filtro)
    bots = set(db.click_bot_trackids(c, engine.BOT_CLICK_JANELA_SEG))
    itens = []

    def _match_q(email, nome, empresa):
        if not q_norm:
            return True
        return q_norm in f"{email} {nome} {empresa}".lower()

    def _base(email, kind, quando, *, cid=None, etapa="", url="",
              detalhe="", gmail="", classe="", acao="", track_id=""):
        email = (email or "").strip().lower()
        nome, empresa = nomes_emp.get(email, ("", ""))
        if not _match_q(email, nome, empresa):
            return None
        return {
            "kind": kind,
            "quando": quando or "",
            "email": email,
            "nome": nome,
            "empresa": empresa,
            "is_contato": email in nomes_emp,
            "campaign_id": cid,
            "campanha": (camp.get(cid) or f"#{cid}") if cid else "",
            "etapa": etapa or "",
            "url": url or "",
            "detalhe": detalhe or "",
            "gmail": gmail or "",
            "classe": classe or "",
            "acao": acao or "",
            "track_id": track_id or "",
        }

    # Aberturas e cliques: dedup por track_id (mesmo filtro dos KPIs).
    sql = """SELECT track_id, type, MAX(email) email, MAX(ts) quando,
                    MAX(campaign_id) campaign_id, MAX(etapa) etapa,
                    MAX(url) url, COUNT(*) toques
             FROM events WHERE type IN ('open','click') AND track_id!=''"""
    params: list = []
    if cid_filtro is not None:
        sql += " AND campaign_id=?"
        params.append(cid_filtro)
    sql += " GROUP BY track_id, type"
    for r in c.execute(sql, params).fetchall():
        tid = r["track_id"] or ""
        if r["type"] == "open":
            if tid not in effective_opens:
                continue
            kind, url = "abertura", ""
            det = (r["quando"] or "")[:16]
            if r["etapa"]:
                det += f" · {r['etapa']}"
        else:
            if tid in bots:
                continue
            kind, url = "clique", (r["url"] or "")
            det = url or ((r["quando"] or "")[:16])
            if r["etapa"] and url:
                det = f"{r['etapa']} · {url}"
            elif r["etapa"]:
                det = f"{r['etapa']} · {det}"
        if r["toques"] and r["toques"] > 1:
            det += f" ({r['toques']}×)"
        it = _base(r["email"], kind, r["quando"] or "", cid=r["campaign_id"],
                   etapa=r["etapa"] or "", url=url, detalhe=det, track_id=tid)
        if it:
            itens.append(it)

    sql = "SELECT email, ts, campaign_id, etapa, track_id FROM events WHERE type='unsubscribe'"
    params = []
    if cid_filtro is not None:
        sql += " AND campaign_id=?"
        params.append(cid_filtro)
    for r in c.execute(sql, params).fetchall():
        det = "descadastrou" + (f" · {r['etapa']}" if r["etapa"] else "")
        it = _base(r["email"], "descadastro", r["ts"] or "", cid=r["campaign_id"],
                   etapa=r["etapa"] or "", detalhe=det, track_id=r["track_id"] or "")
        if it:
            itens.append(it)

    sql = """SELECT r.email, r.classe, r.confianca, r.data, r.resumo, r.msgid, r.acao,
                    (SELECT s.campaign_id FROM sends s
                     WHERE s.thread=r.thread AND r.thread!='' AND s.campaign_id IS NOT NULL
                     ORDER BY s.id DESC LIMIT 1) AS campaign_id,
                    (SELECT s.etapa FROM sends s
                     WHERE s.thread=r.thread AND r.thread!=''
                     ORDER BY s.id DESC LIMIT 1) AS etapa
             FROM replies r"""
    params = []
    if cid_filtro is not None:
        sql += """ WHERE EXISTS (
            SELECT 1 FROM sends s WHERE s.thread=r.thread AND s.campaign_id=? AND r.thread!='')"""
        params.append(cid_filtro)
    for r in c.execute(sql, params).fetchall():
        conf = int(round((r["confianca"] or 0) * 100))
        acao, classe = r["acao"] or "", r["classe"] or ""
        det = f"{classe}" + (f" · {conf}%" if conf else "")
        if r["resumo"]:
            det += f" — {r['resumo']}"
        if acao:
            det += f" [{acao}]"
        gmail = f"https://mail.google.com/mail/u/0/#all/{r['msgid']}" if r["msgid"] else ""
        it = _base(r["email"], "resposta", r["data"] or "", cid=r["campaign_id"],
                   etapa=r["etapa"] or "", detalhe=det, gmail=gmail,
                   classe=classe, acao=acao)
        if it:
            itens.append(it)

    c.close()
    stats = {"abertura": 0, "clique": 0, "resposta": 0, "descadastro": 0}
    for it in itens:
        if it["kind"] in stats:
            stats[it["kind"]] += 1
    if tipo != "todos":
        itens = [it for it in itens if it["kind"] == tipo]
    itens.sort(key=lambda x: x.get("quando") or "", reverse=True)
    total = len(itens)
    itens = itens[:limit]
    return {
        "total": sum(stats.values()),
        "filtrados": total,
        "mostrados": len(itens),
        "tipo": tipo,
        "campaign_id": cid_filtro,
        "q": q,
        "stats": stats,
        "itens": itens,
    }


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


# ---------------- agentes / tokens / conhecimento ----------------

@app.get("/api/agentes")
def agentes_catalogo():
    return {"agentes": agent_usage.catalog_with_status(),
            "vertex": vertex.vertex_status(),
            "grok": grok.grok_status()}


@app.get("/api/agentes/resumo")
def agentes_resumo(days: int = 30):
    return agent_usage.resumo(days=max(1, min(days, 365)))


@app.get("/api/agentes/runs")
def agentes_runs(limit: int = 80, agent: str = "", campaign_id: Optional[int] = None):
    return {"runs": agent_usage.list_runs(limit=max(1, min(limit, 500)),
                                          agent=agent, campaign_id=campaign_id)}


@app.get("/api/agentes/knowledge")
def agentes_knowledge(limit: int = 40, agent: str = ""):
    return {"items": agent_usage.list_knowledge(limit=max(1, min(limit, 200)), agent=agent)}


class KnowledgeBody(BaseModel):
    agent: str = "manual"
    titulo: str
    corpo: str
    campaign_id: Optional[int] = None
    tags: str = ""


@app.post("/api/agentes/knowledge")
def agentes_knowledge_save(b: KnowledgeBody):
    kid = agent_usage.save_knowledge(
        agent=b.agent, titulo=b.titulo, corpo=b.corpo,
        campaign_id=b.campaign_id, tags=b.tags)
    return {"ok": True, "id": kid}


# ---------------- melhorias de campanha (orquestrador) ----------------

class ImprovementRunBody(BaseModel):
    mode: str = "review"  # auto | review | selective
    suggestion_ids: Optional[List[str]] = None
    replace: bool = False  # cancela run ativo e inicia outro


class TaskDecisionBody(BaseModel):
    reason: str = ""
    feedback: str = ""
    edited_patch: Optional[dict] = None


class RollbackBody(BaseModel):
    campaign_version_id: int


@app.get("/api/campaigns/{cid}/improvements/suggestions")
def campaign_improvement_suggestions(cid: int):
    return {"suggestions": improvements.collect_suggestions(cid),
            "enabled": improvements.improvements_enabled()}


@app.get("/api/campaigns/{cid}/improvements/runs")
def campaign_improvement_runs(cid: int):
    rescued = improvements.rescue_stuck_improvement_runs(max_age_sec=180)
    return {"runs": improvements.list_runs_for_campaign(cid),
            "active": improvements.active_run_for_campaign(cid),
            "rescued": rescued}


@app.post("/api/campaigns/{cid}/improvements/runs")
def campaign_improvement_start(cid: int, b: ImprovementRunBody):
    blocked = _guard_campaign_mutation(cid)
    if blocked:
        return blocked
    try:
        return improvements.start_run(
            cid, mode=b.mode or "review",
            suggestion_ids=b.suggestion_ids, replace=bool(b.replace))
    except ValueError as ex:
        return JSONResponse({"erro": str(ex)}, status_code=400)
    except RuntimeError as ex:
        return JSONResponse({"erro": str(ex)}, status_code=409)


@app.get("/api/campaigns/{cid}/improvements/runs/{rid}")
def campaign_improvement_run_get(cid: int, rid: int):
    run = improvements.run_get(rid)
    if not run or run.get("campaign_id") != cid:
        return JSONResponse({"erro": "run não encontrado"}, status_code=404)
    return run


class ImprovementCancelBody(BaseModel):
    abandon_approvals: bool = False
    reason: str = ""


@app.post("/api/improvements/runs/{rid}/cancel")
def improvement_run_cancel(rid: int, b: ImprovementCancelBody = ImprovementCancelBody()):
    blocked = _guard_improvement_run_mutation(rid)
    if blocked:
        return blocked
    try:
        return improvements.cancel_run(
            rid,
            reason=b.reason or "cancelado pelo usuário",
            abandon_approvals=bool(b.abandon_approvals),
        )
    except ValueError as ex:
        return JSONResponse({"erro": str(ex)}, status_code=404)


@app.post("/api/improvements/tasks/{tid}/approve")
def improvement_task_approve(tid: int, b: TaskDecisionBody = TaskDecisionBody()):
    blocked = _guard_improvement_task_mutation(tid)
    if blocked:
        return blocked
    try:
        return improvements.apply_task(tid, approved_by="dashboard",
                                       edited_patch=b.edited_patch)
    except ValueError as ex:
        return JSONResponse({"erro": str(ex)}, status_code=400)
    except RuntimeError as ex:
        return JSONResponse({"erro": str(ex)}, status_code=409)


@app.post("/api/improvements/tasks/{tid}/reject")
def improvement_task_reject(tid: int, b: TaskDecisionBody = TaskDecisionBody()):
    blocked = _guard_improvement_task_mutation(tid)
    if blocked:
        return blocked
    try:
        return improvements.reject_task(tid, reason=b.reason or "")
    except ValueError as ex:
        return JSONResponse({"erro": str(ex)}, status_code=404)


@app.post("/api/improvements/tasks/{tid}/retry")
def improvement_task_retry(tid: int, b: TaskDecisionBody = TaskDecisionBody()):
    blocked = _guard_improvement_task_mutation(tid)
    if blocked:
        return blocked
    try:
        return improvements.retry_task(tid, feedback=b.feedback or b.reason or "")
    except (ValueError, RuntimeError) as ex:
        return JSONResponse({"erro": str(ex)}, status_code=400)


@app.post("/api/improvements/tasks/{tid}/human-done")
def improvement_task_human_done(tid: int, b: TaskDecisionBody = TaskDecisionBody()):
    blocked = _guard_improvement_task_mutation(tid)
    if blocked:
        return blocked
    try:
        return improvements.mark_human_done(tid, note=b.reason or b.feedback or "")
    except ValueError as ex:
        return JSONResponse({"erro": str(ex)}, status_code=400)


@app.post("/api/improvements/tasks/{tid}/convert-copy")
def improvement_task_convert_copy(tid: int, b: TaskDecisionBody = TaskDecisionBody()):
    blocked = _guard_improvement_task_mutation(tid)
    if blocked:
        return blocked
    try:
        return improvements.convert_to_copy_task(tid, feedback=b.feedback or b.reason or "")
    except ValueError as ex:
        return JSONResponse({"erro": str(ex)}, status_code=400)
    except RuntimeError as ex:
        return JSONResponse({"erro": str(ex)}, status_code=409)


@app.get("/api/campaigns/{cid}/versions")
def campaign_versions(cid: int):
    return {"versions": improvements.list_versions(cid)}


@app.post("/api/campaigns/{cid}/rollback")
def campaign_rollback(cid: int, b: RollbackBody):
    blocked = _guard_campaign_mutation(cid)
    if blocked:
        return blocked
    try:
        return improvements.rollback_campaign(cid, b.campaign_version_id)
    except ValueError as ex:
        return JSONResponse({"erro": str(ex)}, status_code=400)


@app.post("/api/campaigns/{cid}/claim-local")
def campaign_claim_local(cid: int):
    """No ambiente local: importa campanha de origem remota e passa a ser a fonte editável."""
    if db.dashboard_env() != "local":
        return JSONResponse(
            {"erro": "Importar como local só faz sentido no ambiente local.",
             "dashboard_env": db.dashboard_env()},
            status_code=403,
        )
    c = db.conn()
    camp = c.execute("SELECT id, origem_ambiente FROM campaigns WHERE id=?", (cid,)).fetchone()
    if not camp:
        c.close()
        return JSONResponse({"erro": "campanha não encontrada"}, status_code=404)
    origem = _campaign_origem(camp)
    if origem == "local":
        c.close()
        return {"id": cid, "origem_ambiente": "local", "ja_local": True,
                "nota": "Campanha já é de origem local."}
    c.execute("UPDATE campaigns SET origem_ambiente='local', atualizada_em=? WHERE id=?",
              (db.now(), cid))
    c.commit()
    c.close()
    return {"id": cid, "origem_ambiente": "local", "ja_local": False,
            "nota": "Campanha importada como local — agora pode editar/ativar neste Mac."}


@app.get("/api/context/preview")
def context_preview(objetivo: str = "", business_unit: str = "", market: str = "", language: str = ""):
    try:
        return ai.business_context.resolve(objective=objetivo, business_unit=business_unit,
                                           market=market, language=language)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


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
