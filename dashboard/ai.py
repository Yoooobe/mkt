#!/usr/bin/env python3
"""4unik.mail — criação de campanhas e templates com IA (claude -p headless).

A IA usa o contexto de dashboard/context/ (KBs em kb/, imagens em images/)
para redigir campanhas e templates. Guardrail: TUDO que a IA cria nasce com
status 'pendente' (template) e 'rascunho' (campanha) — nada é enviável antes
da aprovação explícita da 4Unik no dashboard.
"""
import json
import os
import re
import shutil
import subprocess
import threading

import db

# Modelo usado nas chamadas de IA do 4unik.mail (criação de campanhas/templates).
CLAUDE_MODEL = os.environ.get("FOURUNIK_MAIL_MODEL", "opus")

# O `claude` é um CLI Node; sob o LaunchAgent o PATH é mínimo e não acha o `node`.
# Montamos um PATH robusto para os subprocessos, incluindo os diretórios de node/claude.
def _subprocess_env():
    env = os.environ.copy()
    extra = ["/opt/homebrew/bin", "/usr/local/bin",
             os.path.expanduser("~/.npm-global/bin"),
             "/usr/local/opt/node@22/bin"]
    node = shutil.which("node")
    if node:
        extra.append(os.path.dirname(node))
    env["PATH"] = os.pathsep.join(dict.fromkeys(extra + env.get("PATH", "").split(os.pathsep)))
    # O Claude Code dá precedência a credenciais de API sobre a sessão claude.ai.
    # Isso desabilita os connectors da organização, mesmo quando o usuário já fez
    # login no CLI. O dashboard usa a sessão claude.ai por padrão; as credenciais
    # são removidas SOMENTE deste subprocesso, nunca do ambiente do macOS.
    # FOURUNIK_MAIL_CLAUDE_AUTH=api é a saída explícita para uma instalação que
    # deliberadamente queira usar API em vez dos connectors. Não removemos
    # CLAUDE_CODE_OAUTH_TOKEN: em instalações gerenciadas ele representa a sessão
    # claude.ai atual; removê-lo transforma uma sessão válida em "logged out".
    if env.get("FOURUNIK_MAIL_CLAUDE_AUTH", "claude_ai").lower() != "api":
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            env.pop(key, None)
    return env

CALENDLY = "https://calendly.com/4unik/30min?month=2026-07"
CONTEXT_DIR = db.BASE / "context"
KB_DIR = CONTEXT_DIR / "kb"
IMG_DIR = CONTEXT_DIR / "images"
KB_DIR.mkdir(parents=True, exist_ok=True)
IMG_DIR.mkdir(parents=True, exist_ok=True)
MAX_KB_CHARS = 14000

# Regra operacional para toda geração de aquisição. O KB detalha provas, planos
# e ICPs; este guia fixa o formato e impede que a IA volte ao enquadramento de
# fornecedor de brindes em e-mails, anúncios ou briefings.
GUIA_GTM_CAMPANHAS = """GUIA GTM OBRIGATÓRIO:
- Posicione a 4Unik como Infraestrutura de Engajamento Programável, API-first:
  comportamento programável por rewards, gamificação, checkout e execução física.
- Nunca use em copy os termos "brindes", "kits" ou "produtos promocionais". Prefira
  rewards, behavioral triggers, camada de execução física, checkout transparente e fulfillment automatizado.
- Escolha UM ICP dominante (RH/People, Vendas/RevOps, E-learning/L&D, Comunidades/Eventos ou CTO/Produto)
  e abra pela dor operacional concreta desse ICP.
- Toda sequência deve conter: gancho de dor, como funciona, prova ou hipótese claramente rotulada,
  objeção/ROI e CTA para demonstração. Não repita o mesmo ângulo.
- Só use preços, percentuais, cases e comparativos que estejam nos KBs. Quando um ROI depender de
  premissa, explicite a premissa; não prometa retorno garantido.
- O CTA deve levar ao Calendly ou WhatsApp oficial. Para aquisição, planeje UTMs e os eventos GA4
  generate_lead, schedule_demo, contact_whatsapp ou contact_email.
- Produza somente rascunhos. Não afirme que campanhas, anúncios, GTM ou conteúdo foram publicados."""


def contexto():
    """Concatena os KBs e lista as imagens disponíveis para a IA usar nos emails."""
    partes = []
    for f in sorted(KB_DIR.glob("*.md")):
        partes.append(f"### KB: {f.name}\n{f.read_text(encoding='utf-8', errors='replace')}")
    kb = "\n\n".join(partes)[:MAX_KB_CHARS]
    imgs = [f.name for f in sorted(IMG_DIR.iterdir())
            if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp")]
    return kb, imgs


def listar_contexto():
    kbs = [{"nome": f.name, "tamanho": f.stat().st_size, "preview": f.read_text(encoding="utf-8", errors="replace")[:400]}
           for f in sorted(KB_DIR.glob("*.md"))]
    imgs = [{"nome": f.name, "url": f"/context/images/{f.name}", "tamanho": f.stat().st_size}
            for f in sorted(IMG_DIR.iterdir()) if f.is_file() and not f.name.startswith(".")]
    return {"kb": kbs, "imagens": imgs}


def _claude_json(prompt, timeout=300):
    claude = shutil.which("claude", path=_subprocess_env()["PATH"]) or "claude"
    out = subprocess.run([claude, "--model", CLAUDE_MODEL, "-p", prompt],
                         capture_output=True, text=True, timeout=timeout, env=_subprocess_env())
    txt = (out.stdout or "").strip()
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        err = (out.stderr or "").strip() or txt or f"exit {out.returncode}"
        if "connectors are disabled" in err.lower():
            raise RuntimeError(
                "Claude bloqueou os connectors por priorizar uma credencial de API. "
                "O dashboard já isolou essas credenciais nesta tentativa; reinicie o dashboard e "
                "confirme o login com `claude` no terminal."
            )
        raise RuntimeError(f"claude -p não retornou JSON ({CLAUDE_MODEL}): {err[:300]}")
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"claude -p devolveu JSON inválido: {e} — trecho: {m.group(0)[:200]}")


def claude_status():
    """Diagnóstico leve do Claude, sem prompt nem dados de CRM.

    O processo usa o mesmo ambiente isolado das gerações, evitando que uma chave
    de API herdada pelo LaunchAgent desabilite a sessão claude.ai.
    """
    try:
        env = _subprocess_env()
        claude = shutil.which("claude", path=env["PATH"]) or "claude"
        out = subprocess.run([claude, "auth", "status"], capture_output=True, text=True,
                             timeout=20, env=env)
        raw = (out.stdout or "").strip()
        data = json.loads(raw) if raw.startswith("{") else {}
        ok = bool(data.get("loggedIn")) and out.returncode == 0
        return {"ok": ok, "modelo": CLAUDE_MODEL,
                "auth": data.get("authMethod", ""),
                "mensagem": "Claude pronto para gerar rascunhos" if ok else
                            ((out.stderr or raw or "login não encontrado")[:240])}
    except Exception as exc:
        return {"ok": False, "modelo": CLAUDE_MODEL, "auth": "",
                "mensagem": f"Claude indisponível: {str(exc)[:200]}"}


def sugerir_enriquecimento_contato(contato):
    """Sugere dados comerciais públicos; nunca grava nada no CRM automaticamente."""
    atual = {k: (contato.get(k) or "") for k in (
        "email", "nome", "empresa", "telefone", "cargo", "porte_funcionarios",
        "erp", "etapa_funil", "estado_negocio", "qualificacao", "enriquecimento")}
    return _claude_json(f"""Você é um analista de sales intelligence B2B da 4Unik.
Pesquise, quando as ferramentas/conectores disponíveis permitirem, apenas fontes públicas e profissionais
para completar este contato. Não use, não infira e não solicite dados pessoais sensíveis. Não invente.
Se não encontrar evidência pública confiável, deixe o campo vazio.

Devolva SOMENTE JSON válido, sem markdown, neste formato:
{{"resumo":"o que foi encontrado em 1-3 frases",
  "confianca":0.0,
  "sugestoes":{{"nome":"","empresa":"","telefone":"","cargo":"",
    "porte_funcionarios":"","erp":"","etapa_funil":"","estado_negocio":"",
    "qualificacao":"","enriquecimento":""}},
  "fontes":[{{"titulo":"","url":"","evidencia":""}}],
  "nao_encontrado":["campo"]}}

REGRAS:
- telefone somente se for telefone corporativo publicado; nunca telefone pessoal inferido;
- fontes precisam ser URLs públicas reais; não fabrique URL;
- sugestões devem ser fatos, não copy comercial;
- preserve informações atuais corretas e sugira apenas complementos ou correções justificadas;
- confiança geral deve refletir a qualidade das fontes.

DADOS ATUAIS DO CRM:
{json.dumps(atual, ensure_ascii=False)}""", timeout=180)


GUIA_HTML = """REGRAS DO HTML (inner_html = só o miolo; a moldura oficial shell() é aplicada pelo sistema):
- Estilo inline Gmail-safe, identidade 4Unik: títulos <h1 style="margin:0 0 18px;font-family:Montserrat,Arial,sans-serif;font-size:22px;font-weight:800;color:#0f1720;line-height:1.3"> com spans de destaque color:#1fbf9c; parágrafos <p style="margin:0 0 16px">.
- Cards: <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:18px 0"><tr><td style="border-left:4px solid COR;background-color:#f6f8f9;border-radius:0 10px 10px 0;padding:16px 20px">…</td></tr></table> (COR: #1fbf9c padrão, #66e3c4 benefício, #f2c94c oferta, #ef7d7d dor).
- Variáveis disponíveis: {{nome}}, {{empresa}}, {{cargo}}, {{porte}}, {{erp}}, {{modulo}}, {{data_proposta}}, {{validade}}, {{mensalidade}}.
- Se citar uma imagem do contexto, use <img src="cid-imagem:NOME_DO_ARQUIVO"> para revisao antes de publicar.
- Canais oficiais de retorno: WhatsApp +55 11 2684-4724 (https://wa.me/551126844724) e comercial@4unik.com.br. A moldura já os inclui no rodapé.
- NUNCA invente preços, descontos, prazos ou cases que não estejam no contexto fornecido."""


def criar_template(briefing):
    kb, imgs = contexto()
    res = _claude_json(f"""Você é o copywriter da 4Unik (Time 4Unik). Crie UM template de email comercial.
Devolva SOMENTE JSON válido, sem markdown:
{{"nome": "nome curto do template", "assunto": "...", "preheader": "...",
 "cta": "rótulo do botão", "cta_url": "url do CTA (Calendly/WhatsApp do contexto, ou vazio)",
 "inner_html": "HTML do miolo", "texto_plano": "versão text/plain completa"}}

{GUIA_HTML}

CONTEXTO (KBs da 4Unik):
{kb}

IMAGENS DISPONÍVEIS: {', '.join(imgs) or 'nenhuma'}

BRIEFING DA 4UNIK: {briefing}""")
    c = db.conn()
    c.execute("""INSERT INTO templates_ai(nome,assunto,preheader,cta,cta_url,inner_html,texto_plano,status,origem)
                 VALUES(?,?,?,?,?,?,?,'pendente','ia')""",
              (res.get("nome", "Template IA"), res.get("assunto", ""), res.get("preheader", ""),
               res.get("cta", ""), res.get("cta_url", ""), res.get("inner_html", ""),
               res.get("texto_plano", "")))
    c.commit()
    tid = c.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
    c.close()
    return tid


def classificar_resposta(contato, historico, resposta):
    """Interpreta a resposta de um lead usando os KBs. A IA interpreta; o código decide.
    Devolve dict (classe, confiança, precisa_humano, resposta_sugerida, resumo) ou None."""
    kb, _ = contexto()
    prompt = f"""Você é o assistente comercial do Time 4Unik (4Unik — plataforma de automação de marketing/vendas para empresas com programas de recompensas, RH, employee experience, vendas e plataformas SaaS).
Um lead respondeu a um e-mail de campanha. Interprete e devolva SOMENTE um JSON válido, sem markdown:
{{"classificacao": "pergunta_simples | agendar | interessado | objecao_preco | duvida_complexa | pediu_tempo | recusa_encerrar | outro",
 "confianca": 0.0 a 1.0,
 "intencao_reuniao": true ou false,
 "precisa_humano": true ou false,
 "resposta_sugerida": "resposta pronta, cordial e direta, em pt-BR, no tom da 4Unik, terminando com 'Atenciosamente, Time 4Unik'",
 "resumo": "1-2 frases em pt-BR para o WhatsApp da 4Unik"}}

REGRAS (NUNCA violar):
- Responda SOMENTE com base no CONTEXTO (KBs) abaixo. Se a informação não estiver no contexto, defina precisa_humano=true e não invente.
- PREÇO: use somente faixas/planos presentes no contexto e convide para uma call: {CALENDLY}. NUNCA invente valor fechado, desconto, prazo ou cláusula de contrato.
- "agendar"/"interessado": pediu reunião/call ou demonstrou interesse claro → a resposta_sugerida deve oferecer o link do Calendly ({CALENDLY}).
- "pergunta_simples": dúvida factual/técnica respondível pelo contexto (API, webhooks, fulfillment, gamificacao, carteiras, loja corporativa, governanca).
- "objecao_preco" (achou caro/negociação), "duvida_complexa", "pediu_tempo", "outro" → precisa_humano=true.
- Em qualquer ambiguidade, use confiança < 0.8.

CONTEXTO (KBs da 4Unik):
{kb}

CONTATO: {json.dumps(contato, ensure_ascii=False)}
NOSSO ÚLTIMO E-MAIL (resumo): {(historico or '')[:1200]}
RESPOSTA DO LEAD:
{(resposta or '')[:3000]}"""
    try:
        return _claude_json(prompt)
    except Exception:
        return None


def _fmt_cohort(p):
    """Formata o resumo da audiência (db.resumo_audiencia) como briefing legível para a IA."""
    if not p or not p.get("total"):
        return "público ainda sem dados de enriquecimento — escreva de forma genérica para empresas B2B, RH, SaaS e operacoes com programas de incentivo."

    def lst(key):
        return ", ".join(f"{x['valor']} ({x['n']})" for x in p.get(key, [])) or "—"
    return (f"{p['total']} contatos (empresas com programas de recompensas, RH, employee experience, vendas e plataformas SaaS). "
            f"Porte/tamanho: {lst('porte')}. Sistemas/stack: {lst('erp')}. "
            f"Cargo/persona: {lst('cargo')}. Estágio: {lst('estado')}. "
            f"Qualificação: {lst('qualificacao')}. Motivo de perda: {lst('motivo_perda')}. "
            f"Origem: {lst('origem')}. Já fizeram demonstração: {p.get('fez_demo', 0)}. "
            f"Histórico agregado: {p.get('ja_contatados', 0)} já receberam e-mail; "
            f"{p.get('responderam', 0)} já responderam; último contato mais recente em "
            f"{p.get('ultimo_contato', 'não registrado')}. Qualidade do CRM: "
            f"{p.get('com_telefone', 0)} com telefone cadastrado, "
            f"{p.get('com_historico', 0)} com observações comerciais e "
            f"{p.get('com_etapa_funil', 0)} com etapa de funil preenchida. "
            "Nunca use ou solicite telefone, e-mail, nome ou outro dado individual na copy; "
            "estes dados permanecem apenas no CRM para o time comercial.")


# ---------------- progresso do build em background (via meta) ----------------

# Assinaturas (minúsculas) de erro do `claude -p` por esgotamento de cota/limite de uso.
# Quando o erro do build bate com uma destas, a UI oferece "Retomar Criação".
SINAIS_COTA = ("hit your limit", "usage limit", "limit reached", "resets", "rate limit",
               "rate_limit", "quota", "credit balance", "overloaded", "529", "too many requests")
SINAIS_CONECTOR = ("connectors are disabled", "claude.ai connectors")


def _erro_eh_cota(msg):
    m = (msg or "").lower()
    return any(s in m for s in SINAIS_COTA)


def _tipo_erro(msg):
    m = (msg or "").lower()
    if any(s in m for s in SINAIS_CONECTOR):
        return "conector"
    return "cota" if _erro_eh_cota(msg) else "outro"


def build_progress(cid):
    raw = db.meta_get(f"build_{cid}", "")
    if not raw:
        return None
    try:
        p = json.loads(raw)
    except Exception:
        return None
    # classifica o erro na leitura — assim builds gravados antes desta feature
    # (ex.: a campanha travada por cota) já recebem 'erro_tipo' sem migração de dados.
    if p.get("status") == "erro":
        # Reclassifica também registros antigos já gravados como "outro".
        p["erro_tipo"] = _tipo_erro(p.get("erro", ""))
    return p


def _set_progress(cid, **kw):
    p = build_progress(cid) or {}
    p.update(kw)
    db.meta_set(f"build_{cid}", json.dumps(p, ensure_ascii=False))


REGRA_PUBLICO = ("REGRA DE PÚBLICO (NUNCA violar): prospecção NÃO tem como alvo quem já é CLIENTE "
                 "4Unik. Escreva como abordagem a EMPRESAS B2B que ainda não são clientes.")


def _plano_campanha(objetivo, n_emails, tipo, cadencia, kb, brief):
    """1ª chamada (leve): só o esqueleto — nome, resumo e o ângulo de cada email. Rápida."""
    tipo_desc = "os emails se repetem em ciclo contínuo" if tipo == "infinita" else "sequência única"
    return _claude_json(f"""Você é o estrategista de email marketing da 4Unik (Time 4Unik).
PLANEJE (NÃO escreva os emails ainda) uma campanha de {n_emails} emails. Tipo: {tipo} ({tipo_desc}). {cadencia}.
Distribua a mensagem do início ao fim, em ordem estratégica, SEM repetir ângulos entre os emails.
Devolva SOMENTE JSON válido, sem markdown:
{{"nome": "nome da campanha", "resumo": "1 frase",
 "insight_gtm": {{"icp":"persona e dor", "gancho":"ângulo de entrada", "prova":"case/dado ou hipótese rotulada", "cta":"CTA", "evento_ga4":"um evento permitido", "utm_source":"4unik-mail", "utm_medium":"email"}},
 "emails": [{{"nome": "título curto do email", "angulo": "o foco/conteúdo deste email em 1-2 frases", "offset_dias": 0}}]}}

CONTEXTO (KBs da 4Unik):
{kb}

PERFIL DO PÚBLICO (cohort):
{brief}

{REGRA_PUBLICO}

{GUIA_GTM_CAMPANHAS}

OBJETIVO DA CAMPANHA: {objetivo}""", timeout=180)


def _gerar_email(objetivo, e, todos_angulos, ordem, n, kb, imgs, brief):
    """2ª..N+1ª chamadas (uma por email): escreve UM email completo. Cada uma pequena/rápida."""
    return _claude_json(f"""Você é o copywriter da 4Unik (Time 4Unik). Escreva O EMAIL {ordem} DE {n} de uma campanha.
Devolva SOMENTE JSON válido, sem markdown:
{{"assunto": "...", "preheader": "...", "cta": "rótulo do botão",
 "cta_url": "url do CTA (Calendly/WhatsApp do contexto, ou vazio)",
 "inner_html": "HTML do miolo", "texto_plano": "versão text/plain completa"}}

{GUIA_HTML}

CONTEXTO (KBs da 4Unik):
{kb}

IMAGENS DISPONÍVEIS: {', '.join(imgs) or 'nenhuma'}

PERFIL DO PÚBLICO (cohort) — escreva a copy SOB MEDIDA para este perfil:
{brief}

VARIÁVEIS DE PERSONALIZAÇÃO (use só as que fizerem sentido para um público homogêneo):
{{{{nome}}}}, {{{{empresa}}}}, {{{{cargo}}}}, {{{{porte}}}} (ex.: "Até 500 colaboradores"), {{{{erp}}}}.

{REGRA_PUBLICO}

{GUIA_GTM_CAMPANHAS}

SEQUÊNCIA COMPLETA (para você NÃO repetir o que vai nos outros emails):
{todos_angulos}

>>> ESTE É O EMAIL {ordem}: "{e.get('nome', '')}" — ângulo: {e.get('angulo', '')}
OBJETIVO GERAL DA CAMPANHA: {objetivo}""", timeout=240)


def _steps_existentes(cid):
    c = db.conn()
    n = c.execute("SELECT COUNT(*) AS n FROM campaign_steps WHERE campaign_id=?", (cid,)).fetchone()["n"]
    c.close()
    return n


def _geracao_cancelada(cid):
    """A pausa pode acontecer enquanto o Claude ainda responde.

    O status persistido é a fonte de verdade: a thread antiga não pode gravar
    templates nem voltar a marcar o build como pronto depois do cancelamento.
    """
    p = build_progress(cid) or {}
    return p.get("status") == "erro" and p.get("etapa") == "pausada manualmente"


def _gerar_loop(cid, objetivo, emails, cad_min, kb, imgs, brief):
    """Gera os emails do plano um a um, PULANDO os que já têm step salvo (retomada idempotente).
    Compartilhado pelo build inicial e pela retomada — continua de onde a campanha parou."""
    angulos = "; ".join(f"{i}) {e.get('nome', '')}: {e.get('angulo', '')}"
                        for i, e in enumerate(emails, start=1))
    feitos = _steps_existentes(cid)
    for i in range(feitos + 1, len(emails) + 1):
        if _geracao_cancelada(cid):
            return False
        e = emails[i - 1]
        _set_progress(cid, etapa=f"escrevendo e-mail {i} de {len(emails)}: {e.get('nome', '')}",
                      feitos=i - 1)
        full = _gerar_email(objetivo, e, angulos, i, len(emails), kb, imgs, brief)
        # A chamada acima pode levar minutos. Revalida antes de qualquer escrita
        # para honrar uma pausa feita enquanto a IA estava trabalhando.
        if _geracao_cancelada(cid):
            return False
        c = db.conn()
        c.execute("""INSERT INTO templates_ai(nome,assunto,preheader,cta,cta_url,inner_html,texto_plano,status,origem)
                     VALUES(?,?,?,?,?,?,?,'pendente','ia')""",
                  (e.get("nome", f"Email {i}"), full.get("assunto", ""), full.get("preheader", ""),
                   full.get("cta", ""), full.get("cta_url", ""), full.get("inner_html", ""),
                   full.get("texto_plano", "")))
        tid = c.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
        offset = e.get("offset_dias") if cad_min is None else None
        c.execute("INSERT INTO campaign_steps(campaign_id,ordem,template_ref,offset_dias) VALUES(?,?,?,?)",
                  (cid, i, f"ai:{tid}", offset))
        c.commit()
        c.close()
        _set_progress(cid, feitos=i)
    if _geracao_cancelada(cid):
        return False
    _set_progress(cid, status="pronto", etapa="concluído", feitos=len(emails))
    return True


def _cadencia_desc(cad_min, cad_max):
    return (f"cadência ALEATÓRIA entre {cad_min} e {cad_max} dias entre toques"
            if cad_min is not None else "cadência fixa (defina offset_dias por email)")


def _build_worker(cid, objetivo, tipo, n_emails, cad_min, cad_max, kb, imgs, brief):
    """Gera a campanha email-por-email em background (sem nunca estourar timeout)."""
    try:
        _set_progress(cid, status="gerando", total=n_emails, feitos=0, etapa="planejando a sequência…")
        plano = _plano_campanha(objetivo, n_emails, tipo, _cadencia_desc(cad_min, cad_max), kb, brief)
        emails = (plano.get("emails") or [])[:n_emails]
        if not emails:
            raise RuntimeError("a IA não devolveu o plano de emails")
        c = db.conn()
        insight = plano.get("insight_gtm") or {}
        contexto = " · ".join(x for x in (
            plano.get("resumo", ""),
            f"ICP: {insight.get('icp', '')}" if insight.get("icp") else "",
            f"Gancho: {insight.get('gancho', '')}" if insight.get("gancho") else "",
            f"Prova: {insight.get('prova', '')}" if insight.get("prova") else "",
            f"GA4: {insight.get('evento_ga4', '')}" if insight.get("evento_ga4") else "",
        ) if x)
        c.execute("UPDATE campaigns SET nome=?, contexto=? WHERE id=?",
                  (plano.get("nome") or f"Campanha — {objetivo[:40]}", contexto, cid))
        c.commit()
        c.close()
        # persiste o plano (ângulos) p/ uma retomada futura ser exata (sem replanejar)
        _set_progress(cid, total=len(emails), plano_emails=emails)
        _gerar_loop(cid, objetivo, emails, cad_min, kb, imgs, brief)
    except Exception as ex:
        _set_progress(cid, status="erro", erro=str(ex)[:300], erro_tipo=_tipo_erro(str(ex)))
        print(f"[build campanha {cid}] ERRO: {ex}", flush=True)


def _resume_worker(cid):
    """Retoma a geração de uma campanha que travou (ex.: cota), continuando de onde parou.
    Reusa o plano persistido; se ele se perdeu (build antigo), REPLANEJA e gera só os faltantes."""
    try:
        c = db.conn()
        camp = c.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
        emails_aud = [r["email"] for r in
                      c.execute("SELECT email FROM campaign_contacts WHERE campaign_id=?", (cid,)).fetchall()]
        c.close()
        if not camp:
            raise RuntimeError("campanha não encontrada")
        objetivo = camp["objetivo"] or ""
        cad_min = camp["cad_min_dias"]
        kb, imgs = contexto()
        brief = _fmt_cohort(db.resumo_audiencia(emails_aud))
        prog = build_progress(cid) or {}
        emails = prog.get("plano_emails")
        if not emails:
            total = prog.get("total") or max(1, _steps_existentes(cid))
            _set_progress(cid, status="gerando", etapa="replanejando a sequência…")
            plano = _plano_campanha(objetivo, total, camp["tipo"],
                                    _cadencia_desc(cad_min, camp["cad_max_dias"]), kb, brief)
            emails = (plano.get("emails") or [])[:total]
            if not emails:
                raise RuntimeError("a IA não devolveu o plano de emails")
            _set_progress(cid, plano_emails=emails)
        _set_progress(cid, status="gerando", total=len(emails), erro="", erro_tipo="")
        _gerar_loop(cid, objetivo, emails, cad_min, kb, imgs, brief)
    except Exception as ex:
        _set_progress(cid, status="erro", erro=str(ex)[:300], erro_tipo=_tipo_erro(str(ex)))
        print(f"[retomada campanha {cid}] ERRO: {ex}", flush=True)


def retomar_campanha(cid):
    """Dispara a retomada da geração em background. Só cria templates 'pendente' + steps."""
    _set_progress(cid, status="gerando", erro="", erro_tipo="", etapa="retomando…")
    threading.Thread(target=_resume_worker, args=(cid,), daemon=True).start()
    return cid


def criar_campanha(objetivo, tipo="finita", cad_min=None, cad_max=None,
                   n_emails=3, segmentos=None, apenas_sem_resposta=True, tags=None, sender_id=1,
                   inicio_em="", audience_profile=""):
    """Cria a campanha NA HORA (rascunho) e gera os emails um a um em BACKGROUND (sem timeout).
    Acompanhe o progresso por build_progress(cid) / GET /api/campaigns/{cid} (campo 'build')."""
    kb, imgs = contexto()
    n_emails = max(1, min(int(n_emails or 3), 20))
    # resolve o público ANTES (clientes auto-excluídos por resolve_audience) p/ o briefing do cohort
    segs = segmentos if segmentos else ["quente", "morno", "frio"]
    audiencia = db.resolve_audience(segs, tags, apenas_sem_resposta, perfil=audience_profile)
    brief = _fmt_cohort(db.resumo_audiencia(audiencia))

    c = db.conn()
    c.execute("""INSERT INTO campaigns(nome,objetivo,tipo,status,cad_min_dias,cad_max_dias,contexto,sender_id,inicio_em,audience_profile)
                 VALUES(?,?,?,'rascunho',?,?,?,?,?,?)""",
              (f"Gerando campanha… ({n_emails} e-mails)", objetivo, tipo, cad_min, cad_max,
               "A IA está redigindo os e-mails…", sender_id or 1, inicio_em or "", audience_profile or ""))
    cid = c.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
    for email in audiencia:
        c.execute("INSERT OR IGNORE INTO campaign_contacts(campaign_id,email) VALUES(?,?)", (cid, email))
    c.commit()
    c.close()

    _set_progress(cid, status="gerando", total=n_emails, feitos=0, etapa="iniciando…")
    threading.Thread(target=_build_worker,
                     args=(cid, objetivo, tipo, n_emails, cad_min, cad_max, kb, imgs, brief),
                     daemon=True).start()
    return cid


def criar_campanha_contato(email, objetivo, n_emails=3, sender_id=1, inicio_em=""):
    """Cria campanha IA em rascunho para um único contato, sem resolver audiência ampla."""
    c = db.conn()
    contato = c.execute("SELECT * FROM contacts WHERE email=?", (email,)).fetchone()
    c.close()
    if not contato:
        raise ValueError("contato não encontrado")
    n_emails = max(1, min(int(n_emails or 3), 10))
    kb, imgs = contexto()
    brief = _fmt_cohort(db.resumo_audiencia([email]))
    c = db.conn()
    c.execute("""INSERT INTO campaigns(nome,objetivo,tipo,status,cad_min_dias,cad_max_dias,
                 contexto,sender_id,inicio_em,audience_profile)
                 VALUES(?,?,'finita','rascunho',NULL,NULL,?,?,?,?)""",
              (f"Gerando campanha individual — {contato['empresa'] or contato['nome'] or email}",
               objetivo, "Campanha individual criada pela ficha do CRM", sender_id or 1,
               inicio_em or "", "contato_individual"))
    cid = c.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
    c.execute("INSERT INTO campaign_contacts(campaign_id,email) VALUES(?,?)", (cid, email))
    c.commit()
    c.close()
    _set_progress(cid, status="gerando", total=n_emails, feitos=0, etapa="iniciando…")
    threading.Thread(target=_build_worker,
                     args=(cid, objetivo, "finita", n_emails, None, None, kb, imgs, brief),
                     daemon=True).start()
    return cid


def gerar_briefing_ads(campanha, steps):
    """Briefing Google Ads espelhando uma campanha de email: estrutura pronta para colar
    no Ads (ou importar via CSV do Ads Editor). Nada é criado na conta — só rascunho local."""
    kb, _ = contexto()
    slug = campanha.get("slug") or db.slugify(campanha.get("nome", "")) or f"c{campanha.get('id')}"
    emails = "\n".join(f"- etapa {s.get('ordem')}: {s.get('template_ref')}" for s in steps) or "—"
    res = _claude_json(f"""Você é o gestor de tráfego da 4Unik. Crie um briefing de campanha
Google Ads (rede de pesquisa, anúncio responsivo RSA) que espelhe a campanha de email abaixo,
para o mesmo público e objetivo. Devolva SOMENTE JSON válido, sem markdown:
{{"campaign_name": "{slug}",
 "objetivo": "objetivo em 1 frase",
 "publico": "descrição do público-alvo e segmentação sugerida",
 "orcamento_sugerido": "ex.: R$ 30/dia",
 "keywords": [{{"kw": "palavra-chave", "match": "phrase|exact|broad"}}],
 "ad_groups": [{{"nome": "...",
   "headlines": ["até 15 títulos, cada um com NO MÁXIMO 30 caracteres"],
   "descriptions": ["até 4 descrições, cada uma com NO MÁXIMO 90 caracteres"],
   "final_url": "URL de destino SEM parâmetros utm"}}]}}

CONTEXTO (KBs da 4Unik):
{kb}

{GUIA_GTM_CAMPANHAS}

CAMPANHA DE EMAIL A ESPELHAR:
Nome: {campanha.get('nome')}
Objetivo: {campanha.get('objetivo')}
Slug/utm_campaign: {slug}
Emails da cadência:
{emails}

REGRAS: headlines ≤30 caracteres; descriptions ≤90; 8 a 15 headlines; 2 a 4 descriptions;
keywords em português focadas em recompensas corporativas/gamificação/loja de resgate;
final_url deve ser uma página real da 4unik (plataforma.4unik.com.br/landing/... ou 4unik.com.br).""")
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
    for g in res.get("ad_groups", []):
        g["headlines"] = [h.strip() for h in g.get("headlines", []) if h and len(h.strip()) <= 30][:15]
        g["descriptions"] = [d.strip() for d in g.get("descriptions", []) if d and len(d.strip()) <= 90][:4]
        url = (g.get("final_url") or "https://plataforma.4unik.com.br/landing/").strip()
        p = urlsplit(url)
        q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not k.startswith("utm_")]
        q += [("utm_source", "google"), ("utm_medium", "cpc"), ("utm_campaign", slug)]
        g["final_url"] = urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), p.fragment))
    res["campaign_name"] = slug
    res["gerado_em"] = db.now()
    c = db.conn()
    c.execute("UPDATE campaigns SET ads_briefing=? WHERE id=?",
              (json.dumps(res, ensure_ascii=False), campanha["id"]))
    c.commit()
    c.close()
    return res
