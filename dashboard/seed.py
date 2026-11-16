#!/usr/bin/env python3
"""Yoobe mail — seed de remetentes e campanha exemplo."""
import db

DEMO = "https://corporate.yoobe.co/"
API_PAGE = "https://corporate.yoobe.co/platform/"

P = lambda t: f'<p style="margin:0 0 16px">{t}</p>'
H = lambda t: f'<h1 style="margin:0 0 18px;font-family:Montserrat,Arial,sans-serif;font-size:22px;font-weight:800;color:#0f1720;line-height:1.3">{t}</h1>'

EMAIL_1 = dict(
    nome="Yoobe 1 — Operação de recompensas",
    assunto="{{empresa}} ainda opera reconhecimento com planilha?",
    preheader="A Yoobe conecta campanhas, catálogo corporativo e fulfillment.",
    cta="Conhecer a plataforma Yoobe",
    cta_url=DEMO,
    inner_html=(
        H('Reconhecimento bom nao deveria virar<br><span style="color:#1fbf9c">operacao manual.</span>')
        + P("Olá, {{nome}}. Tudo bem?")
        + P("Programas de incentivo comecam simples: uma meta, um premio, uma campanha interna. O problema aparece quando o RH precisa comprar, armazenar, enviar, rastrear e ainda provar ROI.")
        + P("A <b>Yoobe</b> conecta campanhas, recompensas, catálogo corporativo e fulfillment em uma única plataforma.")
        + P("Se fizer sentido para a <b>{{empresa}}</b>, marcamos uma demo curta para mapear um primeiro caso de uso.")),
    texto_plano=("Olá, {{nome}}. Tudo bem?\n\n"
                 "Programas de incentivo começam simples: uma meta, um prêmio, uma campanha interna. "
                 "O problema aparece quando o RH precisa comprar, armazenar, enviar, rastrear e provar ROI.\n\n"
                 "A Yoobe conecta campanhas, recompensas, catálogo corporativo e fulfillment em uma única plataforma.\n\n"
                 f"Podemos marcar uma demo curta? {DEMO}\n\n"
                 "Atenciosamente,\nTime Yoobe"),
    offset=0)

EMAIL_2 = dict(
    nome="Yoobe 2 — Plataforma e catálogo",
    assunto="Recompensas reais como uma chamada de API",
    preheader="Plataforma corporativa e catálogo em experiências integradas.",
    cta="Ver o catálogo Yoobe",
    cta_url="https://catalogo.yoobe.co/",
    inner_html=(
        H('Da meta batida ao premio entregue,<br><span style="color:#1fbf9c">sem construir tudo do zero.</span>')
        + P("{{nome}}, a Yoobe conecta sua plataforma corporativa a um catálogo de produtos e experiências para colaboradores, clientes e parceiros.")
        + P("Conheça a plataforma em <a href=\"https://corporate.yoobe.co/\">corporate.yoobe.co</a> e explore o catálogo em <a href=\"https://catalogo.yoobe.co/\">catalogo.yoobe.co</a>.")),
    texto_plano=("{{nome}}, a Yoobe conecta sua plataforma corporativa a um catálogo de produtos e experiências para colaboradores, clientes e parceiros.\n\n"
                 "Conheça a plataforma: https://corporate.yoobe.co/\n"
                 "Explore o catálogo: https://catalogo.yoobe.co/\n\n"
                 f"API e integracoes: {API_PAGE}\n\n"
                 "Atenciosamente,\nTime Yoobe"),
    offset=4)

EMAIL_3 = dict(
    nome="Yoobe 3 — ROI e conversa",
    assunto="Como provar ROI de uma campanha de reconhecimento?",
    preheader="Conecte campanhas corporativas a produtos e experiências Yoobe.",
    cta="Conversar com especialista",
    cta_url=DEMO,
    inner_html=(
        H('Campanha sem dados vira custo.<br><span style="color:#1fbf9c">Campanha com dados vira decisao.</span>')
        + P("{{nome}}, a Yoobe ajuda sua empresa a transformar campanhas de reconhecimento em experiências concretas para colaboradores, clientes e parceiros.")
        + P("Se a {{empresa}} quer testar um programa de onboarding, vendas, treinamento ou reconhecimento interno, podemos desenhar um piloto com indicadores claros desde o primeiro dia.")
        + P("Vale uma conversa de 20 minutos?")),
    texto_plano=("{{nome}}, a Yoobe ajuda sua empresa a transformar campanhas de reconhecimento em experiências concretas para colaboradores, clientes e parceiros.\n\n"
                 "Se a {{empresa}} quer testar um programa de onboarding, vendas, treinamento ou reconhecimento interno, "
                 "podemos desenhar um piloto com indicadores claros desde o primeiro dia.\n\n"
                 f"Agenda: {DEMO}\n\n"
                 "Atenciosamente,\nTime Yoobe"),
    offset=10)


def seed_campanha_reativacao():
    """Cria a campanha inicial de exemplo para Yoobe."""
    existing = db.meta_get("seed_yoobe_intro_2026") or db.meta_get("seed_4unik_intro_2026")
    if existing:
        return int(existing)
    c = db.conn()
    c.execute("""INSERT INTO campaigns(nome,objetivo,tipo,status,contexto) VALUES(?,?,?,?,?)""",
              ("Yoobe — Catálogo e plataforma corporativa",
               "Campanha exemplo para apresentar a plataforma corporativa e o catálogo da Yoobe. "
               "Importar e revisar uma audiência antes de ativar.",
               "finita", "rascunho",
               "Contexto público: https://corporate.yoobe.co/ e https://catalogo.yoobe.co/."))
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
    db.meta_set("seed_yoobe_intro_2026", cid)
    print(f"campanha #{cid} Yoobe criada em rascunho")
    return cid


SENDERS = [
    ("genau@yoobe.co", "Yoobe", "Campanhas Yoobe", "genau", "off"),
    ("contact@yoobe.co", "Yoobe Contato", "Caixa corporativa para CRM e respostas", "contact", "off"),
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
