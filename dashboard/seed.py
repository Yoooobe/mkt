#!/usr/bin/env python3
"""4unik.mail — seed de remetentes e campanha exemplo."""
import db

DEMO = "https://calendly.com/4unik/30min?month=2026-07"
API_PAGE = "https://plataforma.4unik.com.br/landing/api-integracoes/"

P = lambda t: f'<p style="margin:0 0 16px">{t}</p>'
H = lambda t: f'<h1 style="margin:0 0 18px;font-family:Montserrat,Arial,sans-serif;font-size:22px;font-weight:800;color:#0f1720;line-height:1.3">{t}</h1>'

EMAIL_1 = dict(
    nome="4Unik 1 — Dor operacional",
    assunto="{{empresa}} ainda opera reconhecimento com planilha?",
    preheader="A 4Unik junta gamificacao, loja corporativa, carteiras e fulfillment.",
    cta="Agendar demo da 4Unik",
    cta_url=DEMO,
    inner_html=(
        H('Reconhecimento bom nao deveria virar<br><span style="color:#1fbf9c">operacao manual.</span>')
        + P("Olá, {{nome}}. Tudo bem?")
        + P("Programas de incentivo comecam simples: uma meta, um premio, uma campanha interna. O problema aparece quando o RH precisa comprar, armazenar, enviar, rastrear e ainda provar ROI.")
        + P("A <b>4Unik</b> resolve essa camada como infraestrutura de recompensas: motor de campanhas, pontos/carteiras, loja corporativa, catalogo e fulfillment em uma unica operacao.")
        + P("Se fizer sentido para a <b>{{empresa}}</b>, marcamos uma demo curta para mapear um primeiro caso de uso.")),
    texto_plano=("Olá, {{nome}}. Tudo bem?\n\n"
                 "Programas de incentivo comecam simples: uma meta, um premio, uma campanha interna. "
                 "O problema aparece quando o RH precisa comprar, armazenar, enviar, rastrear e ainda provar ROI.\n\n"
                 "A 4Unik resolve essa camada como infraestrutura de recompensas: motor de campanhas, pontos/carteiras, "
                 "loja corporativa, catalogo e fulfillment em uma unica operacao.\n\n"
                 f"Podemos marcar uma demo curta? {DEMO}\n\n"
                 "Atenciosamente,\nTime 4Unik"),
    offset=0)

EMAIL_2 = dict(
    nome="4Unik 2 — API e stack",
    assunto="Recompensas reais como uma chamada de API",
    preheader="API REST, webhooks, sandbox, SDKs e fulfillment por tras da experiencia.",
    cta="Conhecer API e integrações",
    cta_url=API_PAGE,
    inner_html=(
        H('Da meta batida ao premio entregue,<br><span style="color:#1fbf9c">sem construir tudo do zero.</span>')
        + P("{{nome}}, a 4Unik pode operar como plataforma completa ou como camada API-first dentro do stack da {{empresa}}.")
        + P("Sua ferramenta dispara o evento; a 4Unik cuida de catalogo, saldo, checkout, envio, rastreio e webhooks de status.")
        + P("Isso funciona para RH, vendas, e-learning, eventos, comunidades e plataformas SaaS que querem oferecer recompensa fisica sem virar operacao logistica.")),
    texto_plano=("{{nome}}, a 4Unik pode operar como plataforma completa ou como camada API-first dentro do stack da {{empresa}}.\n\n"
                 "Sua ferramenta dispara o evento; a 4Unik cuida de catalogo, saldo, checkout, envio, rastreio e webhooks de status.\n\n"
                 f"API e integracoes: {API_PAGE}\n\n"
                 "Atenciosamente,\nTime 4Unik"),
    offset=4)

EMAIL_3 = dict(
    nome="4Unik 3 — ROI e demo",
    assunto="Como provar ROI de uma campanha de reconhecimento?",
    preheader="Budget, taxa de resgate, eNPS e SLA no painel da 4Unik.",
    cta="Conversar com especialista",
    cta_url=DEMO,
    inner_html=(
        H('Campanha sem dados vira custo.<br><span style="color:#1fbf9c">Campanha com dados vira decisao.</span>')
        + P("{{nome}}, o diferencial da 4Unik não é só entregar premios. É dar governanca para o programa: centros de custo, saldo, extrato, aprovacoes, taxa de resgate, SLA e relatorios.")
        + P("Se a {{empresa}} quer testar um programa de onboarding, vendas, treinamento ou reconhecimento interno, podemos desenhar um piloto com indicadores claros desde o primeiro dia.")
        + P("Vale uma conversa de 20 minutos?")),
    texto_plano=("{{nome}}, o diferencial da 4Unik não é só entregar premios. "
                 "É dar governanca para o programa: centros de custo, saldo, extrato, aprovacoes, taxa de resgate, SLA e relatorios.\n\n"
                 "Se a {{empresa}} quer testar um programa de onboarding, vendas, treinamento ou reconhecimento interno, "
                 "podemos desenhar um piloto com indicadores claros desde o primeiro dia.\n\n"
                 f"Agenda: {DEMO}\n\n"
                 "Atenciosamente,\nTime 4Unik"),
    offset=10)


def seed_campanha_reativacao():
    """Compatibilidade com app.py antigo: cria uma campanha exemplo da 4Unik."""
    if db.meta_get("seed_4unik_intro_2026"):
        return int(db.meta_get("seed_4unik_intro_2026"))
    c = db.conn()
    c.execute("""INSERT INTO campaigns(nome,objetivo,tipo,status,contexto) VALUES(?,?,?,?,?)""",
              ("4Unik Intro — Reward Infrastructure",
               "Campanha exemplo para apresentar a 4Unik como infraestrutura de recompensas: "
               "dor operacional, stack/API e ROI. Importar uma base propria antes de ativar.",
               "finita", "rascunho",
               "Baseado no site plataforma.4unik.com.br: reward infrastructure, gamificacao, "
               "carteiras, loja corporativa, API/webhooks, fulfillment e governanca."))
    cid = c.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
    for i, e in enumerate((EMAIL_1, EMAIL_2, EMAIL_3), start=1):
        c.execute("""INSERT INTO templates_ai(nome,assunto,preheader,cta,cta_url,inner_html,texto_plano,status,origem)
                     VALUES(?,?,?,?,?,?,?,'aprovado','seed')""",
                  (e["nome"], e["assunto"], e["preheader"], e["cta"], e["cta_url"],
                   e["inner_html"], e["texto_plano"]))
        tid = c.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
        c.execute("INSERT INTO campaign_steps(campaign_id,ordem,template_ref,offset_dias) VALUES(?,?,?,?)",
                  (cid, i, f"ai:{tid}", e["offset"]))
    c.commit()
    c.close()
    db.meta_set("seed_4unik_intro_2026", cid)
    print(f"campanha #{cid} '4Unik Intro — Reward Infrastructure' criada em rascunho")
    return cid


SENDERS = [
    ("comercial@4unik.com.br", "Comercial 4Unik", "Prospeccao e demos", "", "off"),
    ("contato@4unik.com.br", "4Unik", "Relacionamento e newsletters", "contato", "off"),
]


def seed_senders():
    c = db.conn()
    for email, nome, proposito, slug, auto in SENDERS:
        c.execute("""INSERT INTO senders(email,nome,proposito,slug,signature_url,ativo,auto_reply)
                     VALUES(?,?,?,?,?,1,?)
                     ON CONFLICT(email) DO UPDATE SET
                       nome=excluded.nome, proposito=excluded.proposito, slug=excluded.slug""",
                  (email, nome, proposito, slug, "", auto))
    c.commit()
    c.close()


def seed_tags():
    if db.meta_get("seed_tags_done"):
        return
    c = db.conn()
    for r in c.execute("SELECT email, respondeu, tags FROM contacts").fetchall():
        atual = db.tags_list(r["tags"])
        base = "respondeu" if (r["respondeu"] or "").lower() == "sim" else "sem-resposta"
        if base not in atual:
            atual.append(base)
        c.execute("UPDATE contacts SET tags=? WHERE email=?", (db.tags_str(atual), r["email"]))
    c.commit()
    c.close()
    db.meta_set("seed_tags_done", "1")


if __name__ == "__main__":
    db.init()
    import sync
    sync.sync_all()
    seed_senders()
    seed_campanha_reativacao()
    seed_tags()
