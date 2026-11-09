/* Yoobe.mail — SPA vanilla (hash router) */
const view = document.getElementById("view");
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

async function api(path, opts = {}) {
  opts.headers = Object.assign({"X-API-Key": window.API_KEY}, opts.headers || {});
  if (opts.json) { opts.method = opts.method || "POST"; opts.body = JSON.stringify(opts.json);
    opts.headers["Content-Type"] = "application/json"; delete opts.json; }
  // Dashboard SPA: nunca servir GET antigo do cache do browser (preview/campanha “não muda”).
  if (opts.cache == null) opts.cache = "no-store";
  const r = await fetch(path, opts);
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) { const e = new Error(data.erro || data.detail && JSON.stringify(data.detail) || r.status);
    e.data = (typeof data === "object") ? data : {}; e.status = r.status; throw e; }
  return data;
}

function toast(msg, ms = 3500) {
  const t = document.getElementById("toast");
  t.textContent = msg; t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), ms);
}
const badge = (v, cls) => `<span class="badge b-${(cls || v || "").toLowerCase().replace(/[^a-z_]/g, "")}">${esc(v)}</span>`;
const fmtDt = s => s ? esc(s.slice(0, 16)) : "—";

/** Ambiente do dashboard (local | remoto). Carregado em ensureDashboardMeta(). */
window.DASHBOARD_META = window.DASHBOARD_META || null;

async function ensureDashboardMeta() {
  if (window.DASHBOARD_META) return window.DASHBOARD_META;
  try {
    window.DASHBOARD_META = await api("/api/health");
  } catch (_) {
    window.DASHBOARD_META = {dashboard_env: "local", dono_campanhas: true, espelho: false};
  }
  return window.DASHBOARD_META;
}

function origemBadge(c) {
  const o = (c && c.origem_ambiente) || "local";
  return badge(`origem: ${o}`, o === "remoto" ? "pausado_resposta" : "ativo");
}

function campOrigemBanner(c) {
  if (c.somente_acompanhamento) {
    return `<div class="card card-info" style="margin-bottom:10px" role="status">
      <b>Criada no ambiente local — só acompanhamento.</b>
      <div class="muted" style="margin-top:4px">Alterações (editar, ativar, pausar, reescrever, melhorias) só no Mac
        (<code>localhost:8377</code>). Este remoto é espelho de métricas e leads.</div>
    </div>`;
  }
  if (c.precisa_decisao) {
    return `<div class="card card-info" style="margin-bottom:10px" role="status">
      <b>Campanha criada no remoto.</b>
      <div class="muted" style="margin-top:4px">Decida: importar como local (passa a ser editável neste Mac)
        ou manter só leitura.</div>
      <div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap">
        <button type="button" class="primary" onclick="campClaimLocal(${c.id})">Importar como local</button>
        <button type="button" onclick="toast('Mantida só leitura. Use Importar quando quiser editar.')">Manter só leitura</button>
      </div>
    </div>`;
  }
  return "";
}

function campMutDisabled(c, extraTitle) {
  if (c.somente_acompanhamento) {
    return `disabled title="Só acompanhamento no remoto — altere no Mac (localhost:8377)"`;
  }
  if (c.precisa_decisao) {
    return `disabled title="Importe como local antes de alterar"`;
  }
  return extraTitle ? `disabled title="${esc(extraTitle)}"` : "";
}

async function campClaimLocal(id) {
  try {
    const r = await api(`/api/campaigns/${id}/claim-local`, {method: "POST"});
    toast(r.nota || "Importada como local ✔");
    location.hash = `#/campanha/${id}`;
    route();
  } catch (e) {
    toast("Não foi possível importar: " + e.message, 6000);
  }
}

/** Rótulos de seção → variante visual do bloco. */
const PROSA_SECTION_META = {
  objetivo: {cls: "neutro", label: "Objetivo"},
  resumo: {cls: "resumo", label: "Resumo"},
  "icp e dor": {cls: "icp", label: "ICP e dor"},
  icp: {cls: "icp", label: "ICP"},
  "por que agora": {cls: "agora", label: "Por que agora"},
  "por que cada e-mail": {cls: "resumo", label: "Por que cada e-mail"},
  "por que cada email": {cls: "resumo", label: "Por que cada e-mail"},
  riscos: {cls: "risco", label: "Riscos"},
  conflito: {cls: "risco", label: "Conflito"},
  "conflito com a instrucao do operador": {cls: "risco", label: "Conflito com a instrução"},
  medicao: {cls: "neutro", label: "Medição"},
  "medição": {cls: "neutro", label: "Medição"},
  checklist: {cls: "neutro", label: "Checklist"},
  "checklist antes de ativar": {cls: "neutro", label: "Checklist antes de ativar"},
  "planejamento / contexto": {cls: "neutro", label: "Planejamento / contexto"},
  planejamento: {cls: "neutro", label: "Planejamento"},
  prova: {cls: "neutro", label: "Prova"},
  gancho: {cls: "resumo", label: "Gancho"},
  cta: {cls: "resumo", label: "CTA"},
};

const PROSA_SECTION_RE = /^(Objetivo|Resumo|ICP e dor|ICP|Por que agora|Por que cada e-mail|Por que cada email|Riscos|Conflito(?: com a instru[cç][aã]o do operador)?|CONFLITO(?: COM A INSTRU[CÇ][AÃ]O DO OPERADOR)?|Medição|Medicao|Checklist(?: antes de ativar)?|Planejamento(?: \/ contexto)?|Prova|Gancho|CTA)\s*:\s*/i;

/** Normaliza dumps densos da IA antes de particionar seções/parágrafos. */
function normalizeProsaText(texto) {
  let t = String(texto || "").replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  t = t.replace(/\t+/g, " — ");
  t = t.replace(/\s·\s/g, "\n\n");
  // Mid-line labels → new section lines
  t = t.replace(/\s+(?=(?:Por que cada e-?mail|Medição|Medicao|Riscos|Checklist|Conflito|CONFLITO|Objetivo|Resumo|ICP|Gancho|Prova|CTA)\s*:)/gi, "\n\n");
  // E1/E2 rows (em-dash, colon, or glued)
  t = t.replace(/\s+(?=E\d+\s*[.:—–-])/g, "\n");
  t = t.replace(/(CONFLITO[^:\n]{0,80}:)/gi, "\n\n$1");
  // Checkbox rows
  t = t.replace(/\s+(?=[☐☑]\s)/g, "\n");
  return t.trim();
}

/** Inline markdown seguro sobre texto já escapado. */
function fmtInlineMd(escaped) {
  return String(escaped || "")
    .replace(/`([^`]+)`/g, '<code class="prosa-inline-code">$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/__([^_]+)__/g, "<strong>$1</strong>");
}

/** Corpo de um bloco: parágrafos, listas, callouts (sem fences mermaid). */
function fmtProsaBody(texto) {
  let t = normalizeProsaText(texto);
  if (!t) return '<p class="ai-prosa muted">—</p>';
  const lines = t.split("\n");
  const out = [];
  let para = [];
  let list = null;
  let callout = [];

  const flushPara = () => {
    if (!para.length) return;
    // Preserve line breaks inside a paragraph block (do not flatten to spaces)
    const joined = para.join("\n").trim();
    para = [];
    if (!joined) return;
    const eRowRe = /\bE\d+\s*[.:—–-]/;
    if (eRowRe.test(joined) && (joined.length > 80 || (joined.match(/\bE\d+/g) || []).length > 1)) {
      const chunks = joined.split(/(?=\bE\d+\s*[.:—–-])/).map(s => s.trim()).filter(Boolean);
      if (chunks.length > 1) {
        const head = chunks[0];
        const items = /^E\d+/.test(head) ? chunks : chunks.slice(1);
        if (head && !/^E\d+/.test(head)) {
          out.push(`<p class="ai-prosa">${fmtInlineMd(esc(head))}</p>`);
        }
        out.push(`<ul class="prosa-list prosa-list--emails">${items.map(i =>
          `<li>${fmtInlineMd(esc(i))}</li>`).join("")}</ul>`);
        return;
      }
    }
    // One visual paragraph per original line when multi-line
    const sub = joined.split("\n").map(s => s.trim()).filter(Boolean);
    if (sub.length > 1) {
      sub.forEach(b => {
        if (/^(CONFLITO|ATEN[CÇ][AÃ]O|AVISO)\b/i.test(b) || /^[A-ZÁÉÍÓÚÂÊÔÃÕÀÜ0-9][A-ZÁÉÍÓÚÂÊÔÃÕÀÜ0-9\s,/\-]{12,}:/.test(b)) {
          out.push(`<div class="prosa-callout prosa-callout--warn">${fmtInlineMd(esc(b))}</div>`);
        } else if (b.length > 180) {
          const bits = b.split(/(?<=[.!?…])\s+(?=[A-ZÁÉÍÓÚÂÊÔÃÕÀÜ0-9“"(\[]|E\d)/).map(s => s.trim()).filter(Boolean);
          if (bits.length > 1) bits.forEach(x => out.push(`<p class="ai-prosa">${fmtInlineMd(esc(x))}</p>`));
          else out.push(`<p class="ai-prosa">${fmtInlineMd(esc(b))}</p>`);
        } else {
          out.push(`<p class="ai-prosa">${fmtInlineMd(esc(b))}</p>`);
        }
      });
      return;
    }
    if (joined.length > 160) {
      const bits = joined.split(/(?<=[.!?…])\s+(?=[A-ZÁÉÍÓÚÂÊÔÃÕÀÜ0-9“"(\[]|E\d)/).map(s => s.trim()).filter(Boolean);
      if (bits.length > 1) {
        bits.forEach(b => out.push(`<p class="ai-prosa">${fmtInlineMd(esc(b))}</p>`));
        return;
      }
    }
    out.push(`<p class="ai-prosa">${fmtInlineMd(esc(joined))}</p>`);
  };
  const flushList = () => {
    if (!list || !list.items.length) { list = null; return; }
    out.push(`<ul class="prosa-list">${list.items.map(i =>
      `<li>${fmtInlineMd(esc(i))}</li>`).join("")}</ul>`);
    list = null;
  };
  const flushCallout = () => {
    if (!callout.length) return;
    const body = callout.map(l => fmtInlineMd(esc(l))).join("<br>");
    out.push(`<div class="prosa-callout">${body}</div>`);
    callout = [];
  };

  for (const raw of lines) {
    const line = raw.trimEnd();
    const trimmed = line.trim();
    if (!trimmed) {
      flushPara(); flushList(); flushCallout();
      continue;
    }
    if (/^>\s?/.test(trimmed)) {
      flushPara(); flushList();
      callout.push(trimmed.replace(/^>\s?/, ""));
      continue;
    }
    // Warning / ALL-CAPS label lines become callouts
    if (/^(CONFLITO|ATEN[CÇ][AÃ]O|AVISO)\b/i.test(trimmed) ||
        (/^[A-ZÁÉÍÓÚÂÊÔÃÕÀÜ][A-ZÁÉÍÓÚÂÊÔÃÕÀÜ0-9\s,/\-]{14,}:/.test(trimmed) && trimmed.length < 120)) {
      flushPara(); flushList(); flushCallout();
      out.push(`<div class="prosa-callout prosa-callout--warn">${fmtInlineMd(esc(trimmed))}</div>`);
      continue;
    }
    if (callout.length) flushCallout();
    if (/^[-*•☐☑]\s+/.test(trimmed) || /^E\d+\s*[.:—–-]/.test(trimmed)) {
      flushPara();
      if (!list) list = {items: []};
      list.items.push(trimmed.replace(/^[-*•☐☑]\s+/, ""));
      continue;
    }
    if (list) flushList();
    para.push(trimmed);
  }
  flushPara(); flushList(); flushCallout();
  return out.join("") || '<p class="ai-prosa muted">—</p>';
}

/** Extrai fences mermaid e devolve parts [{type,text}]. Usa matchAll (sem RegExp.exec). */
function splitMermaidFences(texto) {
  const src = String(texto || "");
  const parts = [];
  const re = /```\s*mermaid\s*\n([\s\S]*?)```/gi;
  let last = 0;
  for (const m of src.matchAll(re)) {
    if (m.index > last) parts.push({type: "text", text: src.slice(last, m.index)});
    parts.push({type: "mermaid", text: m[1].trim()});
    last = m.index + m[0].length;
  }
  if (last < src.length) parts.push({type: "text", text: src.slice(last)});
  if (!parts.length) parts.push({type: "text", text: src});
  return parts;
}

function renderMermaidBlock(code) {
  const id = "mmd-" + Math.random().toString(36).slice(2, 10);
  return `<div class="prosa-diagram" data-mermaid-pending="1">
    <p class="prosa-diagram__hint">Diagrama — se não carregar, o fonte está abaixo.</p>
    <pre class="mermaid" id="${id}">${esc(code)}</pre>
    <details><summary class="muted">Ver fonte Mermaid</summary>
      <pre class="prosa-diagram-fallback">${esc(code)}</pre></details>
  </div>`;
}

function _normLabel(s) {
  return String(s || "").toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "");
}

/** Particiona texto em seções rotuladas (Resumo:, ICP e dor:, …). */
function splitLabeledSections(texto) {
  const t = normalizeProsaText(texto);
  if (!t) return [];
  const lines = t.split("\n");
  const sections = [];
  let cur = {key: null, title: null, body: []};

  const push = () => {
    const body = cur.body.join("\n").trim();
    if (!cur.key && !body) return;
    sections.push({key: cur.key, title: cur.title, body});
  };

  for (const line of lines) {
    const m = line.match(PROSA_SECTION_RE);
    if (m) {
      push();
      const rawLabel = m[1];
      const key = _normLabel(rawLabel);
      const metaKey = Object.keys(PROSA_SECTION_META).find(k => _normLabel(k) === key) || key;
      const meta = PROSA_SECTION_META[metaKey] || {cls: "neutro", label: rawLabel};
      cur = {key: meta.cls, title: meta.label, body: [line.slice(m[0].length)]};
      continue;
    }
    cur.body.push(line);
  }
  push();
  if (sections.length === 1 && !sections[0].key) {
    return [{key: null, title: null, body: sections[0].body}];
  }
  return sections.filter(s => s.body || s.key);
}

function fmtRichChunk(texto) {
  return splitMermaidFences(texto).map(p =>
    p.type === "mermaid" ? renderMermaidBlock(p.text) : fmtProsaBody(p.text)
  ).join("");
}

/**
 * Prosa rica: seções rotuladas em caixas coloridas, markdown leve, Mermaid.
 * Mantém fmtProsa como corpo simples para trechos curtos.
 */
function fmtRichProsa(texto) {
  const t = String(texto || "").trim();
  if (!t) return '<p class="ai-prosa muted">—</p>';
  const sections = splitLabeledSections(t);
  const hasLabels = sections.some(s => s.key);
  if (!hasLabels) return fmtRichChunk(t);

  return `<div class="prosa-blocos">${sections.map(s => {
    if (!s.key) return fmtRichChunk(s.body);
    const body = (s.body || "").trim();
    if (!body || body === "—" || body === "-") return "";
    return `<section class="prosa-bloco prosa-bloco--${esc(s.key)}">
      <h4 class="prosa-bloco__title">${esc(s.title || s.key)}</h4>
      ${fmtRichChunk(body)}
    </section>`;
  }).join("")}</div>`;
}

/** Quebra prosa da IA em parágrafos legíveis (escapa HTML). */
function fmtProsa(texto) {
  return fmtProsaBody(texto);
}

/** TL;DR curto para clamps de cards (evita muro de texto no resumo). */
function prosaTldr(texto, max = 180) {
  const t = String(texto || "").trim();
  if (!t) return "";
  const sections = splitLabeledSections(t);
  const resumo = sections.find(s => s.key === "resumo");
  let base = (resumo && resumo.body) ? resumo.body : t;
  base = base.replace(/\s+/g, " ").trim();
  const sentence = base.split(/(?<=[.!?…])\s+/)[0] || base;
  return sentence.length > max ? sentence.slice(0, max - 1) + "…" : sentence;
}


/** Resumo da campanha: TL;DR + blocos por assunto. */
function renderCampResumo(objetivo) {
  const t = String(objetivo || "").trim();
  if (!t) return '<p class="muted">Sem objetivo definido.</p>';
  const sections = splitLabeledSections(t);
  const resumoSec = sections.find(s => s.key === "resumo");
  let tldr = "";
  if (resumoSec && resumoSec.body) {
    tldr = resumoSec.body.split(/(?<=[.!?…])\s+/)[0] || resumoSec.body.slice(0, 220);
  } else {
    const plain = t.replace(PROSA_SECTION_RE, "").replace(/\n+/g, " ").trim();
    tldr = plain.split(/(?<=[.!?…])\s+/)[0] || plain.slice(0, 220);
  }
  tldr = tldr.trim().slice(0, 280);
  return `<div class="camp-resumo">
    <div class="camp-resumo__tldr">
      <span class="camp-resumo__tldr-label">Em uma frase</span>
      ${fmtInlineMd(esc(tldr))}
    </div>
    ${fmtRichProsa(t)}
  </div>`;
}

function scheduleMermaidRender(root) {
  const el = root || document.getElementById("view");
  if (!el || !el.querySelector(".mermaid")) return;
  const run = () => {
    try {
      if (window.mermaid && typeof window.mermaid.run === "function") {
        window.mermaid.initialize({
          startOnLoad: false, theme: "dark",
          securityLevel: "strict", fontFamily: "DM Sans, sans-serif",
        });
        window.mermaid.run({nodes: el.querySelectorAll(".mermaid")});
      }
    } catch (err) {
      console.warn("[mermaid]", err);
    }
  };
  if (window.mermaid) run();
  else {
    let n = 0;
    const iv = setInterval(() => {
      n++;
      if (window.mermaid) { clearInterval(iv); run(); }
      else if (n > 40) clearInterval(iv);
    }, 100);
  }
}

const COMPAT_LABEL = {
  compativel: "compatível",
  pendente_aprovacao: "pendente de aprovação",
  desatualizado: "desatualizado",
  risco_cta: "risco: CTA duplicado no corpo",
  desconhecido: "—",
};
const compatBadge = st => badge(COMPAT_LABEL[st] || st || "—", st || "desconhecido");
// Os envios rodam num ciclo de ~30 min, então a hora real é "a partir de" o agendado.
// Se já venceu, o e-mail sai no próximo ciclo (não num minuto exato).
function envioLabel(dtStr) {
  if (!dtStr) return "—";
  const alvo = new Date(dtStr.replace(" ", "T"));
  if (isNaN(alvo)) return fmtDt(dtStr);
  if (alvo <= new Date()) return "⏳ no próximo ciclo (~30 min)";
  return "a partir de " + fmtDt(dtStr);
}
const CICLO_NOTA = '<small class="muted">⏱️ Os envios são processados em ciclos de ~30 min — a hora real pode variar alguns minutos do horário agendado.</small>';

// Preview isolado: remove conteúdo executável antes de entregar o HTML ao iframe.
// O iframe mantém origem opaca (sem allow-same-origin), mas allow-scripts evita o
// falso erro do console causado por extensões que tentam injetar script em about:blank.
function previewHtmlSeguro(html) {
  const doc = new DOMParser().parseFromString(String(html || ""), "text/html");
  doc.querySelectorAll("script,iframe,object,embed,form,base,meta[http-equiv]").forEach(el => el.remove());
  doc.querySelectorAll("*").forEach(el => {
    [...el.attributes].forEach(a => {
      const nome = a.name.toLowerCase(), valor = String(a.value || "").trim().toLowerCase();
      if (nome.startsWith("on") || ((nome === "href" || nome === "src") && valor.startsWith("javascript:"))) {
        el.removeAttribute(a.name);
      }
    });
  });
  return "<!doctype html>\n" + doc.documentElement.outerHTML;
}

/** Escreve HTML no iframe sem reutilizar documento antigo (srcdoc + marcador único). */
function setPreviewIframe(iframe, html) {
  if (!iframe) return;
  const safe = previewHtmlSeguro(html);
  const stamp = `<!--pv:${Date.now()}:${Math.random().toString(36).slice(2, 8)}-->`;
  iframe.removeAttribute("srcdoc");
  iframe.srcdoc = safe.includes("</body>")
    ? safe.replace(/<\/body>/i, stamp + "</body>")
    : safe + stamp;
}

function _tplHash(s) {
  const str = String(s ?? "");
  let h = 0;
  for (let i = 0; i < str.length; i++) h = ((h << 5) - h + str.charCodeAt(i)) | 0;
  return (h >>> 0).toString(36);
}

/** Converte texto simples → miolo HTML (quando o operador edita só o texto e espera ver no preview). */
function _tplTextoParaInner(texto, oldInner) {
  const plain = String(texto || "").replace(/\r\n/g, "\n").trim();
  if (!plain) return oldInner || "";
  const escText = s => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const ctaMatch = String(oldInner || "").match(/<a\b[^>]*display\s*:\s*inline-block[\s\S]*?<\/a>/i);
  const cta = ctaMatch ? ctaMatch[0] : "";
  const h1m = String(oldInner || "").match(/<h1([^>]*)>/i);
  const h1attrs = h1m
    ? h1m[1]
    : ' style="margin:0 0 18px;font-family:Montserrat,Arial,sans-serif;font-size:22px;font-weight:800;color:#0f1720;line-height:1.3"';
  const blocks = plain.split(/\n\s*\n/).map(b => b.trim()).filter(Boolean);
  if (!blocks.length) return oldInner || "";
  let html = "";
  blocks.forEach((b, i) => {
    const withBr = escText(b).replace(/\n/g, "<br>");
    if (i === 0) html += `<h1${h1attrs}>${withBr}</h1>`;
    else html += `<p style="margin:0 0 16px">${withBr}</p>`;
  });
  if (cta) html += `<div style="margin:24px 0 0;text-align:center">${cta}</div>`;
  return html;
}

function _tplStripHtml(html) {
  const doc = new DOMParser().parseFromString(`<div>${html || ""}</div>`, "text/html");
  return (doc.body.textContent || "").replace(/\s+\n/g, "\n").trim();
}

const _TPL_VOID = new Set([
  "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr",
]);

/** Indenta o miolo HTML para edição (uma tag por linha; texto curto fica inline). */
function _tplPrettyHtml(html) {
  const raw = String(html || "").trim();
  if (!raw) return "";
  const doc = new DOMParser().parseFromString(`<div id="__tpl_root">${raw}</div>`, "text/html");
  const root = doc.getElementById("__tpl_root");
  if (!root) return raw.replace(/></g, ">\n<");

  const escAttr = v => String(v ?? "").replace(/&/g, "&amp;").replace(/"/g, "&quot;");

  function serialize(node, indent) {
    if (node.nodeType === Node.COMMENT_NODE) {
      return `${indent}<!--${node.data}-->`;
    }
    if (node.nodeType === Node.TEXT_NODE) {
      const t = String(node.textContent || "").replace(/\s+/g, " ").trim();
      return t ? indent + t : "";
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return "";
    const tag = node.tagName.toLowerCase();
    const attrs = [...node.attributes]
      .map(a => ` ${a.name}="${escAttr(a.value)}"`)
      .join("");
    if (_TPL_VOID.has(tag)) return `${indent}<${tag}${attrs}>`;
    const kids = [...node.childNodes].filter(k => !(
      k.nodeType === Node.TEXT_NODE && !String(k.textContent || "").trim()
    ));
    if (!kids.length) return `${indent}<${tag}${attrs}></${tag}>`;
    if (kids.length === 1 && kids[0].nodeType === Node.TEXT_NODE) {
      const t = String(kids[0].textContent || "").replace(/\s+/g, " ").trim();
      return `${indent}<${tag}${attrs}>${t}</${tag}>`;
    }
    const pad = indent + "  ";
    const inner = kids.map(k => serialize(k, pad)).filter(Boolean).join("\n");
    return `${indent}<${tag}${attrs}>\n${inner}\n${indent}</${tag}>`;
  }

  return [...root.childNodes].map(n => serialize(n, "")).filter(Boolean).join("\n");
}

/**
 * Monta editor Visual + HTML indentado para o miolo.
 * prefix: "ed" (campanha) ou "tg" (biblioteca)
 * Retorna getHtml() sempre sincronizado com o modo ativo.
 */
function _tplMountBodyEditor(prefix, initialHtml) {
  const ta = document.getElementById(`${prefix}-inner_html`);
  const visual = document.getElementById(`${prefix}-visual`);
  const tabs = document.querySelectorAll(`[data-${prefix}-mode]`);
  if (!ta) return { getHtml: () => "", setHtml: () => {} };

  let mode = "html";
  const pretty = _tplPrettyHtml(initialHtml || "");
  ta.value = pretty || (initialHtml || "");

  const setMode = next => {
    if (next === mode) return;
    if (next === "html" && visual) {
      ta.value = _tplPrettyHtml(visual.innerHTML || "");
    } else if (next === "visual" && visual) {
      visual.innerHTML = ta.value || "";
    }
    mode = next;
    if (visual) visual.hidden = mode !== "visual";
    ta.hidden = mode !== "html";
    tabs.forEach(btn => {
      btn.classList.toggle("primary", btn.getAttribute(`data-${prefix}-mode`) === mode);
    });
  };

  if (visual) {
    visual.innerHTML = ta.value || "";
    visual.hidden = true;
  }
  ta.hidden = false;
  tabs.forEach(btn => {
    btn.onclick = () => setMode(btn.getAttribute(`data-${prefix}-mode`) || "html");
    btn.classList.toggle("primary", btn.getAttribute(`data-${prefix}-mode`) === "html");
  });

  // Tab insere 2 espaços no HTML (em vez de sair do campo).
  ta.addEventListener("keydown", ev => {
    if (ev.key !== "Tab" || ev.altKey || ev.ctrlKey || ev.metaKey) return;
    ev.preventDefault();
    const start = ta.selectionStart, end = ta.selectionEnd;
    const pad = "  ";
    ta.value = ta.value.slice(0, start) + pad + ta.value.slice(end);
    ta.selectionStart = ta.selectionEnd = start + pad.length;
  });

  const fmtBtn = document.getElementById(`${prefix}-format-html`);
  if (fmtBtn) {
    fmtBtn.onclick = () => {
      if (mode === "visual" && visual) ta.value = visual.innerHTML || "";
      ta.value = _tplPrettyHtml(ta.value);
      if (mode === "visual" && visual) visual.innerHTML = ta.value;
      setMode("html");
      toast("HTML indentado ✔", 2000);
    };
  }

  return {
    getHtml() {
      if (mode === "visual" && visual) return visual.innerHTML || "";
      return ta.value || "";
    },
    setHtml(html) {
      const p = _tplPrettyHtml(html || "");
      ta.value = p;
      if (visual) visual.innerHTML = p;
    },
    pretty() {
      const cur = mode === "visual" && visual ? (visual.innerHTML || "") : (ta.value || "");
      const p = _tplPrettyHtml(cur);
      ta.value = p;
      if (visual) visual.innerHTML = p;
      return p;
    },
  };
}

/* ---------- status chip ---------- */
async function pollStatus() {
  try {
    const q = await api("/api/queue/status");
    const chip = document.getElementById("chip");
    const pend = q.fila.pending || 0;
    chip.className = "statuschip " + (q.online ? "ok" : "off");
    chip.textContent = (q.online ? "● online" : "○ offline") + (pend ? ` · fila ${pend}` : "");
    chip.title = "Último ciclo ok: " + (q.last_successful_run || "—");
  } catch (e) { /* servidor reiniciando */ }
}
setInterval(pollStatus, 60000);

/* ---------- TELA: Início (Visão Geral) ---------- */
function humaniza(dtStr) {
  if (!dtStr) return null;
  const alvo = new Date(dtStr.replace(" ", "T"));
  const diffMs = alvo - new Date();
  const dias = Math.floor(diffMs / 86400000);
  const horas = Math.floor(diffMs / 3600000);
  if (diffMs < 0) return {txt: "agora", n: "0", u: "vencido"};
  if (dias >= 1) return {txt: `em ${dias} dia${dias > 1 ? "s" : ""}`, n: dias, u: dias > 1 ? "dias" : "dia"};
  if (horas >= 1) return {txt: `em ${horas}h`, n: horas, u: "horas"};
  return {txt: "em minutos", n: Math.max(1, Math.floor(diffMs / 60000)), u: "min"};
}

let inicioTimer = null;
async function vInicio() {
  // Tracking sync fora do caminho crítico — não bloqueia a home no remoto.
  api("/api/tracking/sync", {method: "POST"}).catch(() => {});
  const [d, estrategia, seoGeo] = await Promise.all([
    api("/api/metrics/overview"),
    api("/api/strategy/status").catch(() => null),
    api("/api/seo-geo/status").catch(() => null),
  ]);
  const p = d.proximo_envio;
  const hum = p ? humaniza(p.next_send_at) : null;
  const vencido = p && (new Date(p.next_send_at.replace(" ", "T")) <= new Date());
  const trackCard = (lab, val, taxa, ico, cls, kind, extra = "") => {
    if (!d.tracking_ativo && val === 0) return `
      <div class="bigkpi muted-card"><div class="lab">${lab}</div><span class="ico">${ico}</span>
        <div class="num">—</div><div class="sub">rastreio não configurado</div></div>`;
    const clr = val > 0 ? ` onclick="abrirDetalhe('${kind}','${lab}')"` : "";
    return `<div class="bigkpi${val > 0 ? " click" : ""}"${clr}><div class="lab">${lab}</div><span class="ico">${ico}</span>
      <div class="num ${cls}">${val}</div>
      <div class="sub"><span class="rate ${cls}">${taxa}%</span> de ${d.rastreaveis} rastreáveis</div>${extra}</div>`;
  };
  const cliquesExtra = (d.cliques_bot > 0)
    ? `<div class="sub" style="margin-top:2px"><a class="lnk" style="color:#ff6b6b" onclick="event.stopPropagation();abrirDetalhe('cliques_bot','Cliques de scanner')">🚫 ${d.cliques_bot} filtrado(s) como scanner</a></div>`
    : "";
  const maxTrend = Math.max(1, ...d.tendencia_14d.flatMap(t => [t.enviados, t.abertos, t.cliques, t.respostas]));
  const px = v => Math.round(v / maxTrend * 70);

  view.innerHTML = `
    <div class="hero">
      <div>
        <div class="next-label">⏱ Próximo envio</div>
        ${p ? `
          <div class="next-when">${vencido ? "No próximo ciclo" : hum.txt}</div>
          <div class="next-meta"><b>${esc(p.empresa || p.nome)}</b> · etapa ${p.etapa_atual + 1}/${p.total_etapas}
            · campanha ${esc(p.campanha)}<br>${envioLabel(p.next_send_at)}</div>
          <div style="margin-top:6px">${CICLO_NOTA}</div>`
          : `<div class="hero-empty">Nenhum envio agendado.<br>Ative uma campanha para começar a cadência. 🚀</div>`}
      </div>
      ${p ? `<div class="countdown"><b>${vencido ? "~30" : hum.n}</b><small>${vencido ? "min" : hum.u}</small></div>` : ""}
    </div>

    ${estrategia ? `<div class="strategy-card">
      <div><div class="strategy-title">✦ Ciclo de estratégia</div>
        <div class="muted">Atualizado ${fmtDt(estrategia.atualizado_em)} · ${esc(estrategia.guardrail || "")}</div></div>
      <div class="strategy-signals">
        <span>📞 <b>${d.telefones ?? 0}</b> telefones</span>
        <span>🗒️ <b>${d.historicos ?? 0}</b> históricos</span>
        <span>🎯 <b>${d.dados_ricos ?? 0}</b> dados ricos</span>
        <span class="${estrategia.claude?.ok ? "sig-ok" : "sig-warn"}">${estrategia.claude?.ok ? "● IA pronta" : "● IA precisa atenção"}</span>
      </div>
      <button onclick="rodarEstrategia()">↻ Atualizar agora</button>
    </div>` : ""}
    ${seoGeo ? `<div class="strategy-card seo-geo-card">
      <div><div class="strategy-title">🔎 Loop contínuo SEO + GEO</div>
        <div class="muted">${esc(seoGeo.modo || "recomendação")} · ${fmtDt(seoGeo.atualizado_em)} · ciclo a cada 30 min</div></div>
      <div class="strategy-signals">
        <span>👀 <b>${seoGeo.metricas?.sessoes ?? 0}</b> sessões</span>
        <span>🌿 <b>${seoGeo.metricas?.sessoes_organicas ?? 0}</b> orgânicas</span>
        <span>🎯 <b>${seoGeo.metricas?.leads ?? 0}</b> leads/eventos</span>
        <span>📝 <b>${(seoGeo.pauta || []).length}</b> pautas</span>
        ${(seoGeo.recomendacoes_grok || []).length ? `<span>✦ <b>${(seoGeo.recomendacoes_grok || []).length}</b> Grok</span>` : ""}
      </div>
      <button onclick="rodarSeoGeo(true)">↻ Atualizar agora</button>
      <button onclick="abrirSeoGeo()">Ver prioridades</button>
    </div>` : ""}

    <div class="bigkpis">
      <div class="bigkpi"><div class="lab">Campanhas ativas</div><span class="ico">🚀</span>
        <div class="num">${d.campanhas_ativas}</div><div class="sub">de ${d.campanhas_total} no total</div></div>
      <div class="bigkpi"><div class="lab">Contatos ativos</div><span class="ico">👥</span>
        <div class="num">${d.contatos_ativos}</div><div class="sub">em cadência · ${d.contatos_pendentes} pendentes</div></div>
      <div class="bigkpi"><div class="lab">Emails enviados</div><span class="ico">📤</span>
        <div class="num">${d.enviados}</div><div class="sub">${d.enviados_30d} nos últimos 30 dias</div></div>
      ${trackCard("Aberturas", d.abertos, d.taxa_abertura, "📬", "cyan", "aberturas")}
      ${trackCard("Cliques", d.cliques, d.taxa_clique, "🖱️", "cyan", "cliques", cliquesExtra)}
      <div class="bigkpi${d.respondidos > 0 ? ' click' : ''}" ${d.respondidos > 0 ? `onclick="abrirDetalhe('respostas','Respostas')"` : ""}>
        <div class="lab">Respostas</div><span class="ico">💬</span>
        <div class="num green">${d.respondidos}</div>
        <div class="sub"><span class="rate green">${d.taxa_resposta}%</span> da base</div></div>
      <div class="bigkpi${(d.descadastros ?? 0) > 0 ? ' click' : ''}" ${(d.descadastros ?? 0) > 0 ? `onclick="abrirDetalhe('descadastros','Descadastros')"` : ""}>
        <div class="lab">Descadastros</div><span class="ico">🚫</span>
        <div class="num">${d.descadastros ?? 0}</div>
        <div class="sub">opt-out da base</div></div>
      <div class="bigkpi"><div class="lab">Devoluções · bounce</div><span class="ico">📭</span>
        <div class="num">${d.bounces_total ?? 0}</div>
        <div class="sub"><span class="rate" style="color:#ff6b6b">${d.taxa_bounce ?? 0}%</span> dos envios não entregues ·
          <a class="lnk" onclick="abrirDetalhe('hard_bounce','Hard bounce — inexistentes')">${d.hard_bounces ?? 0} inexistentes</a> ·
          <a class="lnk" onclick="abrirDetalhe('soft_bounce','Soft bounce — temporários')">${d.soft_bounces ?? 0} soft</a></div></div>
      <div class="bigkpi${(d.removidos_bounce ?? 0) > 0 ? ' click' : ''}" ${(d.removidos_bounce ?? 0) > 0 ? `onclick="abrirDetalhe('removidos','Removidos da base por bounce')"` : ""}>
        <div class="lab">Removidos da base</div><span class="ico">🧹</span>
        <div class="num" style="color:#ff6b6b">${d.removidos_bounce ?? 0}</div>
        <div class="sub"><span class="rate" style="color:#ff6b6b">${d.taxa_removidos ?? 0}%</span> de ${d.total_contatos ?? 0} contatos ·
          <a class="lnk" onclick="event.stopPropagation();abrirDetalhe('invalidos','Inválidos — hard bounce')">${d.invalidos ?? 0} inválidos</a> +
          <a class="lnk" onclick="event.stopPropagation();abrirDetalhe('soft_suprimido','Removidos por soft reincidente')">${d.soft_suprimidos ?? 0} soft 3×</a></div></div>
    </div>

    <div class="card">
      <h3>Tendência · últimos 14 dias</h3>
      <div class="trend">
        ${d.tendencia_14d.map(t => `<div class="col" title="${t.dia}: ${t.enviados} env · ${t.abertos} abert · ${t.cliques} cliq · ${t.respostas} resp">
          <div class="bar b-resp" style="height:${px(t.respostas)}px"></div>
          <div class="bar b-click" style="height:${px(t.cliques)}px"></div>
          <div class="bar b-open" style="height:${px(t.abertos)}px"></div>
          <div class="bar b-env" style="height:${px(t.enviados)}px"></div>
          <div class="d">${t.dia.slice(8)}/${t.dia.slice(5, 7)}</div></div>`).join("")}
      </div>
      <div class="legend">
        <span><i class="b-env" style="background:rgba(249,143,22,.35)"></i>enviados</span>
        <span><i class="b-open" style="background:rgba(245,197,66,.55)"></i>aberturas</span>
        <span><i class="b-click" style="background:#f98f16"></i>cliques</span>
        <span><i class="b-resp" style="background:#2dd4a0"></i>respostas</span>
      </div>
      ${!d.tracking_ativo ? `<p class="muted" style="margin-top:12px">📡 Rastreio de abertura/clique ainda não configurado — defina <code>TRACKING_BASE_URL</code>, <code>TRACKING_SECRET</code> e <code>TRACKING_SYNC_KEY</code> no ambiente. Os demais números são em tempo real.</p>` : `<p class="muted" style="margin-top:12px">📬 Abertura é aproximada: o proxy do Gmail é aceito e prefetch puro é descontado (${d.abertos_maquina}). Um clique humano também confirma a abertura. <span class="live-dot">● ao vivo</span> — atualiza sozinho a cada 45s.</p>`}
    </div>`;
  // auto-atualização enquanto a Visão Geral estiver aberta (quase em tempo real)
  if (inicioTimer) clearInterval(inicioTimer);
  inicioTimer = setInterval(() => {
    if (location.hash === "" || location.hash.startsWith("#/inicio")) vInicio();
    else { clearInterval(inicioTimer); inicioTimer = null; }
  }, 45000);
}

/* ---------- TELA: Campanhas ---------- */
const campFiltro = {q: "", status: "", sender_id: "", ordenar: "recentes"};
let campBuscaTimer = null;

function tempoRel(s) {
  if (!s) return "";
  const t = new Date(String(s).replace(" ", "T"));
  if (isNaN(t)) return fmtDt(s);
  const sec = Math.floor((Date.now() - t) / 1000);
  if (sec < 45) return "agora";
  if (sec < 3600) return `há ${Math.floor(sec / 60)} min`;
  if (sec < 86400) return `há ${Math.floor(sec / 3600)} h`;
  if (sec < 86400 * 30) return `há ${Math.floor(sec / 86400)} d`;
  return fmtDt(s);
}

function campCascadeConfirm(prev, titulo) {
  return confirm(
    `${titulo}\n\n` +
    `• ${prev.campanhas} campanha(s)\n` +
    `• ${prev.steps} etapa(s) de sequência\n` +
    `• ${prev.contatos_campanha} vínculo(s) de contato\n` +
    `• ${prev.fila_cancelar} tarefa(s) de fila\n` +
    `• ${prev.meta_keys || 0} meta(s) (briefing/build/log)\n\n` +
    `CRM e histórico de envios/respostas são mantidos. Não dá pra desfazer.`);
}

function campBuscar(el) {
  campFiltro.q = el.value;
  clearTimeout(campBuscaTimer);
  campBuscaTimer = setTimeout(async () => {
    await vCampanhas();
    const inp = document.getElementById("camp-q");
    if (inp) { inp.focus(); inp.setSelectionRange(inp.value.length, inp.value.length); }
  }, 300);
}
function campFiltrarStatus(s) { campFiltro.status = s; vCampanhas(); }
function campFiltrarSender(v) { campFiltro.sender_id = v; vCampanhas(); }
function campLimparFiltros() {
  campFiltro.q = "";
  campFiltro.status = "";
  campFiltro.sender_id = "";
  campFiltro.ordenar = "recentes";
  vCampanhas();
}

async function campLimparTodas() {
  try {
    const prev = await api("/api/campaigns/bulk-delete", {json: {dry_run: true}});
    if (!prev.campanhas) return toast("Nenhuma campanha para limpar.");
    if (!campCascadeConfirm(prev, "Limpar TODAS as campanhas?")) return;
    const r = await api("/api/campaigns/bulk-delete", {json: {dry_run: false}});
    toast(`${r.campanhas_excluidas} campanha(s) excluída(s)` +
      (r.fila_canceladas ? ` · ${r.fila_canceladas} tarefa(s) de fila cancelada(s)` : "") + " ✔", 6000);
    vCampanhas();
  } catch (e) { toast("Erro ao limpar campanhas: " + e.message, 6000); }
}

async function campExcluirLista(id) {
  try {
    const prev = await api("/api/campaigns/bulk-delete", {json: {ids: [+id], dry_run: true}});
    if (!prev.campanhas) return toast("Campanha não encontrada.");
    let nome = "#" + id;
    try {
      const c = await api("/api/campaigns/" + id);
      if (c && c.nome) nome = c.nome;
    } catch (_) { /* dry-run já validou existência */ }
    if (!campCascadeConfirm(prev, `Excluir a campanha "${nome}"?`)) return;
    await api("/api/campaigns/bulk-delete", {json: {ids: [+id], dry_run: false}});
    toast("Campanha excluída em cascata ✔");
    if (location.hash === `#/campanha/${id}`) location.hash = "#/campanhas";
    else vCampanhas();
  } catch (e) { toast("Erro ao excluir: " + e.message, 6000); }
}

function campCardHtml(c) {
  const st = c.contatos_por_status || {};
  const ativos = st.ativo || 0, pend = st.pendente || 0,
        pausa = st.pausado_resposta || 0, done = st.concluido || 0;
  const b = c.build || {};
  const miniBuild = b.status === "gerando"
    ? (b.stale
      ? badge("travada", "encerrada")
      : badge(`gerando ${b.feitos || 0}/${b.total || "?"}` +
              (b.agente_rotulo ? ` · ${b.agente_rotulo}` : "") +
              (b.eta_s != null ? ` · ${etaLabel(b.eta_s)}` : ""), "pendente"))
    : b.status === "erro" ? badge("geração falhou", "encerrada") : "";
  const objetivo = (c.objetivo || "").trim();
  const quando = tempoRel(c.atualizada_em || c.criado_em);
  const lockMut = c.somente_acompanhamento || c.precisa_decisao;
  const editDisabled = lockMut
    ? campMutDisabled(c)
    : (c.status === "ativa" ? `disabled title="Pause a campanha antes de editar"` : "");
  const delDisabled = lockMut ? campMutDisabled(c) : "";
  return `<article class="card camp-card" aria-labelledby="camp-title-${c.id}">
    <div class="camp-card__head" onclick="location.hash='#/campanha/${c.id}'"
      onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();location.hash='#/campanha/${c.id}'}"
      role="link" tabindex="0" aria-label="Abrir campanha ${esc(c.nome)}">
      <div class="camp-card__icon" aria-hidden="true">📁</div>
      <div class="camp-card__title-wrap">
        <h3 class="camp-card__title" id="camp-title-${c.id}">${esc(c.nome)}</h3>
        <div class="camp-card__meta">
          ${badge(c.status)} ${badge(c.tipo, "pendente")} ${origemBadge(c)} ${miniBuild}
          ${c.somente_acompanhamento ? badge("só acompanhamento", "pendente") : ""}
          ${c.precisa_decisao ? badge("decidir origem", "pausado_resposta") : ""}
          <span>${(c.steps || []).length} e-mail(s)</span>
          <span>${c.total_contatos || 0} contato(s)</span>
          <span>${c.enviados || 0} enviado(s)</span>
          ${c.sender ? `<span translate="no">✉️ ${esc(c.sender.email)}</span>` : ""}
          ${quando ? `<span>atualizada ${quando}</span>` : ""}
        </div>
      </div>
    </div>
    ${campOrigemBanner(c)}
    ${objetivo ? `<details class="camp-card__expand" onclick="event.stopPropagation()">
      <summary aria-label="Expandir objetivo">
        <span class="camp-card__objetivo-clamp">${esc(prosaTldr(objetivo) || objetivo.slice(0, 180))}</span>
      </summary>
      <div class="camp-card__objetivo-full">${fmtRichProsa(objetivo)}</div>
    </details>` : `<p class="muted" style="margin:0">Sem objetivo definido.</p>`}
    <div class="camp-card__meta" style="margin-top:10px">
      ${pend ? badge(pend + " pendentes", "pendente") : ""}
      ${ativos ? badge(ativos + " ativos", "ativo") : ""}
      ${pausa ? badge(pausa + " responderam", "pausado_resposta") : ""}
      ${done ? badge(done + " concluídos", "concluido") : ""}
    </div>
    <div class="camp-card__actions" onclick="event.stopPropagation()">
      <button type="button" class="primary" aria-label="Abrir campanha"
        onclick="location.hash='#/campanha/${c.id}'">Abrir</button>
      <button type="button" aria-label="Editar campanha" ${editDisabled}
        onclick="editarCampanha(${c.id})">Editar</button>
      <button type="button" class="btn-danger" aria-label="Excluir campanha" ${delDisabled}
        onclick="campExcluirLista(${c.id})">Excluir</button>
    </div>
  </article>`;
}

async function vCampanhas() {
  const p = new URLSearchParams();
  if (campFiltro.q.trim()) p.set("q", campFiltro.q.trim());
  if (campFiltro.status) p.set("status", campFiltro.status);
  if (campFiltro.sender_id) p.set("sender_id", campFiltro.sender_id);
  if (campFiltro.ordenar !== "recentes") p.set("ordenar", campFiltro.ordenar);
  const [meta, d, sd] = await Promise.all([
    ensureDashboardMeta(),
    api("/api/campaigns" + (p.toString() ? "?" + p : "")),
    api("/api/senders").catch(() => ({senders: []})),
  ]);
  const espelho = !!meta.espelho;
  const chips = [["", "Todas"], ["rascunho", "Rascunho"], ["ativa", "Ativa"],
                 ["pausada", "Pausada"], ["encerrada", "Encerrada"]];
  const filtrando = !!(campFiltro.q.trim() || campFiltro.status || campFiltro.sender_id
    || campFiltro.ordenar !== "recentes");
  const lista = d.campanhas || [];
  const emptyMsg = filtrando
    ? `<div class="card camp-empty"><strong>Nenhuma campanha com esses filtros</strong>
        Ajuste a busca ou <button type="button" onclick="campLimparFiltros()">limpar filtros</button>.</div>`
    : `<div class="card camp-empty"><strong>Nenhuma campanha ainda</strong>
        ${espelho ? "Espelhe o banco local (push seed) para ver campanhas aqui." : "Crie uma com IA ou a partir de um template."}</div>`;
  const createBtns = espelho
    ? `<span class="muted" style="font-size:13px">Ambiente remoto — só acompanhamento. Crie/edite no Mac.</span>`
    : `<button type="button" class="warn" onclick="campLimparTodas()"
        aria-label="Limpar todas as campanhas">Limpar campanhas</button>
      <button type="button" onclick="sugerirCampanhas()">Sugerir campanhas (IA)</button>
      <button type="button" class="primary" onclick="dlgCampanha()">Nova campanha com IA</button>`;
  view.innerHTML = `
    <div class="toolbar"><h2 style="margin:0;text-wrap:pretty">Campanhas</h2>
      ${espelho ? badge("espelho remoto", "pendente") : badge("local", "ativo")}
      <span class="sp"></span>
      ${createBtns}</div>
    <div class="toolbar filter-bar filter-bar--sticky" role="search" aria-label="Filtros de campanhas">
      <input id="camp-q" type="search" name="camp-q" autocomplete="off" spellcheck="false"
        placeholder="Buscar por nome ou objetivo…" aria-label="Buscar campanhas"
        value="${esc(campFiltro.q)}" style="min-width:220px" oninput="campBuscar(this)">
      ${chips.map(([v, rot]) => `<label class="tagopt${campFiltro.status === v ? " on" : ""}">
        <input type="radio" name="camp-status" value="${v}" ${campFiltro.status === v ? "checked" : ""}
          onchange="campFiltrarStatus(this.value)"> ${rot}</label>`).join("")}
      <select onchange="campFiltrarSender(this.value)" aria-label="Filtrar por remetente">
        <option value="">Todos os remetentes</option>
        ${sd.senders.map(s => `<option value="${s.id}" ${String(campFiltro.sender_id) === String(s.id) ? "selected" : ""}>${esc(s.email)}</option>`).join("")}
      </select>
      <select onchange="campFiltro.ordenar=this.value;vCampanhas()" aria-label="Ordenar campanhas">
        ${[["recentes", "Mais recentes"], ["nome", "Nome (A–Z)"], ["status", "Status"]].map(([v, rot]) =>
          `<option value="${v}" ${campFiltro.ordenar === v ? "selected" : ""}>${rot}</option>`).join("")}
      </select>
      <span class="muted" style="font-variant-numeric:tabular-nums">${d.total} campanha(s)</span>
      ${filtrando ? `<button type="button" onclick="campLimparFiltros()" aria-label="Limpar filtros">Limpar filtros</button>` : ""}
    </div>
    <div id="sug-card"></div>
    ${lista.length ? lista.map(campCardHtml).join("") : emptyMsg}`;
  renderSugestoes();
}

/* ---------- loop de sugestão de campanhas (portfólio IA) ---------- */
let sugTimer = null;

async function sugerirCampanhas() {
  const max = prompt("Quantas campanhas a IA deve sugerir? (1–8)", "4");
  if (max === null) return;
  try {
    const r = await api("/api/campaigns/suggest", {json: {max_campanhas: parseInt(max) || 4}});
    toast(r.nota || "Sugestão iniciada — o estrategista GTM e o copywriter estão trabalhando…", 5000);
    renderSugestoes();
  } catch (e) { toast("Erro ao iniciar sugestão: " + e.message, 6000); }
}

async function gerarPortfolio() {
  if (!confirm("Gerar TODAS as sugestões aprovadas como campanhas em RASCUNHO?\n\n" +
    "A IA escreve os emails de cada campanha em sequência. Nada é ativado ou enviado sem você aprovar.")) return;
  try {
    const r = await api("/api/campaigns/suggest/generate", {json: {}});
    toast(r.nota || "Geração dos rascunhos iniciada.", 5000);
    renderSugestoes();
  } catch (e) { toast("Erro ao gerar rascunhos: " + e.message, 6000); }
}

async function renderSugestoes() {
  const el = document.getElementById("sug-card");
  if (!el) { clearTimeout(sugTimer); return; }
  let s = null;
  try { s = await api("/api/campaigns/suggest"); } catch (e) { /* servidor ocupado */ }
  clearTimeout(sugTimer);
  if (!s || s.status === "vazio") { el.innerHTML = ""; return; }
  const gerando = s.status === "gerando" || s.status === "gerando_rascunhos";
  const criadas = s.criadas || [];
  const cidPor = Object.fromEntries(criadas.map(c => [c.nome, c.cid]));
  const nSug = (s.campanhas || []).length;
  el.innerHTML = `<details class="camp-portfolio" ${gerando || s.status === "erro" ? "open" : ""}>
    <summary>
      <span>Portfólio sugerido pela IA</span>
      ${badge(s.status, s.status === "erro" ? "encerrada" : gerando ? "pendente" : "ativo")}
      <span class="muted" style="font-weight:500">${nSug} ideia(s)</span>
      <span class="sp"></span>
      ${!gerando && nSug ? `<button type="button" class="primary" onclick="event.preventDefault();gerarPortfolio()">Gerar rascunhos</button>` : ""}
    </summary>
    ${gerando ? `<div class="muted">${esc(s.etapa || "trabalhando…")}${s.eta_s != null ? ` · faltam ${etaLabel(s.eta_s)}` : ""}</div>` : ""}
    ${s.status === "erro" ? `<div class="muted">${esc(s.erro || "erro na sugestão")}</div>` : ""}
    ${(s.campanhas || []).map(c => {
      const linha = [c.icp && `ICP: ${c.icp}`, c.gancho && `Gancho: ${c.gancho}`].filter(Boolean).join(" · ");
      return `<div class="inset-panel camp-sug-item">
      <div class="toolbar" style="margin-bottom:4px"><b>${esc(c.nome)}</b>
        ${badge((c.segmentos || []).join(", ") || "—", "pendente")}
        ${c.aprovado === false ? badge("reprovada pelo copywriter", "encerrada") : ""}
        ${cidPor[c.nome] ? `<a href="#/campanha/${cidPor[c.nome]}">rascunho #${cidPor[c.nome]}</a>` : ""}
        <span class="sp"></span><span class="muted">${c.n_emails || 3} e-mail(s)</span></div>
      <div class="muted camp-card__objetivo-clamp">${esc(linha || c.objetivo || "—")}</div>
      ${(c.prova || c.critica || c.objetivo) ? `<details class="camp-card__expand" onclick="event.stopPropagation()">
        <summary aria-label="Detalhes da sugestão"><span class="camp-card__objetivo-clamp">${esc((c.objetivo || "").slice(0, 160) || "Detalhes")}</span></summary>
        <div class="camp-sug-extra">
          ${c.objetivo ? `<div><b>Objetivo</b>${fmtRichProsa(c.objetivo)}</div>` : ""}
          ${c.prova ? `<div><b>Prova</b>${fmtRichProsa(c.prova)}</div>` : ""}
          ${c.critica ? `<div class="prosa-callout"><i>${fmtInlineMd(esc(c.critica))}</i></div>` : ""}
        </div>
      </details>` : ""}
    </div>`;
    }).join("")}
    <div style="margin-top:8px"><small class="muted">Guardrail: tudo nasce em rascunho — nada é ativado sem aprovação. Atualizado: ${esc(s.atualizado_em || "—")}</small></div>
  </details>`;
  if (gerando) sugTimer = setTimeout(renderSugestoes, 5000);
}

async function ncUpdateCount() {
  const el = document.getElementById("nc-count");
  if (!el) return;
  const p = new URLSearchParams({
    segmentos: [...document.querySelectorAll(".nc-seg:checked")].map(x => x.value).join(","),
    tags: [...document.querySelectorAll(".nc-tag:checked")].map(x => x.value).join(","),
    apenas_sem_resposta: document.getElementById("nc-semresp").checked,
    perfil: document.getElementById("nc-perfil").value,
  });
  try { const r = await api("/api/audience?" + p);
    const resumo = r.resumo || {};
    el.textContent = r.total + (r.clientes_excluidos ? ` (${r.clientes_excluidos} cliente(s) excluído(s))` : "");
    const hint = document.getElementById("nc-perfil-hint");
    hint.textContent = `${resumo.com_telefone || 0} com telefone · ${resumo.com_historico || 0} com histórico · ${resumo.com_etapa_funil || 0} com etapa de funil`;
  }
  catch (e) { el.textContent = "?"; }
}

let ncTplRef = "";  // quando setado, o diálogo cria campanha a partir de um template (sem IA)
async function dlgCampanha(templateRef) {
  ncTplRef = templateRef || "";
  const modoTpl = !!ncTplRef;
  // adapta o diálogo: modo template esconde objetivo/nº de emails e mostra nome
  document.getElementById("nc-title").textContent = modoTpl ? "📨 Campanha a partir de template" : "✨ Nova campanha com IA";
  document.getElementById("nc-sub").innerHTML = modoTpl
    ? "Usa este template como o e-mail da campanha — <b>sem IA, instantâneo</b>. Nasce em rascunho; nada é enviado sem você ativar."
    : "Descreva o objetivo em texto livre. A IA usa seus KBs para redigir os emails — tudo nasce em <b>rascunho</b>.";
  document.getElementById("nc-nome-field").style.display = modoTpl ? "" : "none";
  document.getElementById("nc-objetivo-field").style.display = modoTpl ? "none" : "";
  document.getElementById("nc-n-field").style.display = modoTpl ? "none" : "";
  if (modoTpl) {
    document.getElementById("nc-nome").value = "";
    api("/api/templates").then(td => {       // nome default a partir do template
      const tt = (td.templates || []).find(x => x.ref === ncTplRef);
      const el = document.getElementById("nc-nome");
      if (tt && !el.value) el.value = `Campanha — ${tt.nome || tt.assunto || ""}`.slice(0, 80);
    }).catch(() => {});
  }
  const box = document.getElementById("nc-tags");
  box.innerHTML = '<span class="muted">carregando…</span>';
  document.getElementById("dlg-campanha").showModal();
  try {
    const ld = await api("/api/lead-segments");
    const perfil = document.getElementById("nc-perfil");
    perfil.innerHTML = `<option value="">Todos os leads elegíveis</option>` + ld.segmentos.map(s =>
      `<option value="${s.id}">${esc(s.nome)} (${s.contatos})</option>`).join("");
    perfil.onchange = () => {
      const s = ld.segmentos.find(x => x.id === perfil.value);
      if (s && !document.getElementById("nc-objetivo").value.trim()) document.getElementById("nc-objetivo").value = s.objetivo_sugerido;
      ncUpdateCount();
    };
  } catch (e) { /* mantém opção geral */ }
  // remetente: só conectados são selecionáveis (à prova de erros)
  try {
    const sd = await api("/api/senders");
    const sel = document.getElementById("nc-sender");
    const disponiveis = sd.senders.filter(s => s.para_campanha_nova);
    sel.innerHTML = disponiveis.map(s =>
      `<option value="${s.id}" ${s.conectado ? "" : "disabled"}>${esc(s.email)} — ${esc(s.proposito)}${s.conectado ? "" : "  (🔴 precisa login)"}</option>`).join("");
    const first = disponiveis[0];
    if (first) sel.value = first.id;
    const hint = document.getElementById("nc-sender-hint");
    const upd = () => { const s = sd.senders.find(x => x.id == sel.value);
      hint.textContent = s ? `Os e-mails sairão de ${s.email}.` : ""; };
    sel.onchange = upd; upd();
  } catch (e) { /* ignore */ }
  try {
    const td = await api("/api/tags");
    box.innerHTML = td.tags.length
      ? td.tags.map(t => `<label class="tagopt"><input type="checkbox" class="nc-tag" value="${esc(t.tag)}"> ${esc(t.tag)} <small class="muted">${t.contatos}</small></label>`).join("")
      : '<span class="muted">nenhuma tag ainda</span>';
  } catch (e) { box.innerHTML = '<span class="muted">erro ao carregar tags</span>'; }
  // contagem ao vivo: qualquer filtro recalcula
  document.querySelectorAll(".nc-seg, .nc-tag, #nc-semresp, #nc-perfil").forEach(el =>
    el.addEventListener("change", ncUpdateCount));
  ncUpdateCount();
}

document.getElementById("nc-criar").onclick = async () => {
  const senderSel = document.getElementById("nc-sender");
  if (!senderSel.value) return toast("Escolha um remetente conectado.");
  const min = document.getElementById("nc-min").value, max = document.getElementById("nc-max").value;
  const comum = {
    sender_id: +senderSel.value,
    tipo: document.getElementById("nc-tipo").value,
    cad_min: min ? +min : null, cad_max: max ? +max : (min ? +min : null),
    segmentos: [...document.querySelectorAll(".nc-seg:checked")].map(x => x.value),
    apenas_sem_resposta: document.getElementById("nc-semresp").checked,
    tags: [...document.querySelectorAll(".nc-tag:checked")].map(x => x.value),
    inicio_em: document.getElementById("nc-inicio").value || "",
    audience_profile: document.getElementById("nc-perfil").value || "",
  };

  if (ncTplRef) {  // modo template — instantâneo, sem IA
    const body = {...comum, template_ref: ncTplRef, nome: document.getElementById("nc-nome").value.trim()};
    document.getElementById("dlg-campanha").close();
    try {
      const r = await api("/api/campaigns/from-template", {json: body});
      toast(`Campanha #${r.id} criada (${r.contatos} contatos) ✔`, 4000);
      location.hash = "#/campanha/" + r.id; route();
    } catch (e) { toast("Erro: " + e.message, 6000); }
    return;
  }

  const objetivo = document.getElementById("nc-objetivo").value.trim();
  if (!objetivo) return toast("Descreva o objetivo da campanha.");
  const body = {...comum, objetivo, n_emails: +document.getElementById("nc-n").value || 3};
  document.getElementById("dlg-campanha").close();
  view.innerHTML = `<div class="card" style="text-align:center;padding:48px 24px">
    <div class="spinner"></div>
    <h3 style="margin:14px 0 6px">🧠 Criando sua campanha…</h3>
    <p class="muted">A IA vai redigir os ${body.n_emails} e-mails um a um — você acompanha o progresso na próxima tela.</p></div>`;
  try {
    const r = await api("/api/campaigns/ai-create", {json: body});
    toast("Campanha #" + r.id + " criada — gerando os e-mails… ✔", 4000);
    location.hash = "#/campanha/" + r.id;
    route();
  } catch (e) {
    view.innerHTML = `<div class="card"><h3>⚠️ Não consegui criar a campanha</h3>
      <p>${esc(e.message)}</p>
      <button class="primary" onclick="dlgCampanha()">Tentar de novo</button>
      <button onclick="location.hash='#/campanhas';route()">Voltar</button></div>`;
  }
};

/* ---------- TELA: Campanha (detalhe) ---------- */
const etaLabel = s => s == null ? "" : s >= 90 ? `~${Math.round(s / 60)} min` : `~${Math.max(10, Math.round(s / 10) * 10)}s`;
let logRaciocinioAberto = true;   // aberto por padrão durante geração; preserva entre polls

function buildFaseLabel(b) {
  const total = b.total || 0, feitos = b.feitos || 0;
  switch (b.fase) {
    case "planejando": return "Planejando sequência";
    case "escrevendo": return `Escrevendo E${Math.min(feitos + 1, total || 1)}/${total || "?"}`;
    case "briefing": return "Briefing…";
    case "pronto": return "Concluído";
    case "erro": return "Erro";
    default: return b.etapa || "Gerando…";
  }
}

function renderBuildTimeline(b) {
  const total = b.total || 0, feitos = b.feitos || 0;
  const fase = b.fase || "";
  const plano = b.plano_emails || [];
  const n = Math.max(total, plano.length, 1);
  const agente = (b.agente || "").toLowerCase();
  const task = (b.task_type || "").toLowerCase();
  const steps = [{key: "plan", label: "Planejar", agent: "estrategista"}];
  for (let i = 1; i <= n; i++) {
    steps.push({key: "w" + i, label: "E" + i + " escrever", agent: "copywriter", ord: i});
    steps.push({key: "r" + i, label: "E" + i + " revisar", agent: "editor", ord: i});
  }
  steps.push({key: "brief", label: "Briefing", agent: "estrategista_gtm"});
  return '<ol class="build-timeline" aria-label="Pipeline de agentes">' +
    steps.map(s => {
      let st = "pendente";
      if (s.key === "plan") {
        st = (fase !== "planejando" && (b.plano_emails || feitos > 0 || fase === "pronto" || fase === "briefing"))
          ? "feito" : (fase === "planejando" || (agente === "estrategista" && fase !== "pronto") ? "atual" : "pendente");
      } else if (s.key === "brief") {
        st = fase === "pronto" ? "feito" : (fase === "briefing" || agente === "estrategista_gtm" ? "atual" : "pendente");
      } else if (s.key.charAt(0) === "w") {
        const ord = s.ord;
        if (ord <= feitos) st = "feito";
        else if (fase === "escrevendo" && ord === feitos + 1 && (agente === "copywriter" || task.indexOf("gerar_") === 0)) st = "atual";
        else if (fase === "escrevendo" && ord === feitos + 1 && !agente) st = "atual";
      } else if (s.key.charAt(0) === "r") {
        const ord = s.ord;
        if (ord <= feitos) st = "feito";
        else if (fase === "escrevendo" && ord === feitos + 1 && (agente === "editor" || task.indexOf("revisar_") === 0)) st = "atual";
      }
      return '<li class="build-timeline__step build-timeline__step--' + st + '" title="' + esc(s.agent) + '">' +
        '<span class="build-timeline__agent">' + esc(s.agent) + '</span> ' + esc(s.label) + '</li>';
    }).join("") +
  '</ol>';
}

function renderBuildPanel(b, cid) {
  const total = b.total || 0, feitos = b.feitos || 0, pct = b.percent || 0;
  const faseLabel = buildFaseLabel(b);
  const agente = b.agente_rotulo || b.agente || "";
  const eta = b.eta_s != null
    ? `⏱ faltam ${etaLabel(b.eta_s)}${b.eta_estimada ? " <small class='muted'>(estimativa — afina após o 1º e-mail)</small>" : ""}` : "";
  const plano = b.plano_emails || [];
  const log = b.log || [];
  const logOpen = logRaciocinioAberto;
  const staleMin = Math.max(1, Math.round((b.stale_s || 0) / 60));
  const staleBanner = b.stale ? `<div class="build-stale-banner" role="alert">
      <b>Geração parada</b> — último agente: ${esc(agente || "—")} · sem atualização há ~${staleMin} min.
      <div style="margin-top:8px">
        <button type="button" class="green" onclick="campRetomar(${Number(cid) || 0})">🔄 Retomar criação</button>
      </div>
    </div>` : "";
  // preferir logs que citam o agente ativo; senão últimos 8
  const logView = (() => {
    const rev = log.slice().reverse();
    const agenteHit = agente
      ? rev.filter(l => (l.msg || "").toLowerCase().includes(String(agente).toLowerCase().split(" ")[0] || ""))
      : [];
    const pick = (agenteHit.length ? agenteHit : rev).slice(0, 8);
    return pick.length ? pick : rev.slice(0, 8);
  })();
  return `<div class="card card-info">
    ${staleBanner}
    <div class="toolbar" style="margin-bottom:8px">
      ${b.stale ? "" : `<div class="spinner" style="width:18px;height:18px;display:inline-block;vertical-align:middle"></div>`}
      <b style="margin-left:4px">${b.stale ? "⏸ Geração interrompida"
        : (String(b.etapa || "").toLowerCase().includes("reescr") || String(b.etapa || "").toLowerCase().includes("reescrita")
          ? "🔁 Reescrevendo e-mails…" : "🧠 Gerando os e-mails…")}</b>
      <span class="muted">${feitos}/${total || "?"} prontos — ${esc(b.etapa || "")}</span>
      ${agente ? `<span class="agente-chip" title="Agente ativo nesta etapa">Agente: ${esc(agente)}</span>` : ""}
      ${cid ? `<a class="muted" href="#/agentes?campanha=${Number(cid)||0}" style="margin-left:8px;font-size:12px">ver runs desta campanha</a>` : ""}
      <span class="sp"></span><b>${eta}</b></div>
    <div class="progress-meta">
      <span class="progress-fase">${esc(faseLabel)}</span>
      <span class="muted">${pct}%</span>
    </div>
    <div class="progress-track" role="progressbar" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100"
      aria-label="${esc(faseLabel)}">
      <div class="progress-fill" style="width:${pct}%"></div></div>
    ${renderBuildTimeline(b)}
    ${plano.length ? `<div style="margin-top:10px">${plano.map((e, i) => {
      const ord = i + 1;
      const aplicado = !!(e.aplicado || (ord <= feitos));
      const ico = aplicado ? "✅" : (ord === feitos + 1 ? "✍️" : "⏳");
      const assunto = e.assunto || "";
      return `<div style="margin-top:4px">${ico} <b>E${ord} ${esc(e.nome || "")}</b>
        ${aplicado ? `<span class="badge b-valido">na sequência</span>` : ""}
        ${assunto ? `<div class="muted" style="margin-left:22px"><b>Assunto:</b> ${esc(assunto)}</div>` : ""}
        <div class="muted" style="margin-left:22px">${fmtProsa(e.angulo || "")}</div></div>`;
    }).join("")}</div>` : ""}
    ${log.length ? `<details style="margin-top:10px" ${logOpen ? "open" : ""}
        ontoggle="logRaciocinioAberto=this.open">
      <summary style="cursor:pointer"><b>🧩 Raciocínio da IA</b> <small class="muted">(${log.length} passos — o que está sendo considerado)</small></summary>
      <div class="ai-log-box">
        ${logView.map(l => `<div style="margin-bottom:8px">
          <small class="muted">${esc((l.ts || "").slice(11, 16))}</small>
          ${fmtProsa(l.msg || "")}</div>`).join("")}
      </div></details>` : ""}
    <div style="margin-top:8px"><small class="muted">Pode deixar a aba aberta; a tela atualiza sozinha. Se a geração parar (ex.: reload do servidor), use Retomar criação. Ao final a IA escreve o briefing de lançamento.</small></div>
  </div>`;
}


function _actSoftList(g) {
  return (g && g.soft_pending) || [];
}

function renderActivationGuide(g) {
  if (!g) return "";
  const steps = g.steps || [];
  const next = g.next_step;
  const soft = _actSoftList(g);
  const ready = !!g.ready_to_activate;
  const strict = !!g.ready_strict;
  const rows = steps.map((s, i) => {
    const ok = !!s.done;
    const hard = !!s.blocks_activation;
    const softPend = !ok && (s.severity || "soft") === "soft";
    const cls = ok ? "act-step--ok" : (hard ? "act-step--block" : (softPend ? "act-step--soft" : "act-step--wait"));
    const ico = ok ? "✓" : (hard ? String(i + 1) : (softPend ? "!" : "·"));
    let actions = "";
    if (!ok && s.key === "templates") {
      actions = `<div class="act-step__actions">
        <button type="button" class="green act-approve-tpl">Aprovar todos</button>
        <button type="button" class="lnk act-goto-seq">Abrir sequência</button>
      </div>`;
    } else if (!ok && s.key === "checklist") {
      const items = s.items || g.checklist_pending || [];
      actions = `<div class="act-step__actions">
        <button type="button" class="primary act-run-checks">Rodar checks auto</button>
      </div>
      <ul class="act-cl-inline">${items.map(it => {
        const done = ["done", "skipped"].includes(it.status);
        const agent = it.resolvable === "agent" && it.suggestion_id;
        return `<li class="act-cl-item" data-clid="${esc(it.id)}">
          <label class="act-cl-item__lab">
            <input type="checkbox" class="act-cl-toggle" data-clid="${esc(it.id)}" ${done ? "checked" : ""}>
            <span>${esc(it.label || it.id)}</span>
          </label>
          <span class="muted act-cl-item__kind">${esc(it.kind || "manual")}${it.resolvable === "agent" ? " · agente" : ""}</span>
          <input type="text" class="act-cl-evidence" data-clid="${esc(it.id)}"
            placeholder="evidência / o que foi feito" value="${esc(it.evidence || "")}">
          ${agent ? `<button type="button" class="primary act-cl-agent" data-sid="${esc(it.suggestion_id)}">Resolver com agentes</button>` : ""}
          <button type="button" class="lnk act-cl-mark" data-clid="${esc(it.id)}">Marcar feito</button>
        </li>`;
      }).join("")}</ul>`;
    } else if (!ok && s.key === "run_ativo" && g.active_run) {
      actions = `<div class="act-step__actions">
        <button type="button" class="warn act-cancel-run" data-rid="${g.active_run.id}">Cancelar run #${g.active_run.id}</button>
      </div>`;
    } else if (!ok && s.key === "patches") {
      const staleN = (g.patches_awaiting || []).filter(p => !p.ok).length;
      actions = `<div class="act-step__actions">
        <button type="button" class="lnk act-goto-imp">Ir para Melhorias</button>
        ${staleN ? `<button type="button" class="warn act-abandon-stale">Descartar ${staleN} antigo(s)</button>` : ""}
      </div>`;
    }
    const sev = hard ? "obrigatório" : (softPend ? "recomendado" : "");
    return `<li class="act-step ${cls}" data-key="${esc(s.key)}">
      <span class="act-step__n">${ico}</span>
      <div class="act-step__body">
        <b>${esc(s.title)}</b>
        ${sev && !ok ? `<span class="act-sev act-sev--${hard ? "hard" : "soft"}">${sev}</span>` : ""}
        ${!ok && s.detail ? `<div class="muted">${esc(s.detail)}</div>` : ""}
        ${!ok && s.where ? `<div class="act-step__where">→ <b>Onde:</b> ${esc(s.where)}</div>` : ""}
        ${!ok && s.action && s.action !== "—" ? `<div class="act-step__action"><b>Faça:</b> ${esc(s.action)}</div>` : ""}
        ${actions}
      </div>
    </li>`;
  }).join("");
  const nextBox = next ? `<div class="act-next">
      <div><span class="muted">Próximo passo</span><br><b>${esc(next.title)}</b></div>
      <div class="muted">${esc(next.where || "")}${next.action ? " · " + esc(next.action) : ""}</div>
    </div>` : "";
  const staleN = (g.patches_awaiting || []).filter(p => !p.ok).length;
  let badgeHtml;
  if (!ready) badgeHtml = badge("faltam passos obrigatórios", "pendente");
  else if (strict) badgeHtml = badge("pronto para ativar", "ativo");
  else badgeHtml = badge("pode enviar com ressalvas", "duvidoso");
  const softBtns = (ready && soft.length) ? `
      <button type="button" class="green" id="act-ativar-soft">▶ Ativar mesmo assim</button>
      <button type="button" class="primary" id="act-disparar-soft">⚡ Disparar agora mesmo assim</button>` : "";
  return `<div class="card act-guide" id="act-guide">
    <div class="toolbar" style="margin-bottom:8px;flex-wrap:wrap;gap:8px">
      <b>Passo a passo até ativar</b>
      ${badgeHtml}
      <span class="sp"></span>
      ${softBtns}
      ${staleN ? `<button type="button" class="warn act-abandon-stale">Descartar ${staleN} patch(es) antigo(s)</button>` : ""}
      <button type="button" id="act-refresh-guide">↻ Atualizar guia</button>
    </div>
    ${ready && soft.length ? `<div class="act-soft-banner">
      <b>${soft.length} pendência(s) recomendada(s)</b> — não bloqueiam o envio.
      <ul>${soft.map(p => `<li>${esc(p.title)}${p.detail ? `: ${esc(p.detail)}` : ""}</li>`).join("")}</ul>
      Resolva abaixo ou use <b>Ativar / Disparar mesmo assim</b>.
    </div>` : ""}
    ${nextBox}
    <ol class="act-steps">${rows}</ol>
  </div>`;
}

async function loadActivationGuide(cid) {
  try { return await api(`/api/campaigns/${cid}/activation-guide`); }
  catch (_) { return null; }
}

function _actSoftConfirmText(g, verb) {
  const soft = _actSoftList(g);
  const lines = soft.map(p => `• ${p.title}${p.detail ? " — " + p.detail : ""}`).join("\n");
  return `${verb} mesmo com pendências recomendadas?\n\n${lines || "—"}\n\n` +
    `Templates não aprovados serão carimbados como aprovados para o envio.\n` +
    `Confirme para continuar.`;
}

function wireActivationGuide(cid) {
  const box = document.getElementById("act-guide");
  if (!box) return;
  const g = window._activationGuide;
  const n = (window._campanhaAtual && window._campanhaAtual.total_contatos) || 0;
  const ref = document.getElementById("act-refresh-guide");
  if (ref) ref.onclick = () => vCampanha(cid);

  const softAtivar = document.getElementById("act-ativar-soft");
  if (softAtivar) softAtivar.onclick = () => {
    if (!confirm(_actSoftConfirmText(g, "Ativar"))) return;
    campAtivar(cid, n, "ENVIAR_COM_PENDENCIAS");
  };
  const softDisp = document.getElementById("act-disparar-soft");
  if (softDisp) softDisp.onclick = () => {
    if (!confirm(_actSoftConfirmText(g, "Disparar agora"))) return;
    campDispararAgora(cid, n, "ENVIAR_COM_PENDENCIAS");
  };

  box.querySelectorAll(".act-abandon-stale").forEach(abd => {
    abd.onclick = async () => {
      if (!confirm("Descartar patches antigos/desatualizados que não podem ser aplicados?")) return;
      try {
        const r = await api(`/api/campaigns/${cid}/improvements/abandon-stale`, {method: "POST"});
        toast(`${r.n || 0} patch(es) descartado(s)`, 4000);
        vCampanha(cid);
      } catch (e) { toast(e.message, 5000); }
    };
  });

  const ap = box.querySelector(".act-approve-tpl");
  if (ap) ap.onclick = () => checklistAprovarTemplates(cid);

  const seq = box.querySelector(".act-goto-seq");
  if (seq) seq.onclick = () => {
    document.querySelector(".camp-section")?.scrollIntoView({behavior: "smooth"});
    const det = [...document.querySelectorAll("details.camp-section")].find(d =>
      (d.querySelector("summary") || {}).textContent?.includes("Sequência"));
    if (det) det.open = true;
  };

  const runChk = box.querySelector(".act-run-checks");
  if (runChk) runChk.onclick = async () => {
    try {
      toast("Rodando checks automáticos…", 2000);
      await api(`/api/campaigns/${cid}/checklist/run`, {method: "POST"});
      vCampanha(cid);
    } catch (e) { toast("Erro nos checks: " + e.message, 6000); }
  };

  const cancelRun = box.querySelector(".act-cancel-run");
  if (cancelRun) cancelRun.onclick = async () => {
    const rid = cancelRun.dataset.rid;
    if (!confirm(`Cancelar run #${rid}?`)) return;
    try {
      await api(`/api/improvements/runs/${rid}/cancel`, {
        method: "POST", json: {abandon_approvals: true, reason: "cancelado no guia de ativação"},
      });
      toast("Run cancelado", 3000);
      vCampanha(cid);
    } catch (e) { toast(e.message, 5000); }
  };

  const gotoImp = box.querySelector(".act-goto-imp");
  if (gotoImp) gotoImp.onclick = () => {
    const det = [...document.querySelectorAll("details.camp-section")].find(d =>
      (d.querySelector("summary") || {}).textContent?.includes("Melhorias"));
    if (det) { det.open = true; det.scrollIntoView({behavior: "smooth", block: "start"}); }
  };

  async function markCl(itemId, checked, evidence) {
    try {
      const status = checked ? "done" : "pending";
      const ev = checked ? (evidence || "marcado no passo a passo") : "";
      await api(`/api/campaigns/${cid}/checklist/${encodeURIComponent(itemId)}`, {
        method: "PATCH", json: {status, evidence: ev},
      });
      toast(checked ? "Item marcado ✔" : "Item reaberto", 2500);
      vCampanha(cid);
    } catch (e) { toast("Não foi possível atualizar: " + e.message, 5000); }
  }

  box.querySelectorAll(".act-cl-toggle").forEach(cb => {
    cb.onchange = () => {
      const id = cb.dataset.clid;
      const evEl = box.querySelector(`.act-cl-evidence[data-clid="${CSS.escape(id)}"]`);
      markCl(id, cb.checked, evEl ? evEl.value : "");
    };
  });
  box.querySelectorAll(".act-cl-mark").forEach(btn => {
    btn.onclick = () => {
      const id = btn.dataset.clid;
      const evEl = box.querySelector(`.act-cl-evidence[data-clid="${CSS.escape(id)}"]`);
      markCl(id, true, evEl ? evEl.value : "");
    };
  });
  box.querySelectorAll(".act-cl-agent").forEach(btn => {
    btn.onclick = async () => {
      const sid = btn.dataset.sid;
      if (!sid) return;
      if (!confirm("Iniciar run de melhorias só para este item (modo review)?")) return;
      try {
        toast("Agentes trabalhando…", 3000);
        await api(`/api/campaigns/${cid}/improvements/runs`, {
          method: "POST",
          json: {mode: "review", replace: true, suggestion_ids: [sid]},
        });
        toast("Run iniciado — acompanhe em Melhorias", 5000);
        vCampanha(cid);
        const det = [...document.querySelectorAll("details.camp-section")].find(d =>
          (d.querySelector("summary") || {}).textContent?.includes("Melhorias"));
        if (det) { det.open = true; det.scrollIntoView({behavior: "smooth"}); }
      } catch (e) { toast("Não foi possível iniciar: " + e.message, 6000); }
    };
  });
}

async function vCampanha(id) {
  await ensureDashboardMeta();
  const [c, guide] = await Promise.all([
    api("/api/campaigns/" + id),
    loadActivationGuide(id),
  ]);
  window._campanhaAtual = c;
  window._activationGuide = guide;
  const gerando = c.build && c.build.status === "gerando";
  const erroBuild = c.build && c.build.status === "erro";
  const lockMut = !!(c.somente_acompanhamento || c.precisa_decisao);
  const softN = (guide?.soft_pending || []).length;
  const podeAtivarFinal = lockMut ? false : (guide
    ? (!!guide.ready_to_activate && !gerando)
    : (!lockMut && !gerando));
  const nextHint = guide?.next_step
    ? `${guide.next_step.title}${guide.next_step.where ? " → " + guide.next_step.where : ""}`
    : "";
  const ativarTitle = lockMut
    ? (c.somente_acompanhamento
      ? "só acompanhamento no remoto — altere no Mac"
      : "importe como local antes de alterar")
    : gerando ? "aguarde a geração terminar"
    : !podeAtivarFinal ? (nextHint || "conclua remetente e contatos válidos")
    : softN ? (`pode ativar com ${softN} ressalva(s) — confirme no guia ou no botão`)
    : "";
  const banner = gerando ? renderBuildPanel(c.build, c.id)
    : erroBuild ? `<div class="card card-error">
      <b>⚠️ A geração falhou:</b> ${esc(c.build.erro || "erro desconhecido")}
      ${["cota", "conector"].includes(c.build.erro_tipo)
        ? `<div style="margin-top:8px"><small class="muted">${c.build.erro_tipo === "conector" ? "O login do Claude foi ajustado para usar os connectors; clique para tentar novamente." : "A geração parou por limite de uso da IA (cota)."} Os ${c.build.feitos || 0} e-mail(s) já prontos foram salvos — clique para continuar de onde parou.</small><br>
           <button class="green" style="margin-top:8px" ${lockMut ? campMutDisabled(c) : ""} onclick="campRetomar(${c.id})">🔄 Retomar criação</button></div>`
        : `<div style="margin-top:6px"><small class="muted">Os e-mails já gerados ficaram salvos. Você pode excluir e tentar de novo.</small></div>`}</div>`
    : "";
  const versaoChip = `<span class="versao-chip" title="Versão sobe a cada reescrita dos e-mails">v${c.versao || 1}${c.atualizada_em ? ` · atualizada ${fmtDt(c.atualizada_em)}` : ""}</span>
    <span class="versao-chip" title="Compatível = mesma versão da campanha e template aprovado">${c.templates_compativeis || 0}/${c.templates_total || c.steps.length} templates compatíveis</span>`;
  const pendTpl = (c.steps || []).filter(s => s.status_template === "pendente").length;
  const rewriteNote = !gerando && !erroBuild && pendTpl > 0 && (c.versao || 1) > 1
    ? `<div class="card card-info" style="margin-bottom:10px">
        <b>v${c.versao || 1} — ${pendTpl} e-mail(s) novo(s) pendente(s) de aprovação</b>
        <div class="muted" style="margin-top:4px">Resumo e Planejamento foram atualizados com as diretrizes da reescrita.
          Abra o Preview, revise e aprove os templates antes de ativar.</div>
      </div>` : "";
  const exportBtns = c.steps.length ? `
      <button onclick="baixarCampanha(${c.id},'md')">⬇ .md</button>
      <button onclick="baixarCampanha(${c.id},'txt')">⬇ .txt</button>
      ${c.export_docx !== false ? `<button onclick="baixarCampanha(${c.id},'docx')">⬇ Word</button>` : ""}` : "";
  const hist = (c.rewrite_log || []).length ? `<details style="margin-top:10px"><summary style="cursor:pointer"><b>Histórico de versões</b></summary>
      <ul style="margin:8px 0 0;padding-left:18px">${c.rewrite_log.map(r =>
        `<li><b>v${r.versao}</b> · ${fmtDt(r.em)} · ${esc(r.origem || "")}
         ${r.diretrizes ? `<div class="muted">${fmtProsa(r.diretrizes)}</div>` : ""}</li>`).join("")}</ul></details>` : "";
  // toast único ao terminar reescrita (poll de build)
  const prevGerando = window._campBuildGerando === id;
  window._campBuildGerando = gerando ? id : null;
  if (prevGerando && !gerando && c.build && c.build.status === "pronto") {
    const etapa = String(c.build.etapa || "").toLowerCase();
    if (etapa.includes("reescrita") || (c.rewrite_log || []).some(r => r.origem === "rewrite")) {
      const key = `rw-done-${id}-v${c.versao || 1}`;
      if (window._rwDoneToast !== key) {
        window._rwDoneToast = key;
        toast("Resumo + e-mails atualizados (pendentes de aprovação) ✔", 6500);
      }
    }
  }
  const mutLockAttr = lockMut ? campMutDisabled(c) : "";
  const pauseOrActivate = lockMut
    ? (c.status === "ativa"
      ? `<button class="warn" ${mutLockAttr}>⏸ Pausar campanha</button>`
      : `<button class="green" ${mutLockAttr}>▶ ${c.status === "pausada" ? "Retomar campanha" : "Ativar campanha"}</button>`)
    : (c.status === "ativa"
      ? `<button class="warn" onclick="campAction(${c.id},'pause')">⏸ Pausar campanha</button>`
      : `<button class="green" ${podeAtivarFinal ? "" : `disabled title='${esc(ativarTitle || (guide?.next_step?.action || "conclua o passo a passo"))}'`}
             onclick="campAtivar(${c.id},${c.total_contatos})">▶ ${c.status === "pausada" ? "Retomar campanha" : "Ativar campanha"}</button>`);
  const dispararDisabled = lockMut || ((!podeAtivarFinal && c.status !== "ativa") || gerando);
  const dispararTitle = lockMut ? ativarTitle
    : (c.status === "ativa" ? "" : (ativarTitle || "conclua o passo a passo antes de disparar")) || "aguarde a geração";
  const editAttr = lockMut ? mutLockAttr
    : (c.status === "ativa" ? "disabled title='Pause a campanha antes de editar'" : "");
  const rewriteAttr = lockMut ? mutLockAttr
    : (gerando || c.status === "ativa" || !c.steps.length
      ? `disabled title='${gerando ? "aguarde a geração terminar" : c.status === "ativa" ? "pause a campanha antes de reescrever" : "a campanha ainda não tem e-mails"}'`
      : "");
  const testeAttr = lockMut ? mutLockAttr : (c.steps.length ? "" : "disabled");
  const aprovarBtn = (s) => {
    if (s.status_template === "aprovado") return "";
    if (lockMut) return `<button class="green" ${mutLockAttr}>✓ Aprovar</button>`;
    return `<button class="green" onclick="aprovarTpl('${s.template_ref}')">✓ Aprovar</button>`;
  };
  view.innerHTML = `
    <div class="toolbar">
      <a href="#/campanhas">← Campanhas</a>
      <h2 style="margin:0">${esc(c.nome)}</h2> ${badge(c.status)} ${badge(c.tipo, "pendente")} ${origemBadge(c)} ${versaoChip}
      <span class="sp"></span>
      ${pauseOrActivate}
      <button class="primary" ${dispararDisabled ? `disabled title='${esc(dispararTitle)}'` : ""}
        onclick="campDispararAgora(${c.id},${c.total_contatos})"
        title="Ativa (se preciso) e envia a 1ª etapa agora — ignora horário agendado e janela comercial">⚡ Disparar agora</button>
      <button onclick="editarCampanha(${c.id})" ${editAttr}>✏️ Editar</button>
      <button onclick="dlgReescrever(${c.id})" ${rewriteAttr}>🔁 Reescrever e-mails</button>
      <button onclick="campTeste(${c.id})" ${testeAttr}>📨 Enviar teste</button>
      ${exportBtns}
      <button type="button" class="warn" onclick="campExcluir(${c.id})" aria-label="Excluir campanha" ${mutLockAttr}>Excluir</button>
    </div>
    ${campOrigemBanner(c)}
    ${banner}
    ${rewriteNote}
    ${renderActivationGuide(guide)}
    <details class="card camp-section" open>
      <summary class="camp-section__sum">Resumo da campanha</summary>
      <div class="camp-section__body">
        ${c.sender ? `<div style="margin-bottom:8px"><span class="tag">✉️ ${esc(c.sender.email)}</span> <small class="muted">remetente · ${esc(c.sender.proposito || "")}</small></div>` : ""}
        ${c.inicio_em ? `<div style="margin-bottom:8px">📅 <b>1º envio:</b> ${esc(c.inicio_em)} <small class="muted">(próximos pela cadência)</small></div>` : ""}
        ${renderCampResumo(c.objetivo)}
      </div>
    </details>
    <details class="card camp-section">
      <summary class="camp-section__sum">Planejamento / contexto</summary>
      <div class="camp-section__body">${fmtRichProsa(c.contexto)}${hist}</div>
    </details>
    <details class="card camp-section">
      <summary class="camp-section__sum">Por que lançar (briefing)</summary>
      <div class="camp-section__body" id="briefing-card">
        <span class="muted">${gerando ? "será escrito automaticamente ao fim da geração…" : "carregando…"}</span>
      </div>
    </details>
    ${(() => { const m = c.metricas || {};
      const card = (rot, n, pct, cor, clickKey) => {
        const clickable = !!clickKey;
        const attrs = clickable
          ? `role="button" tabindex="0" class="metric-card metric-card--click" data-metric="${clickKey}" title="Clique para ver detalhes"`
          : `class="metric-card"`;
        return `<div ${attrs} style="background:#22093a;border-radius:10px;padding:12px 18px;min-width:120px;text-align:center${clickable ? ";cursor:pointer;outline:1px solid rgba(80,217,251,.25)" : ""}">
            <div style="font-family:Montserrat,Arial,sans-serif;font-size:24px;font-weight:800;color:${cor}">${n}</div>
            <div style="font-size:12px;color:#a3bac6">${rot}${pct ? " · " + pct : ""}${clickable ? " · ver" : ""}</div>
          </div>`;
      };
      return `
    <div class="card" id="camp-metricas">
      <h3 style="margin:0 0 10px">📊 Métricas da campanha</h3>
      <div class="row" style="gap:10px;flex-wrap:wrap">
        ${card("Enviados", m.enviados || 0, "", "#50d9fb")}
        ${card("Aberturas", m.abertos || 0, (m.taxa_abertura || 0) + "%", "#50d9fb")}
        ${card("Cliques", m.cliques || 0, (m.taxa_clique || 0) + "%", "#50d9fb")}
        ${card("Respostas", m.respostas || 0, (m.taxa_resposta || 0) + "%", "#29c287", "replies")}
        ${card("Agendamentos", m.agendados || 0, (m.taxa_agendamento || 0) + "%", "#29c287")}
        ${card("Descadastros", m.descadastros || 0, (m.taxa_descadastro || 0) + "%", "#50d9fb")}
        ${card("E-mails inválidos", m.invalidos || 0, "bounce", "#ff6b6b", "invalidos")}
      </div>
      <div id="camp-metric-panel" style="display:none;margin-top:12px"></div>
      <small class="muted" style="display:block;margin-top:8px">Aberturas/cliques vêm do rastreio (dedup por contato; aberturas de máquina descontadas). Clique em <b>Respostas</b> ou <b>E-mails inválidos</b> para ver a lista. Inválidos = hard bounce. Descadastros contam a partir dos próximos envios.</small>
    </div>`; })()}
    <div class="row">
      <div class="card grow" id="camp-sequencia">
        <h3>Sequência (${c.steps.length} emails)</h3>
        <table><tr><th>#</th><th>Assunto</th><th>Cadência</th><th>Compatibilidade</th><th></th></tr>
        ${c.steps.map(s => `<tr>
          <td>E${s.ordem}</td>
          <td><b>${esc(s.nome)}</b><br><small class="muted">${esc(s.assunto)}</small>
            ${s.versao_template != null ? `<br><small class="muted">template v${s.versao_template}</small>` : ""}</td>
          <td>${s.offset_dias != null ? "D+" + s.offset_dias : `sorteio ${c.cad_min_dias}–${c.cad_max_dias}d`}</td>
          <td>${compatBadge(s.compat_status)} ${badge(s.status_template)}</td>
          <td><button onclick="previewTpl('${s.template_ref}','${esc(c.contatos[0]?.email || "")}','${c.id}',${s.ordem})">👁 Preview / editar</button>
            ${aprovarBtn(s)}</td>
        </tr>`).join("")}</table>
        <div id="tpl-preview-box" style="margin-top:14px"></div>
      </div>
      <div class="card" style="width:330px">
        <h3>Funil do disparo</h3>
        <div class="funnel">${(c.funil_disparo || []).map(x => frow(x.etapa, x.n, c.total_contatos || 1)).join("")}</div>
        <h3 style="margin-top:16px">Progresso da sequência</h3>
        <div class="funnel">${Object.entries(c.contatos_por_etapa).map(([et, n]) =>
          frow(et === "0" ? "aguardando 1º toque" : "etapa " + et + " enviada", n, c.total_contatos)).join("")}</div>
        <h3 style="margin-top:16px">Próximos envios</h3>
        ${c.proximos_envios.length ? c.proximos_envios.map(p =>
          `<div><b>${envioLabel(p.next_send_at)}</b> · etapa ${p.proxima_etapa} → ${esc(p.email)}</div>`).join("")
            + `<div style="margin-top:6px">${CICLO_NOTA}</div>`
          : '<span class="muted">nenhum agendado (campanha em rascunho/pausada)</span>'}
      </div>
    </div>
    <div class="card" id="camp-contatos">
      <h3>Contatos da campanha (${c.contatos.length})</h3>
      <table><tr><th>Empresa</th><th>Contato</th><th>Segmento</th><th>Etapa</th><th>Status</th><th>Próximo envio</th></tr>
      ${c.contatos.map(x => `<tr class="click" onclick="location.hash='#/contato/${encodeURIComponent(x.email)}'">
        <td><b>${esc(x.empresa)}</b></td><td>${esc(x.nome)}<br><small class="muted">${esc(x.email)}</small></td>
        <td>${badge(x.segmento)}</td><td>${x.etapa_atual}/${c.steps.length}</td>
        <td>${badge(x.status)}</td><td>${x.status === "ativo" ? envioLabel(x.next_send_at) : "—"}</td></tr>`).join("")}</table>
    </div>
    <details class="card camp-section" open id="melhorias-section">
      <summary class="camp-section__sum">Melhorias com agentes</summary>
      <div class="camp-section__body" id="melhorias-card">
        <span class="muted">carregando sugestões…</span>
      </div>
    </details>
    <details class="card camp-section">
      <summary class="camp-section__sum">Google Ads (espelho da campanha)</summary>
      <div class="camp-section__body" id="ads-briefing-card">
        <span class="muted">carregando…</span>
      </div>
    </details>`;
  carregarAdsBriefing(c.id);
  if (!gerando) carregarBriefing(c.id, c);
  if (!gerando) carregarMelhorias(c.id, c);
  wireActivationGuide(id);
  wireCampMetricas(id);
  scheduleMermaidRender(view);
  // Restaura preview só quando um save/ação pediu (evita abrir sozinho ao entrar na página).
  if (window._tplPreviewRestore) {
    const st = window._tplPreviewState || {};
    window._tplPreviewRestore = false;
    if (st.ref && !document.getElementById("ed-save")) {
      await previewTpl(st.ref, st.email || "", st.campaignId || id, st.ordem || 0);
    }
  }
  // enquanto a IA estiver gerando os e-mails, atualiza a tela sozinha
  if (gerando) {
    const myhash = location.hash;
    setTimeout(() => {
      if (location.hash !== myhash) return;
      // Não destruir o editor manual a cada 3s — o operador perderia alterações.
      if (document.getElementById("ed-save") || document.getElementById("ed-assunto")) return;
      vCampanha(id);
    }, 3000);
  } else if (window._melhoriasPoll) {
    clearInterval(window._melhoriasPoll);
    window._melhoriasPoll = null;
  }
}

async function baixarCampanha(id, fmt) {
  try {
    const resp = await fetch(`/api/campaigns/${id}/export?fmt=${encodeURIComponent(fmt)}`,
      {headers: {"X-API-Key": window.API_KEY}});
    if (!resp.ok) {
      let msg = "falha no download";
      try { msg = (await resp.json()).erro || msg; } catch (_) {}
      return toast(msg, 6000);
    }
    const blob = await resp.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = (resp.headers.get("Content-Disposition") || "").match(/filename="(.+)"/)?.[1]
      || `campanha-${id}.${fmt}`;
    a.click();
    URL.revokeObjectURL(a.href);
    toast(`Download ${fmt.toUpperCase()} pronto ✔`, 2500);
  } catch (e) { toast("Download falhou: " + e.message, 6000); }
}


/* ---------- melhorias com agentes ---------- */
const IMP_STATUS = {
  pending: "pendente", queued: "na fila", running: "em execução",
  awaiting_approval: "aguardando aprovação", approved: "aprovada",
  applied: "aplicada", rejected: "rejeitada", failed: "falhou", skipped: "pulada",
  cancelled: "cancelado",
};

function _impBusyStatuses() {
  return ["queued", "planning", "running", "applying", "regenerating_templates", "awaiting_approval"];
}

function _impFingerprint(active) {
  if (!active) return "none";
  const tasks = active.tasks || [];
  return [
    active.id, active.status, active.error || "",
    ...tasks.map(t => [
      t.id, t.status, t.iteration || 0,
      t.has_diff ? 1 : 0,
      (t.rejection_reason || "").slice(0, 80),
      t.approved_by || "",
    ].join(":")),
  ].join("|");
}

function _impStructuralKey(active) {
  /** Muda só quando a UI precisa remount (diff/aprovar/terminal), não a cada iter. */
  if (!active) return "none";
  const tasks = active.tasks || [];
  return [
    active.id, active.status,
    ...tasks.map(t => [
      t.id, t.status,
      t.has_diff ? 1 : 0,
      t.status === "running" ? "r" : "",
      (t.status === "failed" || t.status === "skipped") ? (t.rejection_reason || "").slice(0, 40) : "",
    ].join(":")),
  ].join("|");
}

function _impIsInterruptMsg(msg) {
  return /interromp|reload|restart|travado|worker|órfã|orfã|cancelad/i.test(msg || "");
}

function _impSchedulePoll(cid, camp) {
  if (window._melhoriasPoll) clearInterval(window._melhoriasPoll);
  window._melhoriasPoll = setInterval(() => {
    if (location.hash !== "#/campanha/" + cid) {
      clearInterval(window._melhoriasPoll); window._melhoriasPoll = null; return;
    }
    carregarMelhorias(cid, camp || window._campanhaAtual, {light: true});
  }, 3000);
}

function _impUpdateLiveChrome(cid, active, camp) {
  const box = document.getElementById("melhorias-card");
  if (!box || !active) return;
  const host = box.querySelector("[data-imp-progress]");
  if (host) {
    const tmp = document.createElement("div");
    tmp.innerHTML = renderMelhoriasProgress(active);
    const next = tmp.firstElementChild;
    if (next) host.replaceWith(next);
  }
  const tasks = active.tasks || [];
  const campAtiva = camp && camp.status === "ativa";
  box.querySelectorAll(".imp-sug[data-sid]").forEach(li => {
    const sid = li.dataset.sid;
    const task = tasks.find(t => t.source_suggestion_id === sid);
    const badgeEl = li.querySelector(".imp-sug__head .badge, .imp-sug__head span.badge");
    // badge() renders a span — update text via data-imp-badge
    const stEl = li.querySelector("[data-imp-badge]");
    if (stEl) {
      const st = task ? (IMP_STATUS[task.status] || task.status) : "pendente";
      stEl.textContent = st;
      stEl.className = "badge " + (
        task && task.status === "applied" ? "valido"
          : task && ["failed", "rejected", "cancelled"].includes(task.status) ? "invalido"
            : task && task.status === "awaiting_approval" ? "duvidoso"
              : "pendente");
    }
    const btn = li.querySelector(".imp-one");
    const isRunning = task && ["running", "queued", "pending"].includes(task.status);
    if (btn) {
      btn.disabled = !!(campAtiva || isRunning || window._impStartBusy);
      btn.title = isRunning ? "aguarde o agente terminar"
        : (active ? "Cancela o run atual e resolve só esta" : "Resolver esta sugestão");
    }
    let runBar = li.querySelector("[data-imp-running]");
    if (isRunning) {
      if (!runBar) {
        runBar = document.createElement("div");
        runBar.setAttribute("data-imp-running", "1");
        runBar.className = "imp-now";
        const head = li.querySelector(".imp-explain");
        (head || li).after(runBar);
      }
      runBar.innerHTML = `<span class="spinner" style="width:14px;height:14px;display:inline-block;vertical-align:middle"></span>
        <b style="margin-left:6px">Gerando alteração…</b>
        <span class="muted"> iter ${task.iteration || 1} · isso leva 1–3 min. O diff aparece só quando terminar.</span>`;
    } else if (runBar) {
      runBar.remove();
    }
  });
}

async function carregarMelhorias(cid, camp, opts) {
  const light = !!(opts && opts.light);
  const box = document.getElementById("melhorias-card");
  if (!box) return;
  try {
    const [sug, runs] = await Promise.all([
      api(`/api/campaigns/${cid}/improvements/suggestions`),
      api(`/api/campaigns/${cid}/improvements/runs`),
    ]);
    if (Array.isArray(runs.rescued) && runs.rescued.length) {
      const key = "imp-rescued-" + runs.rescued.join(",");
      if (window._impRescuedToast !== key) {
        window._impRescuedToast = key;
        toast("Run de melhorias interrompido (travado) — saiu do spinner. Clique Resolver esta de novo ou Marcar como feito.", 7000);
      }
    }
    let active = runs.active;
    if (active && active.id) {
      active = await api(`/api/campaigns/${cid}/improvements/runs/${active.id}`);
    }
    // Run terminal recente (cancelled/failed) ainda útil na UI
    if (!active && (runs.runs || []).length) {
      const last = runs.runs[0];
      if (last && ["cancelled", "failed", "completed", "awaiting_approval"].includes(last.status)) {
        active = await api(`/api/campaigns/${cid}/improvements/runs/${last.id}`).catch(() => last);
      }
    }

    const structKey = _impStructuralKey(active);
    const canLight = light && box.querySelector(".imp-list")
      && window._melhoriasCid === cid
      && window._melhoriasStructKey === structKey;

    if (canLight) {
      _impUpdateLiveChrome(cid, active, camp || window._campanhaAtual);
      window._melhoriasActive = active;
      window._melhoriasFp = _impFingerprint(active);
    } else {
      const versions = light && window._melhoriasVersions
        ? {versions: window._melhoriasVersions}
        : await api(`/api/campaigns/${cid}/versions`).catch(() => ({versions: []}));
      window._melhoriasVersions = versions.versions || [];
      box.innerHTML = renderMelhoriasPanel(
        cid, sug, active, runs.runs || [], window._melhoriasVersions, camp);
      wireMelhorias(cid, active);
      window._melhoriasCid = cid;
      window._melhoriasActive = active;
      window._melhoriasStructKey = structKey;
      window._melhoriasFp = _impFingerprint(active);
    }

    const busy = active && _impBusyStatuses().includes(active.status);
    const anyRunning = active && (active.tasks || []).some(t =>
      ["running", "queued", "pending", "planning"].includes(t.status));
    if (busy || anyRunning) {
      _impSchedulePoll(cid, camp);
    } else if (window._melhoriasPoll) {
      clearInterval(window._melhoriasPoll); window._melhoriasPoll = null;
    }
  } catch (e) {
    if (!light) box.innerHTML = `<p class="form-feedback error">${esc(e.message)}</p>`;
  }
}

function impHumanAction(taskOrSug) {
  return (taskOrSug && taskOrSug.human_action) || {goto: "briefing", label: "Ir para briefing", hint: "", can_convert_copy: false};
}

function impGoto(cid, goto) {
  const camp = window._campanhaAtual;
  const openSection = (sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    if (el.tagName === "DETAILS") el.open = true;
    el.scrollIntoView({behavior: "smooth", block: "start"});
    return el;
  };
  switch (goto) {
    case "edit_objetivo":
      if (typeof editarCampanha === "function") editarCampanha(cid);
      else openSection(".camp-section");
      break;
    case "briefing":
      openSection("#briefing-card") || openSection("details.camp-section");
      const br = document.getElementById("briefing-card");
      if (br) {
        const host = br.closest("details");
        if (host) host.open = true;
        br.scrollIntoView({behavior: "smooth", block: "start"});
      }
      break;
    case "approve_templates":
    case "preview":
    case "cta_url": {
      openSection("#camp-sequencia");
      const steps = (camp && camp.steps) || [];
      const s0 = steps[0];
      if (s0 && typeof previewTpl === "function") {
        const email = (camp.contatos && camp.contatos[0] && camp.contatos[0].email) || "";
        previewTpl(s0.template_ref, email, String(cid), s0.ordem);
        if (goto === "cta_url") toast("Edite o CTA / URL no preview (inclua UTM)", 4500);
        else if (goto === "approve_templates") toast("Aprove os templates pendentes na sequência", 4000);
      } else {
        toast("Abra Preview / editar na sequência abaixo", 3500);
      }
      break;
    }
    case "contatos":
      openSection("#camp-contatos");
      break;
    default:
      openSection("#melhorias-section");
  }
}

function renderMelhoriasProgress(active) {
  if (!active) return "";
  const totals = active.totals || {};
  const tasks = active.tasks || [];
  const runningTask = tasks.find(t => t.status === "running");
  const awaiting = tasks.filter(t => t.status === "awaiting_approval");
  const skipped = tasks.filter(t => t.status === "skipped");
  const failed = tasks.filter(t => t.status === "failed");
  const applied = tasks.filter(t => t.status === "applied");
  const queuedN = tasks.filter(t => ["queued", "pending", "running"].includes(t.status)).length;
  const stLabel = IMP_STATUS[active.status] || active.status;
  const interrupt = _impIsInterruptMsg(active.error)
    || tasks.some(t => _impIsInterruptMsg(t.rejection_reason));
  const terminalBad = ["cancelled", "failed"].includes(active.status) || interrupt;
  return `<div class="imp-progress" data-imp-progress="1">
      <div><b>Run #${active.id}</b> · ${esc(stLabel)} · modo ${esc(active.mode || "review")}
        ${active.error ? `<div class="form-feedback error" style="margin-top:6px">${esc(active.error)}</div>` : ""}
      </div>
      ${terminalBad && !runningTask ? `<div class="form-feedback error" style="margin-top:8px">
        Interrompido — clique <b>Resolver esta</b> de novo ou <b>Marcar como feito</b>.
      </div>` : ""}
      <div class="muted" style="margin-top:4px">
        ${applied.length} aplicadas · ${awaiting.length} aguardando aprovação ·
        ${skipped.length} puladas (humano) · ${failed.length} falhas ·
        ${queuedN} em fila/execução
        ${totals.cost_brl != null ? " · " + Number(totals.cost_brl).toFixed(2) + " BRL" : ""}
        ${totals.tokens ? " · " + totals.tokens + " tok" : ""}
      </div>
      ${runningTask ? `<div class="imp-now"><span class="spinner" style="width:14px;height:14px;display:inline-block;vertical-align:middle"></span>
        <b style="margin-left:6px">Agora:</b> ${esc(runningTask.title || ("task #" + runningTask.id))}
        ${runningTask.iteration ? ` <span class="muted">(iter ${runningTask.iteration})</span>` : ""}
      </div>` : ""}
      ${awaiting.length ? `<div class="imp-now imp-now--approve"><b>${awaiting.length} patch(es) prontos para aprovar</b> — botões verdes abaixo.</div>` : ""}
      ${applied.length && !runningTask && !awaiting.length ? `<div class="imp-now" style="border-color:#29c287">
        <b>Alteração aplicada</b> — ${applied.length} item(ns) neste run.
      </div>` : ""}
      ${skipped.length ? `<div class="muted" style="margin-top:6px"><b>Por que nem todas foram resolvidas:</b> ${skipped.length} exigem ação humana
        (oferta, compliance, CRM, amostra/estatística, etc.) — veja o motivo em cada item.</div>` : ""}
    </div>`;
}

function renderMelhoriasPanel(cid, sugPack, active, runs, versions, camp) {
  const enabled = sugPack.enabled !== false;
  const suggestions = sugPack.suggestions || [];
  const tasks = (active && active.tasks) || [];
  const campAtiva = camp && camp.status === "ativa";
  const progress = renderMelhoriasProgress(active);

  const sugRows = suggestions.map(s => {
    const task = tasks.find(t => t.source_suggestion_id === s.id);
    const st = task ? (IMP_STATUS[task.status] || task.status) : "pendente";
    const badgeCls = task && ["applied"].includes(task.status) ? "valido"
      : task && ["failed", "rejected", "cancelled"].includes(task.status) ? "invalido"
      : task && task.status === "awaiting_approval" ? "duvidoso"
      : "pendente";
    const interruptTask = task && ["skipped", "failed"].includes(task.status)
      && _impIsInterruptMsg(task.rejection_reason);
    const why = task && (task.rejection_reason || "")
      ? `<div class="imp-why${interruptTask ? " imp-why--interrupt" : ""}"><b>Motivo:</b> ${esc(task.rejection_reason)}</div>`
      : "";
    const interruptBar = interruptTask ? `
      <div class="form-feedback error">Interrompido — clique Resolver esta de novo ou Marcar como feito.</div>` : "";
    const rationale = esc((task && task.rationale) || s.rationale || "").slice(0, 600);
    const ha = impHumanAction(task || s);
    const humanDone = task && task.status === "applied" && task.approved_by === "human";
    const appliedNote = task && task.status === "applied" && !humanDone
      ? `<div class="imp-why imp-why--done"><b>Alteração aplicada</b></div>` : "";
    const humanBar = (task && ["skipped", "rejected", "failed"].includes(task.status)) || (!task && ha.goto) ? `
      <div class="toolbar imp-human-bar">
        <button type="button" class="imp-goto" data-goto="${esc(ha.goto)}" title="${esc(ha.hint || "")}">${esc(ha.label || "Ir para…")}</button>
        ${task && ["skipped", "rejected", "failed"].includes(task.status) ? `
          <button type="button" class="green imp-human-done" data-tid="${task.id}">✓ Marcar como feito</button>
          ${ha.can_convert_copy ? `<button type="button" class="imp-convert-copy" data-tid="${task.id}">Converter em patch de copy/resumo</button>` : ""}
        ` : `<span class="muted">Faça a ação e, após o run, marque como feito</span>`}
      </div>
      ${ha.hint ? `<div class="muted imp-hint">${esc(ha.hint)}</div>` : ""}` : "";
    const humanDoneNote = humanDone ? `<div class="imp-why imp-why--done"><b>Feito (humano):</b> ${esc(task.human_feedback || "ok")}</div>` : "";
    const hasDiff = !!(task && (task.has_diff || (task.proposed_patch && Array.isArray(task.proposed_patch.changes) && task.proposed_patch.changes.length)));
    const isRunning = task && ["running", "queued", "pending"].includes(task.status);
    const runningBar = isRunning ? `
      <div class="imp-now" data-imp-running="1">
        <span class="spinner" style="width:14px;height:14px;display:inline-block;vertical-align:middle"></span>
        <b style="margin-left:6px">Gerando alteração…</b>
        <span class="muted"> iter ${task.iteration || 1} · isso leva 1–3 min. O diff aparece só quando terminar.</span>
      </div>` : "";
    const patchIssues = (task && Array.isArray(task.patch_issues)) ? task.patch_issues : [];
    const staleWarn = task && task.status === "awaiting_approval" && patchIssues.length ? `
      <div class="form-feedback error">Patch desatualizado ou inválido: ${esc(patchIssues.slice(0,3).join(" · "))}
        — use <b>Reexecutar</b> antes de aprovar.</div>` : "";
    const approveBar = task && task.status === "awaiting_approval" && hasDiff ? `
      ${staleWarn}
      <div class="toolbar imp-approve-bar">
        <button type="button" class="green imp-approve" data-tid="${task.id}" ${patchIssues.length ? "disabled" : ""}>✓ Aprovar e aplicar esta alteração</button>
        <button type="button" class="warn imp-reject" data-tid="${task.id}">Rejeitar</button>
        <button type="button" class="imp-retry" data-tid="${task.id}">Reexecutar</button>
      </div>` : (task && task.status === "awaiting_approval" && !hasDiff ? `
      <div class="form-feedback error">Patch vazio — use Reexecutar ou Converter em patch.</div>
      <div class="toolbar"><button type="button" class="imp-retry" data-tid="${task.id}">Reexecutar</button>
        ${ha.can_convert_copy ? `<button type="button" class="imp-convert-copy" data-tid="${task.id}">Converter em patch</button>` : ""}</div>` : "");
    const failBar = task && task.status === "failed" ? `
      <div class="toolbar"><button type="button" class="imp-retry" data-tid="${task.id}">Retry</button>
        ${ha.can_convert_copy ? `<button type="button" class="imp-convert-copy" data-tid="${task.id}">Converter em patch</button>` : ""}
        <button type="button" class="green imp-human-done" data-tid="${task.id}">✓ Marcar como feito</button>
      </div>` : "";
    const patchBlock = hasDiff ? `
      <details class="imp-task-detail" open>
        <summary>Ver o que será alterado (antes → depois)</summary>
        ${renderImpDiff(task)}
      </details>` : (task && !isRunning ? `
      <details class="imp-task-detail">
        <summary>Detalhe técnico da task #${task.id}</summary>
        <div class="muted">${task.assigned_agent_key ? "agente: " + esc(task.assigned_agent_key) + " · " : ""}status ${esc(task.status)}
          ${!hasDiff ? " · ainda sem diff de alteração" : ""}</div>
      </details>` : "");
    const humanOnly = !!(ha && ha.goto && !["preview", "cta_url"].includes(ha.goto)
      && (["oferta","compliance","segmentacao","canal_timing","outro","landing_page"].includes(s.category_hint || task?.category || "")
          || (ha.can_convert_copy === false && ha.goto === "briefing")
          || /60 dias|aprova[cç][aã]o comercial|documentad|checklist|kb\b/i.test((s.title||"")+" "+(s.rationale||""))));
    const agentCopy = !humanOnly && (["copy","criativo"].includes(s.category_hint || task?.category || "") || ha.can_convert_copy);
    return `<li class="imp-sug" data-sid="${esc(s.id)}">
      <div class="imp-sug__head">
        <label><input type="checkbox" class="imp-check" value="${esc(s.id)}" ${!task ? "checked" : ""}>
          <b>${esc(s.title)}</b></label>
        <span class="badge ${badgeCls}" data-imp-badge="1">${esc(st)}</span>
        ${humanOnly ? `<span class="badge" style="background:#e8f0fe;color:#1a73e8">ação humana</span>` : ""}
        ${task && task.assigned_agent_key && !humanOnly ? `<span class="muted">${esc(task.assigned_agent_key)}</span>` : ""}
        ${humanOnly
          ? `<button type="button" class="imp-human-go" data-goto="${esc(ha.goto || "briefing")}" data-sid="${esc(s.id)}">${esc(ha.label || "Ir fazer agora")}</button>`
          : `<button type="button" class="imp-one" data-sid="${esc(s.id)}"
          ${!enabled || campAtiva || isRunning || window._impStartBusy ? "disabled" : ""}
          title="${isRunning ? "aguarde o agente terminar" : (active ? "Cancela o run atual e resolve só esta" : "Resolver esta sugestão")}">Resolver com agente</button>`}
      </div>
      <div class="imp-explain">${rationale || '<span class="muted">Sem explicação no briefing.</span>'}</div>
      ${why}
      ${interruptBar}
      ${humanDoneNote}
      ${appliedNote}
      ${runningBar}
      ${humanBar}
      ${approveBar}
      ${failBar}
      ${patchBlock}
    </li>`;
  }).join("") || '<p class="muted">Nenhuma sugestão — gere o briefing (riscos / checklist / ajustar).</p>';

  const verRows = (versions || []).slice(0, 8).map(v =>
    `<li>v${v.version_number} · ${fmtDt(v.criado_em)} · ${esc(v.created_by)} · ${esc(v.note || "")}
      <button type="button" class="imp-rollback" data-vid="${v.id}">Reverter</button></li>`
  ).join("");

  return `
    <p class="muted">Agentes geram um <b>diff</b> (antes→depois). Só então aparece <b>Aprovar e aplicar esta alteração</b>.
      Isso é diferente de <b>Aprovar templates</b> no briefing (carimbo para poder Ativar a campanha).
      Itens operacionais ficam pulados — use Ir para… / Marcar como feito.</p>
    ${!enabled ? `<p class="form-feedback error">Kill switch: IMPROVEMENTS_ENABLED desligado.</p>` : ""}
    ${progress}
    <div class="toolbar" style="margin-bottom:10px;flex-wrap:wrap;gap:8px">
      <button type="button" class="primary" id="imp-start-review" ${!enabled || !suggestions.length || campAtiva ? "disabled" : ""}>Resolver melhorias com agentes</button>
      <button type="button" id="imp-start-auto" ${!enabled || !suggestions.length || campAtiva ? "disabled" : ""}>Aplicar automaticamente</button>
      <button type="button" id="imp-start-sel" ${!enabled || !suggestions.length || campAtiva ? "disabled" : ""}>Só selecionadas</button>
      ${active && !["completed", "cancelled", "failed"].includes(active.status)
        ? `<button type="button" class="warn" id="imp-cancel" data-rid="${active.id}">Cancelar run ativo</button>` : ""}
    </div>
    <ul class="imp-list">${sugRows}</ul>
    ${verRows ? `<details style="margin-top:12px" open><summary><b>Versões / rollback</b></summary><ul>${verRows}</ul></details>` : ""}
  `;
}

function renderImpDiff(task) {
  const p = task.proposed_patch || {};
  const changes = p.changes || [];
  if (!changes.length) {
    return `<p class="muted">Nenhuma alteração concreta no patch (sem changes[]). Reexecute a task.</p>`;
  }
  const clip = (s, n = 900) => {
    const t = String(s ?? "");
    return t.length > n ? t.slice(0, n) + "…" : t;
  };
  /** Textos longos: se o 1º diff está além do clip, mostra o trecho alterado (não o prefixo idêntico). */
  const focusedPair = (from, to) => {
    const a = String(from ?? ""), b = String(to ?? "");
    if (a === b) {
      return {
        fromHtml: esc(clip(a, 700)),
        toHtml: esc(clip(b, 700)),
        note: "from e to são idênticos — esta alteração não muda o texto.",
        noop: true,
      };
    }
    let i = 0;
    const n = Math.min(a.length, b.length);
    while (i < n && a[i] === b[i]) i++;
    if (i < 420) {
      return { fromHtml: esc(clip(a)), toHtml: esc(clip(b)), note: "", noop: false };
    }
    const start = Math.max(0, i - 180);
    const endA = Math.min(a.length, i + 520);
    const endB = Math.min(b.length, i + 520);
    const pref = start > 0 ? `…[char ${start}] ` : "";
    const sufA = endA < a.length ? "…" : "";
    const sufB = endB < b.length ? "…" : "";
    return {
      fromHtml: esc(pref + a.slice(start, endA) + sufA),
      toHtml: esc(pref + b.slice(start, endB) + sufB),
      note: `Diferença a partir do caractere ${i} (texto longo — mostrando o trecho que muda).`,
      noop: false,
    };
  };
  const rows = changes.map(ch => {
    const path = ch.path || "";
    const isObj = path === "objetivo" || path === "contexto";
    const pair = focusedPair(ch.from, ch.to);
    return `<div class="imp-diff ${isObj ? "imp-diff--scalar" : ""}${pair.noop ? " imp-diff--noop" : ""}">
      <div class="imp-diff__path"><b>Campo:</b> ${esc(path)}${isObj ? " (Resumo da campanha)" : ""}</div>
      ${pair.note ? `<div class="form-feedback ${pair.noop ? "error" : ""}" style="margin:6px 0">${esc(pair.note)}</div>` : ""}
      <div class="imp-diff__from"><b>Antes</b><pre class="imp-diff__pre">${pair.fromHtml}</pre></div>
      <div class="imp-diff__to"><b>Depois</b><pre class="imp-diff__pre">${pair.toHtml}</pre></div>
    </div>`;
  }).join("");
  return `
    ${p.expected_effect ? `<div class="imp-effect"><b>O que muda:</b> ${esc(String(p.expected_effect).slice(0, 400))}</div>` : ""}
    ${rows}
    <div class="muted" style="margin-top:6px">confiança ${Number(task.confidence||0).toFixed(2)} · risco ${esc(task.risk_level||"—")}
      ${p.verification ? " · verificação: " + esc(String(p.verification).slice(0, 200)) : ""}</div>`;
}

function wireMelhorias(cid, active) {
  const start = async (mode, ids, {replace = false} = {}) => {
    if (window._impStartBusy) return;
    const sid0 = ids && ids[0];
    if (sid0 && active) {
      const t = (active.tasks || []).find(x => x.source_suggestion_id === sid0);
      if (t && ["running", "queued", "pending"].includes(t.status)) {
        toast("Já está gerando esta sugestão — aguarde ou cancele o run", 4000);
        return;
      }
    }
    window._impStartBusy = true;
    document.querySelectorAll(".imp-one, #imp-start-review, #imp-start-auto, #imp-start-sel").forEach(b => {
      b.disabled = true;
    });
    try {
      const body = {mode, replace: !!replace};
      if (ids) body.suggestion_ids = ids;
      if (active && !replace && !["completed", "cancelled", "failed"].includes(active.status)) {
        if (!confirm("Já existe um run ativo nesta campanha.\n\nOK = cancelar o atual e iniciar este novo.\nCancelar = manter o run atual.")) {
          window._impStartBusy = false;
          carregarMelhorias(cid, window._campanhaAtual);
          return;
        }
        body.replace = true;
      }
      await api(`/api/campaigns/${cid}/improvements/runs`, {method: "POST", json: body});
      toast(body.replace ? "Run anterior cancelado · novo iniciado" : "Run de melhorias iniciado", 3500);
      carregarMelhorias(cid, window._campanhaAtual);
    } catch (e) { toast("Erro: " + e.message, 6000); }
    finally { window._impStartBusy = false; }
  };
  document.querySelectorAll(".imp-human-go").forEach(btn => {
    btn.onclick = () => {
      const goto = btn.getAttribute("data-goto") || "briefing";
      toast("Ação humana — faça o passo indicado e marque no checklist / Marcar como feito", 5000);
      impGoto(cid, goto);
    };
  });
  const selIds = () => [...document.querySelectorAll(".imp-check:checked")].map(x => x.value);
  const btnR = document.getElementById("imp-start-review");
  if (btnR) btnR.onclick = () => start("review");
  const btnA = document.getElementById("imp-start-auto");
  if (btnA) btnA.onclick = () => {
    if (!confirm("Aplicar automaticamente patches de baixa risco/alta confiança?")) return;
    start("auto", null, {replace: !!(active && !["completed","cancelled","failed"].includes(active.status))});
  };
  const btnS = document.getElementById("imp-start-sel");
  if (btnS) btnS.onclick = () => {
    const ids = selIds();
    if (!ids.length) return toast("Selecione ao menos uma sugestão");
    start("selective", ids);
  };
  const btnC = document.getElementById("imp-cancel");
  if (btnC) btnC.onclick = async () => {
    const keep = confirm("Cancelar o run ativo?\n\nOK = cancelar e DESCARTAR patches ainda não aprovados (libera a campanha).\nCancelar = manter como está.");
    if (!keep) return;
    try {
      await api(`/api/improvements/runs/${btnC.dataset.rid}/cancel`, {
        method: "POST", json: {abandon_approvals: true, reason: "cancelado pelo usuário"},
      });
      toast("Run cancelado — campanha liberada para novo run ou aplicar automático", 4500);
      carregarMelhorias(cid, window._campanhaAtual);
    } catch (e) { toast(e.message, 5000); }
  };
  document.querySelectorAll(".imp-one").forEach(btn => {
    btn.onclick = () => start("selective", [btn.dataset.sid], {
      replace: !!(active && !["completed","cancelled","failed"].includes(active.status)),
    });
  });
  document.querySelectorAll(".imp-approve").forEach(btn => {
    btn.onclick = async () => {
      try {
        await api(`/api/improvements/tasks/${btn.dataset.tid}/approve`, {method: "POST", json: {}});
        toast("Patch aplicado ✔");
        carregarMelhorias(cid, window._campanhaAtual);
        vCampanha(cid);
      } catch (e) { toast(e.message, 6000); }
    };
  });
  document.querySelectorAll(".imp-reject").forEach(btn => {
    btn.onclick = async () => {
      const reason = prompt("Motivo da rejeição:") || "";
      try {
        await api(`/api/improvements/tasks/${btn.dataset.tid}/reject`, {method: "POST", json: {reason}});
        toast("Rejeitada");
        carregarMelhorias(cid, window._campanhaAtual);
      } catch (e) { toast(e.message, 6000); }
    };
  });
  document.querySelectorAll(".imp-retry").forEach(btn => {
    btn.onclick = async () => {
      const feedback = prompt("Feedback para reexecutar:") || "";
      try {
        await api(`/api/improvements/tasks/${btn.dataset.tid}/retry`, {method: "POST", json: {feedback}});
        toast("Reenfileirada");
        carregarMelhorias(cid, window._campanhaAtual);
      } catch (e) { toast(e.message, 6000); }
    };
  });
  document.querySelectorAll(".imp-goto").forEach(btn => {
    btn.onclick = () => impGoto(cid, btn.dataset.goto || "briefing");
  });
  document.querySelectorAll(".imp-human-done").forEach(btn => {
    btn.onclick = async () => {
      const note = prompt("Evidência / nota (ex.: aprovado pelo comercial em 22/07):", "") || "";
      try {
        await api(`/api/improvements/tasks/${btn.dataset.tid}/human-done`, {
          method: "POST", json: {reason: note},
        });
        toast("Marcado como feito ✔", 3500);
        carregarMelhorias(cid, window._campanhaAtual);
        carregarBriefing(cid, window._campanhaAtual);
      } catch (e) { toast(e.message, 6000); }
    };
  });
  document.querySelectorAll(".imp-convert-copy").forEach(btn => {
    btn.onclick = async () => {
      if (!confirm("Pedir ao agente um patch de copy/resumo para esta sugestão?")) return;
      try {
        await api(`/api/improvements/tasks/${btn.dataset.tid}/convert-copy`, {
          method: "POST", json: {},
        });
        toast("Agente gerando patch…", 3500);
        carregarMelhorias(cid, window._campanhaAtual);
      } catch (e) { toast(e.message, 6000); }
    };
  });
  document.querySelectorAll(".imp-rollback").forEach(btn => {
    btn.onclick = async () => {
      if (!confirm("Reverter campanha e templates para esta versão?")) return;
      try {
        await api(`/api/campaigns/${cid}/rollback`, {method: "POST", json: {campaign_version_id: Number(btn.dataset.vid)}});
        toast("Rollback aplicado ✔");
        vCampanha(cid);
      } catch (e) { toast(e.message, 6000); }
    };
  });
}

/* ---------- briefing de lançamento (os porquês / go-no-go) ---------- */
const RECOMENDACAO_BRIEFING = {
  lancar: {rot: "✅ Recomendação: LANÇAR", cor: "#29c287"},
  ajustar: {rot: "🛠 Recomendação: AJUSTAR ANTES", cor: "#f2c94c"},
  nao_lancar: {rot: "⛔ Recomendação: NÃO LANÇAR", cor: "#ff6b6b"},
};

async function carregarBriefing(id, camp) {
  const box = document.getElementById("briefing-card");
  if (!box) return;
  try {
    const r = await api(`/api/campaigns/${id}/briefing`);
    box.innerHTML = renderBriefingLancamento(id, r.briefing, camp || window._campanhaAtual);
    scheduleMermaidRender(box);
  } catch (e) {
    box.innerHTML = `<p class="form-feedback error">${esc(e.message)}</p>`;
  }
}

function renderBriefingLancamento(id, b, camp) {
  const cabecalho = `<p class="muted">Os porquês da campanha — separados por assunto. A IA sugere; a decisão final é sua.</p>`;
  if (!b) return `${cabecalho}
    <button type="button" class="primary" onclick="gerarBriefingLancamento(${id})">Gerar briefing</button>
    <span class="muted"> Também é gerado ao fim da criação da campanha.</span>`;
  const rec = RECOMENDACAO_BRIEFING[b.recomendacao] || {rot: "Recomendação: " + (b.recomendacao || "—"), cor: "#a3bac6"};
  const med = b.medicao || {};
  const steps = (camp && camp.steps) || [];
  const porMap = {};
  (b.por_email || []).forEach(e => { porMap[e.ordem] = e; });
  const ordens = [...new Set([
    ...steps.map(s => s.ordem),
    ...(b.por_email || []).map(e => e.ordem),
  ].filter(x => x != null))].sort((a, b2) => a - b2);
  const blocosEmail = ordens.map(ord => {
    const step = steps.find(s => s.ordem === ord) || {};
    const pe = porMap[ord] || {};
    const assunto = step.assunto || pe.assunto_esperado || "—";
    const alts = (step.assuntos_alt || []).length
      ? `<div class="muted" style="margin-top:4px"><small>Alternativos: ${step.assuntos_alt.map(a => esc(a)).join(" · ")}</small></div>` : "";
    return `<div class="email-porque">
      <div class="toolbar" style="margin:0 0 6px">
        <b>E${ord} — ${esc(step.nome || pe.assunto_esperado || "e-mail")}</b>
        ${step.compat_status ? compatBadge(step.compat_status) : ""}
        ${step.status_template ? badge(step.status_template) : ""}
      </div>
      <div><b>Assunto:</b> ${esc(assunto)}</div>${alts}
      <div style="margin-top:8px"><b>Por que este e-mail</b>${fmtRichProsa(pe.porque_este_angulo || "—")}</div>
      <div><b>Prova / origem</b>${fmtRichProsa(pe.prova_e_origem || "—")}</div>
    </div>`;
  }).join("");
  return `${cabecalho}
    <section class="briefing-bloco briefing-bloco--rec" style="border-left-color:${rec.cor}">
      <h4 class="briefing-bloco__title">${esc(rec.rot)}</h4>
      ${fmtRichProsa(b.justificativa || "")}
    </section>
    <section class="briefing-bloco prosa-bloco--resumo">
      <h4 class="briefing-bloco__title">Resumo</h4>
      ${fmtRichProsa(b.resumo || "—")}
    </section>
    <section class="briefing-bloco briefing-bloco--icp">
      <h4 class="briefing-bloco__title">ICP e dor</h4>
      ${fmtRichProsa(b.icp_e_dor || "—")}
    </section>
    <section class="briefing-bloco briefing-bloco--agora">
      <h4 class="briefing-bloco__title">Por que agora</h4>
      ${fmtRichProsa(b.por_que_agora || "—")}
    </section>
    ${blocosEmail ? `<h4 class="briefing-bloco__title" style="margin-top:14px">Por quê de cada assunto</h4>${blocosEmail}` : ""}
    ${(b.riscos || []).length ? `<section class="briefing-bloco briefing-bloco--risco">
      <h4 class="briefing-bloco__title">Riscos</h4>
      <ul class="prosa-list">${b.riscos.map(r => `<li>${fmtRichProsa(r)}</li>`).join("")}</ul>
    </section>` : ""}
    <section class="briefing-bloco prosa-bloco--neutro">
      <h4 class="briefing-bloco__title">Medição</h4>
      <p class="ai-prosa">utm_campaign=<code>${esc(med.utm_campaign || "—")}</code> ·
        GA4 <code>${esc(med.evento_ga4 || "—")}</code>${med.metrica_decisao ? ` · ${esc(med.metrica_decisao)}` : ""}</p>
    </section>
    ${renderChecklistBlock(id, b.checklist)}
    <div class="toolbar"><small class="muted">gerado em ${esc(b.gerado_em || "—")}</small><span class="sp"></span>
      <button type="button" onclick="baixarCampanha(${id},'md')">.md</button>
      <button type="button" onclick="baixarCampanha(${id},'txt')">.txt</button>
      ${(camp && camp.export_docx === false) ? "" : `<button type="button" onclick="baixarCampanha(${id},'docx')">Word</button>`}
      <button type="button" onclick="gerarBriefingLancamento(${id})">Regenerar briefing</button></div>`;
}

const CHECKLIST_STATUS_LABEL = {
  pending: "pendente", done: "ok", failed: "falhou", skipped: "pulado",
};

function renderChecklistBlock(cid, cl) {
  const items = (cl && cl.items) || [];
  if (!items.length) {
    return `<div class="checklist-box" id="checklist-box">
      <div class="toolbar" style="margin-bottom:10px"><b>Antes de ativar</b><span class="sp"></span>
        <button type="button" class="green" onclick="checklistAprovarTemplates(${cid})">Aprovar templates</button>
        <button type="button" class="primary" onclick="checklistRun(${cid})">Rodar checks</button></div>
      <p class="muted checklist-empty">Nada na checklist ainda. Rode os checks ou regenere o briefing.</p></div>`;
  }
  const ready = !!cl.ready;
  const rows = items.map(it => {
    const st = it.status || "pending";
    const done = st === "done" || st === "skipped";
    const idAttr = esc(it.id);
    const evid = (it.evidence || "").trim();
    return `<li class="checklist-item checklist-item--${esc(st)}" data-id="${idAttr}">
      <div class="checklist-item__row">
        <label class="checklist-item__lab">
          <input type="checkbox" ${done ? "checked" : ""}
            ${it.kind === "auto" && st === "done" ? "disabled" : ""}
            aria-label="${esc(it.label)}"
            onchange="checklistToggle(${cid}, '${idAttr}', this.checked)">
          <span class="checklist-item__label">${esc(it.label)}</span>
        </label>
        ${badge(CHECKLIST_STATUS_LABEL[st] || st, st === "done" ? "ativo" : st === "failed" ? "encerrada" : "pendente")}
        <span class="muted checklist-item__kind">${it.kind === "auto" ? "auto" : "manual"}</span>
      </div>
      ${evid ? `<details class="checklist-item__ev"><summary>Ver evidência</summary><div class="muted">${esc(evid)}</div></details>` : ""}
    </li>`;
  }).join("");
  return `<div class="checklist-box" id="checklist-box">
    <div class="toolbar" style="margin-bottom:10px">
      <b>Antes de ativar</b>
      ${ready ? badge("pronto para ativar", "ativo") : badge("incompleto", "pendente")}
      <span class="sp"></span>
      <button type="button" class="green" onclick="checklistAprovarTemplates(${cid})">Aprovar templates</button>
      <button type="button" class="primary" onclick="checklistRun(${cid})">Rodar checks</button>
    </div>
    ${ready ? `<p class="checklist-ready">Pronto para ativar — revise e use Ativar campanha.</p>` : ""}
    <ul class="checklist-list">${rows}</ul>
  </div>`;
}

async function checklistRefresh(cid, cl) {
  const box = document.getElementById("checklist-box");
  if (box) box.outerHTML = renderChecklistBlock(cid, cl);
  else if (window._campanhaAtual) carregarBriefing(cid, window._campanhaAtual);
}

async function checklistRun(cid) {
  try {
    toast("Rodando checks automáticos…", 2000);
    const r = await api(`/api/campaigns/${cid}/checklist/run`, {method: "POST"});
    await checklistRefresh(cid, r.checklist);
    toast(r.checklist && r.checklist.ready ? "Checklist pronto ✔" : "Checks atualizados — ainda há pendências", 4000);
  } catch (e) { toast("Erro nos checks: " + e.message, 6000); }
}

async function checklistAprovarTemplates(cid) {
  if (!confirm("Aprovar TODOS os templates pendentes?\n\nIsso NÃO aplica melhorias de copy — só libera o status 'aprovado' para poder Ativar a campanha.\n\nSe ainda está pedindo alterações nas Melhorias, cancele e revise o preview antes.")) return;
  try {
    const r = await api(`/api/campaigns/${cid}/approve-templates`, {method: "POST"});
    if (r.checklist) await checklistRefresh(cid, r.checklist);
    else await checklistRun(cid);
    toast(r.nota || "Templates aprovados ✔", 4000);
    // atualiza badges da sequência se estamos no detalhe
    if (location.hash.startsWith("#/campanha/")) vCampanha(cid);
  } catch (e) { toast("Erro ao aprovar templates: " + e.message, 6000); }
}

async function checklistToggle(cid, itemId, checked) {
  try {
    const status = checked ? "done" : "pending";
    const evidence = checked ? "marcado manualmente" : "";
    const r = await api(`/api/campaigns/${cid}/checklist/${encodeURIComponent(itemId)}`, {
      method: "PATCH", json: {status, evidence},
    });
    await checklistRefresh(cid, r.checklist);
  } catch (e) { toast("Não foi possível atualizar o item: " + e.message, 5000); carregarBriefing(cid, window._campanhaAtual); }
}

async function gerarBriefingLancamento(id) {
  const box = document.getElementById("briefing-card");
  if (box) box.innerHTML = `<div class="spinner" style="width:18px;height:18px;display:inline-block;vertical-align:middle"></div>
    <span class="muted"> a IA está consolidando os porquês da campanha (~1 min)…</span>`;
  try {
    const r = await api(`/api/campaigns/${id}/briefing`, {method: "POST"});
    if (box) box.innerHTML = renderBriefingLancamento(id, r.briefing, window._campanhaAtual);
  } catch (e) {
    toast("Erro ao gerar briefing: " + e.message, 6000);
    carregarBriefing(id, window._campanhaAtual);
  }
}

async function carregarAdsBriefing(id) {
  const box = document.getElementById("ads-briefing-card");
  if (!box) return;
  try {
    const r = await api(`/api/campaigns/${id}/ads-briefing`);
    box.innerHTML = renderAdsBriefing(id, r);
  } catch (e) {
    box.innerHTML = `<p class="form-feedback error">${esc(e.message)}</p>`;
  }
}

function renderAdsBriefing(id, r) {
  const utm = r.utm || {};
  const utmStr = `utm_source=${utm.utm_source} · utm_medium=${utm.utm_medium} · utm_campaign=${utm.utm_campaign}`;
  const b = r.briefing;
  const cabecalho = `<p class="muted">Os links dos e-mails já saem com <code>${utmStr}</code>. O briefing abaixo espelha a campanha
    para a rede de pesquisa — nada é criado na sua conta: cole no Google Ads ou importe o CSV no Ads Editor.</p>`;
  if (!b) return `${cabecalho}
    <button type="button" class="primary" onclick="gerarAdsBriefing(${id})">Gerar briefing com IA</button>
    <span class="muted"> (também é gerado automaticamente ao ativar a campanha)</span>`;
  return `${cabecalho}
    <div class="toolbar"><b>${esc(b.campaign_name || "")}</b><span class="sp"></span>
      <button onclick="copiarAdsBriefing(${id})">📋 Copiar briefing</button>
      <button class="green" onclick="baixarCsv('/api/campaigns/${id}/ads-briefing.csv')">⬇ CSV (Ads Editor)</button>
      <button onclick="gerarAdsBriefing(${id})">↻ Regenerar</button></div>
    <p><b>Objetivo:</b> ${esc(b.objetivo || "")}<br><b>Público:</b> ${esc(b.publico || "")}
      ${b.orcamento_sugerido ? `<br><b>Orçamento sugerido:</b> ${esc(b.orcamento_sugerido)}` : ""}</p>
    <div class="row">
      ${(b.ad_groups || []).map(g => `<div class="card grow"><b>${esc(g.nome || "Grupo")}</b>
        <p><small class="muted">URL final: ${esc(g.final_url || "")}</small></p>
        <b>Títulos (${(g.headlines || []).length})</b>
        <ul>${(g.headlines || []).map(h => `<li>${esc(h)} <small class="muted">(${h.length})</small></li>`).join("")}</ul>
        <b>Descrições</b>
        <ul>${(g.descriptions || []).map(d => `<li>${esc(d)} <small class="muted">(${d.length})</small></li>`).join("")}</ul></div>`).join("")}
      <div class="card" style="width:280px"><b>Palavras-chave</b>
        <table><tr><th>Keyword</th><th>Match</th></tr>
        ${(b.keywords || []).map(k => `<tr><td>${esc(k.kw)}</td><td>${esc(k.match || "phrase")}</td></tr>`).join("")}</table></div>
    </div>
    <small class="muted">Gerado em ${esc(b.gerado_em || "")}. No Ads, use a mesma nomenclatura (<code>${esc(b.campaign_name || "")}</code>) para casar com o GA4.</small>`;
}

async function gerarAdsBriefing(id) {
  const box = document.getElementById("ads-briefing-card");
  box.innerHTML = `<div class="spinner" style="width:18px;height:18px;display:inline-block"></div> <span class="muted">a IA está redigindo o briefing (pode levar ~1min)…</span>`;
  try {
    await api(`/api/campaigns/${id}/ads-briefing`, {method: "POST"});
    toast("Briefing Google Ads pronto ✔", 3000);
  } catch (e) { toast("Erro: " + e.message, 6000); }
  carregarAdsBriefing(id);
}

async function copiarAdsBriefing(id) {
  const r = await api(`/api/campaigns/${id}/ads-briefing`);
  await navigator.clipboard.writeText(JSON.stringify(r.briefing, null, 2));
  toast("Briefing copiado 📋", 2500);
}

async function baixarCsv(path) {
  const resp = await fetch(path, {headers: {"X-API-Key": window.API_KEY}});
  const blob = await resp.blob();
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = (resp.headers.get("Content-Disposition") || "").match(/filename="(.+)"/)?.[1] || "ads.csv";
  a.click();
  URL.revokeObjectURL(a.href);
}

async function editarCampanha(id) {
  const c = await api(`/api/campaigns/${id}`);
  const dlg = document.getElementById("dlg-editar-campanha");
  document.getElementById("ec-nome").value = c.nome || "";
  const objetivoEl = document.getElementById("ec-objetivo");
  objetivoEl.value = c.objetivo || "";
  const objetivoOriginal = (c.objetivo || "").trim();
  document.getElementById("ec-tipo").value = c.tipo || "finita";
  document.getElementById("ec-min").value = c.cad_min_dias ?? "";
  document.getElementById("ec-max").value = c.cad_max_dias ?? "";
  document.getElementById("ec-inicio").value = (c.inicio_em || "").replace(" ", "T").slice(0, 16);
  const regenEl = document.getElementById("ec-regenerar");
  // Desmarcado por padrao: mudar horario/cadencia/nome nao regenera.
  // Sugere regenerar so quando o operador altera o objetivo.
  if (regenEl) {
    regenEl.checked = false;
    regenEl.dataset.userTouched = "";
    regenEl.onchange = () => { regenEl.dataset.userTouched = "1"; };
  }
  objetivoEl.oninput = () => {
    if (!regenEl || regenEl.dataset.userTouched === "1") return;
    regenEl.checked = objetivoEl.value.trim() !== objetivoOriginal;
  };
  const sd = await api("/api/senders");
  const sender = document.getElementById("ec-sender");
  const currentSender = (sd.senders || []).find(s => s.id === c.sender_id);
  sender.replaceChildren(new Option(currentSender?.email || "Remetente histórico", String(c.sender_id)));
  sender.disabled = true;
  const btn = document.getElementById("ec-salvar");
  btn.disabled = false;
  btn.onclick = async () => {
    const min = document.getElementById("ec-min").value, max = document.getElementById("ec-max").value;
    const objetivo = objetivoEl.value.trim();
    const nome = document.getElementById("ec-nome").value.trim();
    if (!nome || !objetivo) return toast("Nome e objetivo são obrigatórios.");
    const body = {
      nome, objetivo,
      sender_id: +sender.value, tipo: document.getElementById("ec-tipo").value,
      cad_min: min ? +min : null, cad_max: max ? +max : (min ? +min : null),
      inicio_em: document.getElementById("ec-inicio").value || "",
      regenerar: !!(regenEl && regenEl.checked),
    };
    btn.disabled = true;
    try {
      const r = await api(`/api/campaigns/${id}`, {method: "PATCH", json: body});
      dlg.close();
      toast(r.nota || (r.regenerando ? "Salvo — regenerando e-mails…" : "Campanha atualizada ✔"), 5000);
      location.hash = `#/campanha/${id}`;
      vCampanha(id);
    } catch (e) {
      btn.disabled = false;
      toast("Não foi possível salvar: " + e.message, 7000);
    }
  };
  dlg.showModal();
}
/* ---------- reescrita dos e-mails com novas diretrizes ---------- */
async function dlgReescrever(id) {
  const c = await api(`/api/campaigns/${id}`);
  const box = document.getElementById("rw-steps");
  box.innerHTML = c.steps.map(s =>
    `<label class="tagopt"><input type="checkbox" class="rw-step" value="${s.ordem}" checked>
      E${s.ordem} ${esc(s.nome || "")} <small class="muted">“${esc((s.assunto || "").slice(0, 48))}”</small></label>`).join("");
  document.getElementById("rw-diretrizes").value = "";
  const dlg = document.getElementById("dlg-reescrever");
  document.getElementById("rw-executar").onclick = async () => {
    const diretrizes = document.getElementById("rw-diretrizes").value.trim();
    if (!diretrizes) return toast("Descreva as novas diretrizes para a reescrita.");
    const marcados = [...document.querySelectorAll(".rw-step:checked")].map(x => +x.value);
    if (!marcados.length) return toast("Escolha pelo menos um e-mail para reescrever.");
    // ordens vazio = todos; mandar a lista só quando for um subconjunto
    const ordens = marcados.length === c.steps.length ? null : marcados;
    dlg.close();
    try {
      const r = await api(`/api/campaigns/${id}/rewrite`, {json: {diretrizes, ordens}});
      toast(r.nota || "Reescrevendo os e-mails… 🔁", 5000);
      vCampanha(id);
    } catch (e) { toast("Não foi possível reescrever: " + e.message, 7000); }
  };
  dlg.showModal();
}

const frow = (label, n, total) =>
  `<div class="frow"><span class="flabel">${esc(label)}</span>
   <div class="fbar" style="width:${Math.max(4, n / Math.max(total, 1) * 170)}px"></div>
   <span class="fnum">${n}</span></div>`;

function _softConfirmFromError(e, verb) {
  const soft = (e.data && e.data.soft_pending) || [];
  const lines = soft.length
    ? soft.map(p => `• ${p.title}${p.detail ? " — " + p.detail : ""}`).join("\n")
    : ((e.data && e.data.pendentes) || []).map(x => `• ${x.label || x.id}`).join("\n");
  return confirm(
    `${verb} com pendências recomendadas?\n\n${lines || "—"}\n\n` +
    `Templates não aprovados serão carimbados como aprovados.\nOK = enviar com ENVIAR_COM_PENDENCIAS.`
  );
}

function wireCampMetricas(cid) {
  document.querySelectorAll("#camp-metricas .metric-card--click").forEach(el => {
    const open = () => {
      const key = el.dataset.metric;
      if (key === "replies") campShowReplies(cid);
      else if (key === "invalidos") campShowInvalidos(cid);
    };
    el.onclick = open;
    el.onkeydown = (ev) => {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); open(); }
    };
  });
}

async function campShowReplies(cid) {
  const panel = document.getElementById("camp-metric-panel");
  if (!panel) return;
  panel.style.display = "block";
  panel.innerHTML = `<div class="muted">Carregando respostas…</div>`;
  try {
    const r = await api(`/api/campaigns/${cid}/replies`);
    const rows = r.replies || [];
    if (!rows.length) {
      panel.innerHTML = `<div class="toolbar"><b>Respostas</b><span class="sp"></span>
        <button type="button" onclick="document.getElementById('camp-metric-panel').style.display='none'">Fechar</button></div>
        <p class="muted">Nenhuma resposta registrada nesta campanha.</p>`;
      return;
    }
    panel.innerHTML = `<div class="toolbar" style="margin-bottom:8px"><b>Respostas (${rows.length})</b>
      <span class="sp"></span>
      <button type="button" onclick="document.getElementById('camp-metric-panel').style.display='none'">Fechar</button></div>
      <table><tr><th>Quando</th><th>Contato</th><th>Classe</th><th>Resumo</th><th></th></tr>
      ${rows.map(x => `<tr>
        <td>${esc(x.data || "—")}</td>
        <td><a href="#/contato/${encodeURIComponent(x.email)}">${esc(x.nome || x.email)}</a>
          <br><small class="muted">${esc(x.empresa || "")} · ${esc(x.email)}</small></td>
        <td>${badge(x.classe || "—")}${x.acao ? `<br><small class="muted">${esc(x.acao)}</small>` : ""}</td>
        <td>${esc((x.resumo || "").slice(0, 220))}</td>
        <td><a href="${esc(x.gmail)}" target="_blank" rel="noopener">Gmail</a></td>
      </tr>`).join("")}</table>`;
  } catch (e) {
    panel.innerHTML = `<p class="form-feedback error">Erro ao carregar respostas: ${esc(e.message)}</p>`;
  }
}

async function campShowInvalidos(cid) {
  const panel = document.getElementById("camp-metric-panel");
  if (!panel) return;
  panel.style.display = "block";
  panel.innerHTML = `<div class="muted">Carregando e-mails inválidos…</div>`;
  try {
    const r = await api(`/api/campaigns/${cid}/invalidos`);
    const rows = r.invalidos || [];
    window._campInvalidos = rows;
    if (!rows.length) {
      panel.innerHTML = `<div class="toolbar"><b>E-mails inválidos</b><span class="sp"></span>
        <button type="button" onclick="document.getElementById('camp-metric-panel').style.display='none'">Fechar</button></div>
        <p class="muted">Nenhum contato com status inválido/bounce nesta campanha.</p>`;
      return;
    }
    panel.innerHTML = `<div class="toolbar" style="margin-bottom:8px;flex-wrap:wrap;gap:8px">
      <b>E-mails inválidos (${rows.length})</b>
      <span class="sp"></span>
      <button type="button" class="primary" id="san-inativar">Inativar no CRM</button>
      <button type="button" id="san-campanha">Só desta campanha</button>
      <button type="button" class="warn" id="san-excluir">Excluir do CRM…</button>
      <button type="button" onclick="document.getElementById('camp-metric-panel').style.display='none'">Fechar</button>
    </div>
    <p class="muted" style="margin:0 0 8px">Padrão: <b>Inativar no CRM</b> (tag email_invalido + nao-contatar; não apaga histórico). Excluir remove o contato do banco.</p>
    <table><tr><th><input type="checkbox" id="san-all" checked></th><th>Contato</th><th>Status</th><th>Bounce</th></tr>
      ${rows.map((x, i) => `<tr>
        <td><input type="checkbox" class="san-cb" data-i="${i}" checked></td>
        <td><a href="#/contato/${encodeURIComponent(x.email)}">${esc(x.nome || x.email)}</a>
          <br><small class="muted">${esc(x.empresa || "")} · ${esc(x.email)}</small></td>
        <td>${badge(x.status)}</td>
        <td><small class="muted">${esc(x.bounce_tipo || "—")}${x.bounce_em ? " · " + esc(x.bounce_em) : ""}
          ${x.bounce_diag ? `<br>${esc(String(x.bounce_diag).slice(0, 100))}` : ""}</small></td>
      </tr>`).join("")}</table>`;
    const all = document.getElementById("san-all");
    if (all) all.onchange = () => {
      panel.querySelectorAll(".san-cb").forEach(cb => { cb.checked = all.checked; });
    };
    const selected = () => {
      const out = [];
      panel.querySelectorAll(".san-cb:checked").forEach(cb => {
        const row = rows[Number(cb.dataset.i)];
        if (row) out.push(row.email);
      });
      return out;
    };
    const run = async (action) => {
      const emails = selected();
      if (!emails.length) { toast("Selecione ao menos um e-mail", 3000); return; }
      let confirmacao = "";
      if (action === "excluir_crm") {
        if (!confirm(`Excluir ${emails.length} contato(s) do CRM? Isso apaga histórico.`)) return;
        const t = prompt(`Digite EXCLUIR para confirmar a exclusão de ${emails.length} contato(s):`, "");
        if ((t || "").trim() !== "EXCLUIR") { toast("Cancelado", 2500); return; }
        confirmacao = "EXCLUIR";
      } else if (action === "inativar_crm") {
        if (!confirm(`Inativar ${emails.length} contato(s) no CRM (suprimir envios)?`)) return;
      } else if (!confirm(`Marcar ${emails.length} como inválido só nesta campanha?`)) return;
      try {
        toast("Sanitizando…", 2000);
        const res = await api(`/api/campaigns/${cid}/sanitize-invalidos`, {
          method: "POST",
          json: {action, emails, confirmacao},
        });
        toast(`${res.n || 0} contato(s) · ${action}`, 4000);
        vCampanha(cid);
      } catch (e) {
        toast("Falha: " + e.message, 6000);
      }
    };
    document.getElementById("san-inativar").onclick = () => run("inativar_crm");
    document.getElementById("san-campanha").onclick = () => run("remover_campanha");
    document.getElementById("san-excluir").onclick = () => run("excluir_crm");
  } catch (e) {
    panel.innerHTML = `<p class="form-feedback error">Erro ao carregar inválidos: ${esc(e.message)}</p>`;
  }
}

async function campAtivar(id, n, confirmacao) {
  if (!confirmacao) {
    const soft = _actSoftList(window._activationGuide);
    if (soft.length) {
      if (!confirm(_actSoftConfirmText(window._activationGuide, "Ativar"))) return;
      return campAtivar(id, n, "ENVIAR_COM_PENDENCIAS");
    }
    if (!confirm(`Ativar a campanha para ${n} contato(s)?\n\nOs envios seguem o horário do 1º envio (se houver) e a janela seg–sex 9h–18h. Para enviar a 1ª etapa imediatamente, use “Disparar agora”.`)) return;
  }
  try {
    const opts = confirmacao ? {json: {confirmacao}} : {method: "POST"};
    const r = await api(`/api/campaigns/${id}/activate`, opts);
    toast(`Campanha ativa ✔ ${r.agendados} contato(s) agendados`); vCampanha(id);
  } catch (e) {
    if (e.data && (e.data.requer_soft_confirm || e.data.requer_checklist)) {
      if (_softConfirmFromError(e, "Ativar")) {
        return campAtivar(id, n, "ENVIAR_COM_PENDENCIAS");
      }
      toast("Ativação cancelada — resolva no passo a passo ou confirme as ressalvas.", 5000);
      return;
    }
    if (e.data && e.data.requer_confirmacao) {
      const d = e.data;
      const aviso = `ATENÇÃO: esta campanha inclui ${d.clientes} CLIENTE(S) Yoobe\n` +
        `(${d.ativos} ativos · ${d.inativos} inativos · ${d.possiveis} possíveis).\n\n` +
        `Para enviar para clientes assim mesmo, digite QUERO_ENVIAR abaixo (ou cancele).`;
      const t = prompt(aviso, "");
      if (t && t.trim() === "QUERO_ENVIAR") { return campAtivar(id, n, "QUERO_ENVIAR"); }
      toast("Ativação cancelada — clientes protegidos.", 5000);
      return;
    }
    toast("Não ativada: " + e.message, 6000);
  }
}
async function campDispararAgora(id, n, confirmacao) {
  if (!confirmacao) {
    const soft = _actSoftList(window._activationGuide);
    if (soft.length && (window._campanhaAtual || {}).status !== "ativa") {
      if (!confirm(_actSoftConfirmText(window._activationGuide, "Disparar agora"))) return;
      return campDispararAgora(id, n, "ENVIAR_COM_PENDENCIAS");
    }
    if (!confirm(`Disparar a 1ª etapa AGORA para até ${n} contato(s)?\n\nIsso ativa a campanha se necessário, ignora o horário agendado e a janela comercial, e envia a etapa 1 imediatamente. Os próximos e-mails seguem a cadência.`)) return;
  }
  try {
    const opts = confirmacao ? {json: {confirmacao}} : {method: "POST"};
    const r = await api(`/api/campaigns/${id}/launch-now`, opts);
    toast(r.nota || `Disparo iniciado ✔ ${r.agendados || 0} contato(s)`, 6000);
    vCampanha(id);
  } catch (e) {
    if (e.data && (e.data.requer_soft_confirm || e.data.requer_checklist)) {
      if (_softConfirmFromError(e, "Disparar agora")) {
        return campDispararAgora(id, n, "ENVIAR_COM_PENDENCIAS");
      }
      toast("Disparo cancelado — resolva no passo a passo ou confirme as ressalvas.", 5000);
      return;
    }
    if (e.data && e.data.requer_confirmacao) {
      const d = e.data;
      const aviso = `ATENÇÃO: esta campanha inclui ${d.clientes} CLIENTE(S) Yoobe\n` +
        `(${d.ativos} ativos · ${d.inativos} inativos · ${d.possiveis} possíveis).\n\n` +
        `Para disparar para clientes assim mesmo, digite QUERO_ENVIAR abaixo (ou cancele).`;
      const t = prompt(aviso, "");
      if (t && t.trim() === "QUERO_ENVIAR") { return campDispararAgora(id, n, "QUERO_ENVIAR"); }
      toast("Disparo cancelado — clientes protegidos.", 5000);
      return;
    }
    toast("Não disparada: " + e.message, 6000);
  }
}
async function campAction(id, act) {
  await api(`/api/campaigns/${id}/${act}`, {method: "POST"}); vCampanha(id);
}
async function campRetomar(id) {
  try {
    await api(`/api/campaigns/${id}/resume-build`, {method: "POST"});
    toast("Retomando a geração de onde parou… 🔄", 4000); vCampanha(id);
  } catch (e) {
    toast("Não foi possível retomar: " + e.message, 6000);
  }
}
async function campTeste(id) {
  const email = prompt("Enviar teste para qual e-mail?", "genaujunior@gmail.com");
  if (!email) return;
  try {
    const r = await api(`/api/campaigns/${id}/test-send`, {json: {email, etapa: 1}});
    toast(`Teste enviado para ${r.email} ✔`, 5000);
  } catch (e) { toast("Falha no teste: " + e.message, 7000); }
}
async function campExcluir(id) {
  return campExcluirLista(id);
}
async function aprovarTpl(ref) {
  if (!ref.startsWith("ai:")) return;
  await api(`/api/templates/${ref.slice(3)}/approve`, {method: "POST"});
  toast("Template aprovado ✔"); route();
}

function _tplCampaignCtx() {
  const m = location.hash.match(/^#\/campanha\/(\d+)/);
  const cid = m ? Number(m[1]) : 0;
  const camp = window._campanhaAtual;
  return {cid, camp};
}

function _tplInnerTemBotao(inner) {
  return /<a\b[^>]*display\s*:\s*inline-block/i.test(String(inner || ""));
}

async function previewTpl(ref, email, campaignId = 0, ordem = 0) {
  const meta = ref.startsWith("ai:")
    ? await api(`/api/templates/${ref.slice(3)}/raw`).catch(() => null)
    : null;
  const bodyHash = _tplHash(meta ? meta.inner_html : ref);
  const html = await api(
    `/api/templates/preview?ref=${encodeURIComponent(ref)}`
    + `&email=${encodeURIComponent(email || "")}`
    + `&bh=${bodyHash}&_=${Date.now()}`);
  const box = document.getElementById("tpl-preview-box");
  if (!box) return;
  window._tplPreviewState = {ref, email: email || "", campaignId: campaignId || 0, ordem: ordem || 0};
  const editavel = ref.startsWith("ai:");
  const assunto = (meta && meta.assunto) || "";
  const ctaIgnorado = meta ? _tplInnerTemBotao(meta.inner_html) : false;
  const cid = campaignId || _tplCampaignCtx().cid;
  const bodySnippet = meta ? _tplStripHtml(meta.inner_html || "").slice(0, 140) : "";
  box.innerHTML = `
    <div class="card" style="margin-bottom:8px;padding:12px">
      <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:8px">
        ${editavel
          ? `<button type="button" data-tpl-act="editar">✏️ Editar este e-mail</button>`
          : cid ? `<button type="button" data-tpl-act="clone">✏️ Criar cópia editável</button>`
          : `<small class="muted">template fixo — abra-o dentro de uma campanha para criar uma cópia editável.</small>`}
        <button type="button" data-tpl-act="reload">🔄 Atualizar preview</button>
        <small class="muted">${esc(ref)}${meta && meta.status ? " · " + esc(meta.status) : ""} · corpo #${bodyHash}</small>
      </div>
      ${assunto ? `<div><b>Assunto:</b> ${esc(assunto)}</div>` : ""}
      ${meta && meta.preheader ? `<div class="muted" style="margin-top:4px"><b>Preheader:</b> ${esc(meta.preheader)}</div>` : ""}
      ${bodySnippet ? `<div class="muted" style="margin-top:4px"><b>Corpo (início):</b> ${esc(bodySnippet)}${bodySnippet.length >= 140 ? "…" : ""}</div>` : ""}
      ${ctaIgnorado ? `<div class="muted" style="margin-top:6px;color:var(--gold-bright)">⚠ O miolo já tem botão — os campos CTA da moldura não aparecem no preview (edite o HTML do corpo).</div>` : ""}
    </div>
    <iframe class="preview" sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox" referrerpolicy="no-referrer"></iframe>`;
  setPreviewIframe(box.querySelector("iframe"), html);
  const ed = box.querySelector('[data-tpl-act="editar"]');
  if (ed) ed.onclick = () => editarTpl(ref, email, cid, ordem);
  const cl = box.querySelector('[data-tpl-act="clone"]');
  if (cl) cl.onclick = () => tornarEmailEditavel(cid, ordem, email);
  const rl = box.querySelector('[data-tpl-act="reload"]');
  if (rl) rl.onclick = () => previewTpl(ref, email, cid, ordem);
  box.scrollIntoView({behavior: "smooth"});
}

async function tornarEmailEditavel(campaignId, ordem, email) {
  try {
    const r = await api(`/api/campaigns/${campaignId}/steps/${ordem}/make-editable`, {method: "POST"});
    toast(r.nota || "E-mail pronto para editar ✔", 3500);
    await vCampanha(campaignId);
    await previewTpl(r.ref, email, campaignId, ordem);
  } catch (e) { toast("Não foi possível preparar o e-mail: " + e.message, 5000); }
}

/* Editor manual simples do e-mail (campos de templates_ai). Salvar volta o status p/ pendente. */
async function editarTpl(ref, email, campaignId = 0, ordem = 0) {
  if (!ref.startsWith("ai:")) return;
  const t = await api(`/api/templates/${ref.slice(3)}/raw`);
  const box = document.getElementById("tpl-preview-box");
  if (!box) return;
  const cid = campaignId || _tplCampaignCtx().cid;
  window._tplPreviewState = {ref, email: email || "", campaignId: cid, ordem: ordem || 0};
  window._tplEditorOrig = {
    inner_html: t.inner_html || "",
    texto_plano: t.texto_plano || "",
    assunto: t.assunto || "",
  };
  const ctaIgnorado = _tplInnerTemBotao(t.inner_html);
  const f = (id, label, val) => `<label style="display:block;margin:8px 0 2px;font-size:13px;font-weight:600">${label}</label>
    <input id="ed-${id}" style="width:100%;box-sizing:border-box" value="${esc(val || "")}">`;
  box.innerHTML = `
    <div class="card">
      <b>✏️ Editando o e-mail</b> <small class="muted">— ao salvar, volta a “pendente” para você reconferir antes de ativar.</small>
      ${f("nome", "Nome interno", t.nome)}
      ${f("assunto", "Assunto", t.assunto)}
      ${f("preheader", "Preheader (prévia que aparece na caixa de entrada)", t.preheader)}
      ${f("cta", "Botão da moldura (rótulo)", t.cta)}
      ${f("cta_url", "Botão da moldura (URL)", t.cta_url)}
      ${ctaIgnorado ? `<p class="muted" style="margin:8px 0 0;color:var(--gold-bright)">⚠ Este e-mail já tem botão no HTML do corpo — alterar só os campos “Botão da moldura” não muda o preview. Edite o botão dentro do HTML abaixo.</p>` : ""}
      <label style="display:block;margin:8px 0 2px;font-size:13px;font-weight:600">Corpo do e-mail <span style="color:var(--gold-bright)">(é o que o preview mostra)</span></label>
      <div class="tpl-body-toolbar">
        <button type="button" data-ed-mode="visual">Visual</button>
        <button type="button" data-ed-mode="html" class="primary">HTML</button>
        <button type="button" id="ed-format-html">Indentar HTML</button>
        <button type="button" id="ed-live-preview">👁 Atualizar prévia</button>
        <button type="button" id="ed-sync-texto">Texto simples → HTML</button>
      </div>
      <div id="ed-visual" class="tpl-visual-editor" contenteditable="true" spellcheck="true" hidden></div>
      <textarea id="ed-inner_html" class="tpl-html-editor" rows="18" spellcheck="false"></textarea>
      <small class="muted" id="ed-body-hint" style="display:block;margin-top:6px">Modo HTML já vem indentado. Tab = 2 espaços. Visual edita o texto sem ver as tags.</small>
      <iframe id="ed-live-frame" class="preview" style="margin-top:10px;min-height:280px" sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox" referrerpolicy="no-referrer"></iframe>
      <label style="display:block;margin:12px 0 2px;font-size:13px;font-weight:600">Versão texto simples <span class="muted">(multipart / clientes sem HTML — sozinho NÃO muda o preview)</span></label>
      <textarea id="ed-texto_plano" rows="6" style="width:100%;box-sizing:border-box"></textarea>
      <div style="margin-top:10px">
        <button class="green" type="button" id="ed-save">💾 Salvar</button>
        <button type="button" id="ed-cancel">Cancelar</button>
      </div>
    </div>`;
  const textoEl = document.getElementById("ed-texto_plano");
  textoEl.value = t.texto_plano || "";
  const bodyEd = _tplMountBodyEditor("ed", t.inner_html || "");
  window._tplBodyEditor = bodyEd;
  const refreshLive = async () => {
    try {
      const live = await api("/api/templates/preview-draft", {
        json: {
          preheader: document.getElementById("ed-preheader")?.value || "",
          cta: document.getElementById("ed-cta")?.value || "",
          cta_url: document.getElementById("ed-cta_url")?.value || "",
          inner_html: bodyEd.getHtml(),
          email: email || "",
        },
      });
      setPreviewIframe(document.getElementById("ed-live-frame"), live);
    } catch (e) {
      const hint = document.getElementById("ed-body-hint");
      if (hint) hint.textContent = "Prévia indisponível: " + e.message;
    }
  };
  document.getElementById("ed-live-preview").onclick = refreshLive;
  document.getElementById("ed-sync-texto").onclick = () => {
    bodyEd.setHtml(_tplTextoParaInner(textoEl.value, window._tplEditorOrig?.inner_html || bodyEd.getHtml()));
    toast("Texto simples aplicado no HTML do miolo — confira a prévia", 3500);
    refreshLive();
  };
  textoEl.addEventListener("input", () => {
    const hint = document.getElementById("ed-body-hint");
    if (!hint) return;
    const orig = window._tplEditorOrig || {};
    const nh = s => String(s || "").replace(/>\s+</g, "><").replace(/\s+/g, " ").trim();
    if (textoEl.value !== (orig.texto_plano || "")
        && nh(bodyEd.getHtml()) === nh(orig.inner_html || "")) {
      hint.innerHTML = `<span style="color:var(--gold-bright)">Você alterou só o texto simples — ao salvar, aplicaremos no HTML automaticamente.</span>`;
    }
  });
  document.getElementById("ed-save").onclick = () => salvarTpl(ref, email, cid, ordem);
  document.getElementById("ed-cancel").onclick = () => previewTpl(ref, email, cid, ordem);
  box.scrollIntoView({behavior: "smooth"});
  refreshLive();
}

async function salvarTpl(ref, email, campaignId = 0, ordem = 0) {
  const val = id => {
    const el = document.getElementById("ed-" + id);
    if (!el) throw new Error(`campo ed-${id} ausente — recarregue o editor`);
    return el.value;
  };
  const bodyEd = window._tplBodyEditor;
  let payload;
  try {
    const inner = bodyEd ? bodyEd.getHtml() : val("inner_html");
    payload = {nome: val("nome"), assunto: val("assunto"), preheader: val("preheader"), cta: val("cta"),
               cta_url: val("cta_url"), inner_html: inner, texto_plano: val("texto_plano")};
  } catch (e) { return toast("Erro ao ler o editor: " + e.message, 5000); }
  const orig = window._tplEditorOrig || {};
  const normHtml = s => String(s || "").replace(/>\s+</g, "><").replace(/\s+/g, " ").trim();
  const htmlChanged = normHtml(payload.inner_html) !== normHtml(orig.inner_html);
  const textoChanged = (payload.texto_plano || "") !== (orig.texto_plano || "");
  // Se o operador mudou só o texto simples, sincroniza no HTML — era a causa do
  // “assunto muda, corpo do preview fica igual”.
  if (textoChanged && !htmlChanged) {
    payload.inner_html = _tplTextoParaInner(payload.texto_plano, orig.inner_html || payload.inner_html);
    if (bodyEd) bodyEd.setHtml(payload.inner_html);
  }
  const cid = campaignId || _tplCampaignCtx().cid;
  try {
    const r = await api(`/api/templates/${ref.slice(3)}`, {json: payload});
    // Confirma no banco (anti-cache / anti-race) antes de redesenhar a UI.
    const check = await api(`/api/templates/${ref.slice(3)}/raw`);
    if ((check.assunto || "") !== (payload.assunto || "")
        || (check.inner_html || "") !== (payload.inner_html || "")) {
      throw new Error("o servidor não gravou o corpo/assunto enviados — tente de novo");
    }
    let msg = "E-mail salvo ✔ — sequência e preview atualizados";
    if (textoChanged && !htmlChanged) {
      msg = "E-mail salvo ✔ — texto simples aplicado no corpo HTML do preview";
    }
    if (r.cta_shell_ignored) {
      msg += " (CTA da moldura ignorado: o corpo já tem botão)";
    }
    toast(msg, 5000);
    window._tplEditorOrig = null;
    window._tplPreviewState = {ref, email: email || "", campaignId: cid, ordem: ordem || 0};
    if (cid && location.hash.startsWith("#/campanha/")) {
      window._tplPreviewRestore = true;
      await vCampanha(cid);
      if (!document.querySelector("#tpl-preview-box iframe")) {
        await previewTpl(ref, email, cid, ordem);
      }
    } else {
      await previewTpl(ref, email, cid, ordem);
    }
  } catch (e) { toast("Erro ao salvar: " + e.message, 5000); }
}

/* ---------- TELA: Respostas (engajamento + auto-resposta) ---------- */
const engFiltro = {tipo: "todos", q: "", _qTimer: null};

const ENG_KIND_META = {
  abertura: {label: "Abertura", cls: "pendente", icon: "👁"},
  clique: {label: "Clique", cls: "ativo", icon: "🖱"},
  resposta: {label: "Resposta", cls: "concluido", icon: "💬"},
  descadastro: {label: "Descadastro", cls: "encerrada", icon: "🚫"},
};

function engKindBadge(kind) {
  const m = ENG_KIND_META[kind] || {label: kind, cls: "pendente", icon: "•"};
  return `${m.icon} ${badge(m.label, m.cls)}`;
}

function engDestinoCell(it) {
  if (it.kind !== "clique" || !it.url) return `<span class="muted">—</span>`;
  const short = it.url.length > 64 ? it.url.slice(0, 64) + "…" : it.url;
  return `<a href="${esc(it.url)}" target="_blank" rel="noopener" title="${esc(it.url)}"><small>${esc(short)}</small></a>`;
}

function engFiltroTipo(tipo) {
  engFiltro.tipo = tipo || "todos";
  vRespostas();
}

function engBuscar(el) {
  engFiltro.q = el.value || "";
  clearTimeout(engFiltro._qTimer);
  engFiltro._qTimer = setTimeout(() => vRespostas(), 280);
}

async function vRespostas() {
  // Sync de tracking ao abrir (não bloqueia a tela se falhar).
  try { await api("/api/tracking/sync", {method: "POST"}); } catch (_) { /* offline / sync key */ }
  const p = new URLSearchParams();
  if (engFiltro.tipo && engFiltro.tipo !== "todos") p.set("tipo", engFiltro.tipo);
  if (engFiltro.q.trim()) p.set("q", engFiltro.q.trim());
  p.set("limit", "200");
  const [d, eng] = await Promise.all([
    api("/api/replies"),
    api("/api/engagement" + (p.toString() ? "?" + p : "")),
  ]);
  const cf = d.config, on = cf.auto_reply;
  const st = eng.stats || {};
  const classHtml = Object.entries(cf.classes_seguras).map(([k, desc]) => {
    const ativa = cf.classes_ativas.includes(k);
    return `<label class="classrow ${ativa ? "on" : ""}">
      <span class="switch"><input type="checkbox" class="rc-class" value="${k}" ${ativa ? "checked" : ""} ${on ? "" : "disabled"}><span class="sl"></span></span>
      <span><b>${esc(k)}</b><div class="desc">${esc(desc)}</div></span></label>`;
  }).join("");
  const chips = [
    ["todos", `Todos (${eng.total || 0})`],
    ["abertura", `Aberturas (${st.abertura || 0})`],
    ["clique", `Cliques (${st.clique || 0})`],
    ["resposta", `Respostas (${st.resposta || 0})`],
    ["descadastro", `Descadastros (${st.descadastro || 0})`],
  ];
  const itens = eng.itens || [];
  view.innerHTML = `
    <h2>Respostas</h2>
    <p class="muted" style="margin-top:-6px;margin-bottom:14px">Engajamento das campanhas: aberturas, cliques (com destino), replies e descadastros.</p>
    <div class="statusbox ${on ? "on" : "off"}" style="margin-bottom:16px">
      <label class="switch"><input type="checkbox" id="rc-master" ${on ? "checked" : ""}><span class="sl"></span></label>
      <div style="flex:1">
        <div class="big">${on ? "🟢 Auto-resposta LIGADA" : "🔴 Auto-resposta DESLIGADA"}</div>
        <div class="muted">${on
          ? "A IA responde sozinha os casos seguros marcados abaixo (dentro dos limites). Todo o resto te notifica no WhatsApp."
          : "A IA só te notifica no WhatsApp com o rascunho pronto — nada é enviado sem você."}</div>
      </div>
    </div>
    <div class="row">
      <div class="card grow">
        <h3>Quem a IA responde sozinha</h3>
        <p class="muted" style="margin-top:0">Marque os assuntos que podem sair no automático. ${on ? "" : "(ligue a auto-resposta acima para editar)"}</p>
        ${classHtml}
      </div>
      <div class="card" style="width:340px">
        <h3>Limites de segurança</h3>
        <div>
          <span class="limitchip">confiança ≥ ${Math.round(cf.conf_min * 100)}%</span>
          <span class="limitchip">máx ${cf.max_dia}/dia</span>
          <span class="limitchip">${cf.max_thread}/thread por dia</span>
          <span class="limitchip">kill-switch: PAUSE</span>
        </div>
        <p class="muted" style="margin-bottom:0">⛔ <b>Sempre escala pra você</b> (nunca responde sozinho):
        objeção de preço/negociação, dúvida complexa, "me chama depois", baixa confiança,
        algo fora do KB, ou qualquer menção a valor/desconto/contrato fora do padrão.</p>
      </div>
    </div>
    <div class="card">
      <div class="toolbar"><h3 style="margin:0">Feed de engajamento</h3>
        <span class="muted">✅ ${d.stats.auto_respondidas} auto · 🙋 ${d.stats.escaladas} escaladas · mostrando ${eng.mostrados || 0}/${eng.total || 0}</span>
        <span class="sp"></span>
        <button type="button" onclick="api('/api/tracking/sync',{method:'POST'}).then(r=>{toast(r.ok===false?(r.erro||r.motivo||'sync falhou'):('Tracking ok'+(r.nota?' — '+r.nota:'')));vRespostas()}).catch(e=>toast(e.message,5000))">↻ Sync cliques</button>
        <button type="button" onclick="api('/api/replies/scan',{method:'POST'}).then(r=>{toast('Varredura: '+r.novas+' nova(s)');vRespostas()})">🔄 Varrer replies</button>
      </div>
      <div class="toolbar filter-bar" role="search" aria-label="Filtros de engajamento" style="margin-bottom:10px">
        ${chips.map(([v, rot]) => `<label class="tagopt${engFiltro.tipo === v ? " on" : ""}">
          <input type="radio" name="eng-tipo" value="${v}" ${engFiltro.tipo === v ? "checked" : ""}
            onchange="engFiltroTipo(this.value)"> ${rot}</label>`).join("")}
        <input type="search" placeholder="Buscar e-mail, nome, empresa…" aria-label="Buscar engajamento"
          value="${esc(engFiltro.q)}" style="min-width:200px" oninput="engBuscar(this)">
      </div>
      <table><tr><th>Tipo</th><th>Empresa / Contato</th><th>Quando</th><th>Campanha</th><th>Destino / detalhe</th><th></th></tr>
      ${itens.map(it => {
        const go = it.is_contato
          ? `onclick="location.hash='#/contato/${encodeURIComponent(it.email)}'" class="click"`
          : "";
        return `<tr ${go}>
          <td>${engKindBadge(it.kind)}</td>
          <td><b>${esc(it.empresa || "—")}</b><br><small class="muted">${esc(it.nome || it.email)}</small></td>
          <td>${esc((it.quando || "").slice(0, 16))}</td>
          <td><small>${esc(it.campanha || "—")}${it.etapa ? `<br><span class="muted">${esc(it.etapa)}</span>` : ""}</small></td>
          <td>${it.kind === "clique" ? engDestinoCell(it) : `<small>${esc(it.detalhe || "")}</small>`}</td>
          <td>${it.gmail ? `<a href="${esc(it.gmail)}" target="_blank" rel="noopener" onclick="event.stopPropagation()"><small>Gmail ↗</small></a>` : ""}</td>
        </tr>`;
      }).join("")
        || '<tr><td colspan="6" class="muted">nenhum evento neste filtro ainda — sync cliques ou varra replies</td></tr>'}</table>
    </div>`;

  document.getElementById("rc-master").onchange = async (e) => {
    await api("/api/replies/config", {json: {auto_reply: e.target.checked}});
    toast(e.target.checked ? "Auto-resposta LIGADA ✔" : "Auto-resposta desligada", 2500);
    vRespostas();
  };
  document.querySelectorAll(".rc-class").forEach(el => el.onchange = async () => {
    const classes = [...document.querySelectorAll(".rc-class:checked")].map(x => x.value);
    await api("/api/replies/config", {json: {classes}});
    toast("Assuntos atualizados ✔", 2000);
    vRespostas();
  });
}

/* ---------- TELA: Remetentes (multi-conta) ---------- */
function spill(s, opts = {}) {
  const detailed = !!opts.detailed;
  if (s.ativo === 0) {
    return '<span class="spill inativo">inativo</span>';
  }
  if (s.conectado) {
    return '<span class="spill on">conectado</span>';
  }
  const motivo = (s.status_motivo || "precisa login").trim();
  const curto = motivo.length > 56 ? motivo.slice(0, 56) + "…" : motivo;
  const low = motivo.toLowerCase();
  let label = "desconectado";
  if (low.includes("invalid_grant") || low.includes("expired") || low.includes("revoked")) {
    label = "token expirado";
  } else if (low.includes("ainda não") || low.includes("nao criado") || low.includes("não criado")
             || low.includes("precisa login") || low.includes("perfil ainda")) {
    label = "não conectado";
  }
  const detail = detailed && motivo
    ? `<div class="spill-detail" title="${esc(motivo)}">${esc(curto)}</div>` : "";
  return `<span class="spill off" title="${esc(motivo)}">${esc(label)}</span>${detail}`;
}
const iniciais = e => (e || "?").slice(0, 2).toUpperCase();
const pct = (v, max) => `<div class="barwrap"><i style="width:${Math.min(100, v / Math.max(max, 1) * 100)}%"></i></div>`;

async function vRemetentes() {
  await ensureDashboardMeta();
  const d = await api("/api/senders");
  const ss = d.senders;
  const maxEnv = Math.max(1, ...ss.map(s => s.stats.enviados));
  view.innerHTML = `
    <h2>Remetentes</h2>
    <p class="muted" style="margin-top:0">Campanhas novas saem somente por <b>genau@yoobe.co</b>. As contas antigas ficam disponíveis para consultar o histórico e sincronizar respostas.</p>

    <div class="card">
      <h3>📊 Performance global (todos os remetentes)</h3>
      <div class="table-scroll">
      <table class="perf-table"><tr><th>Remetente</th><th>Propósito</th><th class="col-status">Status</th>
        <th>Enviados</th><th>Abertura</th><th>Clique</th><th>Resposta</th></tr>
      ${ss.map(s => `<tr class="click" onclick="location.hash='#/remetente/${s.id}'">
        <td><b>${esc(s.email)}</b></td><td><small class="muted">${esc(s.proposito)}</small></td>
        <td class="col-status">${spill(s)}</td>
        <td>${s.stats.enviados} ${pct(s.stats.enviados, maxEnv)}</td>
        <td>${s.stats.taxa_abertura}%</td><td>${s.stats.taxa_clique}%</td><td>${s.stats.taxa_resposta}%</td>
      </tr>`).join("")}</table>
      </div>
    </div>

    <div class="toolbar" style="margin-top:6px">
      <h3 style="margin:0">Gerenciar contas</h3>
    </div>
    ${ss.map(s => senderCard(s, d.classes_seguras)).join("")}`;
  wireSenders(d.classes_seguras);
}

function senderCard(s, classesSeguras) {
  const remoto = !!(window.DASHBOARD_META && window.DASHBOARD_META.espelho);
  const conectar = !s.conectado ? `
    <div class="connectbox" id="cb-${s.id}" style="display:none">
      <b>Conectar ${esc(s.email)}</b>
      <div class="cb-body" style="margin-top:6px"></div>
    </div>` : "";
  const auto = s.auto_reply_on;
  const connBtn = s.conectado
    ? `<button onclick="location.hash='#/remetente/${s.id}'">Ver detalhes →</button>`
    : (remoto
      ? `<button class="primary" disabled title="OAuth Gmail só no Mac (localhost:8377). Depois: sync --with-accounts">🔌 Só no Mac</button>`
      : `<button class="primary" onclick="senderConectar(${s.id})">🔌 Conectar conta</button>`);
  return `<div class="sender-card">
    <div class="top">
      <div class="av">${iniciais(s.email)}</div>
      <div class="sender-meta"><span class="em">${esc(s.email)}</span> ${spill(s, {detailed: true})}
        <div class="pp">${esc(s.nome)} · ${esc(s.proposito)}${s.slug ? "" : " · conta principal"}</div></div>
      ${connBtn}
    </div>
    ${conectar}
    <div class="sender-mini">
      <div><b>${s.stats.enviados}</b>enviados</div>
      <div><b>${s.stats.taxa_abertura}%</b>abertura</div>
      <div><b>${s.stats.taxa_clique}%</b>clique</div>
      <div><b>${s.stats.taxa_resposta}%</b>resposta</div>
      <div><b>${s.campanhas}</b>campanha(s)</div>
    </div>
    <div class="row" style="gap:12px">
      <div class="grow" style="min-width:260px">
        <label style="font-size:12.5px;font-weight:700">🤖 Auto-resposta da IA</label>
        <div class="classrow ${auto ? "on" : ""}" style="margin-top:4px">
          <label class="switch"><input type="checkbox" class="sd-auto" data-id="${s.id}" ${auto ? "checked" : ""}><span class="sl"></span></label>
          <span class="desc">${auto ? "Ligada — responde sozinha os assuntos seguros" : "Desligada — só notifica no WhatsApp"}</span>
        </div>
      </div>
      <div style="min-width:280px">
        <label style="font-size:12.5px;font-weight:700">✍️ Assinatura (URL pública da imagem)</label>
        <div style="display:flex;gap:6px;margin-top:4px">
          <input class="sd-sig" data-id="${s.id}" value="${esc(s.signature_url || "")}" placeholder="https://…/assinatura.png" style="flex:1">
          <button onclick="senderSalvarSig(${s.id})">Salvar</button>
        </div>
        ${s.signature_url ? `<img src="${esc(s.signature_url)}" alt="" style="max-height:48px;margin-top:6px;border-radius:6px">` : ""}
      </div>
    </div>
  </div>`;
}

function wireSenders() {
  document.querySelectorAll(".sd-auto").forEach(el => el.onchange = async () => {
    await api(`/api/senders/${el.dataset.id}/autoreply`, {json: {auto_reply: el.checked}});
    toast(el.checked ? "Auto-resposta ligada ✔" : "Auto-resposta desligada", 2200); vRemetentes();
  });
}
async function senderConectar(id) {
  await ensureDashboardMeta();
  const cb = document.getElementById("cb-" + id);
  const body = cb ? cb.querySelector(".cb-body") : null;
  if (window.DASHBOARD_META && window.DASHBOARD_META.espelho) {
    if (cb) {
      cb.style.display = "block";
      body.innerHTML = `<p class="form-feedback error">Conectar Gmail só no <b>Mac</b> (<code>localhost:8377</code>).
        Depois rode <code>./scripts/sync-local-to-remote.sh --with-accounts</code> para a produção usar as credenciais.</p>`;
    }
    toast("OAuth Gmail só no ambiente local", 5000);
    return;
  }
  if (cb) {
    cb.style.display = "block";
    body.innerHTML = '<span class="muted">⏳ gerando o link de autorização do Google…</span>';
    cb.scrollIntoView({behavior: "smooth", block: "center"});
  }
  try {
    const r = await api(`/api/senders/${id}/connect`, {method: "POST"});
    if (!body) return;
    body.innerHTML = `
      <p class="muted" style="margin:0 0 8px">${esc(r.nota || "Abra o link e autorize como a conta do remetente.")}</p>
      <div class="row" style="gap:8px;flex-wrap:wrap;align-items:center">
        <button class="primary cb-open">🔗 Abrir link de conexão</button>
        <button class="cb-copy">📋 Copiar link</button>
        <button class="green" onclick="senderVerificar(${id})">✅ Já autorizei — Verificar</button>
      </div>
      <p style="margin:8px 0 0"><small class="muted">O Google abre <b>localhost:PORTA</b> (esperado). Não feche o dashboard nem clique Conectar de novo até ver Success. Se expirar, gere um link novo.</small></p>`;
    body.querySelector(".cb-open").onclick = () => window.open(r.url, "_blank", "noopener");
    body.querySelector(".cb-copy").onclick = async () => {
      try { await navigator.clipboard.writeText(r.url); toast("Link de conexão copiado ✔", 3000); }
      catch (e) { prompt("Copie o link de conexão:", r.url); }
    };
  } catch (e) {
    const msg = (e.data && e.data.erro) || e.message;
    if (body) body.innerHTML = `<p class="form-feedback error">${esc(msg)}</p>`;
    toast(`Não foi possível preparar a conexão: ${msg}`, 6000);
  }
}
async function senderCriar(ev) {
  ev.preventDefault();
  const email = document.getElementById("sender-email").value.trim();
  const nome = document.getElementById("sender-nome").value.trim();
  const proposito = document.getElementById("sender-proposito").value.trim();
  const submit = document.getElementById("sender-create-submit");
  const feedback = document.getElementById("sender-create-feedback");
  submit.disabled = true; submit.textContent = "Cadastrando…";
  feedback.textContent = ""; feedback.className = "form-feedback";
  try {
    const s = await api("/api/senders", {json: {email, nome, proposito}});
    feedback.textContent = "Remetente cadastrado. Preparando a conexão Gmail…";
    feedback.className = "form-feedback ok";
    toast("Remetente cadastrado. Agora autorize a conta Gmail.", 3500);
    await vRemetentes();
    const card = document.querySelector(`#cb-${s.id}`)?.closest(".sender-card");
    if (card) { card.scrollIntoView({behavior: "smooth", block: "center"}); await senderConectar(s.id); }
  } catch (e) {
    feedback.textContent = `Cadastro não concluído: ${e.message}`;
    feedback.className = "form-feedback error";
    toast(`Cadastro não concluído: ${e.message}`, 6000);
  } finally {
    const current = document.getElementById("sender-create-submit");
    if (current) { current.disabled = false; current.textContent = "Cadastrar remetente"; }
  }
}
async function senderVerificar(id) {
  const st = await api(`/api/senders/${id}/status`);
  toast(st.conectado ? "🟢 Conta conectada!" : "🔴 Ainda não conectada (" + st.motivo + ")", 4000);
  if (st.conectado) vRemetentes();
}
async function senderSalvarSig(id) {
  const el = document.querySelector(`.sd-sig[data-id="${id}"]`);
  await api(`/api/senders/${id}`, {json: {signature_url: el.value.trim()}});
  toast("Assinatura salva ✔"); vRemetentes();
}

async function vRemetente(id) {
  const s = await api("/api/senders/" + id);
  view.innerHTML = `
    <div class="toolbar"><a href="#/remetentes">← Remetentes</a>
      <h2 style="margin:0">${esc(s.email)}</h2> ${spill(s, {detailed: true})}</div>
    <div class="card"><b>${esc(s.nome)}</b> · ${esc(s.proposito)}</div>
    <div class="kpis">
      <div class="kpi"><b>${s.stats.enviados}</b><small>enviados</small></div>
      <div class="kpi"><b>${s.stats.taxa_abertura}%</b><small>abertura (${s.stats.abertos})</small></div>
      <div class="kpi"><b>${s.stats.taxa_clique}%</b><small>clique (${s.stats.cliques})</small></div>
      <div class="kpi"><b>${s.stats.taxa_resposta}%</b><small>resposta (${s.stats.respostas})</small></div>
    </div>
    <div class="row" style="margin-top:14px">
      <div class="card grow"><h3>Campanhas deste remetente</h3>
        <table><tr><th>Campanha</th><th>Status</th><th>Tipo</th></tr>
        ${s.campanhas.map(c => `<tr class="click" onclick="location.hash='#/campanha/${c.id}'">
          <td><b>${esc(c.nome)}</b></td><td>${badge(c.status)}</td><td>${badge(c.tipo, "pendente")}</td></tr>`).join("")
          || '<tr><td colspan="3" class="muted">nenhuma</td></tr>'}</table></div>
      <div class="card grow"><h3>Respostas recebidas</h3>
        <table><tr><th>Contato</th><th>Classe</th><th>Ação</th><th></th></tr>
        ${s.respostas.map(r => `<tr><td>${esc(r.email)}</td><td>${badge(r.classe)}</td>
          <td>${badge(r.acao === "auto-respondida" ? "auto-respondida" : "escalada")}</td>
          <td><a href="${r.gmail}" target="_blank"><small>Gmail ↗</small></a></td></tr>`).join("")
          || '<tr><td colspan="4" class="muted">nenhuma</td></tr>'}</table></div>
    </div>`;
}

/* ---------- TELA: CRM ---------- */
const tagChip = t => `<span class="tag">${esc(t)}</span>`;

async function vCRM() {
  const funis = ["", "novo", "contatado", "follow-up", "último-toque", "respondeu", "pediu-agendamento", "agendou", "encerrado"];
  const [td, ld] = await Promise.all([api("/api/tags"), api("/api/lead-segments")]);
  let crmPage = 1;
  const presets = [
    {rotulo: "🧊 Leads frios", filtros: {"crm-seg": "frio"}},
    {rotulo: "😴 Base inativa", filtros: {"crm-tag": "origem-base-inativa"}},
    {rotulo: "🚫 Clientes inativos", filtros: {"crm-tag": "cliente_inativo_4unik"}},
    {rotulo: "🔁 Reativação", filtros: {"crm-perfil": "telefone_reativacao"}},
    {rotulo: "🏭 Com ERP", filtros: {"crm-tem-erp": true}},
    {rotulo: "Todos", filtros: {}},
  ];
  view.innerHTML = `
    <div class="crm-head"><div><h2>CRM inteligente</h2>
      <p>Prioriza leads completos e com telefone. Busque por qualquer característica comercial.</p></div>
      <div class="toolbar"><button class="primary" onclick="document.getElementById('dlg-contato').showModal()">＋ Novo contato</button>
      <label class="btn">📥 Importar<input type="file" id="crm-up" accept=".csv,.xlsx,.xls" hidden></label>
      <button id="crm-export">⬇ Exportar CSV</button></div></div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px">${presets.map((p, i) =>
      `<button class="crm-preset" data-preset="${i}" style="border-radius:999px;font-size:12.5px;padding:5px 13px">${esc(p.rotulo)}</button>`).join("")}</div>
    <div class="card crm-filters">
      <input id="crm-q" class="crm-search" placeholder="🔎 Nome, empresa, e-mail, telefone, cargo, categoria, interesse, comentário…">
      <select id="crm-perfil"><option value="">🎯 Todos os perfis</option>${ld.segmentos.map(s => `<option value="${s.id}">${esc(s.nome)} (${s.contatos})</option>`).join("")}</select>
      <select id="crm-score"><option value="0">Qualquer score</option><option value="40">Score ≥ 40</option><option value="50">Score ≥ 50</option><option value="70">Score ≥ 70</option></select>
      <select id="crm-funil">${funis.map(f => `<option value="${f}">${f || "Todos os funis"}</option>`).join("")}</select>
      <select id="crm-resp"><option value="">Qualquer resposta</option><option>sim</option><option value="não">Não respondeu</option></select>
      <select id="crm-tag"><option value="">🏷️ Todas as categorias</option>${td.tags.map(t => `<option value="${esc(t.tag)}">${esc(t.tag)} (${t.contatos})</option>`).join("")}</select>
      <select id="crm-seg"><option value="">🌡️ Todos os segmentos</option><option value="quente">🔥 Quente</option><option value="morno">🌤️ Morno</option><option value="frio">🧊 Frio</option></select>
      <input id="crm-erp" placeholder="🏭 ERP contém…">
      <label style="display:flex;align-items:center;gap:6px;font-size:13px"><input type="checkbox" id="crm-tem-erp">Somente com ERP</label>
      <select id="crm-ordem"><option value="prioridade">🔥 Mais quentes primeiro</option><option value="recentes">Mais recentes</option><option value="nome">Nome A–Z</option><option value="empresa">Empresa A–Z</option></select>
      <select id="crm-size"><option value="20">20 por página</option><option value="50" selected>50 por página</option><option value="100">100 por página</option><option value="200">200 por página</option></select>
      <button id="crm-limpar">Limpar filtros</button>
    </div><div id="crm-tbl">carregando…</div>`;
  // Sem default forçado: restaura o último filtro usado (localStorage crm_filtros); 1ª visita = sem filtro.
  const CAMPOS_FILTRO = ["crm-q", "crm-perfil", "crm-score", "crm-funil", "crm-resp", "crm-tag", "crm-seg", "crm-erp", "crm-ordem", "crm-size"];
  const setCampo = (id, v) => {
    const el = document.getElementById(id);
    if (el.tagName === "SELECT" && v && ![...el.options].some(o => o.value === v)) el.add(new Option(v, v));
    el.value = v;
  };
  let salvos = {};
  try { salvos = JSON.parse(localStorage.getItem("crm_filtros") || "{}"); } catch { salvos = {}; }
  CAMPOS_FILTRO.forEach(id => { if (salvos[id] != null && salvos[id] !== "") setCampo(id, String(salvos[id])); });
  document.getElementById("crm-tem-erp").checked = !!salvos["crm-tem-erp"];
  const salvarFiltros = () => {
    const st = Object.fromEntries(CAMPOS_FILTRO.map(id => [id, document.getElementById(id).value]));
    st["crm-tem-erp"] = document.getElementById("crm-tem-erp").checked;
    localStorage.setItem("crm_filtros", JSON.stringify(st));
  };
  const params = (page = crmPage) => {
    const p = new URLSearchParams({
      q: document.getElementById("crm-q").value, funil: document.getElementById("crm-funil").value,
      respondeu: document.getElementById("crm-resp").value, tag: document.getElementById("crm-tag").value,
      perfil: document.getElementById("crm-perfil").value, score_min: document.getElementById("crm-score").value,
      segmento: document.getElementById("crm-seg").value, erp: document.getElementById("crm-erp").value,
      ordenar: document.getElementById("crm-ordem").value, page, page_size: document.getElementById("crm-size").value});
    if (document.getElementById("crm-tem-erp").checked) p.set("tem_erp", "1");
    return p;
  };
  const TAGS_INATIVOS_CRM = ["cliente_inativo_4unik", "origem-base-inativa"];
  const load = async () => {
    salvarFiltros();
    const d = await api("/api/contacts?" + params());
    const r = d.resumo || {};
    document.getElementById("crm-tbl").innerHTML = `
      <div class="crm-summary">
        <div><b>${d.total}</b><span>contatos filtrados</span></div><div><b>${r.telefones || 0}</b><span>com telefone</span></div>
        <div><b>${r.historicos || 0}</b><span>com histórico</span></div><div><b>${r.quentes || 0}</b><span>leads quentes</span></div>
        <div><b>${r.frios || 0}</b><span>🧊 leads frios</span></div><div><b>${r.inativos || 0}</b><span>inativos (tags)</span></div>
      </div>
      <div class="lead-grid">${d.contatos.map(x => `<article class="lead-card" onclick="location.hash='#/contato/${encodeURIComponent(x.email)}'">
        <div class="lead-top"><div><h3>${esc(x.nome || x.email.split("@")[0])}</h3><div class="lead-company">${esc(x.empresa || "Empresa não informada")}</div></div>
          <div class="score ${x.lead_score >= 70 ? "hot" : x.lead_score >= 50 ? "warm" : ""}"><b>${x.lead_score || 0}</b><small>/100</small></div></div>
        <div class="lead-contact"><span>✉ ${esc(x.email)}</span><span class="${x.telefone ? "has-phone" : "muted"}">☎ ${esc(x.telefone || "sem telefone")}</span></div>
        <div class="lead-profile"><b>${esc(x.cargo || "Cargo não informado")}</b><span>${esc(x.porte_assinantes || x.porte_funcionarios || x.erp || "")}</span></div>
        <div class="lead-tags">${badge(x.segmento === "frio" ? "🧊 frio" : (x.segmento || "sem segmento"), x.segmento)} ${badge(x.funil)}${
          (x.tags || []).includes("cliente_inativo_4unik") ? ' <span class="tag" title="cliente_inativo_4unik">🚫 cliente inativo</span>' : ""}${
          (x.tags || []).includes("origem-base-inativa") ? ' <span class="tag" title="origem-base-inativa">😴 base inativa</span>' : ""} ${
          (x.tags || []).filter(t => !TAGS_INATIVOS_CRM.includes(t)).slice(0, 3).map(tagChip).join(" ")}</div>
        <div class="lead-history">${esc(x.qualificacao || x.resumo_historico || x.enriquecimento || "Sem histórico comercial")}</div>
      </article>`).join("") || '<div class="card muted">Nenhum lead encontrado.</div>'}</div>
      <div class="pager"><button id="crm-prev" ${d.page <= 1 ? "disabled" : ""}>← Anterior</button><span>Página <b>${d.page}</b> de <b>${d.pages}</b> · ${d.total} resultado(s)</span><button id="crm-next" ${d.page >= d.pages ? "disabled" : ""}>Próxima →</button></div>`;
    document.getElementById("crm-prev").onclick = () => { crmPage--; load(); };
    document.getElementById("crm-next").onclick = () => { crmPage++; load(); };
  };
  let timer;
  const buscaComAtraso = () => { clearTimeout(timer); timer = setTimeout(() => { crmPage = 1; load(); }, 220); };
  document.getElementById("crm-q").oninput = buscaComAtraso;
  document.getElementById("crm-erp").oninput = buscaComAtraso;
  ["crm-funil", "crm-resp", "crm-tag", "crm-perfil", "crm-score", "crm-seg", "crm-tem-erp", "crm-ordem", "crm-size"].forEach(id => document.getElementById(id).onchange = () => { crmPage = 1; load(); });
  const limparFiltros = () => {
    ["crm-q", "crm-funil", "crm-resp", "crm-tag", "crm-perfil", "crm-seg", "crm-erp"].forEach(id => document.getElementById(id).value = "");
    document.getElementById("crm-score").value = "0";
    document.getElementById("crm-tem-erp").checked = false;
  };
  document.getElementById("crm-limpar").onclick = () => { limparFiltros(); crmPage = 1; load(); };
  document.querySelectorAll(".crm-preset").forEach(btn => btn.onclick = () => {
    limparFiltros();
    const filtros = presets[+btn.dataset.preset].filtros;
    for (const [id, valor] of Object.entries(filtros)) {
      if (id === "crm-tem-erp") document.getElementById(id).checked = !!valor;
      else setCampo(id, valor);
    }
    crmPage = 1; load();
  });
  document.getElementById("crm-export").onclick = async () => {
    const res = await fetch("/api/contacts/export.csv?" + params(1), {headers: {"X-API-Key": window.API_KEY}});
    if (!res.ok) return toast("Não foi possível exportar os leads.");
    const blob = await res.blob(), a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = "leads-yoobe.csv"; a.click(); URL.revokeObjectURL(a.href); toast("CSV exportado ✔");
  };
  document.getElementById("crm-up").onchange = async e => {
    const fd = new FormData(); fd.append("file", e.target.files[0]);
    const r = await api("/api/contacts/upload", {method: "POST", body: fd});
    toast(r.erro || `Importado ✔ ${r.novos} novo(s), ${r.atualizados} atualizado(s), ${r.ignorados.length} ignorado(s)`, 6000);
    load();
  };
  await load();
}

async function rodarEstrategia() {
  try {
    const r = await api("/api/strategy/run", {method: "POST"});
    toast(`Estratégia atualizada · ${r.audiencia?.total || 0} contatos analisados`, 3500);
    vInicio();
  } catch (e) { toast("Erro ao atualizar estratégia: " + e.message, 5000); }
}

/* ---------- Drill-down: lista os contatos por trás de um número (modal) ---------- */
async function abrirDetalhe(kind, titulo) {
  const dlg = document.getElementById("dlg-detalhe");
  document.getElementById("det-titulo").textContent = titulo;
  document.getElementById("det-count").textContent = "";
  const body = document.getElementById("det-body");
  body.innerHTML = '<p class="muted">carregando…</p>';
  dlg.showModal();
  try {
    const d = await api(`/api/metrics/breakdown/${kind}`);
    document.getElementById("det-count").textContent = `${d.total} no total`;
    if (!d.itens.length) { body.innerHTML = '<p class="muted">nenhum registro</p>'; return; }
    body.innerHTML = `<table class="det-tbl"><tbody>${d.itens.map(it => {
      const titulo = esc(it.empresa || it.nome || it.email.split("@")[0]);
      const ir = it.is_contato
        ? `onclick="document.getElementById('dlg-detalhe').close();location.hash='#/contato/${encodeURIComponent(it.email)}'"`
        : "";
      const destino = it.url
        ? `<div style="margin-top:4px"><a href="${esc(it.url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()" title="${esc(it.url)}"><small>↗ ${esc(it.url.length > 72 ? it.url.slice(0, 72) + "…" : it.url)}</small></a></div>`
        : "";
      return `<tr class="${it.is_contato ? "click" : ""}" ${ir}>
        <td><b>${titulo}</b><br><small class="muted">${esc(it.email)}</small></td>
        <td class="det-meta">${esc(it.detalhe || "")}${destino}</td>
        <td class="det-go">${it.is_contato ? "›" : ""}</td></tr>`;
    }).join("")}</tbody></table>`;
  } catch (e) { body.innerHTML = `<p class="muted">erro: ${esc(e.message)}</p>`; }
}

/* ---------- TELA: Contato (timeline) ---------- */
async function vContato(email) {
  const enc = encodeURIComponent(email);
  const [d, validacao, campanhas, remetentes] = await Promise.all([
    api(`/api/contacts/${enc}/timeline`),
    api(`/api/contacts/${enc}/validation`),
    api("/api/campaigns"),
    api("/api/senders"),
  ]);
  const c = d.contato;
  const disponiveis = (campanhas.campanhas || []).filter(k => ["rascunho", "pausada", "ativa"].includes(k.status)
    && !d.campanhas.some(v => v.id === k.id));
  const conectados = (remetentes.senders || []).filter(s => s.conectado);
  const campo = (id, label, value, type = "text", extra = "") => `<label class="contact-field"><span>${label}</span>
    <input id="ce-${id}" type="${type}" value="${esc(value || "")}" ${extra}></label>`;
  const checkOk = validacao.checks.filter(x => x.ok).length;
  view.innerHTML = `
    <div class="contact-hero">
      <div><a href="#/crm">← Voltar ao CRM</a><div class="contact-title"><div class="contact-avatar">${esc((c.nome || c.empresa || "?").slice(0, 1).toUpperCase())}</div>
        <div><h2>${esc(c.nome || "Contato sem nome")}</h2><p>${esc(c.cargo || "Cargo não informado")}${c.empresa ? ` · <b>${esc(c.empresa)}</b>` : ""}</p>
        <div>${badge(c.segmento || "sem segmento", c.segmento || "pendente")} ${badge(c.funil || "novo")}</div></div></div></div>
      <div class="contact-actions">
        <button class="primary" onclick="contactCampaignDialog()">✨ Criar campanha individual</button>
        <button onclick="contactToggleEdit(true)">✏️ Editar dados</button>
        ${c.gmail_thread ? `<a class="btn" href="${c.gmail_thread}" target="_blank">📨 Gmail ↗</a>` : ""}
        <button class="warn" id="btn-del-contato">🗑</button>
      </div>
    </div>
    <div class="contact-kpis">
      <div><span>Completude</span><b>${validacao.score}%</b><i><em style="width:${validacao.score}%"></em></i></div>
      <div><span>Score comercial</span><b>${validacao.lead_score}/100</b><small>utilidade para campanha</small></div>
      <div><span>Validações</span><b>${checkOk}/${validacao.checks.length}</b><small>${validacao.validado ? "cadastro consistente" : "requer revisão"}</small></div>
      <div><span>Relacionamento</span><b>${c.envios || 0} envios</b><small>${c.respondeu === "sim" ? "já respondeu" : "sem resposta"}</small></div>
    </div>
    <div class="contact-layout">
      <section>
        <div class="card contact-card"><div class="card-head"><h3>👤 Contato e empresa</h3><button onclick="contactToggleEdit(true)">Editar</button></div>
          <div class="contact-data-grid">
            <div><span>E-mail</span><b>${esc(c.email)}</b></div><div><span>Telefone / WhatsApp</span><b>${esc(c.telefone || "Não informado")}</b></div>
            <div><span>Empresa</span><b>${esc(c.empresa || "Não informada")}</b></div><div><span>Cargo</span><b>${esc(c.cargo || "Não informado")}</b></div>
            <div><span>Porte</span><b>${esc(c.porte_assinantes || c.porte_funcionarios || "Não informado")}</b></div><div><span>ERP / stack</span><b>${esc(c.erp || "Não informado")}</b></div>
          </div></div>
        <div class="card contact-card"><div class="card-head"><h3>🎯 Inteligência comercial</h3><button class="ai-action" onclick="contactEnrich('${esc(c.email)}')">✦ Enriquecer com IA</button></div>
          <div class="contact-data-grid">
            <div><span>Etapa do funil</span><b>${esc(c.etapa_funil || "Não informada")}</b></div><div><span>Estado do negócio</span><b>${esc(c.estado_negocio || "Não informado")}</b></div>
            <div class="wide"><span>Qualificação / interesse</span><b>${esc(c.qualificacao || "Não informada")}</b></div>
            <div class="wide"><span>Motivo de perda</span><b>${esc(c.motivo_perda || "Não informado")}</b></div>
            <div class="wide note"><span>Histórico e observações</span><p>${esc(c.enriquecimento || "Sem observações registradas.")}</p></div>
          </div><div id="contact-enrich-result"></div></div>
        <div class="card contact-card"><h3>📬 Trilha de relacionamento</h3>
        <div class="timeline">
        ${d.eventos.map(e => e.tipo === "envio" ? `
          <div class="tl-item"><b>📤 ${esc(e.etapa)}</b> <small>${esc(e.data)}</small><br>
            <a href="${e.gmail}" target="_blank"><small>ver no Gmail ↗</small></a></div>` : `
          <div class="tl-item resposta"><b>📩 Respondeu</b> ${badge(e.classe || "?")}
            <small>${esc(e.data)} · confiança ${Math.round((e.confianca || 0) * 100)}%</small><br>
            ${esc(e.resumo)}<br><a href="${e.gmail}" target="_blank"><small>ver no Gmail ↗</small></a></div>`).join("")
          || '<span class="muted">nenhum evento registrado</span>'}
        </div></div>
      </section>
      <aside>
        <div class="card contact-card"><h3>✅ Validação dos dados</h3>
          <div class="validation-list">${validacao.checks.map(x => `<div class="${x.ok ? "ok" : "warn"}"><i>${x.ok ? "✓" : "!"}</i><span><b>${esc(x.label)}</b><small>${esc(x.detail)}</small></span></div>`).join("")}</div>
          <p class="microcopy">${esc(validacao.nota)}</p></div>
        <div class="card contact-card"><h3>🏷️ Categorias e tags</h3><div id="tag-editor"></div></div>
        <div class="card contact-card"><div class="card-head"><h3>🚀 Campanhas</h3></div>
          <div class="campaign-mini-list">${d.campanhas.map(k => `<a href="#/campanha/${k.id}"><b>${esc(k.nome)}</b><span>${badge(k.status)} · etapa ${k.etapa_atual}${k.next_send_at ? " · " + fmtDt(k.next_send_at) : ""}</span></a>`).join("") || '<span class="muted">Ainda não participa de campanhas.</span>'}</div>
          <label class="contact-field compact"><span>Adicionar a uma campanha</span><select id="contact-campaign-select"><option value="">Selecione…</option>${disponiveis.map(k => `<option value="${k.id}" data-status="${esc(k.status)}">${esc(k.nome)} · ${esc(k.status)}</option>`).join("")}</select></label>
          <button class="contact-wide-btn" onclick="contactAddCampaign('${esc(c.email)}')" ${disponiveis.length ? "" : "disabled"}>＋ Adicionar sem ativar</button>
          <button class="primary contact-wide-btn" onclick="contactLaunchNow('${esc(c.email)}')" ${disponiveis.length ? "" : "disabled"}>⚡ Adicionar e disparar agora</button>
          <p class="microcopy">Envia a 1ª etapa imediatamente para este lead e inicia a campanha. Confirmação obrigatória; bloqueios de descadastro, resposta, cliente e limite de contato continuam ativos.</p>
          <button class="primary contact-wide-btn" onclick="contactCampaignDialog()">✨ Gerar campanha só para este lead</button></div>
      </aside>
    </div>
    <div id="contact-edit-panel" class="contact-panel" aria-hidden="true"><div class="contact-panel-inner">
      <div class="card-head"><div><small>ATUALIZAR CADASTRO</small><h2>Editar contato</h2></div><button onclick="contactToggleEdit(false)">✕</button></div>
      <form id="contact-edit-form"><div class="contact-form-grid">
        ${campo("nome", "Nome", c.nome)}${campo("empresa", "Empresa", c.empresa)}${campo("telefone", "Telefone / WhatsApp", c.telefone)}${campo("cargo", "Cargo / persona", c.cargo)}
        <label class="contact-field"><span>Segmento</span><select id="ce-segmento"><option value="">Sem segmento</option>${["quente","morno","frio"].map(x => `<option ${c.segmento === x ? "selected" : ""}>${x}</option>`).join("")}</select></label>
        ${campo("porte_funcionarios", "Porte / funcionários", c.porte_funcionarios || c.porte_assinantes)}${campo("erp", "ERP / stack", c.erp)}${campo("etapa_funil", "Etapa do funil", c.etapa_funil)}${campo("estado_negocio", "Estado do negócio", c.estado_negocio)}
        <label class="contact-field wide"><span>Qualificação / interesse</span><textarea id="ce-qualificacao" rows="3">${esc(c.qualificacao || "")}</textarea></label>
        <label class="contact-field wide"><span>Motivo de perda</span><textarea id="ce-motivo_perda" rows="2">${esc(c.motivo_perda || "")}</textarea></label>
        ${campo("modulos", "Módulos", c.modulos)}${campo("mensalidades", "Mensalidades / valores", c.mensalidades)}${campo("validade", "Validade da proposta", c.validade)}
        <label class="contact-field wide"><span>Histórico e observações comerciais</span><textarea id="ce-enriquecimento" rows="6">${esc(c.enriquecimento || "")}</textarea></label>
      </div><div class="panel-actions"><button type="button" onclick="contactToggleEdit(false)">Cancelar</button><button class="primary" type="submit">💾 Salvar alterações</button></div></form>
    </div></div>
    <dialog id="contact-campaign-dialog"><h2>✨ Campanha individual</h2><p class="muted">A IA usará os dados deste contato. A campanha nasce em rascunho e não será enviada automaticamente.</p>
      <label class="contact-field"><span>Objetivo</span><textarea id="cci-objetivo" rows="4">Retomar a conversa com ${esc(c.nome || "este contato")} da ${esc(c.empresa || "empresa")}, usando o histórico e o interesse registrado no CRM, para gerar uma nova oportunidade comercial.</textarea></label>
      <div class="contact-form-grid">${campo("cci-n", "Quantidade de e-mails", "3", "number", 'min="1" max="5"')}
        <label class="contact-field"><span>Remetente</span><select id="cci-sender">${conectados.map(s => `<option value="${s.id}">${esc(s.email)}</option>`).join("")}</select></label></div>
      <div class="panel-actions"><button onclick="document.getElementById('contact-campaign-dialog').close()">Cancelar</button><button class="primary" onclick="contactCreateCampaign('${esc(c.email)}')" ${conectados.length ? "" : "disabled"}>Criar rascunho com IA</button></div></dialog>`;
  renderTagEditor(c.email, c.tags || []);
  document.getElementById("contact-edit-form").onsubmit = ev => contactSave(ev, c.email);
  const bd = document.getElementById("btn-del-contato");
  if (bd) bd.onclick = () => excluirContato(c.email, c.nome || "", c.empresa || "");
}

/* Exclui o contato (e suas referências) — confirmação simples. */
async function excluirContato(email, nome, empresa) {
  if (!confirm(`Excluir o contato "${nome || email}"${empresa ? " (" + empresa + ")" : ""}?\n\n`
      + `Isso remove o contato do CRM, de todas as campanhas e seu histórico. Não dá pra desfazer.`)) return;
  try {
    await api(`/api/contacts/${encodeURIComponent(email)}`, {method: "DELETE"});
    toast("Contato excluído ✔", 2000);
    location.hash = "#/crm";
  } catch (e) { toast("Erro: " + e.message, 4000); }
}

function contactToggleEdit(open) {
  const p = document.getElementById("contact-edit-panel"); if (!p) return;
  p.classList.toggle("open", open); p.setAttribute("aria-hidden", open ? "false" : "true");
}
async function contactSave(ev, email) {
  ev.preventDefault();
  const ids = ["nome","empresa","telefone","cargo","segmento","porte_funcionarios","erp","etapa_funil","estado_negocio","qualificacao","motivo_perda","modulos","mensalidades","validade","enriquecimento"];
  const body = Object.fromEntries(ids.map(id => [id, document.getElementById("ce-" + id)?.value.trim() || ""]));
  try { await api(`/api/contacts/${encodeURIComponent(email)}/edit`, {json: body}); toast("Contato atualizado ✔"); await vContato(email); }
  catch (e) { toast("Não foi possível salvar: " + e.message, 5000); }
}
async function contactEnrich(email) {
  const box = document.getElementById("contact-enrich-result");
  if (!confirm("Pesquisar informações profissionais públicas sobre este contato?\n\nA IA apenas sugerirá alterações. Nada será gravado sem sua revisão.")) return;
  box.innerHTML = '<div class="enrich-box"><div class="spinner"></div><b> Pesquisando fontes públicas…</b></div>';
  try {
    const r = await api(`/api/contacts/${encodeURIComponent(email)}/enrich`, {json:{confirmar_pesquisa_publica:true}});
    const entries = Object.entries(r.sugestoes || {});
    box.innerHTML = `<div class="enrich-box"><div class="card-head"><div><b>✦ Sugestões da IA</b><small>${Math.round((r.confianca || 0)*100)}% de confiança · nada aplicado</small></div></div>
      <p>${esc(r.resumo || "Pesquisa concluída.")}</p>${entries.length ? `<div class="suggestion-list">${entries.map(([k,v]) => `<label><input type="checkbox" class="enrich-pick" data-field="${esc(k)}" data-value="${esc(v)}" checked><span><b>${esc(k.replaceAll("_"," "))}</b>${esc(v)}</span></label>`).join("")}</div>
      <button class="primary" type="button" id="contact-enrich-apply" data-email="${esc(email)}">Revisar e aplicar selecionados</button>` : '<p class="muted">Nenhum dado novo confiável foi encontrado.</p>'}
      ${(r.fontes || []).length ? `<details><summary>Fontes consultadas (${r.fontes.length})</summary>${r.fontes.map(f => `<p><a href="${esc(f.url)}" target="_blank">${esc(f.titulo || f.url)} ↗</a><br><small>${esc(f.evidencia || "")}</small></p>`).join("")}</details>` : ""}</div>`;
    const applyBtn = document.getElementById("contact-enrich-apply");
    if (applyBtn) applyBtn.onclick = () => contactApplyEnrichment(applyBtn.dataset.email || email);
  } catch(e) { box.innerHTML = `<div class="enrich-box error"><b>Enriquecimento não concluído</b><p>${esc(e.message)}</p></div>`; }
}
function contactApplyEnrichment(email) {
  const picks = [...document.querySelectorAll(".enrich-pick:checked")];
  if (!picks.length) return toast("Selecione pelo menos uma sugestão.");
  contactToggleEdit(true);
  picks.forEach(p => {
    const field = p.dataset.field || "";
    const el = document.getElementById("ce-" + field);
    if (el) el.value = p.dataset.value || "";
  });
  toast("Sugestões colocadas no formulário. Revise e clique em Salvar.", 5000);
}
async function contactAddCampaign(email) {
  const id = +document.getElementById("contact-campaign-select").value; if (!id) return toast("Selecione uma campanha.");
  if (document.querySelector("#contact-campaign-select option:checked")?.dataset.status === "ativa")
    return toast("Esta campanha já está ativa. Use ‘Adicionar e disparar agora’.", 5000);
  try { const r = await api(`/api/contacts/${encodeURIComponent(email)}/campaigns`, {json:{campaign_id:id}}); toast(r.ja_vinculado ? "Contato já estava nessa campanha." : "Contato adicionado à campanha, sem ativar ✔", 4000); vContato(email); }
  catch(e) { toast("Não foi possível adicionar: " + e.message, 5000); }
}
async function abrirSeoGeo() {
  try {
    const r = await api("/api/seo-geo/status");
    const pri = (r.prioridades || []).map(x => `<article class="seo-geo-item"><b>${esc(x.prioridade || "prioridade").toUpperCase()} · ${esc(x.tipo || "")}</b><h4>${esc(x.pagina || "site")}</h4><p>${esc(x.acao || "")}</p><small>${esc(x.evidencia || "")}</small></article>`).join("") || '<p class="muted">Ainda não há prioridades; execute o ciclo após a próxima atualização de métricas.</p>';
    const pauta = (r.pauta || []).map(x => `<article class="seo-geo-item"><b>PAUTA SUGERIDA</b><h4>${esc(x.titulo)}</h4><p>${esc(x.checklist_geo)}</p><small>${esc(x.status)}</small></article>`).join("");
    const grokRecs = (r.recomendacoes_grok || []).map(x => `<article class="seo-geo-item"><b>${esc((x.prioridade || "média").toUpperCase())} · GROK</b><h4>${esc(x.titulo || "recomendação")}</h4><p>${esc(x.acao || "")}</p><small>${esc(x.evidencia || "")}${x.via ? " · via " + esc(x.via) : ""}</small></article>`).join("");
    view.innerHTML = `<div class="toolbar"><div><a href="#/inicio">← Voltar ao início</a><h2 style="margin:6px 0 0">Loop contínuo SEO + GEO</h2></div><span class="sp"></span><button class="primary" onclick="rodarSeoGeo()">↻ Atualizar análise</button></div>
      <div class="card"><p>${esc(r.guardrail || "")}</p><div class="strategy-signals"><span>👀 ${r.metricas?.sessoes ?? 0} sessões</span><span>🌿 ${r.metricas?.sessoes_organicas ?? 0} orgânicas</span><span>🎯 ${r.metricas?.leads ?? 0} eventos de lead</span>${(r.recomendacoes_grok || []).length ? `<span>✦ ${(r.recomendacoes_grok || []).length} Grok</span>` : ""}</div>
      ${(r.erros || []).length ? `<p class="microcopy">⚠ ${esc(r.erros.join(" · "))}</p>` : ""}</div>
      <section class="seo-geo-grid"><div class="card"><h3>Prioridades com evidência</h3>${pri}</div><div class="card"><h3>Pauta GEO para revisão</h3>${pauta || '<p class="muted">Pauta disponível no próximo ciclo.</p>'}</div></section>
      ${grokRecs ? `<div class="card" style="margin-top:12px"><h3>Crítica Grok (revisão humana)</h3>${grokRecs}</div>` : ""}`;
  } catch (e) { toast("Não foi possível abrir o loop SEO/GEO: " + e.message, 6000); }
}
async function rodarSeoGeo(voltarInicio = false) {
  try {
    const r = await api("/api/seo-geo/run", {method: "POST"});
    toast(`SEO/GEO atualizado · ${(r.prioridades || []).length} prioridade(s)`, 4000);
    if (voltarInicio || location.hash === "" || location.hash.startsWith("#/inicio")) vInicio();
    else abrirSeoGeo();
  } catch (e) { toast("Não foi possível atualizar: " + e.message, 6000); }
}
async function contactLaunchNow(email, confirmacao = "") {
  const id = +document.getElementById("contact-campaign-select").value;
  if (!id) return toast("Selecione uma campanha.");
  const nome = document.querySelector("#contact-campaign-select option:checked")?.textContent || "esta campanha";
  if (!confirmacao && !confirm(`Disparar o primeiro e-mail agora para este lead?\n\nIsso adiciona o lead, ativa ${nome} se necessário e envia a etapa 1 imediatamente. Os próximos e-mails seguem a cadência configurada.`)) return;
  try {
    const r = await api(`/api/contacts/${encodeURIComponent(email)}/campaigns/launch-now`, {json:{campaign_id:id, confirmacao}});
    toast(r.enviado ? "E-mail inicial enviado agora ✔" : r.nota, 6000);
    await vContato(email);
  } catch (e) {
    if (e.data?.requer_confirmacao) {
      const t = prompt(`Esta campanha inclui ${e.data.clientes} cliente(s) Yoobe. Para disparar, digite QUERO_ENVIAR.`, "");
      if (t?.trim() === "QUERO_ENVIAR") return contactLaunchNow(email, "QUERO_ENVIAR");
      return toast("Disparo cancelado — clientes protegidos.", 5000);
    }
    toast("Não foi possível disparar: " + e.message, 6000);
  }
}
function contactCampaignDialog() { document.getElementById("contact-campaign-dialog")?.showModal(); }
async function contactCreateCampaign(email) {
  const objetivo = document.getElementById("cci-objetivo").value.trim(), sender_id = +document.getElementById("cci-sender").value;
  const n_emails = +document.getElementById("ce-cci-n").value || 3;
  if (!objetivo) return toast("Informe o objetivo da campanha.");
  document.getElementById("contact-campaign-dialog").close(); toast("Criando campanha individual em rascunho…", 3500);
  try { const r = await api(`/api/contacts/${encodeURIComponent(email)}/campaign-ai`, {json:{objetivo,n_emails,sender_id}}); location.hash = "#/campanha/" + r.id; route(); }
  catch(e) { toast("Campanha não criada: " + e.message, 6000); }
}

/* Editor de tags: chips removíveis + input para adicionar (Enter ou vírgula) */
function renderTagEditor(email, tags) {
  const box = document.getElementById("tag-editor");
  if (!box) return;
  box.innerHTML = `
    <div class="tag-edit">
      ${tags.map(t => `<span class="tag removable">${esc(t)}<b data-tag="${esc(t)}">×</b></span>`).join("")}
      <input id="tag-input" placeholder="+ tag" autocomplete="off">
    </div>
    <small class="muted">Enter para adicionar · clique no × para remover</small>`;
  const save = async (next) => {
    try {
      const r = await api(`/api/contacts/${encodeURIComponent(email)}/tags`, {json: {tags: next}});
      renderTagEditor(email, r.tags);
      toast("Tags atualizadas ✔", 1800);
    } catch (e) { toast("Erro: " + e.message, 4000); }
  };
  box.querySelectorAll("b[data-tag]").forEach(b => b.onclick = () =>
    save(tags.filter(t => t !== b.dataset.tag)));
  const inp = document.getElementById("tag-input");
  inp.onkeydown = (e) => {
    if (e.key === "Enter" || e.key === ",") {
      e.preventDefault();
      const v = inp.value.trim();
      if (v && !tags.includes(v)) save([...tags, v]);
      else inp.value = "";
    }
  };
  inp.focus();
}

/* ---------- TELA: Templates ---------- */
let tplSel = null;
async function vTemplates() {
  const d = await api("/api/templates");
  const grupos = {};
  d.templates.forEach(t => (grupos[t.grupo] = grupos[t.grupo] || []).push(t));
  view.innerHTML = `
    <div class="toolbar"><h2 style="margin:0">Templates</h2><span class="sp"></span>
      <input id="tpl-q" placeholder="🔎 buscar" style="width:200px">
      <button class="primary" onclick="document.getElementById('dlg-template').showModal()">✨ Novo com IA</button></div>
    <div class="row">
      <div class="card tpl-list" style="width:360px" id="tpl-list"></div>
      <div class="grow"><div class="card" id="tpl-head" style="display:none"></div>
        <iframe class="preview" id="tpl-frame" sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox" referrerpolicy="no-referrer"></iframe></div>
    </div>`;
  const render = q => {
    document.getElementById("tpl-list").innerHTML = Object.entries(grupos).map(([g, ts]) => `
      <div class="grp">${esc(g)}</div>` + ts.filter(t => !q || (t.nome + t.assunto).toLowerCase().includes(q))
      .map(t => `<div class="tpl-item ${tplSel === t.ref ? "on" : ""}" onclick="tplOpen('${t.ref}')">
        <b>${esc(t.nome)}</b> ${badge(t.status)}<small>${esc(t.cadencia || t.cta || "")}</small></div>`).join("")).join("");
  };
  document.getElementById("tpl-q").oninput = e => render(e.target.value.toLowerCase());
  render("");
  if (d.templates.length) tplOpen(tplSel || d.templates[0].ref);
}
async function tplOpen(ref) {
  tplSel = ref;
  const d = await api("/api/templates");
  const t = d.templates.find(x => x.ref === ref);
  const head = document.getElementById("tpl-head");
  if (head && t) {
    head.style.display = "block";
    head.innerHTML = `<b>${esc(t.assunto)}</b> ${badge(t.status)}
      <button class="primary" style="margin-left:10px" onclick="dlgCampanha('${t.ref}')">📨 Criar campanha com este template</button>
      ${t.ref.startsWith("ai:") ? `<button style="margin-left:6px" onclick="editarTplGeral('${t.ref}')">✏️ Editar template</button>` : ""}
      ${t.status === "pendente" ? `<button class="green" style="margin-left:6px" onclick="aprovarTpl('${t.ref}')">✓ Aprovar template</button>` : ""}
      <br><small class="muted">${esc(t.ref)} · CTA: ${esc(t.cta || "—")} · variáveis {{nome}} {{empresa}} {{modulo}} resolvidas no envio</small>`;
  }
  document.querySelectorAll(".tpl-item").forEach(el => el.classList.toggle("on", el.getAttribute("onclick").includes(`'${ref}'`)));
  const meta = ref.startsWith("ai:") ? await api(`/api/templates/${ref.slice(3)}/raw`).catch(() => null) : null;
  const html = await api(`/api/templates/preview?ref=${encodeURIComponent(ref)}&bh=${_tplHash(meta?.inner_html || ref)}&_=${Date.now()}`);
  setPreviewIframe(document.getElementById("tpl-frame"), html);
}

async function editarTplGeral(ref) {
  if (!ref.startsWith("ai:")) return;
  const t = await api(`/api/templates/${ref.slice(3)}/raw`);
  const head = document.getElementById("tpl-head");
  if (!head) return;
  window._tplEditorOrig = {inner_html: t.inner_html || "", texto_plano: t.texto_plano || "", assunto: t.assunto || ""};
  const f = (id, label, value) => `<label style="display:block;margin:8px 0 2px;font-size:13px;font-weight:700">${label}</label><input id="tg-${id}" value="${esc(value || "")}" style="width:100%">`;
  head.innerHTML = `<b>✏️ Editando template</b> <small class="muted">Salvar devolve o template para pendente; aprove-o novamente antes de usar em uma campanha.</small>
    ${f("nome", "Nome interno", t.nome)}${f("assunto", "Assunto", t.assunto)}${f("preheader", "Preheader", t.preheader)}${f("cta", "CTA", t.cta)}${f("cta_url", "URL do CTA", t.cta_url)}
    <label style="display:block;margin:8px 0 2px;font-size:13px;font-weight:700">Corpo do e-mail <span style="color:var(--gold-bright)">(é o que o preview mostra)</span></label>
    <div class="tpl-body-toolbar">
      <button type="button" data-tg-mode="visual">Visual</button>
      <button type="button" data-tg-mode="html" class="primary">HTML</button>
      <button type="button" id="tg-format-html">Indentar HTML</button>
      <button type="button" id="tg-sync">Texto simples → HTML</button>
    </div>
    <div id="tg-visual" class="tpl-visual-editor" contenteditable="true" spellcheck="true" hidden></div>
    <textarea id="tg-inner_html" class="tpl-html-editor" rows="16" spellcheck="false"></textarea>
    <label style="display:block;margin:8px 0 2px;font-size:13px;font-weight:700">Texto simples <span class="muted">(sozinho NÃO muda o preview)</span></label><textarea id="tg-texto_plano" rows="4" style="width:100%"></textarea>
    <div style="margin-top:10px">
      <button class="green" id="tg-save">💾 Salvar template</button><button id="tg-cancel">Cancelar</button></div>`;
  document.getElementById("tg-cancel").onclick = () => tplOpen(ref);
  document.getElementById("tg-texto_plano").value = t.texto_plano || "";
  const bodyEd = _tplMountBodyEditor("tg", t.inner_html || "");
  window._tplBodyEditor = bodyEd;
  document.getElementById("tg-sync").onclick = () => {
    const texto = document.getElementById("tg-texto_plano");
    bodyEd.setHtml(_tplTextoParaInner(texto.value, window._tplEditorOrig?.inner_html || bodyEd.getHtml()));
    toast("Texto simples aplicado no HTML", 3000);
  };
  document.getElementById("tg-save").onclick = async () => {
    const v = k => document.getElementById("tg-" + k).value;
    try {
      const payload = {nome:v("nome"), assunto:v("assunto"), preheader:v("preheader"), cta:v("cta"), cta_url:v("cta_url"),
                       inner_html: bodyEd.getHtml(), texto_plano:v("texto_plano")};
      const orig = window._tplEditorOrig || {};
      const normHtml = s => String(s || "").replace(/>\s+</g, "><").replace(/\s+/g, " ").trim();
      if ((payload.texto_plano || "") !== (orig.texto_plano || "")
          && normHtml(payload.inner_html) === normHtml(orig.inner_html)) {
        payload.inner_html = _tplTextoParaInner(payload.texto_plano, orig.inner_html || payload.inner_html);
        bodyEd.setHtml(payload.inner_html);
      }
      await api(`/api/templates/${ref.slice(3)}`, {json: payload});
      const check = await api(`/api/templates/${ref.slice(3)}/raw`);
      if ((check.assunto || "") !== (payload.assunto || "")
          || (check.inner_html || "") !== (payload.inner_html || "")) {
        throw new Error("o servidor não gravou o corpo/assunto enviados");
      }
      toast("Template salvo — revise e aprove antes de enviar ✔", 4000);
      window._tplEditorOrig = null;
      window._tplBodyEditor = null;
      tplSel = ref;
      await vTemplates();
      await tplOpen(ref);
    } catch (e) { toast("Erro ao salvar: " + e.message, 5000); }
  };
}
document.getElementById("nt-criar").onclick = async () => {
  const briefing = document.getElementById("nt-briefing").value.trim();
  if (!briefing) return toast("Escreva o briefing.");
  document.getElementById("dlg-template").close();
  toast("🧠 IA redigindo o template… (até 2 min)", 12000);
  try { const r = await api("/api/templates/ai-create", {json: {briefing}});
    toast("Template criado (pendente de aprovação) ✔"); tplSel = r.ref; vTemplates();
  } catch (e) { toast("Erro na IA: " + e.message, 6000); }
};

/* ---------- TELA: Métricas ---------- */
async function vMetricas() {
  const [s, f, q] = await Promise.all([api("/api/metrics/summary"), api("/api/metrics/funnel"), api("/api/queue/status")]);
  const maxF = Math.max(...f.funil.map(x => x.contatos), 1);
  view.innerHTML = `
    <h2>Métricas</h2>
    <div class="kpis">
      <div class="kpi"><b>${s.total_contatos}</b><small>contatos na base</small></div>
      <div class="kpi"><b>${s.total_enviados}</b><small>emails enviados</small></div>
      <div class="kpi"><b>${s.contatos_que_responderam}</b><small>contatos responderam</small></div>
      <div class="kpi"><b>${s.taxa_resposta}%</b><small>taxa de resposta</small></div>
      <div class="kpi"><b>${q.fila.pending || 0}</b><small>itens na fila</small></div>
    </div>
    <div class="row" style="margin-top:16px">
      <div class="card grow"><h3>Funil da base</h3><div class="funnel">
        ${f.funil.map(x => frow(x.estagio, x.contatos, maxF)).join("")}</div></div>
      <div class="card grow"><h3>Respostas por classificação</h3><div class="funnel">
        ${Object.entries(s.respostas_por_classe).map(([k, n]) => frow(k, n, s.total_respostas || 1)).join("") || '<span class="muted">sem respostas classificadas</span>'}</div>
        <h3 style="margin-top:14px">Base por segmento</h3><div class="funnel">
        ${Object.entries(s.contatos_por_segmento).map(([k, n]) => frow(k, n, s.total_contatos)).join("")}</div></div>
      <div class="card grow"><h3>Envios por campanha</h3>
        <table><tr><th>Campanha</th><th>Status</th><th>Enviados</th></tr>
        ${s.envios_por_campanha.map(c => `<tr><td>${esc(c.nome)}</td><td>${badge(c.status)}</td><td>${c.enviados}</td></tr>`).join("")}</table>
        <h3 style="margin-top:14px">Série mensal de envios</h3><div class="funnel">
        ${s.serie_mensal.map(m => frow(m.mes, m.envios, Math.max(...s.serie_mensal.map(x => x.envios), 1))).join("")}</div></div>
    </div>
    <div class="card"><h3>Fila & resiliência</h3>
      <p>Status: <b>${q.online ? "🟢 online" : "🔴 offline"}</b> · último ciclo ok:
      <b>${esc(q.last_successful_run || "—")}</b> · último sync: <b>${esc(q.last_sync || "—")}</b>
      <button style="margin-left:10px" onclick="api('/api/queue/run',{method:'POST'}).then(r=>{toast('Ciclo: '+JSON.stringify(r));vMetricas()})">▶ Rodar ciclo agora</button>
      <button onclick="api('/api/sync',{method:'POST'}).then(r=>{toast('Sync ✔');vMetricas()})">🔄 Sync local</button></p>
      <table><tr><th>#</th><th>Tipo</th><th>Status</th><th>Tent.</th><th>Agendado</th><th>Nota/Erro</th></tr>
      ${q.ultimos_itens.map(i => `<tr><td>${i.id}</td><td>${esc(i.tipo)}</td><td>${badge(i.status, i.status === "done" ? "aprovado" : i.status === "failed" ? "encerrado" : "pendente")}</td>
        <td>${i.attempts}</td><td>${fmtDt(i.run_at)}</td><td><small>${esc((i.error || "").slice(0, 90))}</small></td></tr>`).join("")}</table></div>`;
}

/* ---------- TELA: Mídia & Google ---------- */
async function vMidia() {
  const s = await api("/api/google-media/status");
  const pill = (ok, label) => `<span class="${ok ? "media-ok" : "media-off"}">${ok ? "●" : "○"} ${label}</span>`;
  view.innerHTML = `
    <div class="toolbar"><h2 style="margin:0">Mídia & Google</h2><span class="sp"></span>
      <button id="gm-discover">⌕ Encontrar GA4</button><button id="gm-discover-gtm">⌕ Encontrar GTM</button><button class="primary" id="gm-sync">↻ Sincronizar métricas</button></div>
    <p class="muted">Consolida dados de Analytics, Tag Manager e Ads para acompanhar campanhas. A integração é <b>somente leitura</b>: não publica tags, nem cria ou altera anúncios.</p>
    <div class="media-health">${pill(s.ga4_pronto, "GA4")} ${pill(s.gtm_pronto, "Tag Manager")} ${pill(s.ads_pronto, "Google Ads")}</div>
    <div class="card" id="gm-result"><div class="media-connected">
      <div><b>GA4 conectado</b><br><small class="muted">Conta ${esc(s.ga4?.account_id || "—")} · Property ${esc(s.ga4?.property_id || "—")} · corporate.yoobe.co ${esc(s.ga4?.measurement_id || "a selecionar")} · catalogo.yoobe.co ${esc(s.ga4?.site_measurement_id || "não informado")}</small></div>
      <div><button id="gm-goals">🎯 Configurar eventos-chave de aquisição</button> <button class="primary" id="gm-sync-top">↻ Atualizar agora</button></div></div>
      <p class="muted">Eventos-chave oficiais: <code>generate_lead</code>, <code>schedule_demo</code>, <code>contact_whatsapp</code> (+55 41 98760-7512) e <code>contact_email</code> (corporate@yoobe.co).</p>
      <div id="gm-metrics"><span class="muted">Carregando métricas salvas…</span></div></div>
    <div class="row">
      <div class="card grow"><h3>Configuração</h3>
        <p class="muted">Credenciais já gravadas não são exibidas. Deixe um campo vazio para mantê-la.</p>
        <div class="field"><label>GA4 Account ID</label><input id="gm-ga4-account" value="${esc(s.ga4?.account_id || "")}" placeholder="ex.: 66932658" style="width:100%"></div>
        <div class="field"><label>GA4 Property ID</label><input id="gm-ga4" value="${esc(s.ga4?.property_id || "")}" placeholder="ex.: 327916606" style="width:100%"></div>
        <div class="field"><label>GA4 Measurement ID — corporate.yoobe.co</label><input id="gm-measurement" value="${esc(s.ga4?.measurement_id || "")}" placeholder="ex.: G-XXXXXXXXXX" style="width:100%"><small class="muted">Stream da plataforma; configure a tag no GTM.</small></div>
        <div class="field"><label>GA4 Measurement ID — catalogo.yoobe.co</label><input id="gm-site-measurement" value="${esc(s.ga4?.site_measurement_id || "")}" placeholder="ex.: G-XXXXXXXXXX" style="width:100%"></div>
        <div class="field"><label>Google tag ID</label><input id="gm-google-tag" value="${esc(s.tags?.google_tag_id || "")}" placeholder="ex.: GT-XXXXXXX" style="width:100%"><small class="muted">Identificação de tag do site; não substitui o Container ID do GTM.</small></div>
        <div class="field"><label>Google Ads tag ID</label><input id="gm-ads-tag" value="${esc(s.tags?.ads_tag_id || "")}" placeholder="ex.: AW-XXXXXXXXX" style="width:100%"><small class="muted">Tag de conversão; não substitui Customer ID nem Developer Token do Ads.</small></div>
        <div class="field"><label>GTM Account ID</label><input id="gm-gtm-account" placeholder="ex.: 12345678" style="width:100%"></div>
        <div class="field"><label>GTM Container ID</label><input id="gm-gtm-container" placeholder="ex.: 12345678" style="width:100%"></div>
        <div class="field"><label>Google Ads Customer ID</label><input id="gm-ads-customer" placeholder="ex.: 123-456-7890" style="width:100%"></div>
        <div class="field"><label>Login Customer ID (MCC, se houver)</label><input id="gm-ads-login" placeholder="opcional" style="width:100%"></div>
        <div class="field"><label>Developer Token Google Ads</label><input id="gm-ads-token" type="password" placeholder="já cadastrado fica oculto" style="width:100%"></div>
        <button class="primary" id="gm-save">Salvar integração</button>
      </div>
      <div class="card grow"><h3>UTM para campanhas</h3>
        <p class="muted">Use esta URL nos anúncios; GA4 atribui sessões e conversões à campanha.</p>
        <div class="field"><label>URL de destino</label><input id="gm-url" placeholder="https://corporate.yoobe.co/..." style="width:100%"></div>
        <div class="row"><div class="field grow"><label>Campanha</label><input id="gm-campaign" placeholder="ex.: demo-julho-2026" style="width:100%"></div>
          <div class="field"><label>Origem</label><input id="gm-source" value="google" style="width:110px"></div>
          <div class="field"><label>Mídia</label><input id="gm-medium" value="cpc" style="width:90px"></div></div>
        <button id="gm-utm">Gerar URL</button><div class="utm-output" id="gm-utm-out">—</div>
        <h3 style="margin-top:22px">Autorização OAuth</h3>
        <p class="muted">A credencial atual precisa ser reautorizada com escopos de mídia. Rode no Terminal e conclua no navegador:</p>
        <code class="media-command">${esc(s.oauth_comando)}</code>
        <p class="muted" style="margin-top:10px">${esc(s.ads_nota || "")}</p>
      </div>
    </div>`;
  const body = () => ({
    GOOGLE_GA4_ACCOUNT_ID: document.getElementById("gm-ga4-account").value.trim(),
    GOOGLE_GA4_PROPERTY_ID: document.getElementById("gm-ga4").value.trim(),
    GOOGLE_GA4_MEASUREMENT_ID: document.getElementById("gm-measurement").value.trim(),
    GOOGLE_GA4_SITE_MEASUREMENT_ID: document.getElementById("gm-site-measurement").value.trim(),
    GOOGLE_TAG_ID: document.getElementById("gm-google-tag").value.trim(),
    GOOGLE_ADS_TAG_ID: document.getElementById("gm-ads-tag").value.trim(),
    GOOGLE_GTM_ACCOUNT_ID: document.getElementById("gm-gtm-account").value.trim(),
    GOOGLE_GTM_CONTAINER_ID: document.getElementById("gm-gtm-container").value.trim(),
    GOOGLE_ADS_CUSTOMER_ID: document.getElementById("gm-ads-customer").value.trim(),
    GOOGLE_ADS_LOGIN_CUSTOMER_ID: document.getElementById("gm-ads-login").value.trim(),
    GOOGLE_ADS_DEVELOPER_TOKEN: document.getElementById("gm-ads-token").value.trim(),
  });
  document.getElementById("gm-save").onclick = async () => {
    try { await api("/api/google-media/config", {json: body()}); toast("Configuração salva ✔", 2500); vMidia(); }
    catch (e) { toast("Erro: " + e.message, 5000); }
  };
  document.getElementById("gm-sync").onclick = async () => {
    const box = document.getElementById("gm-result"); box.innerHTML = '<span class="muted">sincronizando…</span>';
    try {
      const r = await api("/api/google-media/sync", {method: "POST"});
      const block = (name, x) => x ? `<div class="media-metric"><b>${name}</b><br>${Object.entries(x).filter(([k]) => k !== "dias").map(([k,v]) => `${esc(k)}: <strong>${esc(v)}</strong>`).join(" · ")}</div>` : "";
      const campanhas = await api("/api/site/campanhas").catch(() => ({campanhas: []}));
      const funil = (campanhas.campanhas || []).map(x => `<tr><td>${esc(x.nome)}<br><small class="muted">${esc(x.slug)}</small></td><td>${x.enviados}</td><td>${x.cliques}</td><td>${x.sessoes}</td><td>${x.key_events}</td></tr>`).join("");
      box.innerHTML = `${block("GA4", r.ga4)}${block("Tag Manager", r.gtm)}${block("Google Ads", r.ads)}${Object.entries(r.erros || {}).map(([k,v]) => `<p class="form-feedback error">${esc(k)}: ${esc(v)}</p>`).join("") || (!r.ga4 && !r.gtm && !r.ads ? '<span class="muted">Faltam IDs ou escopos para sincronizar.</span>' : "")}
        <div class="media-funnel"><h3>Campanhas × GA4</h3><p class="muted">UTMs são adicionadas automaticamente aos links de campanhas de e-mail.</p><table><tr><th>Campanha</th><th>Enviados</th><th>Cliques</th><th>Sessões</th><th>Key events</th></tr>${funil || '<tr><td colspan="5" class="muted">Sem campanhas com dados no período.</td></tr>'}</table></div>`;
    } catch (e) { box.innerHTML = `<span class="form-feedback error">${esc(e.message)}</span>`; }
  };
  document.getElementById("gm-sync-top").onclick = () => document.getElementById("gm-sync").click();
  document.getElementById("gm-goals").onclick = async () => {
    const btn = document.getElementById("gm-goals"); btn.disabled = true; btn.textContent = "Configurando…";
    try {
      const r = await api("/api/google-media/ga4-contact-goals", {method:"POST"});
      const msg = [...(r.criadas || []).map(x => x + " criada"), ...(r.ja_existiam || []).map(x => x + " já existia")];
      const erros = Object.entries(r.erros || {}).map(([k,v]) => `${k}: ${v}`).join(" · ");
      toast("Metas GA4: " + (msg.join(" · ") || "verifique a configuração") + (erros ? " · pendente — " + erros : ""), 9000);
    } catch(e) { toast("Metas não configuradas: " + e.message, 7000); }
    finally { btn.disabled = false; btn.textContent = "🎯 Configurar eventos-chave de aquisição"; }
  };
  document.getElementById("gm-discover").onclick = async () => {
    const box = document.getElementById("gm-result"); box.innerHTML = '<span class="muted">procurando propriedades GA4…</span>';
    try {
      const r = await api("/api/google-media/ga4-resources");
      if (!r.propriedades.length) { box.innerHTML = '<span class="muted">Nenhuma propriedade GA4 acessível nesta conta.</span>'; return; }
      box.innerHTML = r.propriedades.map(p => `<div class="media-metric"><b>${esc(p.nome)}</b> <small class="muted">Property ${esc(p.id)} · conta ${esc(p.account)}</small><br>
        ${(p.streams || []).map(s => `<button class="green" onclick="usarGa4('${esc(p.id)}','${esc(s.measurement_id)}')">Usar ${esc(s.nome || s.measurement_id)} · ${esc(s.measurement_id || "sem Measurement ID")}</button>`).join(" ") || '<small class="muted">Sem stream web visível.</small>'}</div>`).join("");
    } catch (e) { box.innerHTML = `<span class="form-feedback error">${esc(e.message)}</span>`; }
  };
  document.getElementById("gm-discover-gtm").onclick = async () => {
    const box = document.getElementById("gm-result"); box.innerHTML = '<span class="muted">procurando contas e containers GTM…</span>';
    try {
      const r = await api("/api/google-media/gtm-resources");
      if (!r.contas?.length) { box.innerHTML = '<span class="muted">Nenhuma conta GTM acessível. Dê acesso de Visualizador à conta de serviço no GTM e tente novamente.</span>'; return; }
      box.innerHTML = r.contas.map(a => `<div class="media-metric"><b>${esc(a.nome)}</b><small class="muted"> · Account ${esc(a.id)}</small><br>${(a.containers || []).map(c => `<button class="green" onclick="usarGtm('${esc(a.id)}','${esc(c.id)}')">Usar ${esc(c.nome || c.public_id)} · ${esc(c.public_id || c.id)}</button>`).join(' ') || '<small class="muted">Sem container visível.</small>'}</div>`).join('');
    } catch (e) { box.innerHTML = `<span class="form-feedback error">${esc(e.message)}</span>`; }
  };
  document.getElementById("gm-utm").onclick = async () => {
    try {
      const p = new URLSearchParams({url: document.getElementById("gm-url").value.trim(), campaign: document.getElementById("gm-campaign").value.trim(), source: document.getElementById("gm-source").value.trim(), medium: document.getElementById("gm-medium").value.trim()});
      const r = await api("/api/google-media/utm?" + p); document.getElementById("gm-utm-out").textContent = r.url;
    } catch (e) { toast("Erro: " + e.message, 4000); }
  };
  // A tela sempre abre com o retrato atualizado; o ciclo do dashboard também sincroniza a cada 30 min.
  document.getElementById("gm-sync").click();
}

function usarGa4(propertyId, measurementId) {
  document.getElementById("gm-ga4").value = propertyId;
  document.getElementById("gm-measurement").value = measurementId;
  toast("GA4 selecionado — clique em Salvar integração", 3000);
}

function usarGtm(accountId, containerId) {
  document.getElementById("gm-gtm-account").value = accountId;
  document.getElementById("gm-gtm-container").value = containerId;
  toast("GTM selecionado — clique em Salvar integração", 3000);
}

/* ---------- TELA: Site & Ads (GA4) ---------- */
async function vSite(site = "plataforma", days = 28) {
  view.innerHTML = '<h2>Site & Ads</h2><span class="muted">carregando dados do GA4…</span>';
  const [o, cf, ld] = await Promise.all([
    api(`/api/site/overview?days=${days}&site=${site}`),
    api(`/api/site/campanhas?days=${days}`),
    api(`/api/site/leads`).catch(() => ({leads: []})),
  ]);
  const sel = (v, cur) => v === cur ? "primary" : "";
  const controles = `
    <div class="toolbar"><h2 style="margin:0">Site & Ads</h2><span class="sp"></span>
      <button class="${sel("plataforma", site)}" onclick="vSite('plataforma',${days})">corporate.yoobe.co</button>
      <button class="${sel("site", site)}" onclick="vSite('site',${days})">catalogo.yoobe.co</button>
      <button class="${sel("todos", site)}" onclick="vSite('todos',${days})">Todos os domínios</button>
      &nbsp;·&nbsp;
      <button class="${sel(7, days)}" onclick="vSite('${site}',7)">7d</button>
      <button class="${sel(28, days)}" onclick="vSite('${site}',28)">28d</button>
      <button class="${sel(90, days)}" onclick="vSite('${site}',90)">90d</button></div>`;
  if (!o.configurado) {
    view.innerHTML = `${controles}
      <div class="card"><h3>GA4 não configurado</h3>
      <p class="muted">Defina <code>GA4_PROPERTY_ID</code> (ou o Property ID na aba Mídia & Google) e
      uma credencial — <code>GA4_CREDENTIALS_JSON</code> (service account) ou OAuth gcloud — no
      <code>dashboard/.env</code>. Depois recarregue esta aba.</p></div>`;
    return;
  }
  if (o.erro) {
    view.innerHTML = `${controles}<div class="card"><h3>Erro ao consultar o GA4</h3>
      <p class="form-feedback error">${esc(o.erro)}</p></div>`;
    return;
  }
  const t = o.totais || {};
  const maxD = Math.max(...o.dias.map(d => d.sessoes), 1);
  const fmtDia = d => `${d.slice(6, 8)}/${d.slice(4, 6)}`;
  const funil = cf.campanhas || [];
  const drill = (kind, title, text) => `<div class="card site-drill" onclick="abrirGa4Detalhe('${kind}','${title}','${site}',${days})"><h3>${title} <span class="muted" style="font-size:12px">ver detalhes →</span></h3>${text}</div>`;
  view.innerHTML = `${controles}
    <p class="muted">Visitas e conversões de <b>${esc(o.host || "")}</b> (GA4, últimos ${days} dias${o.cache ? " · cache" : ""}).</p>
    <div class="kpis">
      <div class="kpi"><b>${t.usuarios ?? 0}</b><small>usuários</small></div>
      <div class="kpi"><b>${t.sessoes ?? 0}</b><small>sessões</small></div>
      <div class="kpi"><b>${t.pageviews ?? 0}</b><small>pageviews</small></div>
      <div class="kpi"><b>${t.key_events ?? 0}</b><small>key events</small></div>
      <div class="kpi"><b>${(o.key_events || {}).generate_lead || 0}</b><small>leads (generate_lead)</small></div>
      <div class="kpi"><b>${(o.key_events || {}).contact_whatsapp || 0}</b><small>contatos por WhatsApp</small></div>
      <div class="kpi"><b>${(o.key_events || {}).contact_email || 0}</b><small>contatos por e-mail</small></div>
    </div>
    <div class="site-grid">
      ${drill("origens", "Fontes de tráfego", `<div class="table-scroll"><table><tr><th>Origem / mídia</th><th>Sessões</th><th>Usuários</th><th>Eventos-chave</th></tr>
        ${o.fontes.map(f => `<tr><td>${esc(f.fonte)} / ${esc(f.medium)}</td><td>${f.sessoes}</td><td>${f.usuarios}</td><td>${f.key_events}</td></tr>`).join("") || '<tr><td colspan="4" class="muted">—</td></tr>'}</table></div>`)}
      ${drill("visitas", "Tipo de visita", `<p class="muted">Veja novos × recorrentes, dispositivo, canal, sessões engajadas e conversões.</p><div class="kpis"><div class="kpi"><b>${t.usuarios ?? 0}</b><small>usuários</small></div><div class="kpi"><b>${t.sessoes ?? 0}</b><small>sessões</small></div></div>`)}
      ${drill("paginas", "Páginas e entradas", `<div class="table-scroll"><table><tr><th>Página</th><th>Views</th><th>Usuários</th></tr>
        ${o.paginas.map(p => `<tr><td><small>${esc(p.pagina)}</small></td><td>${p.pageviews}</td><td>${p.usuarios}</td></tr>`).join("") || '<tr><td colspan="3" class="muted">—</td></tr>'}</table></div>`)}
      ${drill("eventos", "Eventos e conversões", `<div class="table-scroll"><table><tr><th>Evento</th><th>Total</th></tr>
        ${Object.entries(o.key_events || {}).map(([k, n]) => `<tr><td>${esc(k)}</td><td>${n}</td></tr>`).join("") || '<tr><td colspan="2" class="muted">nenhum key event no período</td></tr>'}</table></div>`)}
      ${drill("leads", "Geração de leads", `<p class="muted">O GA4 conta os <b>eventos</b> (anônimos). A identidade de quem preencheu o formulário está no card <b>🧲 Leads identificados</b> logo abaixo.</p><div class="kpis"><div class="kpi"><b>${(o.key_events || {}).generate_lead || 0}</b><small>generate_lead</small></div><div class="kpi"><b>${(o.key_events || {}).contact_whatsapp || 0}</b><small>contact_whatsapp</small></div><div class="kpi"><b>${(o.key_events || {}).contact_email || 0}</b><small>contact_email</small></div></div>`)}
      <div class="card"><h3>Sessões por dia</h3><div class="funnel">
        ${o.dias.map(d => frow(fmtDia(d.data), d.sessoes, maxD)).join("") || '<span class="muted">sem dados no período</span>'}</div></div>
    </div>
    <div class="card"><h3>🧲 Leads identificados (formulário do site)</h3>
      <p class="muted">Cada envio do formulário vira um documento <code>leadSubmission</code> no Sanity
      (Studio → Leads e audiência) e um email para <b>corporate@yoobe.co</b>. A coluna Origem é o
      <code>source</code> do formulário (qual bloco/página gerou o lead). Aqui você importa direto para o CRM.</p>
      ${ld.erro ? `<p class="form-feedback error">${esc(ld.erro)}</p>` : ""}
      <div class="table-scroll"><table><tr><th>Quando</th><th>Nome</th><th>Email</th><th>Empresa</th><th>Telefone</th><th>Origem</th><th>Mensagem</th><th></th></tr>
      ${(ld.leads || []).map(l => `<tr><td><small>${esc(l.quando)}</small></td><td><b>${esc(l.nome)}</b></td>
        <td>${l.no_crm ? `<a href="#/contato/${encodeURIComponent(l.email)}">${esc(l.email)}</a>` : esc(l.email)}</td>
        <td>${esc(l.empresa)}</td><td>${esc(l.telefone)}</td><td><span class="tag">${esc(l.origem)}</span></td>
        <td><small class="muted">${esc((l.mensagem || "").slice(0, 60))}</small></td>
        <td>${l.no_crm ? '<small class="muted">✓ no CRM</small>'
          : `<button class="green" onclick='importarLeadCrm(${JSON.stringify(l).replace(/'/g, "&#39;")})'>+ CRM</button>`}</td></tr>`).join("")
        || '<tr><td colspan="8" class="muted">nenhum lead recebido do site ainda</td></tr>'}</table></div>
    </div>
    <div class="card"><h3>Campanhas de email × site (funil ponta-a-ponta)</h3>
      <p class="muted">Cliques humanos vêm do tracking próprio; sessões e key events vêm do GA4 casados por
      <code>utm_campaign</code> = slug da campanha (links dos emails já saem com UTM automático).</p>
      <div class="table-scroll"><table><tr><th>Campanha</th><th>Status</th><th>Enviados</th><th>Abertos</th><th>Cliques</th><th>Sessões</th><th>Key events</th></tr>
      ${funil.map(x => `<tr><td><a href="#/campanha/${x.id}">${esc(x.nome)}</a><br><small class="muted">utm: ${esc(x.slug)}</small></td>
        <td>${badge(x.status)}</td><td>${x.enviados}</td><td>${x.abertos}</td><td>${x.cliques}</td><td>${x.sessoes}</td><td>${x.key_events}</td></tr>`).join("") || '<tr><td colspan="7" class="muted">nenhuma campanha ativa/encerrada ainda</td></tr>'}</table>
      </div>
      ${(cf.ga4_sem_campanha_local || []).length ? `<p class="muted">Também no GA4 com utm_source=yoobe-mail, sem campanha local correspondente: ${cf.ga4_sem_campanha_local.map(x => esc(x.utm_campaign)).join(", ")}</p>` : ""}
    </div>`;
}

async function importarLeadCrm(l) {
  try {
    await api("/api/contacts", {json: {email: l.email, nome: l.nome, empresa: l.empresa,
      telefone: l.telefone, segmento: "quente", origem: "site",
      tags: "lead-site" + (l.origem ? "," + l.origem : ""),
      enriquecimento: l.mensagem ? "Mensagem do formulário: " + l.mensagem : ""}});
    toast(`Lead ${l.email} importado para o CRM ✔`, 3000);
    vSite();
  } catch (e) { toast("Erro ao importar: " + e.message, 5000); }
}

async function abrirGa4Detalhe(kind, titulo, site, days) {
  const dlg = document.getElementById("dlg-detalhe"), body = document.getElementById("det-body");
  document.getElementById("det-titulo").textContent = titulo;
  document.getElementById("det-count").textContent = `GA4 · ${days} dias`;
  body.innerHTML = '<p class="muted">carregando detalhes do GA4…</p>'; dlg.showModal();
  try {
    const r = await api(`/api/site/drilldown?kind=${encodeURIComponent(kind)}&site=${encodeURIComponent(site)}&days=${days}`);
    if (r.erro) { body.innerHTML = `<p class="form-feedback error">${esc(r.erro)}</p>`; return; }
    const head = (r.labels || []).map(x => `<th>${esc(x)}</th>`).join("");
    const rows = (r.linhas || []).map(line => `<tr>${line.map(x => `<td>${esc(x)}</td>`).join("")}</tr>`).join("");
    body.innerHTML = `<p class="muted">${r.cache ? "Resultado em cache (até 15 min)." : "Atualizado agora."}</p><div class="table-scroll"><table><tr>${head}</tr>${rows || `<tr><td colspan="${(r.labels || []).length}" class="muted">Sem registros para este período.</td></tr>`}</table></div>`;
  } catch (e) { body.innerHTML = `<p class="form-feedback error">${esc(e.message)}</p>`; }
}

/* ---------- TELA: Contexto IA ---------- */
async function vContexto() {
  const d = await api("/api/context");
  view.innerHTML = `
    <div class="toolbar"><h2 style="margin:0">Contexto da IA</h2><span class="sp"></span>
      <button class="primary" onclick="document.getElementById('dlg-kb').showModal()">📚 Novo KB</button>
      <label class="btn">🖼 Enviar imagem<input type="file" id="ctx-img" accept="image/*" hidden></label></div>
    <p class="muted">Tudo aqui é injetado nos prompts de criação de campanhas e templates:
    KBs (markdown) viram conhecimento e as imagens ficam disponíveis para a IA citar nos emails.
    Skills ativos: <b>gtm-engineer-growth-specialist</b> (interno), <b>email-creative-builder</b> (aaron-he-zhu, Apache-2.0),
    <b>copywriting</b> / <b>copy-editing</b> / <b>email-sequence</b> / <b>growth-engine</b> (community).
    Abrir esta tela sincroniza os skills no KB e semeia o Conhecimento dos agentes.</p>
    <div class="row">
      <div class="card grow"><h3>KBs (dashboard/context/kb/)</h3>
        ${d.kb.map(k => `<div class="card"><b>${esc(k.nome)}</b> <small class="muted">${k.tamanho} bytes</small>
          ${k.skill ? ` ${badge(k.licenca || "skill", ["Apache-2.0","interno","community"].includes(k.licenca) ? "valido" : "pendente")}`
            + (k.autor ? ` <small class="muted">© ${esc(k.autor)}</small>` : "")
            + (k.url ? ` <a href="${esc(k.url)}" target="_blank" rel="noopener">fonte</a>` : "") : ""}
          <pre class="json">${esc(k.preview)}…</pre></div>`).join("") || '<span class="muted">nenhum KB ainda</span>'}</div>
      <div class="card" style="width:380px"><h3>Imagens (dashboard/context/images/)</h3>
        <div class="imggrid">${d.imagens.map(i => `<figure style="margin:0"><img src="${i.url}" alt="${esc(i.nome)}">
          <figcaption><small>${esc(i.nome)}</small></figcaption></figure>`).join("") || '<span class="muted">nenhuma imagem</span>'}</div></div>
    </div>`;
  document.getElementById("ctx-img").onchange = async e => {
    const fd = new FormData(); fd.append("file", e.target.files[0]);
    await api("/api/context/images", {method: "POST", body: fd});
    toast("Imagem adicionada ✔"); vContexto();
  };
}
document.getElementById("kb-salvar").onclick = async () => {
  await api("/api/context/kb", {json: {nome: document.getElementById("kb-nome").value || "novo-kb.md",
    conteudo: document.getElementById("kb-conteudo").value}});
  document.getElementById("dlg-kb").close(); toast("KB salvo ✔"); vContexto();
};

document.getElementById("ct-salvar").onclick = async () => {
  const g = id => document.getElementById(id).value.trim();
  const email = g("ct-email");
  if (!email) return toast("Informe o e-mail.");
  const body = {
    email, nome: g("ct-nome"), empresa: g("ct-empresa"), modulos: g("ct-modulos"),
    mensalidades: g("ct-mensalidades"), setup: g("ct-setup"), validade: g("ct-validade"),
    telefone: g("ct-telefone"), cargo: g("ct-cargo"), porte_funcionarios: g("ct-porte"),
    erp: g("ct-erp"), etapa_funil: g("ct-etapa"), qualificacao: g("ct-qualificacao"),
    motivo_perda: g("ct-motivo-perda"), enriquecimento: g("ct-enriquecimento"),
    segmento: document.getElementById("ct-segmento").value,
    respondeu: document.getElementById("ct-respondeu").value,
    tags: g("ct-tags"), origem: g("ct-origem") || "manual",
  };
  try {
    const r = await api("/api/contacts", {json: body});
    document.getElementById("dlg-contato").close();
    toast(r.criado ? `Contato criado ✔ (${email})` : `Contato atualizado ✔ (${email})`, 3500);
    ["ct-email", "ct-nome", "ct-empresa", "ct-telefone", "ct-modulos", "ct-mensalidades", "ct-setup",
     "ct-validade", "ct-cargo", "ct-porte", "ct-erp", "ct-etapa", "ct-qualificacao", "ct-motivo-perda",
     "ct-enriquecimento", "ct-tags"].forEach(id => document.getElementById(id).value = "");
    document.getElementById("ct-origem").value = "manual";
    if (location.hash.startsWith("#/crm")) route();
  } catch (e) { toast("Erro: " + e.message, 5000); }
};

/* ---------- TELA: Qualidade (nomes + validação Firecrawl/Vertex) ---------- */
let nmStatus = "todos";   // todos | pendente | aprovado | aplicado | rejeitado
let nmPollTimer = null;
let nmFormState = { tags: "", seg: "", origem: "", conf: "0.90" };
let nmEdits = Object.create(null);  // email → valor editado localmente
let nmDelegBound = false;

function nomesFiltrar(s) { nmStatus = s; vNomes(); }

function _nmCaptureForm() {
  const tags = document.getElementById("ql-tags");
  const seg = document.getElementById("ql-seg");
  const origem = document.getElementById("ql-origem");
  const conf = document.getElementById("nm-conf");
  if (tags) nmFormState.tags = tags.value;
  if (seg) nmFormState.seg = seg.value;
  if (origem) nmFormState.origem = origem.value;
  if (conf) nmFormState.conf = conf.value;
  document.querySelectorAll("input[data-role='nome-sugerido']").forEach(inp => {
    const em = inp.dataset.email || "";
    if (em) nmEdits[em] = inp.value;
  });
}

function _nmRestoreForm() {
  const tags = document.getElementById("ql-tags");
  const seg = document.getElementById("ql-seg");
  const origem = document.getElementById("ql-origem");
  const conf = document.getElementById("nm-conf");
  if (tags) tags.value = nmFormState.tags || "";
  if (seg) seg.value = nmFormState.seg || "";
  if (origem) origem.value = nmFormState.origem || "";
  if (conf) conf.value = nmFormState.conf || "0.90";
  document.querySelectorAll("input[data-role='nome-sugerido']").forEach(inp => {
    const em = inp.dataset.email || "";
    if (em && nmEdits[em] != null) inp.value = nmEdits[em];
  });
}

function _nmBannerHtml(prog, lv) {
  const crmC = lv.contagens_crm || lv.contagens || {};
  const analisando = prog.status === "analisando";
  const validando = lv.status === "analisando";
  const partial = (prog.feitos || 0) > 0 || (prog.total || 0) > 0;
  const banner = analisando
    ? `<div class="card card-info" id="nm-progress">
        <div class="spinner" style="width:18px;height:18px;display:inline-block;vertical-align:middle"></div>
        <b style="margin-left:8px">Analisando leads (Vertex name_analyzer + evidência site/e-mail)…</b>
        <span class="muted" data-role="nm-prog-detail"> ${prog.feitos || 0}/${prog.total || "?"} — ${esc(prog.etapa || "")}${prog.auto_aplicados ? ` · auto ${prog.auto_aplicados}` : ""}</span>
        <button type="button" class="warn" style="margin-left:12px" data-nm-action="cancelar">Cancelar / destravar</button></div>`
    : prog.status === "erro"
    ? `<div class="card card-error" id="nm-progress"><b>A análise de nomes falhou / foi interrompida:</b> ${esc(prog.erro || "")}
        ${partial ? `<div class="muted" style="margin-top:6px">Progresso parcial: ${prog.feitos || 0}/${prog.total || "?"}. Já resolvidos em name_fixes são pulados na retomada.</div>
        <button type="button" class="primary" style="margin-top:8px" data-nm-action="retomar">Retomar análise</button>` : ""}</div>`
    : prog.status === "pronto" && (prog.auto_aplicados || prog.propostas != null)
    ? `<div class="card" id="nm-progress"><b>Última análise:</b> ${esc(prog.etapa || "concluído")} ·
        propostas ${prog.propostas || 0} · auto-aplicados ${prog.auto_aplicados || 0} (conf. ≥ 80%)</div>`
    : `<div id="nm-progress" hidden></div>`;
  const bannerV = validando
    ? `<div class="card card-info" id="lv-progress">
        <div class="spinner" style="width:18px;height:18px;display:inline-block;vertical-align:middle"></div>
        <b style="margin-left:8px">Validando leads (Firecrawl + Vertex)…</b>
        <span class="muted" data-role="lv-prog-detail"> ${lv.feitos || 0}/${lv.total || "?"} — ${esc(lv.etapa || "")}</span></div>`
    : lv.status === "erro"
    ? `<div class="card card-error" id="lv-progress"><b>Validação falhou:</b> ${esc(lv.erro || "")}</div>`
    : lv.status === "pronto"
    ? `<div class="card" id="lv-progress"><b>Última validação:</b> ${esc(lv.etapa || "concluído")} ·
        válidos ${crmC.valido || (lv.contagens && lv.contagens.valido) || 0} ·
        duvidosos ${crmC.duvidoso || (lv.contagens && lv.contagens.duvidoso) || 0} ·
        desatualizados ${crmC.desatualizado || (lv.contagens && lv.contagens.desatualizado) || 0} ·
        inválidos ${crmC.invalido || (lv.contagens && lv.contagens.invalido) || 0}</div>`
    : `<div id="lv-progress" hidden></div>`;
  return { banner, bannerV, analisando, validando };
}

/** Atualiza só os banners — não reconstrói a tabela (preserva edições). */
async function vNomesProgresso() {
  if (location.hash !== "#/nomes" && !location.hash.startsWith("#/nomes")) return;
  let prog, lv;
  try {
    prog = await api("/api/nomes/analise");
    lv = await api("/api/leads/validacao");
  } catch (_) { return; }
  const analisando = prog.status === "analisando";
  const validando = lv.status === "analisando";
  const { banner, bannerV } = _nmBannerHtml(prog, lv);
  const nmEl = document.getElementById("nm-progress");
  const lvEl = document.getElementById("lv-progress");
  if (nmEl) {
    const tmp = document.createElement("div");
    tmp.innerHTML = banner;
    nmEl.replaceWith(tmp.firstElementChild || tmp);
  }
  if (lvEl) {
    const tmp = document.createElement("div");
    tmp.innerHTML = bannerV;
    lvEl.replaceWith(tmp.firstElementChild || tmp);
  }
  const btnA = document.getElementById("nm-btn-analisar");
  const btnV = document.getElementById("nm-btn-validar");
  if (btnA) btnA.disabled = analisando;
  if (btnV) btnV.disabled = validando;

  if (analisando || validando) {
    if (nmPollTimer) clearTimeout(nmPollTimer);
    nmPollTimer = setTimeout(() => {
      if (location.hash === "#/nomes" || location.hash.startsWith("#/nomes")) vNomesProgresso();
    }, 3000);
  } else {
    // Terminou: um único refresh completo para contagens/propostas
    if (nmPollTimer) { clearTimeout(nmPollTimer); nmPollTimer = null; }
    vNomes();
  }
}

function _nmBindDelegation() {
  if (nmDelegBound) return;
  nmDelegBound = true;
  view.addEventListener("click", async (ev) => {
    const t = ev.target.closest("[data-nm-action]");
    if (!t || !view.contains(t)) return;
    const action = t.getAttribute("data-nm-action");
    const email = t.getAttribute("data-email") || "";
    const acao = t.getAttribute("data-acao") || "";
    if (action === "aprovar") return nomesAprovarUm(email, acao);
    if (action === "rejeitar") return nomesRejeitarUm(email);
    if (action === "pendente") return nomesPendente(email);
    if (action === "filtrar") return nomesFiltrar(t.getAttribute("data-status") || "todos");
    if (action === "aprovar-lote") return nomesAprovarConf();
    if (action === "usar-max-conf") return nomesUsarMaxConf();
    if (action === "aplicar") return nomesAplicar(+t.getAttribute("data-n") || 0);
    if (action === "analisar" || action === "retomar") return nomesAnalisar();
    if (action === "cancelar") return nomesCancelar();
    if (action === "validar") return leadsValidar();
  });
  view.addEventListener("input", (ev) => {
    const inp = ev.target;
    if (!inp || !inp.matches) return;
    if (inp.matches("input[data-role='nome-sugerido']")) {
      const em = inp.dataset.email || "";
      if (em) nmEdits[em] = inp.value;
      return;
    }
    if (inp.id === "nm-conf") {
      nmFormState.conf = inp.value;
      _nmUpdateElegiveisHint();
    }
  });
}

function _nmParseConf(raw) {
  const s = String(raw ?? "").trim().replace(",", ".");
  const n = parseFloat(s);
  if (!Number.isFinite(n)) return 0.9;
  return Math.min(1, Math.max(0, n));
}

function _nmElegiveisCount(porConf, min) {
  let n = 0;
  for (const [c, cnt] of Object.entries(porConf || {})) {
    if (parseFloat(c) + 1e-9 >= min) n += Number(cnt) || 0;
  }
  return n;
}

function _nmUpdateElegiveisHint() {
  const el = document.getElementById("nm-elegiveis");
  if (!el) return;
  const por = window.__nmPorConfPendente || {};
  const maxC = window.__nmMaxConfPendente;
  const min = _nmParseConf(document.getElementById("nm-conf")?.value || nmFormState.conf || "0.9");
  const eleg = _nmElegiveisCount(por, min);
  const maxTxt = maxC == null ? "—" : `${Math.round(maxC * 100)}%`;
  if (eleg === 0) {
    el.innerHTML = `<span style="color:var(--gold-bright)">0 elegíveis com ≥ ${Math.round(min * 100)}%`
      + (maxC != null ? ` — máx. nas pendentes: ${maxTxt} (baixe o limiar ou use “Usar máx.”)` : "")
      + `</span>`;
  } else {
    el.textContent = `${eleg} elegível(is) com ≥ ${Math.round(min * 100)}% · máx. pendente: ${maxTxt}`;
  }
}

async function vNomes() {
  _nmCaptureForm();
  if (nmPollTimer) { clearTimeout(nmPollTimer); nmPollTimer = null; }
  _nmBindDelegation();
  const prog = await api("/api/nomes/analise");
  const d = await api(`/api/nomes/propostas?status=${nmStatus === "todos" ? "" : nmStatus}`);
  const lv = await api("/api/leads/validacao");
  const counts = d.counts || {};
  const stats = d.stats || {};
  const aprovados = counts.aprovado || 0;
  window.__nmPorConfPendente = stats.por_conf_pendente || {};
  window.__nmMaxConfPendente = stats.max_conf_pendente;
  const filtros = ["todos", "pendente", "aprovado", "aplicado", "rejeitado"];
  const { banner, bannerV, analisando, validando } = _nmBannerHtml(prog, lv);
  const confAtual = _nmParseConf(nmFormState.conf || "0.9");
  const elegiveis = _nmElegiveisCount(window.__nmPorConfPendente, confAtual);
  const maxPend = window.__nmMaxConfPendente;
  const maxPendPct = maxPend == null ? null : Math.round(maxPend * 100);

  const rows = (d.propostas || []).map(p => {
    const em = p.email || "";
    const sugVal = nmEdits[em] != null ? nmEdits[em] : (p.nome_sugerido || "");
    const extras = [
      p.empresa_sugerida ? `emp: ${p.empresa_sugerida}` : "",
      p.cargo_sugerido ? `cargo: ${p.cargo_sugerido}` : "",
      p.telefone_sugerido ? `tel: ${p.telefone_sugerido}` : "",
    ].filter(Boolean).join(" · ");
    const statusBadge = p.auto
      ? badge("auto", "aplicado") + " " + badge(p.status)
      : badge(p.status);
    return `<tr data-email="${esc(em)}" ${p.confianca < 0.7 ? 'class="card-warn-row"' : ""}>
        <td><b>${esc(p.empresa || "—")}</b><br><small class="muted">${esc(em)}</small></td>
        <td>${esc(p.nome_atual || "—")}</td>
        <td>${p.acao === "limpar"
              ? '<span class="muted">(limpar — não é pessoa)</span>'
              : `<input id="nm-sug-${esc(em)}" data-role="nome-sugerido" data-email="${esc(em)}" value="${esc(sugVal)}" style="width:100%;box-sizing:border-box">`}${extras ? `<br><small class="muted">${esc(extras)}</small>` : ""}</td>
        <td>${badge(p.acao)}</td>
        <td>${Math.round((p.confianca || 0) * 100)}%</td>
        <td>${statusBadge}</td>
        <td><small>${esc(p.motivo || "")}</small></td>
        <td style="white-space:nowrap">
          ${p.status !== "aplicado" ? `
          <button class="green" type="button" title="aprovar" data-nm-action="aprovar" data-email="${esc(em)}" data-acao="${esc(p.acao || "")}">✓</button>
          <button class="warn" type="button" title="rejeitar" data-nm-action="rejeitar" data-email="${esc(em)}">✗</button>
          ${p.status === "aprovado" ? `<button type="button" title="voltar a pendente" data-nm-action="pendente" data-email="${esc(em)}">↩</button>` : ""}` : "—"}</td>
      </tr>`;
  }).join("");

  const html = [
    `<div class="toolbar"><h2 style="margin:0">Qualidade — Nomes & Leads</h2>`,
    `<span class="sp"></span>`,
    `<button class="primary" type="button" id="nm-btn-analisar" data-nm-action="analisar" ${analisando ? "disabled" : ""}>Analisar leads</button>`,
    `<button type="button" id="nm-btn-validar" data-nm-action="validar" ${validando ? "disabled" : ""}>Validar leads (Vertex + Firecrawl)</button>`,
    `</div>`,
    banner, bannerV,
    `<div class="card"><b>Filtro da análise/validação</b>`,
    `<div style="margin-top:8px;display:flex;flex-wrap:wrap;gap:8px;align-items:center">`,
    `<label>tag <input id="ql-tags" placeholder="ex: inbound" style="width:120px"></label>`,
    `<label>segmento <input id="ql-seg" placeholder="opcional" style="width:120px"></label>`,
    `<label>origem <input id="ql-origem" placeholder="opcional" style="width:120px"></label>`,
    `</div>`,
    `<p class="muted" style="margin:8px 0 0">A análise usa evidência do site/domínio (Firecrawl) + Vertex: corrige nomes, preenche campos vazios e auto-aplica com confiança ≥ 80%. Validar leads continua à parte (status/supressão).</p></div>`,
    `<div class="card"><b>Propostas:</b> ${counts.pendente || 0} pendente(s) · ${aprovados} aprovada(s) · ${counts.aplicado || 0} já aplicada(s) · ${counts.rejeitado || 0} rejeitada(s)`,
    `<div style="margin-top:10px">Aprovar todas pendentes com confiança ≥ `,
    `<input id="nm-conf" type="number" step="0.05" min="0" max="1" value="${esc(String(confAtual))}" style="width:70px"> `,
    `<button type="button" data-nm-action="aprovar-lote">✓ Aprovar em lote</button>`,
    (maxPend != null && elegiveis === 0
      ? ` <button type="button" data-nm-action="usar-max-conf" title="Ajustar limiar ao máximo das pendentes">Usar máx. (${maxPendPct}%)</button>`
      : ""),
    `<span class="sp"></span>`,
    `<button class="green" type="button" data-nm-action="aplicar" data-n="${aprovados}" ${aprovados ? "" : "disabled"}>💾 Aplicar aprovados (${aprovados})</button></div>`,
    `<p id="nm-elegiveis" class="muted" style="margin:8px 0 0"></p>`,
    `<div style="margin-top:10px">Filtrar: ${filtros.map(s =>
      `<button type="button" data-nm-action="filtrar" data-status="${s}" ${s === nmStatus ? 'class="primary"' : ""}>${s}${counts[s] != null ? " (" + counts[s] + ")" : ""}</button>`).join(" ")}</div></div>`,
    `<div class="card"><h3>Propostas — ${nmStatus} (${d.total})</h3>`,
    d.total === 0
      ? '<span class="muted">Nada aqui. Troque o filtro acima ou rode a análise.</span>'
      : `<table><tr><th>Empresa</th><th>Nome atual</th><th>→ Sugerido (+ campos)</th><th>Ação</th><th>Conf.</th><th>Status</th><th>Motivo</th><th></th></tr>${rows}</table>`,
    `</div>`
  ].join("");

  view.innerHTML = html;
  _nmRestoreForm();
  _nmUpdateElegiveisHint();

  if (analisando || validando) {
    nmPollTimer = setTimeout(() => {
      if (location.hash === "#/nomes" || location.hash.startsWith("#/nomes")) vNomesProgresso();
    }, 3000);
  }
}

function _qualidadeFiltros() {
  return {
    tags: (document.getElementById("ql-tags")?.value || "").trim() || undefined,
    segmento: (document.getElementById("ql-seg")?.value || "").trim() || undefined,
    origem: (document.getElementById("ql-origem")?.value || "").trim() || undefined,
  };
}

async function nomesAnalisar() {
  try {
    await api("/api/nomes/analisar", {json: _qualidadeFiltros()});
    toast("Análise iniciada (Vertex + evidência site/e-mail)", 3500); vNomes();
  } catch (e) { toast("Erro: " + e.message, 5000); }
}
async function nomesCancelar() {
  try {
    const r = await api("/api/nomes/cancel", {method: "POST", json: {}});
    toast(r.msg || "Análise destravada — pode retomar", 4000);
    vNomes();
  } catch (e) { toast("Erro: " + e.message, 5000); }
}
async function leadsValidar() {
  try {
    await api("/api/leads/validar", {json: _qualidadeFiltros()});
    toast("Validação de leads iniciada (Firecrawl + Vertex)", 3500); vNomes();
  } catch (e) { toast("Erro: " + e.message, 5000); }
}

function nomesUsarMaxConf() {
  const maxC = window.__nmMaxConfPendente;
  if (maxC == null) return toast("Não há propostas pendentes.", 3000);
  const conf = document.getElementById("nm-conf");
  const v = String(Math.round(maxC * 100) / 100);
  if (conf) conf.value = v;
  nmFormState.conf = v;
  _nmUpdateElegiveisHint();
  toast(`Limiar ajustado para ${Math.round(maxC * 100)}% — clique em Aprovar em lote.`, 4000);
}

async function nomesAprovarConf() {
  try {
    const min = _nmParseConf(document.getElementById("nm-conf")?.value || nmFormState.conf || "0.9");
    nmFormState.conf = String(min);
    const r = await api("/api/nomes/aprovar-lote", {json: {min_conf: min}});
    const n = Number(r.aprovados) || 0;
    if (n === 0) {
      const maxC = r.max_conf_pendente;
      const maxTxt = maxC == null ? "nenhuma pendente" : `máx. atual ${(Math.round(maxC * 100))}%`;
      toast(`Nada aprovado: 0 pendentes com ≥ ${Math.round(min * 100)}% (${maxTxt}). Baixe o limiar ou use “Usar máx.”.`, 7000);
    } else {
      toast(`${n} proposta(s) aprovada(s) ✔ — agora clique em Aplicar aprovados.`, 4500);
    }
    vNomes();
  } catch (e) { toast("Erro ao aprovar em lote: " + e.message, 5000); }
}

async function nomesAprovarUm(email, acao) {
  try {
    if (!email) return toast("E-mail da proposta ausente.", 4000);
    const row = document.querySelector(`tr[data-email="${CSS.escape ? CSS.escape(email) : email.replace(/"/g, '\\"')}"]`);
    const inp = (row && row.querySelector('input[data-role="nome-sugerido"]'))
      || document.getElementById("nm-sug-" + email);
    const body = {status: "aprovado"};
    if (acao !== "limpar" && inp) {
      body.nome_sugerido = inp.value;
      nmEdits[email] = inp.value;
    }
    await api(`/api/nomes/propostas/${encodeURIComponent(email)}`, {json: body});
    const sug = body.nome_sugerido;
    toast(sug ? `Aprovada ✔ (${sug})` : "Aprovada ✔", 2000);
    delete nmEdits[email];
    vNomes();
  } catch (e) { toast("Erro ao aprovar: " + e.message, 5000); }
}

async function nomesRejeitarUm(email) {
  try {
    await api(`/api/nomes/propostas/${encodeURIComponent(email)}`, {json: {status: "rejeitado"}});
    delete nmEdits[email];
    toast("Rejeitada", 1500); vNomes();
  } catch (e) { toast("Erro ao rejeitar: " + e.message, 5000); }
}

async function nomesPendente(email) {
  try {
    await api(`/api/nomes/propostas/${encodeURIComponent(email)}`, {json: {status: "pendente"}});
    toast("Voltou a pendente", 1500); vNomes();
  } catch (e) { toast("Erro: " + e.message, 5000); }
}

async function nomesAplicar(n) {
  try {
    if (!n) {
      return toast("Nenhuma proposta aprovada para aplicar. Use Aprovar em lote (com limiar que tenha elegíveis) antes.", 5500);
    }
    if (!confirm(`Aplicar ${n} correção(ões) aos contatos? Isso altera nome/empresa/cargo/telefone no CRM (não envia nada).`)) return;
    const r = await api("/api/nomes/aplicar", {method: "POST"});
    const aplicados = Number(r.aplicados) || 0;
    const totalAplicado = (r.counts && r.counts.aplicado) || "?";
    if (aplicados === 0) {
      toast("Nada aplicado — não havia propostas no status aprovado.", 5000);
    } else {
      toast(`${aplicados} lead(s) atualizado(s) ✔ · total aplicado no CRM: ${totalAplicado}`, 5000);
    }
    vNomes();
  } catch (e) { toast("Erro ao aplicar: " + e.message, 5000); }
}


async function vAgentes() {
  const days = Number(sessionStorage.getItem("ag-days") || 30);
  const params = new URLSearchParams((location.hash.split("?")[1] || ""));
  const filterCamp = params.get("campanha") || sessionStorage.getItem("ag-campanha") || "";
  if (params.get("campanha")) sessionStorage.setItem("ag-campanha", params.get("campanha"));
  const campId = filterCamp ? Number(filterCamp) : null;
  const [cat, res, runs, know] = await Promise.all([
    api("/api/agentes"),
    api(`/api/agentes/resumo?days=${days}`),
    api(`/api/agentes/runs?limit=60${campId ? "&campaign_id=" + campId : ""}`),
    api("/api/agentes/knowledge?limit=30"),
  ]);
  const fmtN = n => Number(n || 0).toLocaleString("pt-BR");
  const fmtUsd = n => "$" + Number(n || 0).toFixed(4);
  const fmtBrl = n => "R$ " + Number(n || 0).toFixed(2);
  const cards = (cat.agentes || []).map(a => {
    const st = a.status === "ativo" ? badge("ativo", "valido") : badge("ocioso", "pendente");
    const jobs = (a.em_andamento || []).map(j =>
      `<a href="${esc(j.href || "#")}">#${j.campaign_id}</a> ${esc(j.etapa || j.kind || "")}`
    ).join(" · ");
    return `
    <div class="agent-card agent-card--${esc(a.status || "ocioso")}">
      <h4>${esc(a.nome)} ${st} ${a.resource_configured ? badge("Agent Engine", "valido") : badge(a.via || "generate_json", "duvidoso")}</h4>
      <div class="meta">${esc(a.papel || "")}<br>
        tier ${esc(a.tier)} · ${fmtN(a.runs)} runs · ${fmtN(a.tokens)} tok · ${fmtUsd(a.cost_usd)}
        ${a.cost_brl != null ? " · " + fmtBrl(a.cost_brl) : ""}
        <br>24h: ${fmtN(a.runs_24h)} runs · ${fmtN(a.tokens_24h)} tok · ${fmtUsd(a.cost_usd_24h)}
        ${a.aprovacao_pct != null ? `<br>aprovação: ${a.aprovacao_pct}%` : ""}
        ${a.last_at ? `<br>último: ${fmtDt(a.last_at)}` : ""}
        ${(a.skills && a.skills.length) ? `<br><small>skill: ${esc(a.skills.join(" · "))}</small>` : ""}
        ${jobs ? `<br><b>em andamento:</b> ${jobs}` : ""}
      </div>
    </div>`;
  }).join("");
  const runRows = (runs.runs || []).map(r => `<tr>
      <td>${fmtDt(r.criado_em)}</td>
      <td>${esc(r.agent)}${r.fallback ? " " + badge("fallback", "duvidoso") : ""}</td>
      <td>${esc(r.task_type || "—")}</td>
      <td>${r.campaign_id != null ? `<a href="#/campanha/${r.campaign_id}">#${r.campaign_id}</a>` : "—"}</td>
      <td>${fmtN(r.total_tokens)}</td>
      <td>${fmtUsd(r.cost_usd)}</td>
      <td>${r.latency_ms || 0}ms</td>
      <td>${r.ok ? badge("ok", "valido") : badge("erro", "invalido")}</td>
      <td><small>${esc(r.detalhe || r.erro || "")}</small></td>
    </tr>`).join("");
  const knowRows = (know.items || []).map(k => `
    <article class="card" style="margin-top:8px">
      <b>${esc(k.titulo)}</b>
      <div class="muted" style="font-size:12px;margin:4px 0">${esc(k.agent)} · ${fmtDt(k.criado_em)}
        ${k.campaign_id != null ? ` · <a href="#/campanha/${k.campaign_id}">campanha #${k.campaign_id}</a>` : ""}
        ${k.tags ? " · " + esc(k.tags) : ""}</div>
      <div class="ai-prosa">${fmtRichProsa(k.corpo || "")}</div>
    </article>`).join("") || '<p class="muted">Nenhum conhecimento armazenado ainda. Planos de campanha passam a gravar aqui automaticamente.</p>';

  const activeFlat = Object.entries(res.active || {}).flatMap(([ag, jobs]) =>
    (jobs || []).map(j => ({ag, ...j})));
  const activeBox = activeFlat.length
    ? `<div class="card"><b>Agentes em execução agora</b>
        <ul>${activeFlat.map(j => `<li><b>${esc(j.ag)}</b> · <a href="${esc(j.href || "#")}">campanha #${j.campaign_id}</a> · ${esc(j.etapa || j.kind || "")}</li>`).join("")}</ul>
      </div>` : "";

  view.innerHTML = `
    <div class="toolbar"><h2 style="margin:0">Agentes & tokens</h2><span class="sp"></span>
      ${campId ? `<span class="tag">filtro campanha #${campId}</span>
        <button type="button" id="ag-clear-camp">Limpar filtro</button>` : ""}
      <label>janela <select id="ag-days">
        ${[7,14,30,90].map(d => `<option value="${d}" ${d===days?"selected":""}>${d}d</option>`).join("")}
      </select></label>
      <button type="button" id="ag-refresh">↻ Atualizar</button>
    </div>
    <div class="card">
      <b>Vertex</b> — ${cat.vertex?.ok ? badge("pronto", "valido") : badge("indisponível", "invalido")}
      <span class="muted"> ${esc(cat.vertex?.mensagem || "")} · ${esc(cat.vertex?.modelo || "")}</span>
      <div style="margin-top:8px"><b>Grok</b> — ${cat.grok?.ok ? badge("pronto", "valido") : badge(cat.grok?.enabled === false ? "off" : "indisponível", "invalido")}
      <span class="muted"> ${esc(cat.grok?.mensagem || "")} · ${esc(cat.grok?.modelo || "")}${cat.grok?.xai_fallback ? " · fallback xAI" : ""}</span></div>
      <div class="agent-stats">
        <div class="agent-stat"><b>${fmtN(res.total_runs)}</b><span>runs (${days}d)</span></div>
        <div class="agent-stat"><b>${fmtN(res.runs_24h)}</b><span>runs 24h</span></div>
        <div class="agent-stat"><b>${fmtN(res.total_tokens)}</b><span>tokens</span></div>
        <div class="agent-stat"><b>${fmtUsd(res.cost_usd)}</b><span>custo est.</span></div>
        <div class="agent-stat"><b>${fmtBrl(res.cost_brl)}</b><span>custo BRL</span></div>
        <div class="agent-stat"><b>${fmtN(res.fallbacks)}</b><span>fallbacks</span></div>
        <div class="agent-stat"><b>${fmtN(res.erros)}</b><span>erros</span></div>
      </div>
      <p class="muted" style="margin:0">Catálogo em <code>config/agents.json</code>. Custo BRL via meta <code>usd_brl_rate</code>.</p>
    </div>
    ${activeBox}
    <div class="card"><b>Catálogo de agentes</b>
      <div class="agent-grid">${cards}</div>
    </div>
    <div class="card"><b>Gastos por agente (${days}d)</b>
      <table><tr><th>Agente</th><th>Runs</th><th>Tokens</th><th>Custo</th><th>Latência média</th></tr>
      ${(res.por_agente||[]).map(a => `<tr>
        <td>${esc(a.agent)}</td><td>${fmtN(a.n)}</td><td>${fmtN(a.tokens)}</td>
        <td>${fmtUsd(a.cost_usd)}</td><td>${Math.round(a.latency_ms_avg||0)}ms</td></tr>`).join("")
        || '<tr><td colspan="5" class="muted">Sem runs ainda — rode uma análise ou gere uma campanha.</td></tr>'}
      </table>
    </div>
    <div class="card"><b>Por campanha</b>
      <table><tr><th>Campanha</th><th>Runs</th><th>Tokens</th><th>Custo</th></tr>
      ${(res.por_campanha||[]).map(a => `<tr>
        <td><a href="#/campanha/${a.campaign_id}">#${a.campaign_id}</a>
          <button type="button" class="linkish" data-camp="${a.campaign_id}">filtrar</button></td>
        <td>${fmtN(a.n)}</td><td>${fmtN(a.tokens)}</td><td>${fmtUsd(a.cost_usd)}</td></tr>`).join("")
        || '<tr><td colspan="4" class="muted">Nenhuma campanha com gasto registrado.</td></tr>'}
      </table>
    </div>
    <div class="card"><h3>Últimas tarefas${campId ? " (campanha #" + campId + ")" : ""}</h3>
      <table><tr><th>Quando</th><th>Agente</th><th>Tarefa</th><th>Camp.</th><th>Tok</th><th>Custo</th><th>Lat.</th><th>Status</th><th>Detalhe</th></tr>
      ${runRows || '<tr><td colspan="9" class="muted">Vazio</td></tr>'}</table>
    </div>
    <div class="card"><h3>Conhecimento gerado</h3>
      <p class="muted">Armazenado em SQLite (<code>agent_knowledge</code>) — planos, insights e notas dos agentes. KBs markdown continuam em Contexto IA.</p>
      ${knowRows}
    </div>`;

  document.getElementById("ag-days").onchange = (ev) => {
    sessionStorage.setItem("ag-days", ev.target.value);
    vAgentes();
  };
  document.getElementById("ag-refresh").onclick = () => vAgentes();
  const clr = document.getElementById("ag-clear-camp");
  if (clr) clr.onclick = () => { sessionStorage.removeItem("ag-campanha"); location.hash = "#/agentes"; vAgentes(); };
  view.querySelectorAll("[data-camp]").forEach(btn => {
    btn.onclick = () => {
      sessionStorage.setItem("ag-campanha", btn.getAttribute("data-camp"));
      location.hash = "#/agentes?campanha=" + btn.getAttribute("data-camp");
      vAgentes();
    };
  });
}

/* ---------- router ---------- */
async function route() {
  const h = location.hash || "#/inicio";
  const [, r, arg] = h.split("/");
  // Limpa estado de preview ao sair da campanha (ou trocar de id).
  const st = window._tplPreviewState;
  if (!(r === "campanha" && st && String(st.campaignId) === String(arg || ""))) {
    window._tplPreviewState = null;
    window._tplPreviewRestore = false;
  }
  if (inicioTimer && r !== "inicio" && h !== "") { clearInterval(inicioTimer); inicioTimer = null; }
  document.querySelectorAll("nav a").forEach(a =>
    a.classList.toggle("on", h.startsWith("#/" + a.dataset.r) ||
      (a.dataset.r === "campanhas" && r === "campanha") ||
      (a.dataset.r === "remetentes" && r === "remetente") ||
      (a.dataset.r === "crm" && r === "contato")));
  view.innerHTML = '<div class="card muted">carregando…</div>';
  try {
    if (r === "campanhas") await vCampanhas();
    else if (r === "campanha" && arg) await vCampanha(arg);
    else if (r === "crm") await vCRM();
    else if (r === "nomes") await vNomes();
    else if (r === "respostas") await vRespostas();
    else if (r === "remetentes") await vRemetentes();
    else if (r === "remetente" && arg) await vRemetente(arg);
    else if (r === "contato" && arg) await vContato(decodeURIComponent(arg));
    else if (r === "templates") await vTemplates();
    else if (r === "metricas") await vMetricas();
    else if (r === "agentes") await vAgentes();
    else if (r === "site") await vSite();
    else if (r === "midia") await vMidia();
    else if (r === "contexto") await vContexto();
    else await vInicio();
  } catch (e) { view.innerHTML = `<div class="card">⚠️ ${esc(e.message)}</div>`; }
}
window.addEventListener("hashchange", route);
route(); pollStatus();
