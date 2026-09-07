"""Agente ADK: decide se/como gastar créditos Firecrawl (economia).

Espelha o planejador em dashboard/leads.py. Default: domain_only (1 scrape).
"""
from google.adk.agents import Agent

INSTRUCTION = """Você planeja o uso MÍNIMO de Firecrawl para validar um lead B2B.
Orçamento apertado: preferir 0 ou 1 crédito. Devolva SOMENTE JSON:
{"usar_firecrawl": false, "modo": "skip|domain_only|search_one",
 "query": "", "url_preferida": "https://empresa.com.br", "motivo": "curto"}

REGRAS:
- free-mail (gmail/hotmail/…) → usar_firecrawl=false, modo=skip
- se já há evidência/cache → skip
- domínio corporativo sem evidência → usar_firecrawl=true, modo=domain_only,
  url_preferida=https://{dominio}
- search_one só se o domínio for genérico/hosting e empresa tiver nome claro;
  query curta com site:{dominio}
- Nunca peça mais de uma URL. Nunca invente URL que não derive do domínio.
"""

root_agent = Agent(
    name="scrape_planner",
    model="gemini-2.5-flash",
    description="Planeja scrape Firecrawl com mínimo de créditos (JSON).",
    instruction=INSTRUCTION,
)
