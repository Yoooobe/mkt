"""Agente ADK: validação de frescor/enriquecimento de leads B2B da Yoobe.

Consome CRM + trechos Firecrawl; devolve status e sugestões em JSON.
A escrita no CRM e as tags de supressão ficam no dashboard.
"""
from google.adk.agents import Agent

GUARDRAIL = (
    "GUARDRAIL: você produz SOMENTE JSON de avaliação. Nunca afirme que o contato "
    "foi suprimido ou que e-mails foram enviados — isso é decisão do código."
)

INSTRUCTION = f"""Você é um validador de QUALIDADE DE LEADS B2B da Yoobe (Brasil).
Com base nos DADOS DO CRM e na EVIDÊNCIA PÚBLICA (Firecrawl), avalie se o contato
ainda é útil para e-mail comercial. Não invente fatos fora da evidência.

Devolva SOMENTE JSON válido (um objeto, ou {{"resultados":[...]}} se lote):
{{"email":"...","status":"valido|duvidoso|desatualizado|invalido","na_empresa":true,
 "nome_sugerido":"","cargo_sugerido":"","empresa_sugerida":"","confianca":0.0,
 "motivo":"curto","fontes":[{{"url":"","evidencia":""}}],
 "acao_recomendada":"manter|enriquecer|suprimir|revisar",
 "sugestoes":{{"nome":"","empresa":"","telefone":"","cargo":"","enriquecimento":""}}}}

REGRAS:
- "invalido": e-mail claramente genérico/errado ou domínio morto sem presença.
- "desatualizado": evidência forte de que a pessoa NÃO está mais na empresa → acao_recomendada=suprimir.
- "duvidoso": pouca evidência; não suprima.
- "valido": evidência compatível ou ausência de sinal negativo com dados CRM coerentes.
- Não invente telefone pessoal; telefone só se corporativo publicado na evidência.
- fontes[].url só de URLs presentes na evidência.
- confianca alta só com evidência clara.
- {GUARDRAIL}"""

root_agent = Agent(
    name="lead_validator",
    model="gemini-2.5-flash",
    description="Valida frescor de leads B2B com evidência pública (JSON, nunca envia).",
    instruction=INSTRUCTION,
)
