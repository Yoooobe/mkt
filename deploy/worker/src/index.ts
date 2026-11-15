// 4unik.mail — roteia todo request para a instância única do dashboard.
// Padrão getContainer/DurableObject do @cloudflare/containers (docs 2026).
// Tipos de Env: preferir `npx wrangler types` após mudar bindings/secrets.
import { Container, getContainer } from "@cloudflare/containers";

interface Env {
  DASHBOARD: DurableObjectNamespace<DashboardContainer>;
  // Secrets (wrangler secret put ...) — repassados ao container via envVars.
  API_KEY: string;
  TRACKING_BASE_URL: string;
  TRACKING_SECRET: string;
  TRACKING_SYNC_KEY: string;
  R2_ACCESS_KEY_ID: string;
  R2_SECRET_ACCESS_KEY: string;
  R2_ENDPOINT: string;
  // Qualidade de leads (Vertex Agent Engine + Firecrawl + lead-quality Worker)
  FIRECRAWL_API_KEY?: string;
  FIRECRAWL_CREDIT_BUDGET?: string;
  FIRECRAWL_MAX_LEADS_PER_JOB?: string;
  LEAD_EVIDENCE_TTL_DAYS?: string;
  VERTEX_AGENT_NAME_ANALYZER?: string;
  VERTEX_AGENT_LEAD_VALIDATOR?: string;
  VERTEX_AGENT_SCRAPE_PLANNER?: string;
  VERTEX_AGENT_LOCATION?: string;
  VERTEX_PROJECT_ID?: string;
  VERTEX_CREDENTIALS_JSON?: string;
  VERTEX_LOCATION?: string;
  FOURUNIK_MAIL_PROVIDER?: string;
  LEAD_QUALITY_WORKER_URL?: string;
  LEAD_QUALITY_SYNC_KEY?: string;
  // Site & Ads / GA4 (espelho do dashboard/.env local)
  GA4_PROPERTY_ID?: string;
  GA4_CREDENTIALS_JSON?: string;
  GA4_MEASUREMENT_ID?: string;
  GA4_MP_API_SECRET?: string;
  GA4_HOSTNAME_PLATAFORMA?: string;
  GA4_HOSTNAME_LOJA?: string;
  GOOGLE_GA4_ACCOUNT_ID?: string;
  GOOGLE_GA4_PROPERTY_ID?: string;
  GOOGLE_GA4_MEASUREMENT_ID?: string;
  GOOGLE_GA4_SITE_MEASUREMENT_ID?: string;
  GOOGLE_TAG_ID?: string;
  GOOGLE_ADS_TAG_ID?: string;
  GOOGLE_GTM_ACCOUNT_ID?: string;
  GOOGLE_GTM_CONTAINER_ID?: string;
  GOOGLE_ADS_CUSTOMER_ID?: string;
  GOOGLE_ADS_LOGIN_CUSTOMER_ID?: string;
  GOOGLE_ADS_DEVELOPER_TOKEN?: string;
  GMAIL_OAUTH_CLIENT_ID?: string;
  GMAIL_OAUTH_CLIENT_SECRET?: string;
  GMAIL_OAUTH_REDIRECT_URI?: string;
  GMAIL_OAUTH_ENCRYPTION_KEY?: string;
  VERTEX_MODEL_PRO?: string;
  VERTEX_MODEL_FLASH?: string;
  LEADS_NOTIFY_EMAIL?: string;
  // Plain var (wrangler.jsonc vars) — "1" força seed no boot (rebootstrap).
  FORCE_SEED_DB?: string;
  // local | operacional | remoto (espelho de acompanhamento).
  DASHBOARD_ENV?: string;
}

export class DashboardContainer extends Container<Env> {
  defaultPort = 8000;
  // Cold start = restore Litestream do R2 (~20–40s). Com 10m o container
  // dormia entre sessões e cada visita pagava o restore de novo. 6h mantém
  // a instância quente no uso normal e ainda escala a zero depois do expediente.
  sleepAfter = "6h";
  envVars = {
    API_KEY: this.env.API_KEY,
    TRACKING_BASE_URL: this.env.TRACKING_BASE_URL,
    TRACKING_SECRET: this.env.TRACKING_SECRET,
    TRACKING_SYNC_KEY: this.env.TRACKING_SYNC_KEY,
    R2_ACCESS_KEY_ID: this.env.R2_ACCESS_KEY_ID,
    R2_SECRET_ACCESS_KEY: this.env.R2_SECRET_ACCESS_KEY,
    R2_ENDPOINT: this.env.R2_ENDPOINT,
    FORCE_SEED_DB: this.env.FORCE_SEED_DB ?? "",
    DASHBOARD_ENV: this.env.DASHBOARD_ENV ?? "remoto",
    FIRECRAWL_API_KEY: this.env.FIRECRAWL_API_KEY ?? "",
    FIRECRAWL_CREDIT_BUDGET: this.env.FIRECRAWL_CREDIT_BUDGET ?? "35",
    FIRECRAWL_MAX_LEADS_PER_JOB: this.env.FIRECRAWL_MAX_LEADS_PER_JOB ?? "10",
    LEAD_EVIDENCE_TTL_DAYS: this.env.LEAD_EVIDENCE_TTL_DAYS ?? "30",
    VERTEX_AGENT_NAME_ANALYZER: this.env.VERTEX_AGENT_NAME_ANALYZER ?? "",
    VERTEX_AGENT_LEAD_VALIDATOR: this.env.VERTEX_AGENT_LEAD_VALIDATOR ?? "",
    VERTEX_AGENT_SCRAPE_PLANNER: this.env.VERTEX_AGENT_SCRAPE_PLANNER ?? "",
    VERTEX_AGENT_LOCATION: this.env.VERTEX_AGENT_LOCATION ?? "us-central1",
    VERTEX_PROJECT_ID: this.env.VERTEX_PROJECT_ID ?? "",
    VERTEX_CREDENTIALS_JSON: this.env.VERTEX_CREDENTIALS_JSON ?? "",
    VERTEX_LOCATION: this.env.VERTEX_LOCATION ?? "",
    FOURUNIK_MAIL_PROVIDER: this.env.FOURUNIK_MAIL_PROVIDER ?? "vertex",
    LEAD_QUALITY_WORKER_URL: this.env.LEAD_QUALITY_WORKER_URL ?? "",
    LEAD_QUALITY_SYNC_KEY: this.env.LEAD_QUALITY_SYNC_KEY ?? "",
    VERTEX_MODEL_PRO: this.env.VERTEX_MODEL_PRO ?? "",
    VERTEX_MODEL_FLASH: this.env.VERTEX_MODEL_FLASH ?? "",
    LEADS_NOTIFY_EMAIL: this.env.LEADS_NOTIFY_EMAIL ?? "",
    GA4_PROPERTY_ID: this.env.GA4_PROPERTY_ID ?? "",
    GA4_CREDENTIALS_JSON: this.env.GA4_CREDENTIALS_JSON ?? "",
    GA4_MEASUREMENT_ID: this.env.GA4_MEASUREMENT_ID ?? "",
    GA4_MP_API_SECRET: this.env.GA4_MP_API_SECRET ?? "",
    GA4_HOSTNAME_PLATAFORMA: this.env.GA4_HOSTNAME_PLATAFORMA ?? "",
    GA4_HOSTNAME_LOJA: this.env.GA4_HOSTNAME_LOJA ?? "",
    GOOGLE_GA4_ACCOUNT_ID: this.env.GOOGLE_GA4_ACCOUNT_ID ?? "",
    GOOGLE_GA4_PROPERTY_ID: this.env.GOOGLE_GA4_PROPERTY_ID ?? "",
    GOOGLE_GA4_MEASUREMENT_ID: this.env.GOOGLE_GA4_MEASUREMENT_ID ?? "",
    GOOGLE_GA4_SITE_MEASUREMENT_ID: this.env.GOOGLE_GA4_SITE_MEASUREMENT_ID ?? "",
    GOOGLE_TAG_ID: this.env.GOOGLE_TAG_ID ?? "",
    GOOGLE_ADS_TAG_ID: this.env.GOOGLE_ADS_TAG_ID ?? "",
    GOOGLE_GTM_ACCOUNT_ID: this.env.GOOGLE_GTM_ACCOUNT_ID ?? "",
    GOOGLE_GTM_CONTAINER_ID: this.env.GOOGLE_GTM_CONTAINER_ID ?? "",
    GOOGLE_ADS_CUSTOMER_ID: this.env.GOOGLE_ADS_CUSTOMER_ID ?? "",
    GOOGLE_ADS_LOGIN_CUSTOMER_ID: this.env.GOOGLE_ADS_LOGIN_CUSTOMER_ID ?? "",
    GOOGLE_ADS_DEVELOPER_TOKEN: this.env.GOOGLE_ADS_DEVELOPER_TOKEN ?? "",
    GMAIL_OAUTH_CLIENT_ID: this.env.GMAIL_OAUTH_CLIENT_ID ?? "",
    GMAIL_OAUTH_CLIENT_SECRET: this.env.GMAIL_OAUTH_CLIENT_SECRET ?? "",
    GMAIL_OAUTH_REDIRECT_URI: this.env.GMAIL_OAUTH_REDIRECT_URI ?? "",
    GMAIL_OAUTH_ENCRYPTION_KEY: this.env.GMAIL_OAUTH_ENCRYPTION_KEY ?? "",
  };
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    // Nome fixo "main" = sempre a MESMA instância (SQLite exige escritor único).
    return getContainer(env.DASHBOARD, "main").fetch(request);
  },
};
