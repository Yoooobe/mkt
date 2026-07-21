// 4unik.mail — roteia todo request para a instância única do dashboard.
// Padrão getContainer/DurableObject do @cloudflare/containers (docs 2026).
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
  // Plain var (wrangler.jsonc vars) — "1" força seed no boot (rebootstrap).
  FORCE_SEED_DB?: string;
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
  };
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    // Nome fixo "main" = sempre a MESMA instância (SQLite exige escritor único).
    return getContainer(env.DASHBOARD, "main").fetch(request);
  },
};
