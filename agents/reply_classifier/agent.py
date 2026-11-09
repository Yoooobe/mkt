"""Agente ADK: classificador de respostas de leads da Yoobe.

Espelha ai.classificar_resposta (mesmas classes e regras): a IA interpreta,
o código decide. Pronto para deploy no Vertex AI Agent Engine.
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
    "do código e da equipe Yoobe, nunca suas."
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

CALENDLY = "https://corporate.yoobe.co/"

INSTRUCTION = f"""Você é o assistente comercial do Time Yoobe. Um lead respondeu a um
e-mail de campanha. Interprete a resposta e devolva SOMENTE JSON válido:
{{"classificacao": "pergunta_simples | agendar | interessado | objecao_preco | duvida_complexa | pediu_tempo | recusa_encerrar | outro",
  "confianca": 0.0,
  "intencao_reuniao": false,
  "precisa_humano": false,
  "resposta_sugerida": "resposta cordial em pt-BR terminando com 'Atenciosamente, Time Yoobe'",
  "resumo": "1-2 frases em pt-BR"}}

REGRAS (NUNCA violar):
- Responda SOMENTE com base nas KBs abaixo; sem informação no contexto → precisa_humano=true.
- PREÇO: use somente faixas/planos das KBs e convide para call: {CALENDLY}. Nunca invente valor.
- "agendar"/"interessado" → resposta_sugerida oferece o Calendly.
- "objecao_preco", "duvida_complexa", "pediu_tempo", "outro" → precisa_humano=true.
- Ambiguidade → confianca < 0.8.
- {GUARDRAIL}

CONTEXTO (KBs da Yoobe):
{kb_context()}"""

root_agent = Agent(
    name="reply_classifier",
    model="gemini-2.5-flash",
    description="Classifica respostas de leads da Yoobe e sugere resposta (JSON, nunca envia).",
    instruction=INSTRUCTION,
)
