/**
 * 4unik.mail — lead-quality Worker (economia: ≤1–2 créditos Firecrawl / lead).
 *
 * POST /enqueue  (X-Sync-Key) → Queue LEAD_SCRAPE
 * queue consumer → scrape homepage (ou search_one) → POST /api/leads/evidence
 */

export interface ScrapeJob {
  job_id: string;
  email: string;
  nome: string;
  empresa: string;
  domain: string;
  modo?: "skip" | "domain_only" | "search_one";
  query?: string;
  url_preferida?: string;
}

interface Env {
  LEAD_SCRAPE: Queue<ScrapeJob>;
  FIRECRAWL_API_KEY: string;
  SYNC_KEY: string;
  DASHBOARD_URL: string;
  DASHBOARD_API_KEY: string;
}

const FIRECRAWL = "https://api.firecrawl.dev/v1";
const FREE = new Set([
  "gmail.com", "hotmail.com", "outlook.com", "yahoo.com", "icloud.com",
  "live.com", "msn.com", "uol.com.br", "bol.com.br", "terra.com.br",
]);

function log(fields: Record<string, unknown>) {
  console.log(JSON.stringify({ ts: new Date().toISOString(), ...fields }));
}

function emailHash(email: string): string {
  const [local, domain] = (email || "").split("@");
  return `${(local || "").slice(0, 2)}…@${domain || "?"}`;
}

async function firecrawlSearch(env: Env, query: string) {
  const r = await fetch(`${FIRECRAWL}/search`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.FIRECRAWL_API_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ query: query.slice(0, 400), limit: 1 }),
  });
  if (!r.ok) throw new Error(`search ${r.status}: ${(await r.text()).slice(0, 200)}`);
  const data = (await r.json()) as { data?: Array<Record<string, string>> };
  return (data.data || [])
    .map((it) => ({
      url: (it.url || it.link || "").trim(),
      title: (it.title || "").slice(0, 200),
      description: (it.description || it.snippet || "").slice(0, 500),
    }))
    .filter((x) => x.url);
}

async function firecrawlScrape(env: Env, url: string) {
  const r = await fetch(`${FIRECRAWL}/scrape`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.FIRECRAWL_API_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      url,
      formats: ["markdown", "links"],
      onlyMainContent: true,
      maxAge: 172800000,
    }),
  });
  if (!r.ok) throw new Error(`scrape ${r.status}: ${(await r.text()).slice(0, 200)}`);
  const body = (await r.json()) as {
    data?: { markdown?: string; links?: string[]; metadata?: { title?: string } };
  };
  const data = body.data || {};
  return {
    url,
    markdown: (data.markdown || "").slice(0, 12000),
    title: (data.metadata?.title || "").slice(0, 200),
  };
}

async function gatherEvidence(env: Env, job: ScrapeJob) {
  const domain = job.domain || (job.email.includes("@") ? job.email.split("@")[1] : "");
  const modo = job.modo || "domain_only";
  const errors: string[] = [];
  let credits = 0;
  let hits: Array<{ url: string; title: string; description: string }> = [];
  const pages: Array<{ url: string; title: string; markdown: string }> = [];
  const scrapedUrls: string[] = [];

  if (modo === "skip" || FREE.has(domain)) {
    return {
      email: job.email,
      nome: job.nome,
      empresa: job.empresa,
      domain,
      query: job.query || "",
      search_hits: [],
      pages: [],
      urls: [],
      errors: ["skip"],
      modo: "skip",
      credits_spent: 0,
    };
  }

  const target =
    (job.url_preferida && job.url_preferida.startsWith("http")
      ? job.url_preferida
      : domain
        ? `https://${domain}`
        : "");

  if (target) {
    try {
      const page = await firecrawlScrape(env, target);
      credits += 1;
      if ((page.markdown || "").trim()) {
        pages.push({
          url: page.url,
          title: page.title,
          markdown: page.markdown.slice(0, 4000),
        });
        scrapedUrls.push(target);
      }
    } catch (e) {
      errors.push(`scrape: ${String(e).slice(0, 120)}`);
    }
  }

  if (!pages.length && modo === "search_one") {
    const query =
      (job.query || "").trim() ||
      `${job.empresa || domain} site:${domain}`.trim();
    try {
      hits = await firecrawlSearch(env, query);
      credits += 1;
      if (hits[0]) {
        try {
          const page = await firecrawlScrape(env, hits[0].url);
          credits += 1;
          if ((page.markdown || "").trim()) {
            pages.push({
              url: page.url,
              title: page.title,
              markdown: page.markdown.slice(0, 4000),
            });
            scrapedUrls.push(hits[0].url);
          }
        } catch (e) {
          errors.push(`scrape hit: ${String(e).slice(0, 100)}`);
        }
      }
    } catch (e) {
      errors.push(`search: ${String(e).slice(0, 120)}`);
    }
  }

  return {
    email: job.email,
    nome: job.nome,
    empresa: job.empresa,
    domain,
    query: job.query || "",
    search_hits: hits.slice(0, 1),
    pages: pages.slice(0, 1),
    urls: scrapedUrls.slice(0, 1),
    errors: errors.slice(0, 6),
    modo,
    credits_spent: credits,
  };
}

async function postEvidence(env: Env, job: ScrapeJob, payload: Record<string, unknown>) {
  const base = (env.DASHBOARD_URL || "").replace(/\/$/, "");
  if (!base) throw new Error("DASHBOARD_URL não configurado");
  const r = await fetch(`${base}/api/leads/evidence`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-API-Key": env.DASHBOARD_API_KEY,
      "X-Sync-Key": env.SYNC_KEY,
    },
    body: JSON.stringify({
      job_id: job.job_id,
      email: job.email,
      payload,
    }),
  });
  if (!r.ok) {
    throw new Error(`evidence ${r.status}: ${(await r.text()).slice(0, 240)}`);
  }
}

function authOk(req: Request, env: Env): boolean {
  const sync = req.headers.get("X-Sync-Key") || "";
  const api = req.headers.get("X-API-Key") || "";
  return Boolean(env.SYNC_KEY) && (sync === env.SYNC_KEY || api === env.SYNC_KEY);
}

export default {
  async fetch(req: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(req.url);
    if (url.pathname === "/" || url.pathname === "/healthz") {
      return Response.json({
        ok: true,
        service: "4unik-lead-quality",
        routes: ["GET /", "GET /healthz", "POST /enqueue"],
      });
    }

    if (url.pathname === "/enqueue" && req.method === "POST") {
      if (!authOk(req, env)) {
        return Response.json({ erro: "X-Sync-Key inválida" }, { status: 401 });
      }
      let body: {
        job_id?: string;
        contacts?: Array<{
          email?: string;
          nome?: string;
          empresa?: string;
          domain?: string;
          modo?: ScrapeJob["modo"];
          query?: string;
          url_preferida?: string;
        }>;
      };
      try {
        body = await req.json();
      } catch {
        return Response.json({ erro: "JSON inválido" }, { status: 400 });
      }
      const jobId = (body.job_id || "").trim() || crypto.randomUUID();
      const contacts = body.contacts || [];
      let queued = 0;
      for (const c of contacts) {
        const email = (c.email || "").trim().toLowerCase();
        if (!email) continue;
        if ((c.modo || "domain_only") === "skip") continue;
        const job: ScrapeJob = {
          job_id: jobId,
          email,
          nome: c.nome || "",
          empresa: c.empresa || "",
          domain: c.domain || (email.includes("@") ? email.split("@")[1] : ""),
          modo: c.modo || "domain_only",
          query: c.query || "",
          url_preferida: c.url_preferida || "",
        };
        await env.LEAD_SCRAPE.send(job);
        queued += 1;
      }
      ctx.waitUntil(Promise.resolve(log({ event: "enqueue", job_id: jobId, queued })));
      return Response.json({ ok: true, job_id: jobId, queued }, { status: 202 });
    }

    return Response.json({ erro: "not found" }, { status: 404 });
  },

  async queue(batch: MessageBatch<ScrapeJob>, env: Env): Promise<void> {
    for (const msg of batch.messages) {
      const job = msg.body;
      const t0 = Date.now();
      try {
        if (!env.FIRECRAWL_API_KEY) throw new Error("FIRECRAWL_API_KEY ausente");
        const payload = await gatherEvidence(env, job);
        await postEvidence(env, job, payload);
        log({
          event: "scrape_ok",
          job_id: job.job_id,
          email_hash: emailHash(job.email),
          credits_spent: payload.credits_spent,
          urls: (payload.urls || []).length,
          latency_ms: Date.now() - t0,
        });
        msg.ack();
      } catch (e) {
        log({
          event: "scrape_err",
          job_id: job.job_id,
          email_hash: emailHash(job.email),
          error: String(e).slice(0, 240),
          latency_ms: Date.now() - t0,
        });
        msg.retry();
      }
    }
  },
};
