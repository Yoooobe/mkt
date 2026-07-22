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
import time
from pathlib import Path

import db
import vertex

# Modelo usado nas chamadas de IA do 4unik.mail (criação de campanhas/templates).
CLAUDE_MODEL = os.environ.get("FOURUNIK_MAIL_MODEL", "opus")

# Provider da IA: "vertex" (Gemini no projeto GCP mkt-4unik, default) ou
# "claude" (CLI local). Quando o primário falha, o outro é tentado como
# fallback se estiver disponível — a política "IA sugere, código decide"
# vale igualmente para os dois transportes.
AI_PROVIDER = (vertex.env_get("FOURUNIK_MAIL_PROVIDER", "vertex") or "vertex").strip().lower()

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
# As chamadas de portfólio (sugestão de campanhas) são poucas e estratégicas:
# recebem um contexto maior para enxergar os skills + KBs inteiros.
MAX_KB_PORTFOLIO = 24000

# Fontes ORIGINAIS do skill GTM (workspace primeiro; fallback para a instalação do Codex).
# sync_skill_kbs() as espelha em context/ para o copywriter usar sempre a versão atual.
_SKILL_GTM_DIRS = [
    db.GWS_DIR.parent / ".cursor" / "skills" / "gtm-engineer-growth-specialist",
    Path.home() / ".codex" / "skills" / "gtm-engineer-growth-specialist",
]

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


def sync_skill_kbs():
    """Espelha os mds ORIGINAIS do skill GTM em context/ (fonte de verdade dos skills).

    SKILL.md e platform-and-growth.md entram em kb/ com prefixo '0-' para serem
    lidos PRIMEIRO pelo contexto(); competitive-landscape.md fica em context/
    (fora do prompt padrão), disponível para consulta. Copia apenas quando a
    fonte é mais nova. Devolve os nomes sincronizados nesta chamada."""
    skill_dir = next((d for d in _SKILL_GTM_DIRS if (d / "SKILL.md").exists()), None)
    if not skill_dir:
        return []
    alvos = [
        (skill_dir / "SKILL.md", KB_DIR / "0-skill-gtm-growth.md"),
        (skill_dir / "references" / "platform-and-growth.md", KB_DIR / "0-skill-platform-growth.md"),
        (skill_dir / "references" / "competitive-landscape.md",
         CONTEXT_DIR / "skill-competitive-landscape.md"),
    ]
    sincronizados = []
    for src, dst in alvos:
        if src.exists() and (not dst.exists() or src.stat().st_mtime > dst.stat().st_mtime):
            shutil.copy2(src, dst)
            sincronizados.append(dst.name)
    return sincronizados


def contexto(max_chars=MAX_KB_CHARS):
    """Concatena os KBs e lista as imagens disponíveis para a IA usar nos emails."""
    partes = []
    for f in sorted(KB_DIR.glob("*.md")):
        partes.append(f"### KB: {f.name}\n{f.read_text(encoding='utf-8', errors='replace')}")
    kb = "\n\n".join(partes)[:max_chars]
    imgs = [f.name for f in sorted(IMG_DIR.iterdir())
            if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp")]
    return kb, imgs


def listar_contexto():
    kbs = [{"nome": f.name, "tamanho": f.stat().st_size, "preview": f.read_text(encoding="utf-8", errors="replace")[:400]}
           for f in sorted(KB_DIR.glob("*.md"))]
    imgs = [{"nome": f.name, "url": f"/context/images/{f.name}", "tamanho": f.stat().st_size}
            for f in sorted(IMG_DIR.iterdir()) if f.is_file() and not f.name.startswith(".")]
    return {"kb": kbs, "imagens": imgs}


def _vertex_json(prompt, timeout=300, tier="pro"):
    """Gemini via Vertex. tier 'flash' = classificações/lotes rápidos (sem thinking);
    tier 'pro' = gerações de campanha/copy/estratégia."""
    if tier == "flash":
        return vertex.generate_json(prompt, model=vertex.MODEL_FLASH,
                                    timeout=timeout, thinking_budget=0)
    return vertex.generate_json(prompt, model=vertex.MODEL_PRO, timeout=timeout)


def _ai_json(prompt, timeout=300, tier="pro"):
    """Ponto único de geração JSON. Usa o provider configurado e cai para o
    outro transporte quando o primário falha (ex.: CLI claude ausente no
    container da nuvem, ou Vertex sem rede)."""
    primary, fallback = ((_vertex_json, _claude_json) if AI_PROVIDER != "claude"
                         else (_claude_json, _vertex_json))
    try:
        if primary is _vertex_json:
            return primary(prompt, timeout=timeout, tier=tier)
        return primary(prompt, timeout=timeout)
    except Exception as exc:
        try:
            if fallback is _vertex_json:
                return fallback(prompt, timeout=timeout, tier=tier)
            return fallback(prompt, timeout=timeout)
        except Exception:
            raise exc


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


def ai_status():
    """Status do provider de IA ativo (para a UI/estratégia). Vertex valida a
    credencial sem gastar tokens; claude consulta o login do CLI."""
    if AI_PROVIDER != "claude":
        st = vertex.vertex_status()
        st["provider"] = "vertex"
        return st
    st = claude_status()
    st["provider"] = "claude"
    return st


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
    return _ai_json(f"""Você é um analista de sales intelligence B2B da 4Unik.
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
{json.dumps(atual, ensure_ascii=False)}""", timeout=180, tier="flash")


GUIA_HTML = """REGRAS DO HTML (inner_html = só o miolo; a moldura oficial shell() é aplicada pelo sistema):
- Estilo inline Gmail-safe, identidade 4Unik: títulos <h1 style="margin:0 0 18px;font-family:Montserrat,Arial,sans-serif;font-size:22px;font-weight:800;color:#0f1720;line-height:1.3"> com spans de destaque color:#1fbf9c; parágrafos <p style="margin:0 0 16px">.
- Cards: <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:18px 0"><tr><td style="border-left:4px solid COR;background-color:#f6f8f9;border-radius:0 10px 10px 0;padding:16px 20px">…</td></tr></table> (COR: #1fbf9c padrão, #66e3c4 benefício, #f2c94c oferta, #ef7d7d dor).
- REGRA DO CTA (obrigatória): NUNCA coloque botão no inner_html — nenhuma âncora estilizada
  como botão (display:inline-block, background, padding). O SISTEMA renderiza o ÚNICO botão
  oficial ao final do e-mail usando os campos "cta" (rótulo) e "cta_url". No corpo, refira-se
  ao CTA apenas em texto corrido.
- Se quiser um 2º caminho de conversão no corpo, ele deve ser DIFERENTE do CTA do botão
  (ex.: botão agenda demo; texto pede "responda este e-mail com X"). JAMAIS repita o mesmo
  pedido/rótulo duas vezes no mesmo e-mail.
- Variáveis disponíveis: {{nome}}, {{empresa}}, {{cargo}}, {{porte}}, {{erp}}, {{modulo}}, {{data_proposta}}, {{validade}}, {{mensalidade}}.
- Se citar uma imagem do contexto, use <img src="cid-imagem:NOME_DO_ARQUIVO"> para revisao antes de publicar.
- Canais oficiais de retorno: WhatsApp +55 11 2684-4724 (https://wa.me/551126844724) e comercial@4unik.com.br. A moldura já os inclui no rodapé.
- NUNCA invente preços, descontos, prazos ou cases que não estejam no contexto fornecido."""


def criar_template(briefing):
    kb, imgs = contexto()
    res = _ai_json(f"""Você é o copywriter da 4Unik (Time 4Unik). Crie UM template de email comercial.
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
        return _ai_json(prompt, tier="flash")
    except Exception:
        return None


def _fmt_cohort(p, audience_profile=""):
    """Formata o resumo da audiência (db.resumo_audiencia) como briefing legível para a IA."""
    if not p or not p.get("total"):
        return "público ainda sem dados de enriquecimento — escreva de forma genérica para empresas B2B, RH, SaaS e operacoes com programas de incentivo."

    def lst(key):
        return ", ".join(f"{x['valor']} ({x['n']})" for x in p.get(key, [])) or "—"
    perfil = f"Perfil de audiência selecionado no dashboard: {audience_profile}. " if audience_profile else ""
    return (f"{p['total']} contatos (empresas com programas de recompensas, RH, employee experience, vendas e plataformas SaaS). "
            f"{perfil}"
            f"Temperatura (quente/morno/frio): {lst('temperatura')}; dominante: "
            f"{p.get('temperatura_dominante') or 'indefinida'}. "
            f"Porte/tamanho: {lst('porte')}. Sistemas/stack: {lst('erp')}. "
            f"Cargo/persona: {lst('cargo')}. Estágio do negócio: {lst('estado')}. "
            f"Etapa de funil (CRM): {lst('etapa_funil')}. "
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


# Antes do 1º email concluído não há amostra de duração: o ETA usa este chute
# (rotulado eta_estimada=True na resposta) e passa a usar a média real depois.
CHUTE_EMAIL_S = 90
MAX_LOG_BUILD = 60


def _com_eta(p):
    """Deriva percent/eta_s na LEITURA (registros antigos ganham os campos sem migração).
    eta_s = média das durações já medidas × itens restantes."""
    total, feitos = p.get("total") or 0, p.get("feitos") or 0
    p["percent"] = int(feitos * 100 / total) if total else 0
    if p.get("status") in ("gerando", "gerando_rascunhos") and total:
        dur = [d for d in (p.get("duracoes_s") or []) if d]
        media = (sum(dur) / len(dur)) if dur else (p.get("chute_item_s") or CHUTE_EMAIL_S)
        p["eta_s"] = int(media * max(0, total - feitos))
        p["eta_estimada"] = not dur
    return p


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
    return _com_eta(p)


def _set_progress(cid, **kw):
    p = build_progress(cid) or {}
    p.update(kw)
    # percent/eta_s/eta_estimada são derivados na leitura; não persistir valores velhos
    for k in ("percent", "eta_s", "eta_estimada"):
        p.pop(k, None)
    db.meta_set(f"build_{cid}", json.dumps(p, ensure_ascii=False))


def _log_progress(cid, msg):
    """Linha do tempo do raciocínio da IA, visível na UI durante (e depois) do build."""
    p = build_progress(cid) or {}
    log = (p.get("log") or [])[-(MAX_LOG_BUILD - 1):]
    log.append({"ts": db.now(), "msg": str(msg)[:400]})
    _set_progress(cid, log=log)


REGRA_PUBLICO = ("REGRA DE PÚBLICO (NUNCA violar): prospecção NÃO tem como alvo quem já é CLIENTE "
                 "4Unik. Escreva como abordagem a EMPRESAS B2B que ainda não são clientes.")


# Mapa segmento → estratégia de copy (temperatura dominante do cohort define o bloco
# injetado no planejamento e em cada email). Os baits são ganchos de engajamento,
# nunca promessas: tudo que for oferta/prova continua limitado ao que está nos KBs.
ESTRATEGIA_FUNIL = {
    "frio": {
        "rotulo": "FRIO / topo de funil (TOF) — o lead não lembra da 4Unik",
        "diretrizes": (
            "- Abra com pattern interrupt: primeira linha inesperada, específica e curta; nada de 'espero que esteja bem'.\n"
            "- Gancho de curiosidade ou guerrilha: contraste provocador, pergunta incômoda sobre a operação, benchmark do setor.\n"
            "- Use baits de engajamento leves: 'responda com X e te mando Y', mini-diagnóstico gratuito, benchmark comparativo.\n"
            "- ZERO pressão de demo no 1º email: o CTA inicial pede micro-compromisso (responder, ver benchmark), não agenda.\n"
            "- Tom: curioso e generoso; eduque antes de vender."),
        "cadencia": ("E1 pattern interrupt + dor latente, CTA de micro-compromisso (sem demo) → "
                     "E2 como a solução funciona, ancorada na dor do E1 → "
                     "E3 prova/benchmark ou hipótese rotulada + bait de resposta → "
                     "E4 gancho vertical (recorte por setor/persona) e último toque, CTA leve de conversa"),
    },
    "morno": {
        "rotulo": "MORNO / meio de funil (MOF) — já houve contato, falta convicção",
        "diretrizes": (
            "- Lidere com prova: caso, dado ou contraste antes/depois que esteja nos KBs (ou hipótese claramente rotulada).\n"
            "- Quebre a objeção mais provável do cohort (integração, esforço de implantação, ROI incerto) de frente.\n"
            "- Use bait de conteúdo: material, comparativo ou diagnóstico que aprofunde a conversa.\n"
            "- CTA intermediário: convite para ver algo concreto (walkthrough, benchmark), demo só como opção natural.\n"
            "- Tom: consultivo; retome o contexto sem soar cobrança."),
        "cadencia": ("E1 reabertura com prova ou contraste antes/depois → "
                     "E2 quebra da objeção dominante com mecânica concreta → "
                     "E3 caso/ROI com premissas explícitas + bait de conteúdo → "
                     "E4 recorte vertical/último toque com convite direto a conversa"),
    },
    "quente": {
        "rotulo": "QUENTE / fundo de funil (BOF) — pronto para decisão",
        "diretrizes": (
            "- ROI concreto: números e premissas dos KBs, explicitando cada premissa; nunca retorno garantido.\n"
            "- Urgência honesta: janela real (ciclo de implantação, planejamento de trimestre), sem escassez fabricada.\n"
            "- Ataque a objeção final (preço, timing, aprovação interna) com resposta objetiva.\n"
            "- CTA direto de demo/agenda (Calendly/WhatsApp) em TODOS os emails, sem rodeio.\n"
            "- Tom: direto e respeitoso do tempo do lead; emails mais curtos."),
        "cadencia": ("E1 dor já validada + ROI concreto com premissas, CTA de agenda → "
                     "E2 mecânica da solução e esforço real de implantação → "
                     "E3 prova/ROI aprofundado + quebra da objeção final → "
                     "E4 último toque vertical, urgência honesta e CTA direto de demo"),
    },
}


def _estrategia_bloco(temperatura):
    """Bloco de estratégia por temperatura dominante do cohort, injetado nos prompts.
    Sem temperatura conhecida, assume 'frio' (abordagem mais conservadora)."""
    e = ESTRATEGIA_FUNIL.get((temperatura or "").strip().lower()) or ESTRATEGIA_FUNIL["frio"]
    return (f"ESTRATÉGIA DO SEGMENTO — público dominante: {e['rotulo']}\n"
            f"{e['diretrizes']}\n"
            f"ESQUELETO DE CADÊNCIA (adapte ao nº de emails da campanha; base: "
            f"E1 dor/gancho → E2 solução → E3 prova/ROI → E4 vertical/último toque):\n"
            f"{e['cadencia']}")


def _plano_campanha(objetivo, n_emails, tipo, cadencia, kb, brief, temperatura=""):
    """1ª chamada (leve): só o esqueleto — nome, resumo e o ângulo de cada email. Rápida."""
    tipo_desc = "os emails se repetem em ciclo contínuo" if tipo == "infinita" else "sequência única"
    estrategia = _estrategia_bloco(temperatura)
    return _ai_json(f"""Você é o estrategista de email marketing da 4Unik (Time 4Unik).
PLANEJE (NÃO escreva os emails ainda) uma campanha de {n_emails} emails. Tipo: {tipo} ({tipo_desc}). {cadencia}.
Distribua a mensagem do início ao fim, em ordem estratégica, SEM repetir ângulos entre os emails.
Siga o ESQUELETO DE CADÊNCIA da estratégia do segmento abaixo, adaptando-o ao nº de emails.
Devolva SOMENTE JSON válido, sem markdown:
{{"nome": "nome da campanha", "resumo": "1 frase",
 "insight_gtm": {{"icp":"persona e dor", "gancho":"ângulo de entrada", "prova":"case/dado ou hipótese rotulada", "cta":"CTA", "evento_ga4":"um evento permitido", "utm_source":"4unik-mail", "utm_medium":"email"}},
 "emails": [{{"nome": "título curto do email", "angulo": "o foco/conteúdo deste email em 1-2 frases", "offset_dias": 0}}]}}

CONTEXTO (KBs da 4Unik):
{kb}

PERFIL DO PÚBLICO (cohort):
{brief}

{estrategia}

{REGRA_PUBLICO}

{GUIA_GTM_CAMPANHAS}

OBJETIVO DA CAMPANHA: {objetivo}""", timeout=180)


def _gerar_email(objetivo, e, todos_angulos, ordem, n, kb, imgs, brief, estrategia):
    """2ª..N+1ª chamadas (uma por email): escreve UM email completo. Cada uma pequena/rápida."""
    return _ai_json(f"""Você é o copywriter da 4Unik (Time 4Unik). Escreva O EMAIL {ordem} DE {n} de uma campanha.
Devolva SOMENTE JSON válido, sem markdown:
{{"assunto": "assunto principal",
 "assuntos_alternativos": ["variante com abordagem de CURIOSIDADE",
   "variante com ESPECIFICIDADE NUMÉRICA ou GUERRILHA/CONTRASTE"],
 "preheader": "...", "cta": "rótulo do botão",
 "cta_url": "url do CTA (Calendly/WhatsApp do contexto, ou vazio)",
 "inner_html": "HTML do miolo", "texto_plano": "versão text/plain completa"}}

REGRA DOS ASSUNTOS: além do assunto principal, escreva EXATAMENTE 2 variantes com abordagens
DISTINTAS entre si e do principal (curiosidade, especificidade numérica, guerrilha/contraste).
Números só se vierem do contexto/KBs. Todas devem funcionar para o mesmo corpo de email.

{GUIA_HTML}

CONTEXTO (KBs da 4Unik):
{kb}

IMAGENS DISPONÍVEIS: {', '.join(imgs) or 'nenhuma'}

PERFIL DO PÚBLICO (cohort) — escreva a copy SOB MEDIDA para este perfil:
{brief}

{estrategia}

VARIÁVEIS DE PERSONALIZAÇÃO (use só as que fizerem sentido para um público homogêneo):
{{{{nome}}}}, {{{{empresa}}}}, {{{{cargo}}}}, {{{{porte}}}} (ex.: "Até 500 colaboradores"), {{{{erp}}}}.

{REGRA_PUBLICO}

{GUIA_GTM_CAMPANHAS}

SEQUÊNCIA COMPLETA (para você NÃO repetir o que vai nos outros emails):
{todos_angulos}

>>> ESTE É O EMAIL {ordem}: "{e.get('nome', '')}" — ângulo: {e.get('angulo', '')}
OBJETIVO GERAL DA CAMPANHA: {objetivo}""", timeout=240)


def _revisar_email(email_json, estrategia, brief):
    """Passo de crítica (editor-chefe): avalia gancho, aderência ao funil/segmento, CTA e
    clareza; pode reescrever assunto/preheader/cta e ajustar o texto. Uma chamada por email.
    Qualquer falha (exceção, JSON incompleto) devolve o email original — nunca quebra o build."""
    try:
        rev = _ai_json(f"""Você é o editor-chefe de email marketing da 4Unik (Time 4Unik).
Revise o email abaixo como um editor exigente. Avalie:
1. GANCHO: o assunto e a primeira linha param o scroll? São específicos, não genéricos?
2. FUNIL/SEGMENTO: a copy respeita a estratégia do segmento abaixo (pressão de CTA, tom, baits)?
3. CTA: único, coerente com a etapa, rótulo acionável? O botão oficial é renderizado pelo
   SISTEMA (campos cta/cta_url): se o inner_html contiver QUALQUER botão (âncora com
   display:inline-block/background/padding), REMOVA-O do inner_html — senão o e-mail sai com
   dois botões repetidos. Se houver um 2º pedido de ação no corpo, ele deve ser DIFERENTE do
   CTA do botão (nunca o mesmo pedido/rótulo duas vezes).
4. CLAREZA: frases curtas, sem jargão vazio, benefício concreto?
Reescreva o que estiver fraco (assunto, assuntos_alternativos, preheader, cta e ajustes no texto).
Se já estiver forte, devolva-o como está. NÃO acrescente fatos, preços, cases ou números novos;
preserve as variáveis {{{{nome}}}}, {{{{empresa}}}} etc. e o estilo inline do HTML.
Devolva SOMENTE JSON válido, sem markdown, no MESMO formato do email (campos: assunto,
assuntos_alternativos, preheader, cta, cta_url, inner_html, texto_plano).

{estrategia}

{REGRA_PUBLICO}

{GUIA_GTM_CAMPANHAS}

PERFIL DO PÚBLICO (cohort):
{brief}

EMAIL A REVISAR:
{json.dumps(email_json, ensure_ascii=False)}""", timeout=240)
        if not isinstance(rev, dict) or not (rev.get("assunto") and rev.get("inner_html")
                                             and rev.get("texto_plano")):
            return email_json
        # campos ausentes na revisão herdam o original (revisão parcial não perde conteúdo)
        return {**email_json, **{k: v for k, v in rev.items() if v}}
    except Exception as ex:
        print(f"[revisão de email] falhou, mantendo original: {ex}", flush=True)
        return email_json


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


def _gerar_loop(cid, objetivo, emails, cad_min, kb, imgs, brief, temperatura=""):
    """Gera os emails do plano um a um, PULANDO os que já têm step salvo (retomada idempotente).
    Compartilhado pelo build inicial e pela retomada — continua de onde a campanha parou."""
    estrategia = _estrategia_bloco(temperatura)
    angulos = "; ".join(f"{i}) {e.get('nome', '')}: {e.get('angulo', '')}"
                        for i, e in enumerate(emails, start=1))
    feitos = _steps_existentes(cid)
    for i in range(feitos + 1, len(emails) + 1):
        if _geracao_cancelada(cid):
            return False
        e = emails[i - 1]
        t0 = time.time()
        _set_progress(cid, etapa=f"escrevendo e-mail {i} de {len(emails)}: {e.get('nome', '')}",
                      feitos=i - 1)
        full = _gerar_email(objetivo, e, angulos, i, len(emails), kb, imgs, brief, estrategia)
        if _geracao_cancelada(cid):
            return False
        _log_progress(cid, f"e-mail {i} escrito — assunto: “{full.get('assunto', '')}”")
        _set_progress(cid, etapa=f"revisando gancho/CTA do e-mail {i} de {len(emails)}")
        antes = {k: (full.get(k) or "") for k in ("assunto", "preheader", "cta")}
        full = _revisar_email(full, estrategia, brief)
        # A chamada acima pode levar minutos. Revalida antes de qualquer escrita
        # para honrar uma pausa feita enquanto a IA estava trabalhando.
        if _geracao_cancelada(cid):
            return False
        mudou = [f"{k}: “{antes[k]}” → “{full.get(k, '')}”"
                 for k in antes if (full.get(k) or "") != antes[k]]
        _log_progress(cid, f"revisão do e-mail {i}: " +
                      ("ajustou " + " · ".join(mudou) if mudou
                       else "aprovado sem ajustes de assunto/CTA"))
        alts = full.get("assuntos_alternativos") or []
        if not isinstance(alts, list):
            alts = []
        alts_json = json.dumps([str(a) for a in alts[:2]], ensure_ascii=False)
        c = db.conn()
        versao = c.execute("SELECT versao FROM campaigns WHERE id=?", (cid,)).fetchone()
        vcamp = int((versao["versao"] if versao else None) or 1)
        c.execute("""INSERT INTO templates_ai(nome,assunto,preheader,cta,cta_url,inner_html,texto_plano,
                                              status,origem,assuntos_alt,campaign_id,versao_campanha)
                     VALUES(?,?,?,?,?,?,?,'pendente','ia',?,?,?)""",
                  (e.get("nome", f"Email {i}"), full.get("assunto", ""), full.get("preheader", ""),
                   full.get("cta", ""), full.get("cta_url", ""), full.get("inner_html", ""),
                   full.get("texto_plano", ""), alts_json, cid, vcamp))
        tid = c.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
        offset = e.get("offset_dias") if cad_min is None else None
        c.execute("INSERT INTO campaign_steps(campaign_id,ordem,template_ref,offset_dias) VALUES(?,?,?,?)",
                  (cid, i, f"ai:{tid}", offset))
        c.commit()
        c.close()
        dur = ((build_progress(cid) or {}).get("duracoes_s") or [])[-19:]
        dur.append(round(time.time() - t0, 1))
        _set_progress(cid, feitos=i, duracoes_s=dur)
    if _geracao_cancelada(cid):
        return False
    _set_progress(cid, etapa="escrevendo o briefing de lançamento…", feitos=len(emails))
    _log_progress(cid, "todos os e-mails gerados — preparando o briefing de lançamento")
    try:                                   # best-effort: falha não bloqueia o build
        gerar_briefing_lancamento(cid)
        _log_progress(cid, "briefing de lançamento pronto — revise antes de ativar")
    except Exception as ex:
        print(f"[briefing campanha {cid}] falhou (gere manualmente): {ex}", flush=True)
    _set_progress(cid, status="pronto", etapa="concluído", feitos=len(emails))
    return True


def _cadencia_desc(cad_min, cad_max):
    return (f"cadência ALEATÓRIA entre {cad_min} e {cad_max} dias entre toques"
            if cad_min is not None else "cadência fixa (defina offset_dias por email)")


def _build_worker(cid, objetivo, tipo, n_emails, cad_min, cad_max, kb, imgs, brief, temperatura=""):
    """Gera a campanha email-por-email em background (sem nunca estourar timeout)."""
    try:
        _set_progress(cid, status="gerando", total=n_emails, feitos=0,
                      etapa="planejando a sequência…", iniciado_em=db.now(),
                      duracoes_s=[], log=[])
        _log_progress(cid, f"estrategista planejando {n_emails} e-mail(s) para cohort "
                           f"{temperatura or 'sem temperatura dominante'}")
        plano = _plano_campanha(objetivo, n_emails, tipo, _cadencia_desc(cad_min, cad_max),
                                kb, brief, temperatura)
        emails = (plano.get("emails") or [])[:n_emails]
        if not emails:
            raise RuntimeError("a IA não devolveu o plano de emails")
        c = db.conn()
        insight = plano.get("insight_gtm") or {}
        contexto = "\n\n".join(x for x in (
            plano.get("resumo", ""),
            f"ICP: {insight.get('icp', '')}" if insight.get("icp") else "",
            f"Gancho: {insight.get('gancho', '')}" if insight.get("gancho") else "",
            f"Prova: {insight.get('prova', '')}" if insight.get("prova") else "",
            f"GA4: {insight.get('evento_ga4', '')}" if insight.get("evento_ga4") else "",
            f"Segmento: {temperatura}" if temperatura else "",
        ) if x)
        c.execute("""UPDATE campaigns SET nome=?, contexto=?,
                            atualizada_em=COALESCE(NULLIF(atualizada_em,''), ?)
                     WHERE id=?""",
                  (plano.get("nome") or f"Campanha — {objetivo[:40]}", contexto,
                   db.now(), cid))
        c.commit()
        c.close()
        # persiste o plano (ângulos) p/ uma retomada futura ser exata (sem replanejar)
        _set_progress(cid, total=len(emails), plano_emails=emails, temperatura=temperatura)
        _log_progress(cid, f"plano pronto — ICP: {insight.get('icp', '—')} · "
                           f"gancho: {insight.get('gancho', '—')} · "
                           f"prova: {insight.get('prova', '—')}")
        _log_progress(cid, "ângulos escolhidos: " + "; ".join(
            f"E{i} “{e.get('nome', '')}” ({e.get('angulo', '')})"
            for i, e in enumerate(emails, start=1)))
        _gerar_loop(cid, objetivo, emails, cad_min, kb, imgs, brief, temperatura)
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
        resumo = db.resumo_audiencia(emails_aud)
        brief = _fmt_cohort(resumo, camp["audience_profile"] or "")
        prog = build_progress(cid) or {}
        temperatura = prog.get("temperatura") or resumo.get("temperatura_dominante") or ""
        emails = prog.get("plano_emails")
        if not emails:
            total = prog.get("total") or max(1, _steps_existentes(cid))
            _set_progress(cid, status="gerando", etapa="replanejando a sequência…")
            plano = _plano_campanha(objetivo, total, camp["tipo"],
                                    _cadencia_desc(cad_min, camp["cad_max_dias"]), kb, brief,
                                    temperatura)
            emails = (plano.get("emails") or [])[:total]
            if not emails:
                raise RuntimeError("a IA não devolveu o plano de emails")
            _set_progress(cid, plano_emails=emails, temperatura=temperatura)
        _set_progress(cid, status="gerando", total=len(emails), erro="", erro_tipo="")
        _gerar_loop(cid, objetivo, emails, cad_min, kb, imgs, brief, temperatura)
    except Exception as ex:
        _set_progress(cid, status="erro", erro=str(ex)[:300], erro_tipo=_tipo_erro(str(ex)))
        print(f"[retomada campanha {cid}] ERRO: {ex}", flush=True)


def retomar_campanha(cid):
    """Dispara a retomada da geração em background. Só cria templates 'pendente' + steps."""
    _set_progress(cid, status="gerando", erro="", erro_tipo="", etapa="retomando…")
    threading.Thread(target=_resume_worker, args=(cid,), daemon=True).start()
    return cid


# ---------------- reescrita dos e-mails com novas diretrizes ----------------
# Quando os e-mails gerados não agradam, a reescrita cria NOVAS versões guiadas
# pelas diretrizes do usuário + análise da campanha (métricas reais) + briefing
# de lançamento. Guardrails: novas versões nascem 'pendente' (exigem aprovação),
# os steps passam a apontar para elas e as versões antigas ficam preservadas em
# templates_ai como histórico. Só campanhas NÃO ativas podem ser reescritas.

def _analise_campanha(cid):
    """Resumo objetivo de desempenho + briefing de lançamento, injetado no prompt
    de reescrita para a IA decidir O QUE mudar (não só COMO escrever)."""
    c = db.conn()
    enviados = c.execute("SELECT COUNT(*) AS n FROM sends WHERE campaign_id=?",
                         (cid,)).fetchone()["n"]
    aberturas = c.execute("SELECT COUNT(DISTINCT track_id) AS n FROM events "
                          "WHERE campaign_id=? AND type='open'", (cid,)).fetchone()["n"]
    cliques = c.execute("SELECT COUNT(DISTINCT track_id) AS n FROM events "
                        "WHERE campaign_id=? AND type='click'", (cid,)).fetchone()["n"]
    respostas = c.execute("""SELECT COUNT(DISTINCT r.msgid) AS n FROM replies r
                             JOIN sends s ON s.thread=r.thread
                             WHERE s.campaign_id=? AND r.thread!=''""", (cid,)).fetchone()["n"]
    c.close()
    partes = []
    if enviados:
        partes.append(f"DESEMPENHO REAL até agora: {enviados} enviados, {aberturas} aberturas, "
                      f"{cliques} cliques, {respostas} respostas. Se aberturas estão baixas o "
                      f"problema é assunto/remetente; se abrem e não clicam, é corpo/CTA.")
    else:
        partes.append("A campanha ainda NÃO teve envios — a reescrita é preventiva "
                      "(baseie-se nas diretrizes e no briefing).")
    b = briefing_lancamento(cid) or {}
    if b:
        riscos = "; ".join(str(r) for r in (b.get("riscos") or []))
        partes.append(f"BRIEFING DE LANÇAMENTO — resumo: {b.get('resumo', '—')} · "
                      f"recomendação: {b.get('recomendacao', '—')} "
                      f"({b.get('justificativa', '')}) · riscos apontados: {riscos or '—'}")
    return "\n".join(partes)


def _reescrever_email(objetivo, atual, diretrizes, analise, ordem, n, kb, imgs, brief,
                      estrategia, angulos):
    """Uma chamada por e-mail: reescreve a versão atual seguindo as novas diretrizes."""
    return _ai_json(f"""Você é o copywriter da 4Unik (Time 4Unik). REESCREVA o e-mail {ordem} de {n}
de uma campanha existente. As NOVAS DIRETRIZES abaixo têm PRIORIDADE MÁXIMA — elas vieram do dono da
campanha após analisar a versão atual. Mantenha o que funciona; mude o que as diretrizes pedirem.
Devolva SOMENTE JSON válido, sem markdown:
{{"assunto": "assunto principal",
 "assuntos_alternativos": ["variante com abordagem de CURIOSIDADE",
   "variante com ESPECIFICIDADE NUMÉRICA ou GUERRILHA/CONTRASTE"],
 "preheader": "...", "cta": "rótulo do botão",
 "cta_url": "url do CTA (Calendly/WhatsApp do contexto, ou vazio)",
 "inner_html": "HTML do miolo", "texto_plano": "versão text/plain completa"}}

NOVAS DIRETRIZES DO DONO DA CAMPANHA (prioridade máxima):
{diretrizes}

ANÁLISE DA CAMPANHA (use como insight para decidir o que mudar):
{analise}

{GUIA_HTML}

CONTEXTO (KBs da 4Unik):
{kb}

IMAGENS DISPONÍVEIS: {', '.join(imgs) or 'nenhuma'}

PERFIL DO PÚBLICO (cohort):
{brief}

{estrategia}

VARIÁVEIS DE PERSONALIZAÇÃO (use só as que fizerem sentido):
{{{{nome}}}}, {{{{empresa}}}}, {{{{cargo}}}}, {{{{porte}}}}, {{{{erp}}}}.

{REGRA_PUBLICO}

{GUIA_GTM_CAMPANHAS}

SEQUÊNCIA COMPLETA (para NÃO repetir o que vai nos outros e-mails):
{angulos}

>>> VERSÃO ATUAL DO E-MAIL {ordem} (a melhorar segundo as diretrizes):
{json.dumps(atual, ensure_ascii=False)}

OBJETIVO GERAL DA CAMPANHA: {objetivo}""", timeout=240)


def _rewrite_worker(cid, diretrizes, ordens):
    """Reescreve os e-mails selecionados em background, um a um, com progresso na
    mesma infra do build (a tela da campanha mostra a barra e o raciocínio)."""
    try:
        c = db.conn()
        camp = c.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
        steps = c.execute("""SELECT cs.ordem, cs.template_ref, t.nome, t.assunto, t.preheader,
                                    t.cta, t.cta_url, t.inner_html, t.texto_plano
                             FROM campaign_steps cs
                             LEFT JOIN templates_ai t ON cs.template_ref='ai:'||t.id
                             WHERE cs.campaign_id=? ORDER BY cs.ordem""", (cid,)).fetchall()
        emails_aud = [r["email"] for r in c.execute(
            "SELECT email FROM campaign_contacts WHERE campaign_id=?", (cid,)).fetchall()]
        c.close()
        if not camp:
            raise RuntimeError("campanha não encontrada")
        # steps builtin (campanha de template fixo) não têm o que reescrever aqui
        alvo = [s for s in steps if (s["inner_html"] or "")
                and (not ordens or s["ordem"] in ordens)]
        if not alvo:
            raise RuntimeError("nenhum e-mail de IA elegível para reescrita "
                               "(templates fixos não são reescritos)")
        _set_progress(cid, status="gerando", total=len(alvo), feitos=0, iniciado_em=db.now(),
                      etapa="analisando a campanha e as novas diretrizes…", duracoes_s=[])
        _log_progress(cid, f"reescrita iniciada ({len(alvo)} e-mail(s)) — diretrizes: "
                           f"{diretrizes[:220]}")
        objetivo = camp["objetivo"] or ""
        kb, imgs = contexto()
        resumo = db.resumo_audiencia(emails_aud)
        brief = _fmt_cohort(resumo, camp["audience_profile"] or "")
        temperatura = ((build_progress(cid) or {}).get("temperatura")
                       or resumo.get("temperatura_dominante") or "")
        estrategia = _estrategia_bloco(temperatura)
        analise = _analise_campanha(cid)
        angulos = "; ".join(f"{s['ordem']}) {s['nome'] or ''}: assunto atual “{s['assunto'] or ''}”"
                            for s in steps)
        feitos = 0
        templates_novos = []
        # bump de versão ANTES de gravar os novos templates (eles herdam a versão nova)
        nova_versao = db.campaign_bump_versao(
            cid, diretrizes=diretrizes, ordens=[s["ordem"] for s in alvo], origem="rewrite")
        _log_progress(cid, f"campanha agora na versão {nova_versao}")
        for s in alvo:
            if _geracao_cancelada(cid):
                return
            t0 = time.time()
            _set_progress(cid, etapa=f"reescrevendo e-mail {s['ordem']}: {s['nome'] or ''}",
                          feitos=feitos)
            atual = {k: (s[k] or "") for k in ("assunto", "preheader", "cta", "cta_url",
                                               "inner_html", "texto_plano")}
            novo = _reescrever_email(objetivo, atual, diretrizes, analise, s["ordem"],
                                     len(steps), kb, imgs, brief, estrategia, angulos)
            if not isinstance(novo, dict) or not (novo.get("assunto") and novo.get("inner_html")
                                                  and novo.get("texto_plano")):
                raise RuntimeError(f"a IA não devolveu o e-mail {s['ordem']} reescrito completo")
            if _geracao_cancelada(cid):
                return
            _set_progress(cid, etapa=f"revisando gancho/CTA do e-mail {s['ordem']}")
            novo = _revisar_email(novo, estrategia, brief)
            if _geracao_cancelada(cid):
                return
            alts = novo.get("assuntos_alternativos")
            alts = alts if isinstance(alts, list) else []
            alts_json = json.dumps([str(a) for a in alts[:2]], ensure_ascii=False)
            antigo_id = None
            ref = s["template_ref"] or ""
            if ref.startswith("ai:"):
                try:
                    antigo_id = int(ref.split(":", 1)[1])
                except ValueError:
                    antigo_id = None
            c = db.conn()
            c.execute("""INSERT INTO templates_ai(nome,assunto,preheader,cta,cta_url,inner_html,
                                                  texto_plano,status,origem,assuntos_alt,
                                                  campaign_id,versao_campanha)
                         VALUES(?,?,?,?,?,?,?,'pendente','ia',?,?,?)""",
                      (s["nome"] or f"Email {s['ordem']}", novo.get("assunto", ""),
                       novo.get("preheader", ""), novo.get("cta", ""), novo.get("cta_url", ""),
                       novo.get("inner_html", ""), novo.get("texto_plano", ""), alts_json,
                       cid, nova_versao))
            tid = c.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
            if antigo_id:
                c.execute("UPDATE templates_ai SET substituido_por=? WHERE id=?",
                          (tid, antigo_id))
            c.execute("UPDATE campaign_steps SET template_ref=? WHERE campaign_id=? AND ordem=?",
                      (f"ai:{tid}", cid, s["ordem"]))
            c.commit()
            c.close()
            templates_novos.append({"ordem": s["ordem"], "template_id": tid,
                                    "antigo_id": antigo_id})
            feitos += 1
            dur = ((build_progress(cid) or {}).get("duracoes_s") or [])[-19:]
            dur.append(round(time.time() - t0, 1))
            _set_progress(cid, feitos=feitos, duracoes_s=dur)
            _log_progress(cid, f"e-mail {s['ordem']} reescrito — assunto: "
                               f"“{novo.get('assunto', '')}” (antes: “{s['assunto'] or ''}”)")
        # completa o log da versão com os ids gerados
        try:
            log = json.loads(db.meta_get(f"rewrite_log_{cid}", "[]") or "[]")
            if isinstance(log, list) and log and log[-1].get("versao") == nova_versao:
                log[-1]["templates_novos"] = templates_novos
                db.meta_set(f"rewrite_log_{cid}", json.dumps(log[-40:], ensure_ascii=False))
        except Exception:
            pass
        _set_progress(cid, etapa="atualizando o briefing de lançamento…")
        try:                                   # best-effort: falha não bloqueia a reescrita
            gerar_briefing_lancamento(cid)
            _log_progress(cid, "briefing de lançamento atualizado com a nova versão dos e-mails")
        except Exception as ex:
            print(f"[reescrita campanha {cid}] briefing falhou (gere manualmente): {ex}", flush=True)
        _set_progress(cid, status="pronto",
                      etapa=f"reescrita concluída (v{nova_versao}) — revise e aprove os novos e-mails")
        _log_progress(cid, f"reescrita concluída — campanha v{nova_versao}; novos e-mails "
                           "PENDENTES; versões antigas guardadas como histórico")
    except Exception as ex:
        _set_progress(cid, status="erro", erro=str(ex)[:300], erro_tipo=_tipo_erro(str(ex)))
        print(f"[reescrita campanha {cid}] ERRO: {ex}", flush=True)


def reescrever_campanha(cid, diretrizes, ordens=None):
    """Dispara a reescrita em background. Valida os guardrails NA HORA (erro imediato
    na API); o trabalho pesado acontece no worker, acompanhável via build_progress."""
    diretrizes = (diretrizes or "").strip()
    if not diretrizes:
        raise ValueError("descreva as novas diretrizes para a reescrita")
    c = db.conn()
    camp = c.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
    n_steps = c.execute("SELECT COUNT(*) AS n FROM campaign_steps WHERE campaign_id=?",
                        (cid,)).fetchone()["n"]
    c.close()
    if not camp:
        raise ValueError("campanha não encontrada")
    if camp["status"] == "ativa":
        raise ValueError("pause a campanha antes de reescrever os e-mails")
    if not n_steps:
        raise ValueError("a campanha ainda não tem e-mails gerados")
    prog = build_progress(cid) or {}
    if prog.get("status") == "gerando":
        raise ValueError("aguarde: já existe uma geração em andamento nesta campanha")
    ordens = sorted({int(o) for o in (ordens or [])})
    _set_progress(cid, status="gerando", erro="", erro_tipo="",
                  etapa="preparando a reescrita…", feitos=0,
                  total=len(ordens) or n_steps)
    threading.Thread(target=_rewrite_worker, args=(cid, diretrizes, ordens),
                     daemon=True).start()
    return {"id": cid, "emails": ordens or "todos"}


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
    resumo = db.resumo_audiencia(audiencia)
    brief = _fmt_cohort(resumo, audience_profile)
    temperatura = resumo.get("temperatura_dominante") or ""

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

    _set_progress(cid, status="gerando", total=n_emails, feitos=0, etapa="iniciando…",
                  temperatura=temperatura)
    threading.Thread(target=_build_worker,
                     args=(cid, objetivo, tipo, n_emails, cad_min, cad_max, kb, imgs, brief,
                           temperatura),
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
    resumo = db.resumo_audiencia([email])
    brief = _fmt_cohort(resumo, "contato_individual")
    temperatura = resumo.get("temperatura_dominante") or (contato["segmento"] or "")
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
    _set_progress(cid, status="gerando", total=n_emails, feitos=0, etapa="iniciando…",
                  temperatura=temperatura)
    threading.Thread(target=_build_worker,
                     args=(cid, objetivo, "finita", n_emails, None, None, kb, imgs, brief,
                           temperatura),
                     daemon=True).start()
    return cid


def gerar_briefing_ads(campanha, steps):
    """Briefing Google Ads espelhando uma campanha de email: estrutura pronta para colar
    no Ads (ou importar via CSV do Ads Editor). Nada é criado na conta — só rascunho local."""
    kb, _ = contexto()
    slug = campanha.get("slug") or db.slugify(campanha.get("nome", "")) or f"c{campanha.get('id')}"
    emails = "\n".join(f"- etapa {s.get('ordem')}: {s.get('template_ref')}" for s in steps) or "—"
    res = _ai_json(f"""Você é o gestor de tráfego da 4Unik. Crie um briefing de campanha
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


# ---------------- briefing de lançamento (os porquês da campanha) ----------------
# Documento de decisão go/no-go: por que esta campanha, por que cada email,
# qual prova sustenta cada ângulo (e de onde veio), riscos e recomendação.
# É recomendação — a ativação continua manual.

def briefing_lancamento(cid):
    raw = db.meta_get(f"briefing_{cid}", "")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _dados_campanha_para_briefing(cid):
    c = db.conn()
    camp = c.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
    if not camp:
        c.close()
        raise ValueError("campanha não encontrada")
    steps = []
    for s in c.execute("""SELECT cs.ordem, cs.template_ref, cs.offset_dias, t.assunto, t.nome
                          FROM campaign_steps cs
                          LEFT JOIN templates_ai t ON cs.template_ref = 'ai:' || t.id
                          WHERE cs.campaign_id=? ORDER BY cs.ordem""", (cid,)).fetchall():
        steps.append({"ordem": s["ordem"], "nome": s["nome"] or s["template_ref"],
                      "assunto": s["assunto"] or "", "offset_dias": s["offset_dias"]})
    emails_aud = [r["email"] for r in c.execute(
        "SELECT email FROM campaign_contacts WHERE campaign_id=?", (cid,)).fetchall()]
    c.close()
    prog = build_progress(cid) or {}
    # se a campanha nasceu do portfólio, recupera a crítica do copywriter
    critica = ""
    port = suggest_status() or {}
    nome_port = next((x.get("nome") for x in port.get("criadas", []) if x.get("cid") == cid), None)
    if nome_port:
        sug = next((s for s in port.get("campanhas", []) if s.get("nome") == nome_port), {})
        critica = sug.get("critica", "")
    return dict(camp), steps, emails_aud, prog, critica


def gerar_briefing_lancamento(cid):
    """UMA chamada de IA que consolida o que já foi decidido/persistido em um
    briefing de decisão. Persiste em meta briefing_{cid} e devolve o JSON."""
    camp, steps, emails_aud, prog, critica = _dados_campanha_para_briefing(cid)
    kb, _ = contexto()
    resumo = db.resumo_audiencia(emails_aud)
    brief_cohort = _fmt_cohort(resumo, camp.get("audience_profile") or "")
    plano = prog.get("plano_emails") or []
    angulos = "\n".join(f"- E{i} “{e.get('nome', '')}”: {e.get('angulo', '')}"
                        for i, e in enumerate(plano, start=1)) or "—"
    seq = "\n".join(f"- E{s['ordem']} “{s['nome']}” — assunto: “{s['assunto']}”"
                    for s in steps) or "—"
    res = _ai_json(f"""Você é o estrategista GTM e editor-chefe da 4Unik (Time 4Unik).
Escreva o BRIEFING DE LANÇAMENTO desta campanha de email já gerada: os PORQUÊS que permitem
à 4Unik decidir se ativa ou não. Seja honesto: aponte riscos e fraquezas reais; a recomendação
"nao_lancar" ou "ajustar" é tão válida quanto "lancar". Toda prova citada deve indicar de QUAL
KB veio; se não estiver nos KBs, rotule como hipótese.
FORMATAÇÃO: use frases curtas e separe parágrafos com \\n\\n nos campos de prosa (resumo,
icp_e_dor, por_que_agora, porque_este_angulo, prova_e_origem, justificativa).
Devolva SOMENTE JSON válido, sem markdown:
{{"resumo": "a campanha em 2-3 frases (parágrafos com \\\\n\\\\n)",
 "icp_e_dor": "quem é o alvo e qual dor operacional concreta esta campanha ataca",
 "por_que_agora": "por que faz sentido para ESTE cohort neste momento (use os dados do cohort)",
 "por_email": [{{"ordem": 1, "assunto_esperado": "assunto real ou esperado deste e-mail",
   "porque_este_angulo": "por que este ângulo nesta posição da cadência",
   "prova_e_origem": "prova usada e KB de origem, ou 'hipótese rotulada: …'"}}],
 "riscos": ["risco concreto e por quê (ex.: base fria grande, cohort sem histórico, prova fraca)"],
 "medicao": {{"utm_campaign": "slug", "evento_ga4": "evento esperado",
   "metrica_decisao": "qual número decide se a campanha funcionou e em quanto tempo avaliar"}},
 "recomendacao": "lancar | ajustar | nao_lancar",
 "justificativa": "2-3 frases fundamentando a recomendação",
 "checklist_pre_lancamento": ["item objetivo a conferir antes de ativar"]}}

{REGRA_PUBLICO}

{GUIA_GTM_CAMPANHAS}

CONTEXTO (KBs da 4Unik):
{kb}

CAMPANHA:
Nome: {camp.get('nome')} · Tipo: {camp.get('tipo')} · Objetivo: {camp.get('objetivo')}
Contexto/plano registrado: {camp.get('contexto')}
Temperatura do cohort: {prog.get('temperatura') or resumo.get('temperatura_dominante') or '—'}

ÂNGULOS PLANEJADOS:
{angulos}

SEQUÊNCIA GERADA (assuntos reais):
{seq}

PERFIL REAL DO PÚBLICO (cohort):
{brief_cohort}

CRÍTICA DO COPYWRITER NO PORTFÓLIO (se houver): {critica or '—'}""", timeout=240)
    res["gerado_em"] = db.now()
    res["campanha_id"] = cid
    db.meta_set(f"briefing_{cid}", json.dumps(res, ensure_ascii=False))
    return res


# ---------------- loop de sugestão de campanhas (portfólio) ----------------
# Camada ACIMA de criar_campanha(): o estrategista GTM propõe o portfólio
# completo de campanhas por ICP/temperatura, o copywriter critica e refina em
# loop, e a geração cria cada sugestão como RASCUNHO pela pipeline existente.
# Guardrail inalterado: nada é ativado nem enviado sem aprovação humana.

SUGGEST_KEY = "campaign_suggestions"
MAX_RODADAS_PORTFOLIO = 2


def suggest_status():
    raw = db.meta_get(SUGGEST_KEY, "")
    if not raw:
        return None
    try:
        return _com_eta(json.loads(raw))
    except Exception:
        return None


def _suggest_set(**kw):
    p = suggest_status() or {}
    p.update(kw)
    for k in ("percent", "eta_s", "eta_estimada"):
        p.pop(k, None)
    p["atualizado_em"] = db.now()
    db.meta_set(SUGGEST_KEY, json.dumps(p, ensure_ascii=False))


def _briefing_cohorts():
    """Retrato agregado do CRM por temperatura (sem PII) + visão geral do ciclo
    de estratégia já persistido em meta (evita reimportar strategy: circular)."""
    partes = []
    for temp in ("quente", "morno", "frio"):
        try:
            emails = db.resolve_audience([temp], None, True)
            resumo = db.resumo_audiencia(emails)
        except Exception:
            resumo = {"total": 0}
        partes.append(f"--- COHORT {temp.upper()} ({resumo.get('total', 0)} contatos) ---\n"
                      f"{_fmt_cohort(resumo)}")
    try:
        snap = json.loads(db.meta_get("strategy_loop", "") or "{}")
        partes.append("--- VISÃO GERAL DO FUNIL ---\n"
                      f"Campanhas ativas: {snap.get('campanhas_ativas', 0)}; "
                      f"rascunhos aguardando aprovação: {snap.get('campanhas_rascunho', 0)}.")
    except Exception:
        pass
    return "\n\n".join(partes)


_FORMATO_PORTFOLIO = """{"campanhas": [{
  "nome": "nome curto da campanha",
  "icp": "persona e dor operacional concreta",
  "objetivo": "objetivo da campanha em 1-2 frases (vira o briefing da geração dos emails)",
  "segmentos": ["quente" ou "morno" ou "frio" — 1 temperatura por campanha],
  "n_emails": 3,
  "tipo": "finita",
  "cad_min": null, "cad_max": null,
  "gancho": "ângulo de entrada",
  "prova": "case/dado dos KBs ou hipótese CLARAMENTE rotulada",
  "evento_ga4": "generate_lead | schedule_demo | contact_whatsapp | contact_email",
  "utm_campaign": "slug-da-campanha"}]}"""


def _portfolio_estrategista(kb, brief, max_campanhas, feedback=""):
    extra = (f"\nFEEDBACK DO COPYWRITER NA RODADA ANTERIOR (corrija tudo que foi apontado):\n{feedback}"
             if feedback else "")
    return _ai_json(f"""Você é o estrategista GTM da 4Unik (Time 4Unik), operando o skill
"GTM Engineer & Growth Specialist" (os KBs com prefixo 0-skill são os mds originais do skill:
trate-os como fonte de verdade de posicionamento, provas, planos e ICPs).
PROPONHA um portfólio de até {max_campanhas} campanhas de email DISTINTAS entre si — cada uma
com UM ICP dominante e UMA temperatura de cohort — cobrindo os cohorts com contatos disponíveis.
NÃO escreva os emails; só o plano de cada campanha. Prefira poucas campanhas fortes a variações
genéricas. Devolva SOMENTE JSON válido, sem markdown, neste formato:
{_FORMATO_PORTFOLIO}

Regras do plano: n_emails entre 3 e 5; tipo "finita" com cad_min/cad_max null (cadência fixa),
ou "infinita" com cad_min/cad_max em dias; utm_campaign em kebab-case; não proponha campanha
para cohort com 0 contatos.

CONTEXTO (skills + KBs da 4Unik):
{kb}

PERFIL DOS COHORTS DO CRM:
{brief}

{REGRA_PUBLICO}

{GUIA_GTM_CAMPANHAS}{extra}""", timeout=240)


def _portfolio_critica(portfolio, kb, brief):
    """Crítica do copywriter/editor-chefe sobre o portfólio. Devolve o portfólio
    refinado com 'aprovado' por campanha + 'aprovado_geral'. Falha nunca quebra o
    loop: devolve None e o chamador mantém a proposta original."""
    try:
        rev = _ai_json(f"""Você é o copywriter e editor-chefe da 4Unik (Time 4Unik).
Critique o portfólio de campanhas abaixo como um editor exigente:
1. ÂNGULOS: há campanhas repetindo gancho/ICP? Cada uma tem dor operacional concreta?
2. EVIDÊNCIA: toda prova está nos KBs ou rotulada como hipótese? Nada inventado?
3. FUNIL: a pressão de CTA combina com a temperatura do cohort (frio sem pressão de demo)?
4. MENSURAÇÃO: evento_ga4 permitido e utm_campaign coerente?
Refine o que estiver fraco (pode reescrever nome, objetivo, gancho, prova) e marque cada
campanha com "aprovado": true/false e "critica": "1 frase com o motivo". Remova campanhas
irrecuperáveis. Devolva SOMENTE JSON válido, sem markdown, no MESMO formato do portfólio,
acrescentando os campos "aprovado" e "critica" por campanha e, no topo,
"aprovado_geral": true/false (true = o portfólio inteiro está pronto para gerar).

CONTEXTO (skills + KBs da 4Unik):
{kb}

PERFIL DOS COHORTS DO CRM:
{brief}

{REGRA_PUBLICO}

{GUIA_GTM_CAMPANHAS}

PORTFÓLIO A CRITICAR:
{json.dumps(portfolio, ensure_ascii=False)}""", timeout=240)
        if not isinstance(rev, dict) or not rev.get("campanhas"):
            return None
        return rev
    except Exception as ex:
        print(f"[crítica do portfólio] falhou, mantendo proposta: {ex}", flush=True)
        return None


def sugerir_campanhas(max_campanhas=5):
    """Loop estrategista → crítica do copywriter (até MAX_RODADAS_PORTFOLIO rodadas).
    Persiste o portfólio em meta e devolve o status final. Não gera nada ainda."""
    max_campanhas = max(1, min(int(max_campanhas or 5), 8))
    sincronizados = sync_skill_kbs()
    kb, _ = contexto(MAX_KB_PORTFOLIO)
    _suggest_set(status="gerando", etapa="analisando cohorts do CRM…", erro="",
                 campanhas=[], criadas=[], skills_sincronizados=sincronizados)
    brief = _briefing_cohorts()
    campanhas, feedback = [], ""
    for rodada in range(1, MAX_RODADAS_PORTFOLIO + 1):
        _suggest_set(etapa=f"estrategista GTM propondo portfólio (rodada {rodada})…", rodada=rodada)
        proposta = _portfolio_estrategista(kb, brief, max_campanhas, feedback)
        campanhas = (proposta.get("campanhas") or [])[:max_campanhas]
        if not campanhas:
            raise RuntimeError("a IA não devolveu sugestões de campanha")
        _suggest_set(campanhas=campanhas, etapa=f"copywriter criticando o portfólio (rodada {rodada})…")
        rev = _portfolio_critica({"campanhas": campanhas}, kb, brief)
        if rev:
            campanhas = (rev.get("campanhas") or campanhas)[:max_campanhas]
            _suggest_set(campanhas=campanhas)
            if rev.get("aprovado_geral") and all(c.get("aprovado", True) for c in campanhas):
                break
            feedback = "; ".join(f"{c.get('nome', '?')}: {c.get('critica', '')}"
                                 for c in campanhas if c.get("critica"))
        else:
            break
    _suggest_set(status="pronto", campanhas=campanhas,
                 etapa="portfólio pronto — revise e gere os rascunhos")
    return suggest_status()


def gerar_portfolio(sender_id=1):
    """Gera cada sugestão aprovada como campanha RASCUNHO via criar_campanha(),
    SEQUENCIALMENTE (espera o build de uma terminar antes da próxima, para não
    disparar N gerações do claude em paralelo). Idempotente: pula as já criadas."""
    p = suggest_status() or {}
    aprovadas = [s for s in p.get("campanhas", []) if s.get("aprovado", True)]
    if not aprovadas:
        raise RuntimeError("nenhuma sugestão aprovada no portfólio — rode a sugestão antes")
    criadas = p.get("criadas") or []
    ja_criadas = {c.get("nome") for c in criadas}
    # ETA por campanha: plano + (escrita+revisão) por email — chute de ~90s por chamada
    medio_emails = sum(int(s.get("n_emails") or 3) for s in aprovadas) / len(aprovadas)
    _suggest_set(status="gerando_rascunhos", total=len(aprovadas), feitos=len(criadas),
                 duracoes_s=[], chute_item_s=int(CHUTE_EMAIL_S * (medio_emails + 1)))
    for i, s in enumerate(aprovadas, 1):
        if s.get("nome") in ja_criadas:
            continue
        t0 = time.time()
        _suggest_set(etapa=f"gerando rascunho {i} de {len(aprovadas)}: {s.get('nome', '')}")
        objetivo = " ".join(x for x in (
            s.get("objetivo", ""),
            f"ICP: {s.get('icp', '')}." if s.get("icp") else "",
            f"Gancho: {s.get('gancho', '')}." if s.get("gancho") else "",
            f"Prova (só se estiver nos KBs): {s.get('prova', '')}." if s.get("prova") else "",
        ) if x)
        segs = [t for t in (s.get("segmentos") or []) if t in ("quente", "morno", "frio")]
        cid = criar_campanha(objetivo, s.get("tipo") or "finita",
                             s.get("cad_min"), s.get("cad_max"),
                             s.get("n_emails") or 3, segs or None, True, None, sender_id)
        criadas.append({"cid": cid, "nome": s.get("nome", f"Campanha {i}")})
        _suggest_set(criadas=criadas)
        while True:                       # sequencial: espera este build acabar
            bp = build_progress(cid) or {}
            if bp.get("status") in ("pronto", "erro"):
                break
            time.sleep(5)
        dur = ((suggest_status() or {}).get("duracoes_s") or [])[-9:]
        dur.append(round(time.time() - t0, 1))
        _suggest_set(feitos=i, duracoes_s=dur)
    _suggest_set(status="pronto",
                 etapa="rascunhos gerados — revise, aprove os emails e ative no dashboard")
    return suggest_status()


def _portfolio_worker(max_campanhas, auto_gerar, sender_id):
    try:
        sugerir_campanhas(max_campanhas)
        if auto_gerar:
            gerar_portfolio(sender_id)
    except Exception as ex:
        _suggest_set(status="erro", erro=str(ex)[:300], erro_tipo=_tipo_erro(str(ex)))
        print(f"[sugestão de campanhas] ERRO: {ex}", flush=True)


def iniciar_sugestao(max_campanhas=5, auto_gerar=False, sender_id=1):
    """Dispara o loop de sugestão (e opcionalmente a geração) em background."""
    p = suggest_status() or {}
    if p.get("status") in ("gerando", "gerando_rascunhos"):
        return {"status": p["status"], "nota": "já existe um loop de sugestão em andamento",
                "etapa": p.get("etapa", "")}
    _suggest_set(status="gerando", etapa="iniciando…", erro="", campanhas=[], criadas=[])
    threading.Thread(target=_portfolio_worker,
                     args=(max_campanhas, auto_gerar, sender_id), daemon=True).start()
    return {"status": "gerando",
            "nota": "Sugestão de campanhas iniciada. Acompanhe em GET /api/campaigns/suggest."}


def iniciar_geracao_portfolio(sender_id=1):
    """Dispara a geração dos rascunhos do portfólio já sugerido, em background."""
    p = suggest_status() or {}
    if p.get("status") in ("gerando", "gerando_rascunhos"):
        return {"status": p["status"], "nota": "geração já em andamento", "etapa": p.get("etapa", "")}
    if not p.get("campanhas"):
        raise RuntimeError("nenhum portfólio sugerido — rode POST /api/campaigns/suggest antes")

    def worker():
        try:
            gerar_portfolio(sender_id)
        except Exception as ex:
            _suggest_set(status="erro", erro=str(ex)[:300], erro_tipo=_tipo_erro(str(ex)))
            print(f"[geração do portfólio] ERRO: {ex}", flush=True)
    _suggest_set(status="gerando_rascunhos", etapa="iniciando geração dos rascunhos…", erro="")
    threading.Thread(target=worker, daemon=True).start()
    return {"status": "gerando_rascunhos",
            "nota": "Geração dos rascunhos iniciada. Nada será ativado ou enviado automaticamente."}
