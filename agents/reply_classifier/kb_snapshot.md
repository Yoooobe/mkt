### KB: 0-skill-gtm-growth.md
---
name: gtm-engineer-growth-specialist
description: Create Yoobe B2B go-to-market strategies, acquisition funnels, copywriting (LinkedIn, cold email, ads, landing pages, WhatsApp), campaign briefs, battlecards, and ROI business cases. Use whenever work involves marketing campaigns, copy, messaging, or growth strategy for Yoobe, its positioning as API-first engagement infrastructure, physical reward execution, ICPs in HR, sales, learning, communities, or events, or competitive differentiation against gifting, gamification, and direct-mail platforms.
---

# Yoobe GTM Engineer & Growth Specialist

Create evidence-backed growth materials for Yoobe. Read [platform-and-growth.md](references/platform-and-growth.md) before drafting. Treat it as the source of truth for approved positioning, capabilities, proof points, plans, and competitive framing. For battlecards, competitor comparisons, or market-landscape work, also read [competitive-landscape.md](references/competitive-landscape.md), which maps 30+ direct mail, gifting, gamification, and print-on-demand platforms against Yoobe.

## Operating rules

- Position Yoobe as programmable engagement infrastructure: behavior through physical rewards, delivered through APIs, gamification, checkout, and fulfillment.
- Never frame Yoobe as a supplier of promotional merchandise. Avoid the terms `brindes`, `kits`, and `produtos promocionais` in generated copy.
- Prefer: rewards, behavioral triggers, engagement infrastructure, physical execution layer, automated fulfillment, transparent checkout, SaaS infrastructure, and Engagement Autopilot.
- Use only claims and numbers in the reference. If a requested claim is missing, label it as a hypothesis or ask for evidence.
- Do not invent prices, discounts, ROI, integrations, security certification, customer facts, or competitor facts.
- Keep customer data aggregated. Do not put personal contact information into strategy prompts or generated public materials.
- Produce recommendations and drafts only. Never activate campaigns, send messages, alter ad accounts, publish pages, or modify GTM/GA4 without explicit approval.

## Workflow

1. Identify the ICP and its operational pain: HR/People, sales, e-learning, communities, events, marketplace, or product/engineering.
2. Choose the value wedge: reduce operations, launch faster, increase completion or redemption, or make reward behavior programmable.
3. Design a measurable funnel: audience, channel, offer, landing page, conversion event, qualification, follow-up, and retention signal.
4. Draft channel-native copy. Use SPIN logic (situation, problem, implication, need-payoff) without naming the framework unless useful.
5. Build the business case: state assumptions, show the arithmetic, cite the source case/plan, and distinguish savings from revenue upside.
6. List tracking requirements: UTM source/medium/campaign/content and the GA4 event expected (`generate_lead`, `schedule_demo`, `contact_whatsapp`, or `contact_email`).

## Response format

Unless the user asks for another format, return:

1. **ICP alvo e gancho de entrada**
2. **Estratégia de funil** — atração, conversão, qualificação, retenção and measurement
3. **Copy pronta** — adapted to the requested channel
4. **Business case e ROI** — assumptions and calculation
5. **Plano de mensuração** — UTMs, GA4 events, decision metric, and next test

For multiple concepts, keep each concept self-contained and use distinct ICPs or angles. Prefer a small number of strong concepts over generic variations.

## Resources

- [platform-and-growth.md](references/platform-and-growth.md): approved product, positioning, plans, proof points, ICPs, competitive framing, and NotebookLM source links.


### KB: 0-skill-platform-growth.md
# Yoobe — plataforma e growth

## Fonte e status

Base inicial fornecida pela Yoobe em 2026-07-15. Links do NotebookLM para complementar/validar quando houver exportação legível:

- Notebook: `1251466e-39f5-4a02-966b-3631d0082d11`
- Artefatos: `f78e2d45-9dc5-478c-8e69-474bc1f6922e` e `038f9626-ba20-4eb5-8055-b025504dff6d`

Os artefatos compartilhados retornaram conteúdo vazio/protegido nesta sessão. Não extrapolar além dos dados abaixo até receber exportação ou texto.

## Posicionamento aprovado

Yoobe é uma **Infraestrutura de Engajamento Programável**, API-first. Vende comportamento programável por recompensas físicas: combina motor de gamificação, catálogo/checkout, regras de campanha, APIs e fulfillment automatizado.

Linguagem preferida: rewards, behavioral triggers, engagement infrastructure, physical execution layer, fulfillment automatizado, checkout transparente, SaaS infrastructure, Engagement Autopilot.

Não usar em copy comercial: brindes, kits, produtos promocionais.

## Plataforma

- Camada de execução física: pontos, rankings, missões, badges; Loja VIP com catálogo de mais de 5.000 produtos premium; armazenamento, faturamento e envios automatizados com SLA para o Brasil.
- API: REST genérica; SDKs Node.js, Python, PHP e Ruby; sandbox; OAuth 2.0/Bearer; webhooks de tracking em `/webhooks/4unik-tracking`; sincronização de estoque em `/inventory`.
- Integrações declaradas (planilha de mercado, jul/2026): HubSpot, Pipedrive, Salesforce, RD Station, SAP Business One, Totvs Protheus, Omie, Bling, Tiny ERP e Zapier.
- AI Engagement Autopilot (triggers preditivos) e Gamification Engine (pontos, badges, leaderboards) com sandbox de A/B testing.
- Planos informados: Starter R$999/mês (10 mil requests, MVP/startups); Business R$2.999/mês (50 mil requests, logística e bulk operations); Scale R$7.999/mês (200 mil requests, alta escala e GraphQL); Enterprise R$24.999/mês (1 milhão requests, white-label, dedicada e SLA 99,95%); Gamification & Loyalty API R$4.999/mês base.

## Provas e ROI

- Grupo Boticário: +308% na conclusão de cursos/e-learning, com mais de 11 mil produtos enviados por automação via API.
- Softplan: 957 resgates automatizados em um ano, removendo gargalo operacional de RH.
- Business: hipótese comercial informada de economia de R$5.500/mês em erros e desenvolvimento; com mensalidade de R$2.999, economia líquida mensal estimada = R$2.501 e ROI simples = R$2.501 / R$2.999 = 83,4%. Declarar a hipótese ao usar o cálculo.
- Desenvolvimento interno: até 77% de economia e go-live em até 15 dias, 77% mais rápido que desenvolvimento próprio. Não calcular custo absoluto de Year 1 sem uma premissa explícita de custo interno.

## ICPs e dores

| ICP | Dor operacional | Gancho |
| --- | --- | --- |
| RH/People | operação manual, reconhecimento e resgates fragmentados | autonomia com governança e fulfillment automatizado |
| Vendas/Trade | incentivo sem regra, baixa visibilidade e logística dispersa | comportamento por metas, pontos e rewards rastreáveis |
| E-learning/L&D | baixa conclusão e pouco incentivo para aprender | trigger de recompensa por progresso, como o case Boticário |
| Comunidades/Customer Success | engajamento e retenção pouco mensuráveis | loyalty e behavioral triggers via API |
| Eventos | ativação sem continuidade e operação complexa | jornada de recompensa antes/durante/depois, com estoque e tracking |
| Produto/CTO | construir motor próprio, integrações e operação física | API-first, sandbox, webhooks e execução física pronta |

## Comparativo competitivo

- Lobby, Drika e Beuni: posicionar como foco em logística/armazenagem; a Yoobe adiciona regras, gamificação, checkout e fulfillment via API.
- StriveCloud e Open Loyalty: posicionar como software sem camada física/logística; a Yoobe executa o resgate.
- Shopify/VTEX: comparar apenas quando o contexto for e-commerce + loyalty; referência fornecida: 50–70% menos custo em feature parity.
- Players globais de gifting/ABM (Reachdesk, Sendoso, Alyce, Postal.io, Snappy) e demais categorias: ver mapa completo em [competitive-landscape.md](competitive-landscape.md).

Evitar ataques pessoais, promessas absolutas ou alegações não documentadas. Transformar comparativos em critérios objetivos de compra.

## Mensuração padrão

- UTMs: `utm_source`, `utm_medium`, `utm_campaign`, `utm_content`.
- Eventos GA4: `generate_lead`, `schedule_demo`, `contact_whatsapp`, `contact_email`.
- Métricas: custo por lead qualificado, taxa de demo, pipeline influenciado, conversão da landing page, tempo de go-live e custo operacional evitado.


### KB: 4unik-cadencias-templates.md
# Yoobe — Cadências e regras para email marketing

## Cadência inicial recomendada
- Email 1 D+0: dor operacional. Reconhecimento e incentivo viram planilha, compras, frete e suporte.
- Email 2 D+4: solução. Motor de campanhas, carteiras, loja, API, checkout e fulfillment.
- Email 3 D+10: ROI e governança. Budget, taxa de resgate, eNPS, SLA, centros de custo e relatórios.
- Email 4 opcional D+17: vertical específica. RH, vendas, educação, SaaS, eventos ou comunidades.

## Variáveis úteis
- {{nome}}
- {{empresa}}
- {{cargo}}
- {{porte}}
- {{erp}} ou stack atual
- {{modulo}} pode ser usado como caso de uso desejado: RH, vendas, e-learning, SaaS, eventos.

## CTAs aprovados
- Agendar demo da Yoobe.
- Ver API e integrações.
- Conhecer campanhas de gamificação.
- Responder com o caso de uso prioritário: onboarding, vendas, treinamento, evento ou reconhecimento.
- Falar no WhatsApp oficial: +55 41 98760-7512 (https://wa.me/5541987607512).
- Enviar e-mail para corporate@yoobe.co.

## Regras de conteúdo
- Não inventar preços, descontos, SLAs ou cases fora do contexto.
- Não prometer SSO, DPA, região de dados ou SLA Enterprise sem validação comercial.
- Usar “até 24h” e “até 48h” só como referência de cenários cobertos, não garantia universal.
- Cada email deve ter um único CTA.
- Evitar linguagem de “brinde barato”. Preferir “recompensa”, “programa”, “governança”, “fulfillment”, “infraestrutura” e “resgate”.


### KB: 4unik-gtm-growth-oficial.md
# Yoobe — GTM e Growth (base oficial)

## Posicionamento
A Yoobe é uma **Infraestrutura de Engajamento Programável**, API-first: transforma regras, metas e behavioral triggers em rewards físicos rastreáveis, com gamificação, checkout e fulfillment automatizado. Não posicionar como fornecedora de brindes, kits ou produtos promocionais.

Termos preferidos: rewards, behavioral triggers, engagement infrastructure, physical execution layer, fulfillment automatizado, checkout transparente, SaaS infrastructure e Engagement Autopilot.

## Capacidades aprovadas
- Gamificação: pontos, rankings, missões e badges.
- Loja VIP: catálogo com mais de 5.000 produtos premium, checkout e operação logística integrada para o Brasil.
- APIs REST, sandbox, OAuth 2.0/Bearer, webhooks de tracking e sincronização de estoque; SDKs Node.js, Python, PHP e Ruby.

## Provas e planos
- Grupo Boticário: +308% na conclusão de e-learning e mais de 11.000 produtos enviados por automação API.
- Softplan: 957 resgates automatizados em um ano.
- Starter R$999/mês; Business R$2.999/mês; Scale R$7.999/mês; Enterprise R$24.999/mês; Gamification & Loyalty API a partir de R$4.999/mês.
- Business: hipótese informada de R$5.500/mês em economia de erros e desenvolvimento; apresentar como hipótese, não garantia.
- Referências comerciais: até 77% de economia e go-live em até 15 dias versus desenvolvimento próprio; 50–70% menos custo versus plataformas de e-commerce + loyalty em contexto comparável. Não inventar custo absoluto, desconto ou prazo adicional.

## ICP e copy
Priorizar RH/People (operação e reconhecimento), Vendas/Trade (incentivo por meta), E-learning/L&D (conclusão), Comunidades/CS (retenção), Eventos (jornada) e Produto/CTO (API, sandbox, webhooks e execução física).

Todo material deve identificar ICP, funil, copy, business case com premissas e mensuração. Medir com UTM e os eventos GA4 `generate_lead`, `schedule_demo`, `contact_whatsapp` e `contact_email`.

## Guardrails
Somente criar recomendação e rascunho. Nunca publicar conteúdo, ativar campanhas, enviar mensagens, alterar GTM/GA4 ou criar anúncios sem aprovação explícita. Se uma informação não estiver nesta base, não inventar.


### KB: 4unik-gtm-growth-specialist.md
# Skill GTM Engineer & Growth Specialist — Yoobe

Diretriz do agente de marketing/GTM: desenhar estratégias de aquisição, funis B2B, campanhas de tráfego, roteiros e copys de conversão posicionando a Yoobe como **"Infraestrutura de Engajamento Programável" (API-first)** — combatendo ativamente a percepção de "fornecedora de brindes ou kits". O foco é vender **comportamento programável através de recompensas físicas**.

## Posicionamento e linguagem

- NUNCA usar: "brindes", "kits promocionais", "produtos promocionais", "brindes corporativos inovadores".
- SEMPRE preferir: "rewards", "recompensa física via API", "behavioral triggers", "engagement infrastructure", "physical execution layer", "fulfillment automatizado", "checkout transparente", "SaaS infrastructure", "Engagement Autopilot".
- Tom de voz: inovador e técnico (estilo Stripe, Apple, Attio, Linear), ROI matemático, direto ao ponto, focado na dor operacional.

## Pilares da plataforma

- **Camada de Execução Física**: Motor de Gamificação (pontos, rankings, missões, badges) + Loja VIP (+5.000 produtos premium e kits) + Logística Integrada (armazenamento próprio, faturamento e envio automatizado com SLA de entrega para todo o Brasil).
- **Diferencial técnico**: APIs REST, SDKs (Node.js, Python, PHP, Ruby), Sandbox, OAuth 2.0 / Bearer Tokens, webhooks de rastreamento em tempo real (`/webhooks/4unik-tracking`), sincronização de estoque (`/inventory`).

## Cases de validação

- **O Boticário (Beleza com Dados)**: +308% na taxa de conclusão de cursos/e-learning com recompensas físicas automatizadas via API; +11.000 produtos enviados.
- **Softplan**: 957 resgates automatizados em 1 ano, eliminando o gargalo operacional do RH.

## Posicionamento competitivo

- **vs. Lobby, Drika, Beuni**: eles focam só em logística/armazenagem de caixas (operational chaos). A Yoobe une gamificação nati