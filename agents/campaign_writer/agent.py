"""Agente ADK: copywriter/estrategista de campanhas de e-mail da 4Unik.

Equivale aos fluxos de geração de campanha do dashboard (ai.py: plano → copy →
revisão), pronto para deploy no Vertex AI Agent Engine. A saída é sempre JSON
de rascunho — quem aprova e envia é o dashboard.

Craft de assunto/corpo: skill Email Creative Builder (aaron-he-zhu /
aaron-marketing-skills, Apache-2.0), espelhado em 0-skill-email-* nos KBs.
"""
from pathlib import Path

from google.adk.agents import Agent

# Autocontido para o deploy no Agent Engine: só a pasta do agente é empacotada,
# então as KBs viajam como snapshot local (kb_snapshot.md, gerado a partir de
# dashboard/context/kb — regenere antes de redeploy quando as KBs mudarem).
_HERE = Path(__file__).resolve().parent

GUARDRAIL = (
    "GUARDRAIL: você produz SOMENTE rascunhos e classificações. Nunca afirme que "
    "algo foi enviado, publicado ou ativado — a aprovação e o envio são decisões "
    "do código e da equipe 4Unik, nunca suas."
)

SKILL_CREDIT = (
    "SKILL CREDIT: Email Creative Builder — aaron-he-zhu / aaron-marketing-skills "
    "(Apache-2.0). https://github.com/aaron-he-zhu/aaron-marketing-skills/tree/main/"
    "email/engage/email-creative-builder — use 0-skill-email-* nos KBs para craft "
    "de assuntos ousados, preheader, corpo e 1 CTA."
)


def kb_context(max_chars: int = 14000) -> str:
    snap = _HERE / "kb_snapshot.md"
    if snap.exists():
        return snap.read_text(encoding="utf-8", errors="replace")[:max_chars]
    kb_dir = _HERE.parent.parent / "dashboard" / "context" / "kb"
    if not kb_dir.exists():
        return ""
    partes = [f"### KB: {f.name}\n{f.read_text(encoding='utf-8', errors='replace')}"
              for f in sorted(kb_dir.glob("*.md"))]
    return "\n\n".join(partes)[:max_chars]

INSTRUCTION = f"""Você é o copywriter e estrategista de e-mail marketing da 4Unik (Time 4Unik).

Posicione a 4Unik como Infraestrutura de Engajamento Programável, API-first:
comportamento programável por rewards, gamificação, checkout e execução física.
Nunca use em copy os termos "brindes", "kits" ou "produtos promocionais".

{SKILL_CREDIT}

Ao receber um briefing, devolva SOMENTE JSON válido com a campanha:
{{"nome": "...", "objetivo": "...",
  "emails": [{{"ordem": 1,
               "assunto": "assunto principal ousado",
               "assuntos_alternativos": ["curiosidade", "benefício/especificidade", "pergunta ou contraste"],
               "preheader": "...",
               "cta": "rótulo", "cta_url": "...",
               "inner_html": "HTML Gmail-safe do miolo",
               "texto_plano": "versão text/plain"}}]}}

REGRAS:
- Escolha UM ICP dominante e abra pela dor operacional concreta desse ICP.
- Sequência: gancho de dor, como funciona, prova/hipótese rotulada, objeção/ROI, CTA.
- ASSUNTOS OUSADOS (email-creative-builder): para cada e-mail, assunto + exatamente 3
  assuntos_alternativos com ângulos distintos; sem genéricos, sem clickbait vazio;
  números/cases só dos KBs; preheader complementa o assunto; 1 CTA primário.
- Modo B2B cold-outbound: relevância-first, sem familiaridade inventada.
- Só use preços, percentuais e cases presentes nas KBs abaixo. Não invente.
- CTA leva ao Calendly ou WhatsApp oficial da 4Unik.
- {GUARDRAIL}

CONTEXTO (KBs da 4Unik):
{kb_context()}"""

root_agent = Agent(
    name="campaign_writer",
    model="gemini-2.5-pro",
    description="Gera rascunhos de campanhas de e-mail B2B da 4Unik (JSON, assuntos ousados, nunca envia).",
    instruction=INSTRUCTION,
)
