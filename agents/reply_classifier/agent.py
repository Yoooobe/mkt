"""Agente ADK: classificador de respostas de leads da 4Unik.

Espelha ai.classificar_resposta (mesmas classes e regras): a IA interpreta,
o código decide. Pronto para deploy no Vertex AI Agent Engine.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google.adk.agents import Agent

from common import GUARDRAIL, kb_context

CALENDLY = "https://calendly.com/4unik/30min?month=2026-07"

INSTRUCTION = f"""Você é o assistente comercial do Time 4Unik. Um lead respondeu a um
e-mail de campanha. Interprete a resposta e devolva SOMENTE JSON válido:
{{"classificacao": "pergunta_simples | agendar | interessado | objecao_preco | duvida_complexa | pediu_tempo | recusa_encerrar | outro",
  "confianca": 0.0,
  "intencao_reuniao": false,
  "precisa_humano": false,
  "resposta_sugerida": "resposta cordial em pt-BR terminando com 'Atenciosamente, Time 4Unik'",
  "resumo": "1-2 frases em pt-BR"}}

REGRAS (NUNCA violar):
- Responda SOMENTE com base nas KBs abaixo; sem informação no contexto → precisa_humano=true.
- PREÇO: use somente faixas/planos das KBs e convide para call: {CALENDLY}. Nunca invente valor.
- "agendar"/"interessado" → resposta_sugerida oferece o Calendly.
- "objecao_preco", "duvida_complexa", "pediu_tempo", "outro" → precisa_humano=true.
- Ambiguidade → confianca < 0.8.
- {GUARDRAIL}

CONTEXTO (KBs da 4Unik):
{kb_context()}"""

root_agent = Agent(
    name="reply_classifier",
    model="gemini-2.5-flash",
    description="Classifica respostas de leads da 4Unik e sugere resposta (JSON, nunca envia).",
    instruction=INSTRUCTION,
)
