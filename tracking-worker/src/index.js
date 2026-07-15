/**
 * 4unik.mail — coletor de rastreio de e-mail (Cloudflare Worker + D1).
 *
 * Sempre-online: registra cada abertura/clique na borda, sem depender do notebook.
 * Token stateless assinado por HMAC (mesmo segredo do dashboard, em db.py).
 *
 *   GET /o/<token>        -> grava 'open', devolve GIF 1x1 (no-store)
 *   GET /c/<token>        -> grava 'click', 302 para a URL original do token
 *   GET /events?since=N   -> (X-Sync-Key) eventos com id > N, para o dashboard puxar
 *   GET /healthz          -> ok
 *
 * Token: base64url(JSON{sid,email,cid,etapa,u}) + "." + base64url(HMAC_SHA256(secret))
 */

const GIF_1x1 = Uint8Array.from([
  0x47, 0x49, 0x46, 0x38, 0x39, 0x61, 0x01, 0x00, 0x01, 0x00, 0x80, 0x00, 0x00,
  0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x21, 0xf9, 0x04, 0x01, 0x00, 0x00, 0x00,
  0x00, 0x2c, 0x00, 0x00, 0x00, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00, 0x02, 0x02,
  0x44, 0x01, 0x00, 0x3b,
]);

function b64uDecode(s) {
  s = s.replace(/-/g, "+").replace(/_/g, "/");
  s += "=".repeat((4 - (s.length % 4)) % 4);
  const bin = atob(s);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}
const bytesToB64u = (bytes) =>
  btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

async function verifyToken(token, secret) {
  const dot = token.lastIndexOf(".");
  if (dot < 0) return null;
  const rawBytes = b64uDecode(token.slice(0, dot));
  const sigBytes = b64uDecode(token.slice(dot + 1));
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" }, false, ["verify"]
  );
  const ok = await crypto.subtle.verify("HMAC", key, sigBytes, rawBytes);
  if (!ok) return null;
  try { return JSON.parse(new TextDecoder().decode(rawBytes)); }
  catch { return null; }
}

// Heurística de prefetch de máquina (Apple Mail Privacy / Google proxy) p/ descontar.
function isPrefetch(ua) {
  const u = (ua || "").toLowerCase();
  return u.includes("googleimageproxy") || u.includes("ggpht") ||
         u.includes("applemail") || u.includes("apple mail") ||
         u.includes("preload") ? 1 : 0;
}

async function logEvent(env, type, p, req, url) {
  const ua = req.headers.get("user-agent") || "";
  const country = req.headers.get("cf-ipcountry") || "";
  await env.DB.prepare(
    `INSERT INTO events (type, track_id, email, campaign_id, etapa, url, ts, ua, cf_country, prefetch)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
  ).bind(
    type, p.sid || "", p.email || "", p.cid ?? null, String(p.etapa ?? ""),
    url || "", new Date().toISOString(), ua, country,
    type === "open" ? isPrefetch(ua) : 0
  ).run();
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    const path = url.pathname;

    if (path === "/healthz") return new Response("ok");

    // pixel de abertura
    if (path.startsWith("/o/")) {
      const p = await verifyToken(decodeURIComponent(path.slice(3)), env.TRACKING_SECRET);
      if (p) { try { await logEvent(env, "open", p, req, ""); } catch (e) {} }
      return new Response(GIF_1x1, {
        headers: {
          "Content-Type": "image/gif",
          "Cache-Control": "no-store, no-cache, must-revalidate, private",
          "Pragma": "no-cache",
        },
      });
    }

    // clique -> 302 para a URL original
    if (path.startsWith("/c/")) {
      const p = await verifyToken(decodeURIComponent(path.slice(3)), env.TRACKING_SECRET);
      const dest = p && p.u ? p.u : "https://plataforma.4unik.com.br";
      if (p) { try { await logEvent(env, "click", p, req, dest); } catch (e) {} }
      return new Response(null, {
        status: 302,
        headers: { "Location": dest, "Cache-Control": "no-store" },
      });
    }

    // descadastro (unsubscribe) — link visível (GET) + one-click RFC 8058 (POST)
    if (path.startsWith("/u/")) {
      const p = await verifyToken(decodeURIComponent(path.slice(3)), env.TRACKING_SECRET);
      if (p) { try { await logEvent(env, "unsubscribe", p, req, ""); } catch (e) {} }
      if (req.method === "POST") {
        // one-click: provedores de e-mail esperam 200 sem corpo
        return new Response("ok", { status: 200, headers: { "Cache-Control": "no-store" } });
      }
      const html = `<!DOCTYPE html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Descadastro — 4Unik</title></head>
<body style="margin:0;background:#f0f3f5;font-family:'DM Sans',Arial,sans-serif;color:#1a0a2e">
<div style="max-width:480px;margin:48px auto;background:#fff;border-radius:14px;overflow:hidden;box-shadow:0 6px 24px rgba(34,9,58,.08)">
<div style="background:linear-gradient(160deg,#1f2933,#0f1720);padding:24px 32px">
<span style="font-family:Montserrat,Arial,sans-serif;font-size:22px;font-weight:800;color:#fff">4<span style="color:#66e3c4">Unik</span></span></div>
<div style="padding:32px">
<h1 style="font-family:Montserrat,Arial,sans-serif;font-size:20px;color:#22093a;margin:0 0 12px">Pronto, você foi descadastrado ✓</h1>
<p style="font-size:15px;line-height:1.6;margin:0 0 8px">${p && p.email ? p.email : "Seu e-mail"} não receberá mais a newsletter nem as comunicações da 4Unik.</p>
<p style="font-size:13px;color:#6b7c87;margin:14px 0 0">Mudou de ideia? É só responder a qualquer e-mail nosso que a gente reativa. Obrigado por ter acompanhado até aqui!</p>
</div></div></body></html>`;
      return new Response(html, {
        status: 200,
        headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" },
      });
    }

    // pull de eventos pelo dashboard (cursor)
    if (path === "/events") {
      if (req.headers.get("X-Sync-Key") !== env.TRACKING_SYNC_KEY) {
        return new Response(JSON.stringify({ erro: "sync key inválida" }), {
          status: 401, headers: { "Content-Type": "application/json" } });
      }
      const since = parseInt(url.searchParams.get("since") || "0", 10) || 0;
      const limit = Math.min(parseInt(url.searchParams.get("limit") || "500", 10) || 500, 1000);
      const rs = await env.DB.prepare(
        `SELECT id, type, track_id, email, campaign_id, etapa, url, ts, ua, cf_country, prefetch
         FROM events WHERE id > ? ORDER BY id ASC LIMIT ?`
      ).bind(since, limit).all();
      const rows = rs.results || [];
      const cursor = rows.length ? rows[rows.length - 1].id : since;
      return new Response(JSON.stringify({ cursor, count: rows.length, events: rows }), {
        headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
      });
    }

    return new Response("4unik.mail tracking", { status: 404 });
  },
};
