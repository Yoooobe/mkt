# Agentes ADK do Yoobe.mail (Vertex AI Agent Engine)

Agentes prontos para deploy no **Vertex AI Agent Engine** do projeto `mkt-4unik`
(console: Agent Platform → Overview). O dashboard usa `dashboard/agent_engine.py`
(com fallback para `dashboard/vertex.py` generateContent) quando os resource
names estão em `VERTEX_AGENT_*`.

| Agente | Modelo | Equivalente no dashboard |
|--------|--------|--------------------------|
| `campaign_writer` | gemini-2.5-pro | geração de campanha/copy (`ai.py`: plano → copy → revisão) |
| `reply_classifier` | gemini-2.5-flash | `ai.classificar_resposta` (mesmas classes e regras) |
| `name_analyzer` | gemini-2.5-flash | `nomes.analisar_lote` (qualidade de nomes) |
| `lead_validator` | gemini-2.5-flash | `leads.validate_one` / enriquecimento com Firecrawl |
| `scrape_planner` | gemini-2.5-flash | decide skip/domain_only/search_one (economia Firecrawl) |

As instruções de `campaign_writer` / `reply_classifier` embutem as KBs de
`../dashboard/context/kb/` no momento do build/deploy — redeploy após atualizar
as KBs. `name_analyzer` e `lead_validator` são autocontidos (sem KB snapshot).

### Skills / créditos (copywriter)

O `campaign_writer` e o copy do dashboard (`ai._gerar_email` / `_revisar_email`)
usam o skill **Email Creative Builder** (aaron-he-zhu /
[aaron-marketing-skills](https://github.com/aaron-he-zhu/aaron-marketing-skills/tree/main/email/engage/email-creative-builder),
**Apache-2.0**), espelhado em:

- `dashboard/context/kb/0-skill-email-creative-builder.md`
- `dashboard/context/kb/0-skill-email-subject-specs.md`
- `dashboard/context/kb/0-skill-email-creative-modes.md`

Fonte local do skill: `mkt/.cursor/skills/email-creative-builder/` (sync via
`ai.sync_skill_kbs()`). Regenere `campaign_writer/kb_snapshot.md` após atualizar
esses KBs e faça redeploy do Agent Engine.

## Rodar localmente

```bash
cd agents
.venv/bin/pip install -r requirements.txt   # venv própria (não é a do dashboard)
export GOOGLE_GENAI_USE_VERTEXAI=TRUE
export GOOGLE_CLOUD_PROJECT=mkt-4unik
export GOOGLE_CLOUD_LOCATION=global
export GOOGLE_APPLICATION_CREDENTIALS=~/.config/Yoobe/vertex-sa.json
.venv/bin/adk run name_analyzer
.venv/bin/adk run lead_validator
.venv/bin/adk run scrape_planner
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
  name_analyzer

.venv/bin/adk deploy agent_engine \
  --project mkt-4unik \
  --region us-central1 \
  --staging_bucket gs://mkt-4unik-agents \
  lead_validator
```

(Idem para `campaign_writer` e `reply_classifier`.) Os agentes aparecem em
Console → Vertex AI → Agent Platform (projeto `mkt-4unik`).

Após o deploy, copie o resource name para o dashboard:

```bash
# dashboard/.env (local) ou wrangler secret put (nuvem)
VERTEX_AGENT_NAME_ANALYZER=projects/.../locations/us-central1/reasoningEngines/...
VERTEX_AGENT_LEAD_VALIDATOR=projects/.../locations/us-central1/reasoningEngines/...
VERTEX_AGENT_SCRAPE_PLANNER=projects/.../locations/us-central1/reasoningEngines/...
VERTEX_AGENT_LOCATION=us-central1
```

O cliente está em `dashboard/agent_engine.py`: se o resource estiver vazio ou
falhar, cai em `vertex.generate_json` com o mesmo prompt.

Guardrail inalterado: agentes só produzem rascunhos/classificações em JSON;
aprovação, tags de supressão e envio continuam no código do dashboard.
