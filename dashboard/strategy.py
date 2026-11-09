#!/usr/bin/env python3
"""Ciclo local de estratégia do Yoobe.mail.

Não invoca skills do Codex nem cria/envia campanhas: consolida as capacidades
existentes do dashboard em um retrato seguro e auditável para o time comercial.
"""
import json

import ai
import db
import ga4
import grok
import sync
import google_media


SEO_GEO_THEMES = (
    ("programa de incentivo", "Como estruturar um programa de incentivo corporativo que escala"),
    ("premiação corporativa", "Premiação corporativa: formatos, regras e governança"),
    ("loyalty", "Plataforma de loyalty B2B: quando usar e como integrar"),
    ("gamificação", "Gamificação corporativa: métricas, exemplos e erros comuns"),
    ("carteira digital", "Carteira digital de benefícios e rewards: arquitetura e operação"),
    ("API de rewards", "API de rewards: integração, webhooks e segurança"),
    ("catálogo de prêmios", "Catálogo de prêmios corporativos: curadoria, logística e controle"),
    ("fulfillment", "Fulfillment de premiação: do pedido à entrega com rastreabilidade"),
)


def _num(value):
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def seo_geo_run(days=28):
    """Gera uma pauta auditável de SEO/GEO a partir dos dados já conectados.

    O ciclo é deliberadamente de recomendação: mede, prioriza e registra o que
    deve ser revisado. Nunca publica páginas, altera metadados ou cria conteúdo
    no site sem revisão humana explícita.
    """
    base = {
        "ativo": True, "dias": days, "atualizado_em": db.now(),
        "modo": "recomendação com aprovação humana",
        "guardrail": "O agente mede e prioriza; não publica conteúdo nem altera SEO técnico automaticamente.",
        "metricas": {}, "prioridades": [], "pauta": [], "erros": [],
    }
    overview = ga4.site_overview(days=days, site="plataforma")
    paginas = ga4.site_drilldown("paginas", days=days, site="plataforma")
    leads = ga4.site_drilldown("leads", days=days, site="plataforma")
    if overview.get("erro"):
        base["erros"].append("GA4: " + str(overview["erro"])[:180])
    if paginas.get("erro"):
        base["erros"].append("Páginas: " + str(paginas["erro"])[:180])
    if leads.get("erro"):
        base["erros"].append("Leads: " + str(leads["erro"])[:180])
    if not overview.get("configurado"):
        base["erros"].append("GA4 ainda não está configurado para o ciclo SEO/GEO.")
        return base

    totals = overview.get("totais") or {}
    events = overview.get("key_events") or {}
    base["metricas"] = {
        "usuarios": _num(totals.get("usuarios")), "sessoes": _num(totals.get("sessoes")),
        "pageviews": _num(totals.get("pageviews")), "key_events": _num(totals.get("key_events")),
        "leads": sum(_num(events.get(k)) for k in ga4.KEY_EVENTS),
    }

    # Linhas de página: landing page, page path, sessões, views, usuários, key events.
    ranking = []
    for row in paginas.get("linhas") or []:
        if len(row) < 6:
            continue
        landing, path = str(row[0] or "/"), str(row[1] or "/")
        sessoes, views, usuarios, conversoes = map(_num, row[2:6])
        page = landing if landing not in {"", "(not set)", "(não definido)"} else path
        if page in {"", "(not set)", "(não definido)"}:
            page = "/"
        ranking.append({"pagina": page, "sessoes": sessoes, "views": views,
                        "usuarios": usuarios, "conversoes": conversoes})
    ranking.sort(key=lambda x: (x["sessoes"], x["views"]), reverse=True)
    base["paginas_monitoradas"] = ranking[:10]
    for p in ranking[:5]:
        if p["sessoes"] >= 20 and not p["conversoes"]:
            base["prioridades"].append({
                "tipo": "conversão", "pagina": p["pagina"], "prioridade": "alta",
                "acao": "Revisar proposta de valor, CTA, prova social e FAQ desta página.",
                "evidencia": f"{p['sessoes']} sessões e 0 key events em {days} dias.",
            })
        elif p["sessoes"]:
            base["prioridades"].append({
                "tipo": "otimização", "pagina": p["pagina"], "prioridade": "média",
                "acao": "Atualizar resposta direta, entidades, FAQ e links internos; manter o CTA medido.",
                "evidencia": f"{p['sessoes']} sessões e {p['conversoes']} key events em {days} dias.",
            })

    # Fontes e eventos determinam se o gargalo é descoberta, conteúdo ou conversão.
    fontes = overview.get("fontes") or []
    organic = sum(_num(x.get("sessoes")) for x in fontes
                  if "organic" in (str(x.get("medium")) + " " + str(x.get("fonte"))).lower())
    base["metricas"]["sessoes_organicas"] = organic
    if organic == 0:
        base["prioridades"].append({
            "tipo": "descoberta", "pagina": "site", "prioridade": "alta",
            "acao": "Validar Google Search Console, indexação, sitemap, canonicals e cobertura de páginas estratégicas.",
            "evidencia": "GA4 não identificou sessões orgânicas no período analisado.",
        })

    lead_events = sum(_num(row[-2]) for row in (leads.get("linhas") or []) if len(row) >= 2)
    if lead_events == 0:
        base["prioridades"].append({
            "tipo": "tracking", "pagina": "site", "prioridade": "alta",
            "acao": "Validar no GTM/GA4 os eventos generate_lead, schedule_demo, contact_whatsapp e contact_email.",
            "evidencia": "Nenhum evento de lead atribuído nas dimensões de aquisição/página.",
        })

    # Pauta fixa de autoridade, rotacionada para não gerar ruído e sempre ligada a uma intenção.
    cursor = int(db.meta_get("seo_geo_theme_cursor", "0") or 0)
    for i in range(3):
        tema, titulo = SEO_GEO_THEMES[(cursor + i) % len(SEO_GEO_THEMES)]
        base["pauta"].append({
            "tema": tema, "titulo": titulo,
            "formato": "guia + FAQ + exemplos verificáveis",
            "checklist_geo": "resposta direta, público, quando usar, integração, limites, métricas e fontes/case real",
            "status": "sugerido — requer revisão e publicação manual",
        })
    db.meta_set("seo_geo_theme_cursor", (cursor + 3) % len(SEO_GEO_THEMES))
    # Mostra no máximo as ações de maior impacto, sem esconder a auditoria de páginas.
    ordem = {"alta": 0, "média": 1, "baixa": 2}
    base["prioridades"].sort(key=lambda x: ordem.get(x["prioridade"], 9))
    base["prioridades"] = base["prioridades"][:6]
    base["recomendacoes_grok"] = _seo_geo_grok_recs(base)
    return base


def _seo_geo_grok_recs(snapshot: dict) -> list:
    """2–3 recomendações narrativas via Grok (só sugestão; nunca publica)."""
    if not grok.enabled():
        return []
    try:
        payload = {
            "metricas": snapshot.get("metricas") or {},
            "prioridades": snapshot.get("prioridades") or [],
            "pauta": snapshot.get("pauta") or [],
            "paginas_monitoradas": (snapshot.get("paginas_monitoradas") or [])[:5],
            "dias": snapshot.get("dias") or 28,
        }
        out = grok.generate_json(
            f"""Você é o crítico SEO/GEO da Yoobe (B2B rewards / loyalty / incentivos).
Com base no snapshot GA4 abaixo, devolva SOMENTE JSON:
{{"recomendacoes":[{{"titulo":"...","acao":"1-2 frases práticas","prioridade":"alta|média|baixa","evidencia":"dado do snapshot"}}]}}
Regras: no máximo 3 itens; não invente métricas; não proponha publicar/alterar o site
automaticamente — só recomendações para revisão humana.

SNAPSHOT:
{json.dumps(payload, ensure_ascii=False)}""",
            timeout=90, agent="grok_critic", task_type="seo_geo_recs",
            detalhe="seo_geo_recomendacoes",
        )
        recs = out.get("recomendacoes") or []
        clean = []
        for r in recs[:3]:
            if not isinstance(r, dict):
                continue
            clean.append({
                "titulo": str(r.get("titulo") or "")[:160],
                "acao": str(r.get("acao") or "")[:400],
                "prioridade": str(r.get("prioridade") or "média")[:20],
                "evidencia": str(r.get("evidencia") or "")[:240],
                "via": out.get("_via") or "grok",
            })
        return clean
    except Exception as ex:
        return [{"titulo": "Grok indisponível neste ciclo",
                 "acao": "Prioridades heurísticas GA4 seguem válidas; crítica Grok será tentada no próximo ciclo.",
                 "prioridade": "baixa",
                 "evidencia": str(ex)[:180],
                 "via": "fallback"}]


def run(reason="periodic", perform_sync=False):
    """Atualiza dados, saúde do CRM e prontidão de IA; retorna o snapshot salvo."""
    db.init()
    sync_result = sync.sync_all() if perform_sync else {}
    tags_corrigidas = db.backfill_respondeu_tags()
    c = db.conn()
    # Listas técnicas/opt-out são retenção e supressão, não público estratégico.
    emails = [r["email"] for r in c.execute(
        "SELECT email FROM contacts WHERE ','||tags||',' NOT LIKE '%,nao-contatar,%'").fetchall()]
    ativos = c.execute("SELECT COUNT(*) AS n FROM campaigns WHERE status='ativa'").fetchone()["n"]
    rascunhos = c.execute("SELECT COUNT(*) AS n FROM campaigns WHERE status='rascunho'").fetchone()["n"]
    c.close()
    cohort = db.resumo_audiencia(emails)
    total = cohort.get("total", 0)
    faltam_telefone = max(0, total - cohort.get("com_telefone", 0))
    faltam_historico = max(0, total - cohort.get("com_historico", 0))
    # GA4/GTM/Ads em leitura: atualiza o painel sem alterar campanhas ou tags.
    # Erros de mídia são devolvidos no snapshot e nunca interrompem o ciclo comercial.
    media = google_media.sync(days=30)
    seo_geo = seo_geo_run(days=28)
    snapshot = {
        "atualizado_em": db.now(), "motivo": reason, "sync": sync_result,
        "tags_corrigidas": tags_corrigidas, "campanhas_ativas": ativos,
        "campanhas_rascunho": rascunhos, "audiencia": cohort,
        "qualidade": {
            "telefone_preenchido": cohort.get("com_telefone", 0),
            "historico_preenchido": cohort.get("com_historico", 0),
            "faltam_telefone": faltam_telefone, "faltam_historico": faltam_historico,
        },
        "claude": ai.ai_status(),   # chave mantida por compat; reflete o provider ativo (vertex/claude)
        "midia_google": media,
        "seo_geo": seo_geo,
        "guardrail": "Ciclo apenas atualiza dados e estratégia; não gera, aprova, ativa ou envia campanhas.",
    }
    db.meta_set("strategy_loop", json.dumps(snapshot, ensure_ascii=False))
    db.meta_set("seo_geo_loop", json.dumps(seo_geo, ensure_ascii=False))
    return snapshot


def status():
    """Leitura barata do snapshot. Nunca reexecuta o ciclo no page load remoto.

    Rodar strategy.run() aqui bloqueava a home por dezenas de segundos (GA4 +
    resumo de ~60k contacts). O ciclo só roda no startup em background, no
    POST /api/strategy/run ou nos loops periódicos.
    """
    raw = db.meta_get("strategy_loop", "")
    if not raw:
        return {"atualizado_em": "", "motivo": "ainda não gerado",
                "guardrail": "Clique em Atualizar estratégia para gerar o primeiro snapshot."}
    try:
        return json.loads(raw)
    except Exception:
        return {"atualizado_em": "", "motivo": "snapshot inválido",
                "erro": "meta strategy_loop corrompida — rode POST /api/strategy/run"}


def seo_geo_status():
    """Leitura barata do snapshot SEO/GEO — mesmo guardrail de status()."""
    raw = db.meta_get("seo_geo_loop", "")
    if not raw:
        return {"ativo": False, "motivo": "ainda não gerado",
                "guardrail": "Rode o ciclo de estratégia para gerar a pauta SEO/GEO."}
    try:
        return json.loads(raw)
    except Exception:
        return {"ativo": False, "motivo": "snapshot inválido",
                "erro": "meta seo_geo_loop corrompida — rode POST /api/seo-geo/run"}
