# Agentes ADK do 4unik.mail (Vertex AI Agent Engine)

Agentes prontos para deploy no **Vertex AI Agent Engine** do projeto `mkt-4unik`
(console: Agent Platform → Overview). O dashboard hoje usa o Vertex direto
(`dashboard/vertex.py`); estes agentes são a evolução dos mesmos fluxos para
rodarem gerenciados na nuvem.

| Agente | Modelo | Equivalente no dashboard |
|--------|--------|--------------------------|
| `campaign_writer` | gemini-2.5-pro | geração de campanha/copy (`ai.py`: plano → copy → revisão) |
| `reply_classifier` | gemini-2.5-flash | `ai.classificar_resposta` (mesmas classes e regras) |

As instruções embutem as KBs de `../dashboard/context/kb/` no momento do
build/deploy — redeploy após atualizar as KBs.

## Rodar localmente

```bash
cd agents
.venv/bin/pip install -r requirements.txt   # venv própria (não é a do dashboard)
export GOOGLE_GENAI_USE_VERTEXAI=TRUE
export GOOGLE_CLOUD_PROJECT=mkt-4unik
export GOOGLE_CLOUD_LOCATION=global
export GOOGLE_APPLICATION_CREDENTIALS=~/.config/4unik/vertex-sa.json
.venv/bin/adk run campaign_writer      # chat no terminal
.venv/bin/adk web                      # UI web de teste
```

## Deploy no Agent Engine

Requer um bucket de staging no projeto (criar uma vez:
`gcloud storage buckets create gs://mkt-4unik-agents --project=mkt-4unik --location=us-central1`).

```bash
cd agents
.venv/bin/adk deploy agent_engine \
  --project mkt-4unik \
  --region us-central1 \
  --staging_bucket gs://mkt-4unik-agents \
  campaign_writer
```

(Idem para `reply_classifier`.) Os agentes aparecem em
Console → Vertex AI → Agent Platform (projeto `mkt-4unik`).

## Como o dashboard chamará o agente remoto (futuro)

Depois do deploy, trocar o transporte em `dashboard/ai.py` (`_ai_json`) por uma
chamada ao Agent Engine com a mesma service account:

```python
import vertexai
from vertexai import agent_engines

vertexai.init(project="mkt-4unik", location="us-central1")
agent = agent_engines.get("RESOURCE_NAME_DO_DEPLOY")
for event in agent.stream_query(user_id="dashboard", message=prompt):
    ...
```

Guardrail inalterado: agentes só produzem rascunhos/classificações em JSON;
aprovação e envio continuam no código do dashboard.
