#!/usr/bin/env python3
"""4unik.mail — validação/enriquecimento de leads (Firecrawl econômico + Vertex).

Orquestra: scrape_planner → Firecrawl (≤1 crédito) → lead_validator → CRM.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Optional

import requests

import agent_engine
import db
import firecrawl
import nomes
import vertex

PROMPT_VALIDATOR = """Você é um validador de QUALIDADE DE LEADS B2B da 4Unik (Brasil).
Com base nos DADOS DO CRM e na EVIDÊNCIA PÚBLICA (Firecrawl), avalie se o contato
ainda é útil para e-mail comercial. Não invente fatos fora da evidência.

Devolva SOMENTE JSON válido (um objeto por contato, ou {"resultados":[...]} se lote):
{"email":"...","status":"valido|duvidoso|desatualizado|invalido","na_empresa":true,
 "nome_sugerido":"","cargo_sugerido":"","empresa_sugerida":"","confianca":0.0,
 "motivo":"curto","fontes":[{"url":"","evidencia":""}],
 "acao_recomendada":"manter|enriquecer|suprimir|revisar",
 "sugestoes":{"nome":"","empresa":"","telefone":"","cargo":"","enriquecimento":""}}

REGRAS:
- "invalido": e-mail claramente genérico/errado ou domínio morto sem presença.
- "desatualizado": evidência forte de que a pessoa NÃO está mais na empresa → acao_recomendada=suprimir.
- "duvidoso": pouca evidência; não suprima.
- "valido": evidência compatível ou ausência de sinal negativo com dados CRM coerentes.
- Não invente telefone pessoal; telefone só se corporativo publicado na evidência.
- fontes[].url só de URLs presentes na evidência.
- confianca alta só com evidência clara."""

PROMPT_PLANNER = """Você planeja o uso MÍNIMO de Firecrawl para validar um lead B2B.
Orçamento apertado: preferir 0 ou 1 crédito. Devolva SOMENTE JSON:
{"usar_firecrawl": false, "modo": "skip|domain_only|search_one",
 "query": "", "url_preferida": "https://empresa.com.br", "motivo": "curto"}

REGRAS:
- free-mail → usar_firecrawl=false, modo=skip
- se has_cache=true → skip
- domínio corporativo sem cache → usar_firecrawl=true, modo=domain_only,
  url_preferida=https://{dominio}
- search_one só se domínio for genérico e empresa tiver nome claro
- Nunca peça mais de uma URL."""


def _ttl() -> int:
    return firecrawl.evidence_ttl_days()


def _meta_set_validacao(**kw):
    p = {}
    raw = db.meta_get("leads_validacao", "")
    if raw:
        try:
            p = json.loads(raw)
        except Exception:
            p = {}
    p.update(kw)
    p["firecrawl_credits_used"] = firecrawl.credits_used()
    p["firecrawl_credits_remaining"] = firecrawl.credits_remaining()
    db.meta_set("leads_validacao", json.dumps(p, ensure_ascii=False))


def progresso_validacao() -> dict:
    raw = db.meta_get("leads_validacao", "")
    base = {"status": "idle",
            "firecrawl_credits_used": firecrawl.credits_used(),
            "firecrawl_credits_remaining": firecrawl.credits_remaining(),
            "firecrawl_credit_budget": firecrawl.credit_budget()}
    if not raw:
        return base
    try:
        return {**base, **json.loads(raw)}
    except Exception:
        return base


def _worker_url() -> str:
    return (vertex.env_get("LEAD_QUALITY_WORKER_URL", "") or "").strip().rstrip("/")


def plan_scrape(contato: dict) -> dict:
    """Decide skip / domain_only / search_one. Regras de código + Vertex flash."""
    email = (contato.get("email") or "").strip().lower()
    domain = firecrawl.domain_from_email(email)
    has_email_cache = db.get_lead_evidence(email, max_age_days=_ttl()) is not None
    has_domain_cache = firecrawl.get_domain_cache(domain) is not None

    if firecrawl.is_free_mail(email):
        return {"usar_firecrawl": False, "modo": "skip", "query": "",
                "url_preferida": "", "motivo": "free-mail"}
    if has_email_cache or has_domain_cache:
        return {"usar_firecrawl": False, "modo": "skip", "query": "",
                "url_preferida": f"https://{domain}" if domain else "",
                "motivo": "cache fresco (email ou domínio)"}
    if not firecrawl.budget_ok(1):
        return {"usar_firecrawl": False, "modo": "skip", "query": "",
                "url_preferida": "", "motivo": "budget Firecrawl esgotado"}

    default = {
        "usar_firecrawl": True,
        "modo": "domain_only",
        "query": "",
        "url_preferida": f"https://{domain}" if domain else "",
        "motivo": "default domain_only",
    }
    crm = {
        "email": email, "nome": contato.get("nome", ""),
        "empresa": contato.get("empresa", ""), "domain": domain,
        "has_cache": False, "budget_remaining": firecrawl.credits_remaining(),
    }
    try:
        res = agent_engine.scrape_planner_json(
            f"{PROMPT_PLANNER}\n\nCONTATO:\n{json.dumps(crm, ensure_ascii=False)}",
            timeout=45)
    except Exception:
        return default

    modo = res.get("modo") if res.get("modo") in ("skip", "domain_only", "search_one") else "domain_only"
    usar = bool(res.get("usar_firecrawl")) and modo != "skip"
    url = (res.get("url_preferida") or default["url_preferida"] or "").strip()
    if not url.startswith("http") and domain:
        url = f"https://{domain}"
    return {
        "usar_firecrawl": usar,
        "modo": modo if usar else "skip",
        "query": (res.get("query") or "")[:200],
        "url_preferida": url,
        "motivo": (res.get("motivo") or "")[:120],
    }


def enqueue_scrape_jobs(job_id: str, contacts: list[dict]) -> dict:
    """Enfileira scrapes no Worker remoto (cada contato já traz plano)."""
    url = _worker_url()
    if not url or not contacts:
        return {"queued": 0, "mode": "none"}
    api_key = vertex.env_get("API_KEY", "") or ""
    sync_key = (vertex.env_get("LEAD_QUALITY_SYNC_KEY", "") or
                vertex.env_get("TRACKING_SYNC_KEY", "") or api_key)
    payload_contacts = []
    for c in contacts:
        if not c.get("email"):
            continue
        plan = c.get("_plan") or plan_scrape(c)
        if not plan.get("usar_firecrawl"):
            continue
        payload_contacts.append({
            "email": c["email"],
            "nome": c.get("nome", ""),
            "empresa": c.get("empresa", ""),
            "domain": firecrawl.domain_from_email(c["email"]),
            "modo": plan.get("modo", "domain_only"),
            "query": plan.get("query", ""),
            "url_preferida": plan.get("url_preferida", ""),
        })
    if not payload_contacts:
        return {"queued": 0, "mode": "none", "motivo": "nenhum contato precisa de scrape"}
    try:
        r = requests.post(
            f"{url}/enqueue",
            headers={"Content-Type": "application/json",
                     "X-Sync-Key": sync_key,
                     "X-API-Key": api_key},
            json={"job_id": job_id, "contacts": payload_contacts},
            timeout=30,
        )
        if r.status_code >= 400:
            return {"queued": 0, "mode": "error", "erro": r.text[:200]}
        data = r.json() if r.text else {}
        return {"queued": int(data.get("queued") or len(payload_contacts)),
                "mode": "queue", **{k: data[k] for k in data if k != "queued"}}
    except Exception as exc:
        return {"queued": 0, "mode": "error", "erro": str(exc)[:200]}


def resolve_contacts(origem: str = None, tags: str = None, segmento: str = None,
                     emails: list = None, campaign_id: int = None) -> list[dict]:
    c = db.conn()
    if emails:
        ems = [e.strip().lower() for e in emails if (e or "").strip()]
        if not ems:
            c.close()
            return []
        placeholders = ",".join("?" * len(ems))
        rows = c.execute(
            f"SELECT email,nome,empresa,cargo,telefone,tags,validacao_status,validado_em "
            f"FROM contacts WHERE email IN ({placeholders})", ems).fetchall()
    elif campaign_id:
        rows = c.execute(
            """SELECT ct.email,ct.nome,ct.empresa,ct.cargo,ct.telefone,ct.tags,
                      ct.validacao_status,ct.validado_em
               FROM campaign_contacts cc JOIN contacts ct ON ct.email=cc.email
               WHERE cc.campaign_id=?""", (campaign_id,)).fetchall()
    else:
        sql = ("SELECT email,nome,empresa,cargo,telefone,tags,validacao_status,validado_em "
               "FROM contacts WHERE 1=1")
        args = []
        if origem:
            sql += " AND origem=?"
            args.append(origem)
        if segmento:
            sql += " AND segmento=?"
            args.append(segmento)
        rows = c.execute(sql, args).fetchall()
    c.close()
    tagset = set(db.tags_list(tags)) if tags else set()
    out = []
    for r in rows:
        d = dict(r)
        if tagset and not (tagset & set(db.tags_list(d.get("tags", "")))):
            continue
        out.append(d)
    return out


def needs_fresh_evidence(email: str) -> bool:
    if db.get_lead_evidence(email, max_age_days=_ttl()) is not None:
        return False
    domain = firecrawl.domain_from_email(email)
    if firecrawl.get_domain_cache(domain) is not None:
        return False
    return True


def _evidence_thin(evidence: Optional[dict]) -> bool:
    """True se não há página/hit útil (homepage falhou ou skip)."""
    if not evidence:
        return True
    pages = evidence.get("pages") or []
    hits = evidence.get("search_hits") or []
    has_md = any((p.get("markdown") or "").strip() for p in pages)
    return not has_md and not hits


def _search_one_plan(contato: dict, domain: str) -> dict:
    empresa = (contato.get("empresa") or "").strip()
    nome = (contato.get("nome") or "").strip()
    query = f"{empresa or domain} {nome}".strip() if domain else (empresa or nome)
    if domain:
        query = f"{empresa or domain} site:{domain}".strip()
    return {
        "usar_firecrawl": True,
        "modo": "search_one",
        "query": query,
        "url_preferida": f"https://{domain}" if domain else "",
        "motivo": "escalada: domain_only sem página útil",
    }


def _resolve_evidence(contato: dict, plan: Optional[dict] = None,
                      escalate_search: bool = False) -> Optional[dict]:
    """Resolve evidência (cache → scrape). Se escalate_search e homepage vazia
    com nome suspeito/vazio, tenta search_one uma vez (1 crédito extra)."""
    email = (contato.get("email") or "").strip().lower()
    ev = db.get_lead_evidence(email, max_age_days=_ttl())
    if ev:
        return ev.get("payload")
    domain = firecrawl.domain_from_email(email)
    dcache = firecrawl.get_domain_cache(domain)
    if dcache and dcache.get("payload"):
        payload = dict(dcache["payload"])
        payload["email"] = email
        payload["from_domain_cache"] = True
        db.save_lead_evidence(email, payload, urls=payload.get("urls") or [])
        return payload
    plan = plan or plan_scrape(contato)
    if not plan.get("usar_firecrawl") or not firecrawl.disponivel():
        return None
    evidence = firecrawl.gather_lead_evidence(
        nome=contato.get("nome", ""), empresa=contato.get("empresa", ""),
        email=email, plan=plan)
    # Escalada: domain_only falhou + lead precisa de nome → search_one
    if (escalate_search and _evidence_thin(evidence)
            and (plan.get("modo") or "") == "domain_only"
            and not firecrawl.is_free_mail(email)
            and firecrawl.budget_ok(1)):
        nome_ok, _ = nomes.suspeito(
            contato.get("nome"), email, contato.get("empresa", ""))
        if nome_ok or not (contato.get("nome") or "").strip():
            plan2 = _search_one_plan(contato, domain)
            evidence = firecrawl.gather_lead_evidence(
                nome=contato.get("nome", ""), empresa=contato.get("empresa", ""),
                email=email, plan=plan2)
    if evidence and (evidence.get("pages") or evidence.get("search_hits")):
        db.save_lead_evidence(email, evidence, urls=evidence.get("urls") or [])
    return evidence


def validate_one(contato: dict, evidence: Optional[dict] = None) -> dict:
    email = (contato.get("email") or "").strip().lower()
    plan = None
    if evidence is None:
        plan = plan_scrape(contato)
        evidence = _resolve_evidence(contato, plan=plan)

    crm = {k: (contato.get(k) or "") for k in (
        "email", "nome", "empresa", "cargo", "telefone")}
    prompt = (
        f"{PROMPT_VALIDATOR}\n\nDADOS DO CRM:\n{json.dumps(crm, ensure_ascii=False)}\n\n"
        f"EVIDÊNCIA PÚBLICA:\n{firecrawl.evidence_text_for_agent(evidence)}"
    )
    try:
        res = agent_engine.lead_validator_json(prompt, timeout=180)
    except Exception as exc:
        res = {"email": email, "status": "duvidoso", "na_empresa": True,
               "confianca": 0.0, "motivo": f"IA indisponível: {str(exc)[:80]}",
               "acao_recomendada": "revisar", "fontes": [], "sugestoes": {}}

    if res.get("resultados"):
        for item in res["resultados"]:
            if (item.get("email") or "").strip().lower() == email:
                res = item
                break

    status = res.get("status") if res.get("status") in (
        "valido", "duvidoso", "desatualizado", "invalido") else "duvidoso"
    # Sem evidência e free-mail / budget → duvidoso (não inventar invalido)
    if not evidence and status == "valido":
        status = "duvidoso"
        res["motivo"] = (res.get("motivo") or "") + " (sem evidência pública)"
    acao = res.get("acao_recomendada") or "manter"
    if acao == "suprimir" and status not in ("desatualizado", "invalido"):
        status = "desatualizado"
    motivo = (res.get("motivo") or "")[:240]
    db.set_validacao_contato(email, status, motivo)
    res["email"] = email
    res["status"] = status
    if plan:
        res["scrape_plan"] = plan
    return res


def apply_evidence_and_validate(email: str, payload: dict, job_id: str = "") -> dict:
    email = (email or "").strip().lower()
    urls = payload.get("urls") or []
    # Worker pode ter gasto créditos remotamente — registrar se informado
    spent = int(payload.get("credits_spent") or 0)
    if spent > 0:
        # credits já contabilizados no Worker local sync; no remoto o dashboard
        # incrementa quando recebe (Worker não tem meta SQLite)
        cur = firecrawl.credits_used()
        db.meta_set("firecrawl_credits_used", str(cur + spent))
    db.save_lead_evidence(email, payload, urls=urls, job_id=job_id)
    domain = payload.get("domain") or firecrawl.domain_from_email(email)
    if payload.get("pages") and domain:
        firecrawl.save_domain_cache(domain, payload, urls=urls)
    c = db.conn()
    row = c.execute(
        "SELECT email,nome,empresa,cargo,telefone,tags FROM contacts WHERE email=?",
        (email,)).fetchone()
    c.close()
    contato = dict(row) if row else {"email": email, "nome": payload.get("nome", ""),
                                     "empresa": payload.get("empresa", "")}
    return validate_one(contato, evidence=payload)


def start_validacao(origem: str = None, tags: str = None, segmento: str = None,
                    emails: list = None, campaign_id: int = None) -> dict:
    prog = progresso_validacao()
    if prog.get("status") == "analisando":
        return {"erro": "já existe uma validação em andamento", "status": 409}

    contacts = resolve_contacts(origem=origem, tags=tags, segmento=segmento,
                                emails=emails, campaign_id=campaign_id)
    max_n = firecrawl.max_leads_per_job()
    if len(contacts) > max_n:
        return {
            "erro": (f"job com {len(contacts)} contatos excede "
                     f"FIRECRAWL_MAX_LEADS_PER_JOB={max_n}. "
                     "Filtre por emails/tag ou aumente o cap com cuidado (budget≈40)."),
            "status": 409, "total": len(contacts), "max": max_n,
        }
    if not firecrawl.budget_ok(1) and any(needs_fresh_evidence(c["email"]) for c in contacts):
        # ainda pode validar só com cache/Vertex
        pass

    job_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    stale = [c for c in contacts if needs_fresh_evidence(c["email"])]
    fresh = [c for c in contacts if not needs_fresh_evidence(c["email"])]

    _meta_set_validacao(
        status="analisando", job_id=job_id, total=len(contacts),
        stale=len(stale), fresh=len(fresh), feitos=0,
        etapa="iniciando…", erro="", contagens={},
    )

    def worker():
        try:
            feitos = 0
            contagens = {"valido": 0, "duvidoso": 0, "desatualizado": 0, "invalido": 0}

            for contato in fresh:
                res = validate_one(contato)
                contagens[res.get("status", "duvidoso")] = contagens.get(
                    res.get("status", "duvidoso"), 0) + 1
                feitos += 1
                _meta_set_validacao(feitos=feitos, etapa=f"validados {feitos}/{len(contacts)}",
                                    contagens=contagens)

            # Planeja scrapes (Vertex) e só enfileira quem precisa
            need_scrape = []
            skip_scrape = []
            for contato in stale:
                plan = plan_scrape(contato)
                contato = dict(contato)
                contato["_plan"] = plan
                if plan.get("usar_firecrawl"):
                    need_scrape.append(contato)
                else:
                    skip_scrape.append(contato)

            for contato in skip_scrape:
                res = validate_one(contato)
                contagens[res.get("status", "duvidoso")] = contagens.get(
                    res.get("status", "duvidoso"), 0) + 1
                feitos += 1
                _meta_set_validacao(feitos=feitos, etapa=f"validados {feitos}/{len(contacts)}",
                                    contagens=contagens)

            enq = enqueue_scrape_jobs(job_id, need_scrape)
            remaining = list(need_scrape)
            if enq.get("mode") == "queue" and enq.get("queued", 0) > 0:
                _meta_set_validacao(
                    etapa=f"scrape enfileirado ({enq['queued']})",
                    pending_scrape=enq["queued"], enqueue=enq)
                deadline = time.time() + min(90, 10 + len(remaining) * 4)
                pending = {c["email"].lower(): c for c in remaining}
                while pending and time.time() < deadline:
                    done = []
                    for em, contato in list(pending.items()):
                        if not needs_fresh_evidence(em):
                            res = validate_one(contato)
                            contagens[res.get("status", "duvidoso")] = contagens.get(
                                res.get("status", "duvidoso"), 0) + 1
                            feitos += 1
                            done.append(em)
                    for em in done:
                        pending.pop(em, None)
                    if done:
                        _meta_set_validacao(
                            feitos=feitos, etapa=f"validados {feitos}/{len(contacts)}",
                            contagens=contagens, pending_scrape=len(pending))
                    if pending:
                        time.sleep(2)
                remaining = list(pending.values())
            elif enq.get("mode") == "error":
                _meta_set_validacao(etapa=f"queue falhou, sync: {enq.get('erro','')[:80]}")

            for contato in remaining:
                res = validate_one(contato)
                contagens[res.get("status", "duvidoso")] = contagens.get(
                    res.get("status", "duvidoso"), 0) + 1
                feitos += 1
                _meta_set_validacao(feitos=feitos, etapa=f"validados {feitos}/{len(contacts)}",
                                    contagens=contagens)

            _meta_set_validacao(status="pronto", etapa="concluído", feitos=feitos,
                                contagens=contagens, pending_scrape=0)
        except Exception as ex:
            _meta_set_validacao(status="erro", erro=str(ex)[:300])

    threading.Thread(target=worker, daemon=True).start()
    return {"status": "analisando", "job_id": job_id, "total": len(contacts),
            "stale": len(stale), "fresh": len(fresh),
            "firecrawl_credits_remaining": firecrawl.credits_remaining()}


def enqueue_stale_for_campaign(campaign_id: int) -> dict:
    contacts = resolve_contacts(campaign_id=campaign_id)
    stale = [c for c in contacts if needs_fresh_evidence(c["email"])]
    # Cap rígido no activate residual
    stale = stale[: firecrawl.max_leads_per_job()]
    if not stale:
        return {"queued": 0, "stale": 0}
    for i, c in enumerate(stale):
        stale[i] = dict(c)
        stale[i]["_plan"] = plan_scrape(c)
    need = [c for c in stale if c["_plan"].get("usar_firecrawl")]
    job_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-act-" + uuid.uuid4().hex[:6]
    enq = enqueue_scrape_jobs(job_id, need)
    if enq.get("mode") != "queue" or not enq.get("queued"):
        for contato in need[:5]:  # sync sample mínimo
            try:
                validate_one(contato)
            except Exception:
                pass
        return {"queued": 0, "stale": len(stale), "sync_sample": min(5, len(need)),
                "enqueue": enq}
    return {"queued": enq.get("queued", 0), "stale": len(stale), "job_id": job_id,
            "enqueue": enq}


def campaign_validation_report(campaign_id: int) -> dict[str, Any]:
    contacts = resolve_contacts(campaign_id=campaign_id)
    validos, excluidos, duvidosos, stale = [], [], [], []
    for ct in contacts:
        email = ct["email"]
        tags = set(db.tags_list(ct.get("tags", "")))
        status = (ct.get("validacao_status") or "").strip().lower()
        if tags & set(db.TAGS_SUPRIMIDAS) or status in db.VALIDACAO_BLOQUEIA:
            excluidos.append(email)
        elif status == "duvidoso":
            duvidosos.append(email)
            validos.append(email)
        elif status == "valido" or (status == "" and not needs_fresh_evidence(email)):
            validos.append(email)
        elif status == "" and needs_fresh_evidence(email):
            stale.append(email)
            duvidosos.append(email)
            validos.append(email)
        else:
            validos.append(email)
    return {
        "total": len(contacts),
        "validos": len(validos),
        "excluidos": len(excluidos),
        "duvidosos": len(duvidosos),
        "stale": len(stale),
        "emails_validos": validos,
        "emails_excluidos": excluidos,
        "amostra_excluidos": excluidos[:8],
        "amostra_stale": stale[:8],
    }


def sugerir_enriquecimento(contato: dict) -> dict:
    email = (contato.get("email") or "").strip().lower()
    plan = plan_scrape(contato)
    evidence = _resolve_evidence(contato, plan=plan, escalate_search=True)
    res = validate_one(contato, evidence=evidence)
    sug = res.get("sugestoes") or {}
    fontes = res.get("fontes") or []
    if evidence and not fontes:
        for u in (evidence.get("urls") or [])[:3]:
            fontes.append({"url": u, "evidencia": "página pública coletada"})
    sugestoes = {
        "nome": sug.get("nome") or res.get("nome_sugerido") or "",
        "empresa": sug.get("empresa") or res.get("empresa_sugerida") or "",
        "telefone": sug.get("telefone") or "",
        "cargo": sug.get("cargo") or res.get("cargo_sugerido") or "",
        "porte_funcionarios": sug.get("porte_funcionarios") or "",
        "erp": sug.get("erp") or "",
        "etapa_funil": sug.get("etapa_funil") or "",
        "estado_negocio": sug.get("estado_negocio") or "",
        "qualificacao": sug.get("qualificacao") or "",
        "enriquecimento": sug.get("enriquecimento") or "",
    }
    # Campos críticos ainda vazios no CRM e sem sugestão da pesquisa
    nao_encontrado = []
    for campo in nomes.CAMPOS_CRITICOS:
        crm_val = (contato.get(campo) or "").strip()
        sug_val = (sugestoes.get(campo) or "").strip()
        if not crm_val and not sug_val:
            nao_encontrado.append(campo)
    return {
        "resumo": res.get("motivo") or "Pesquisa concluída.",
        "confianca": float(res.get("confianca") or 0),
        "sugestoes": sugestoes,
        "fontes": fontes,
        "nao_encontrado": nao_encontrado,
        "validacao_status": res.get("status"),
        "acao_recomendada": res.get("acao_recomendada"),
        "scrape_plan": plan,
        "firecrawl_credits_remaining": firecrawl.credits_remaining(),
    }
