/* 4unik.mail — SPA vanilla (hash router) */
const view = document.getElementById("view");
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

async function api(path, opts = {}) {
  opts.headers = Object.assign({"X-API-Key": window.API_KEY}, opts.headers || {});
  if (opts.json) { opts.method = opts.method || "POST"; opts.body = JSON.stringify(opts.json);
    opts.headers["Content-Type"] = "application/json"; delete opts.json; }
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
  try { await api("/api/tracking/sync", {method: "POST"}); } catch (e) { /* offline: segue com o que tem */ }
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
        <span class="${estrategia.claude?.ok ? "sig-ok" : "sig-warn"}">${estrategia.claude?.ok ? "● Claude pronto" : "● Claude precisa atenção"}</span>
      </div>
      <button onclick="rodarEstrategia()">↻ Atualizar agora</button>
    </div>` : ""}
    ${seoGeo ? `<div class="strategy-card seo-geo-card">
      <div><div class="strategy-title">🔎 Loop SEO + GEO</div>
        <div class="muted">${esc(seoGeo.modo || "recomendação")} · ${fmtDt(seoGeo.atualizado_em)}</div></div>
      <div class="strategy-signals">
        <span>👀 <b>${seoGeo.metricas?.sessoes ?? 0}</b> sessões</span>
        <span>🌿 <b>${seoGeo.metricas?.sessoes_organicas ?? 0}</b> orgânicas</span>
        <span>🎯 <b>${seoGeo.metricas?.leads ?? 0}</b> leads/eventos</span>
        <span>📝 <b>${(seoGeo.pauta || []).length}</b> pautas</span>
      </div>
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
        <span><i class="b-env" style="background:#c9b8e6"></i>enviados</span>
        <span><i class="b-open" style="background:#9fd8e8"></i>aberturas</span>
        <span><i class="b-click" style="background:#45bbd8"></i>cliques</span>
        <span><i class="b-resp" style="background:#29c287"></i>respostas</span>
      </div>
      ${!d.tracking_ativo ? `<p class="muted" style="margin-top:12px">📡 Rastreio de abertura/clique ainda não configurado — defina <code>TRACKING_BASE_URL</code>, <code>TRACKING_SECRET</code> e <code>TRACKING_SYNC_KEY</code> no ambiente. Os demais números são em tempo real.</p>` : `<p class="muted" style="margin-top:12px">📬 Abertura é aproximada (Gmail/Apple inflam por prefetch — ${d.abertos_maquina} aberturas de máquina já descontadas). Clique e resposta são os sinais sólidos. <span style="color:#157a52">● ao vivo</span> — atualiza sozinho a cada 45s.</p>`}
    </div>`;
  // auto-atualização enquanto a Visão Geral estiver aberta (quase em tempo real)
  if (inicioTimer) clearInterval(inicioTimer);
  inicioTimer = setInterval(() => {
    if (location.hash === "" || location.hash.startsWith("#/inicio")) vInicio();
    else { clearInterval(inicioTimer); inicioTimer = null; }
  }, 45000);
}

/* ---------- TELA: Campanhas ---------- */
async function vCampanhas() {
  const d = await api("/api/campaigns");
  view.innerHTML = `
    <div class="toolbar"><h2 style="margin:0">Campanhas</h2><span class="sp"></span>
      <button class="primary" onclick="dlgCampanha()">✨ Nova campanha com IA</button></div>
    ${d.campanhas.map(c => {
      const ativos = c.contatos_por_status.ativo || 0, pend = c.contatos_por_status.pendente || 0,
            pausa = c.contatos_por_status.pausado_resposta || 0, done = c.contatos_por_status.concluido || 0;
      return `<div class="card click" onclick="location.hash='#/campanha/${c.id}'">
        <div class="toolbar" style="margin-bottom:6px">
          <h3 style="margin:0">${esc(c.nome)}</h3>
          ${badge(c.status)} ${badge(c.tipo, "pendente")}
          ${c.cad_min_dias != null ? `<span class="muted">cadência aleatória ${c.cad_min_dias}–${c.cad_max_dias} dias</span>` : ""}
          <span class="sp"></span><span class="muted">${c.steps.length} email(s) · ${c.total_contatos} contato(s) · ${c.enviados} enviado(s)</span>
        </div>
        <div class="muted">${esc(c.objetivo)}</div>
        ${c.sender ? `<div style="margin-top:6px"><span class="tag">✉️ ${esc(c.sender.email)}</span> <small class="muted">${esc(c.sender.proposito || "")}</small></div>` : ""}
        <div style="margin-top:8px">${pend ? badge(pend + " pendentes", "pendente") + " " : ""}
          ${ativos ? badge(ativos + " ativos", "ativo") + " " : ""}
          ${pausa ? badge(pausa + " responderam (pausados)", "pausado_resposta") + " " : ""}
          ${done ? badge(done + " concluídos", "concluido") : ""}</div>
      </div>`; }).join("") || '<div class="card muted">Nenhuma campanha ainda.</div>'}`;
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
    sel.innerHTML = sd.senders.map(s =>
      `<option value="${s.id}" ${s.conectado ? "" : "disabled"}>${esc(s.email)} — ${esc(s.proposito)}${s.conectado ? "" : "  (🔴 precisa login)"}</option>`).join("");
    const first = sd.senders.find(s => s.conectado);
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
async function vCampanha(id) {
  const c = await api("/api/campaigns/" + id);
  const gerando = c.build && c.build.status === "gerando";
  const erroBuild = c.build && c.build.status === "erro";
  const podeAtivar = !gerando && c.steps.length > 0 && c.steps.every(s => s.status_template === "aprovado");
  const banner = gerando ? `<div class="card" style="border-left:4px solid #45bbd8;background:#f2fbfd">
      <div class="spinner" style="width:18px;height:18px;display:inline-block;vertical-align:middle"></div>
      <b style="margin-left:8px">🧠 Gerando os e-mails…</b>
      <span class="muted"> ${(c.build.feitos || 0)}/${c.build.total || "?"} prontos — ${esc(c.build.etapa || "")}</span>
      <div style="margin-top:6px"><small class="muted">Pode deixar a aba aberta; a tela atualiza sozinha.</small></div></div>`
    : erroBuild ? `<div class="card" style="border-left:4px solid #ff6b6b;background:#fff5f5">
      <b>⚠️ A geração falhou:</b> ${esc(c.build.erro || "erro desconhecido")}
      ${["cota", "conector"].includes(c.build.erro_tipo)
        ? `<div style="margin-top:8px"><small class="muted">${c.build.erro_tipo === "conector" ? "O login do Claude foi ajustado para usar os connectors; clique para tentar novamente." : "A geração parou por limite de uso da IA (cota)."} Os ${c.build.feitos || 0} e-mail(s) já prontos foram salvos — clique para continuar de onde parou.</small><br>
           <button class="green" style="margin-top:8px" onclick="campRetomar(${c.id})">🔄 Retomar Criação</button></div>`
        : `<div style="margin-top:6px"><small class="muted">Os e-mails já gerados ficaram salvos. Você pode excluir e tentar de novo.</small></div>`}</div>`
    : "";
  view.innerHTML = `
    <div class="toolbar">
      <a href="#/campanhas">← Campanhas</a>
      <h2 style="margin:0">${esc(c.nome)}</h2> ${badge(c.status)} ${badge(c.tipo, "pendente")}
      <span class="sp"></span>
      ${c.status === "ativa"
        ? `<button class="warn" onclick="campAction(${c.id},'pause')">⏸ Pausar campanha</button>`
        : `<button class="green" ${podeAtivar ? "" : `disabled title='${gerando ? "aguarde a geração terminar" : "aprove todos os templates antes"}'`}
             onclick="campAtivar(${c.id},${c.total_contatos})">▶ ${c.status === "pausada" ? "Retomar campanha" : "Ativar campanha"}</button>`}
      <button onclick="editarCampanha(${c.id})" ${c.status === "ativa" ? "disabled title='Pause a campanha antes de editar'" : ""}>✏️ Editar</button>
      <button onclick="campTeste(${c.id})" ${c.steps.length ? "" : "disabled"}>📨 Enviar teste</button>
      <button class="warn" onclick="campExcluir(${c.id}, ${JSON.stringify(c.nome)})">🗑 Excluir</button>
    </div>
    ${banner}
    <div class="card">
      ${c.sender ? `<div style="margin-bottom:8px"><span class="tag">✉️ ${esc(c.sender.email)}</span> <small class="muted">remetente · ${esc(c.sender.proposito || "")}</small></div>` : ""}
      ${c.inicio_em ? `<div style="margin-bottom:8px">📅 <b>1º envio:</b> ${esc(c.inicio_em)} <small class="muted">(próximos pela cadência)</small></div>` : ""}
      <b>Objetivo:</b> ${esc(c.objetivo)}<br>
      <small class="muted">${esc(c.contexto)}</small></div>
    ${(() => { const m = c.metricas || {}; return `
    <div class="card">
      <h3 style="margin:0 0 10px">📊 Métricas da campanha</h3>
      <div class="row" style="gap:10px;flex-wrap:wrap">
        ${[["Enviados", m.enviados || 0, "", "#50d9fb"],
           ["Aberturas", (m.abertos || 0), (m.taxa_abertura || 0) + "%", "#50d9fb"],
           ["Cliques", (m.cliques || 0), (m.taxa_clique || 0) + "%", "#50d9fb"],
           ["Respostas", (m.respostas || 0), (m.taxa_resposta || 0) + "%", "#29c287"],
           ["Agendamentos", (m.agendados || 0), (m.taxa_agendamento || 0) + "%", "#29c287"],
           ["Descadastros", (m.descadastros || 0), (m.taxa_descadastro || 0) + "%", "#50d9fb"],
           ["E-mails inválidos", (m.invalidos || 0), "bounce", "#ff6b6b"]].map(([rot, n, pct, cor]) => `
          <div style="background:#22093a;border-radius:10px;padding:12px 18px;min-width:120px;text-align:center">
            <div style="font-family:Montserrat,Arial,sans-serif;font-size:24px;font-weight:800;color:${cor}">${n}</div>
            <div style="font-size:12px;color:#a3bac6">${rot}${pct ? " · " + pct : ""}</div>
          </div>`).join("")}
      </div>
      <small class="muted" style="display:block;margin-top:8px">Aberturas/cliques vêm do rastreio (dedup por contato; aberturas de máquina descontadas). <b>E-mails inválidos</b> = endereços que voltaram como inexistentes (hard bounce) e foram removidos do envio. Descadastros por campanha contam a partir dos próximos envios.</small>
    </div>`; })()}
    <div class="row">
      <div class="card grow">
        <h3>Sequência (${c.steps.length} emails)</h3>
        <table><tr><th>#</th><th>Email</th><th>Cadência</th><th>Template</th><th></th></tr>
        ${c.steps.map(s => `<tr>
          <td>${s.ordem}</td><td>${esc(s.nome)}<br><small class="muted">${esc(s.assunto)}</small></td>
          <td>${s.offset_dias != null ? "D+" + s.offset_dias : `sorteio ${c.cad_min_dias}–${c.cad_max_dias}d`}</td>
          <td>${badge(s.status_template)}</td>
          <td><button onclick="previewTpl('${s.template_ref}','${esc(c.contatos[0]?.email || "")}','${c.id}',${s.ordem})">👁 Preview / editar</button>
            ${s.status_template !== "aprovado" ? `<button class="green" onclick="aprovarTpl('${s.template_ref}')">✓ Aprovar</button>` : ""}</td>
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
    <div class="card">
      <h3>Contatos da campanha (${c.contatos.length})</h3>
      <table><tr><th>Empresa</th><th>Contato</th><th>Segmento</th><th>Etapa</th><th>Status</th><th>Próximo envio</th></tr>
      ${c.contatos.map(x => `<tr class="click" onclick="location.hash='#/contato/${encodeURIComponent(x.email)}'">
        <td><b>${esc(x.empresa)}</b></td><td>${esc(x.nome)}<br><small class="muted">${esc(x.email)}</small></td>
        <td>${badge(x.segmento)}</td><td>${x.etapa_atual}/${c.steps.length}</td>
        <td>${badge(x.status)}</td><td>${x.status === "ativo" ? envioLabel(x.next_send_at) : "—"}</td></tr>`).join("")}</table>
    </div>
    <div class="card" id="ads-briefing-card"><h3>📣 Google Ads (espelho da campanha)</h3>
      <span class="muted">carregando…</span></div>`;
  carregarAdsBriefing(c.id);
  // enquanto a IA estiver gerando os e-mails, atualiza a tela sozinha
  if (gerando) {
    const myhash = location.hash;
    setTimeout(() => { if (location.hash === myhash) vCampanha(id); }, 3000);
  }
}

async function carregarAdsBriefing(id) {
  const box = document.getElementById("ads-briefing-card");
  if (!box) return;
  try {
    const r = await api(`/api/campaigns/${id}/ads-briefing`);
    box.innerHTML = renderAdsBriefing(id, r);
  } catch (e) {
    box.innerHTML = `<h3>📣 Google Ads (espelho da campanha)</h3><p class="form-feedback error">${esc(e.message)}</p>`;
  }
}

function renderAdsBriefing(id, r) {
  const utm = r.utm || {};
  const utmStr = `utm_source=${utm.utm_source} · utm_medium=${utm.utm_medium} · utm_campaign=${utm.utm_campaign}`;
  const b = r.briefing;
  const cabecalho = `<h3>📣 Google Ads (espelho da campanha)</h3>
    <p class="muted">Os links dos emails já saem com <code>${utmStr}</code>. O briefing abaixo espelha a campanha
    para a rede de pesquisa — nada é criado na sua conta: cole no Google Ads ou importe o CSV no Ads Editor.</p>`;
  if (!b) return `${cabecalho}
    <button class="primary" onclick="gerarAdsBriefing(${id})">✨ Gerar briefing com IA</button>
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
  box.innerHTML = `<h3>📣 Google Ads</h3><div class="spinner" style="width:18px;height:18px;display:inline-block"></div> <span class="muted">a IA está redigindo o briefing (pode levar ~1min)…</span>`;
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
  document.getElementById("ec-objetivo").value = c.objetivo || "";
  document.getElementById("ec-tipo").value = c.tipo || "finita";
  document.getElementById("ec-min").value = c.cad_min_dias ?? "";
  document.getElementById("ec-max").value = c.cad_max_dias ?? "";
  document.getElementById("ec-inicio").value = (c.inicio_em || "").replace(" ", "T").slice(0, 16);
  const sd = await api("/api/senders");
  const sender = document.getElementById("ec-sender");
  sender.innerHTML = sd.senders.map(s => `<option value="${s.id}" ${s.id === c.sender_id ? "selected" : ""}>${esc(s.email)} — ${esc(s.proposito || "")}${s.conectado ? "" : " (precisa login)"}</option>`).join("");
  document.getElementById("ec-salvar").onclick = async () => {
    const min = document.getElementById("ec-min").value, max = document.getElementById("ec-max").value;
    const body = {nome: document.getElementById("ec-nome").value, objetivo: document.getElementById("ec-objetivo").value,
      sender_id: +sender.value, tipo: document.getElementById("ec-tipo").value,
      cad_min: min ? +min : null, cad_max: max ? +max : (min ? +min : null),
      inicio_em: document.getElementById("ec-inicio").value || ""};
    try { const r = await api(`/api/campaigns/${id}`, {method: "PATCH", json: body});
      dlg.close(); toast(r.nota || "Campanha atualizada ✔"); vCampanha(id);
    } catch (e) { toast("Não foi possível salvar: " + e.message, 6000); }
  };
  dlg.showModal();
}
const frow = (label, n, total) =>
  `<div class="frow"><span class="flabel">${esc(label)}</span>
   <div class="fbar" style="width:${Math.max(4, n / Math.max(total, 1) * 170)}px"></div>
   <span class="fnum">${n}</span></div>`;

async function campAtivar(id, n, confirmacao) {
  if (!confirmacao &&
      !confirm(`Ativar a campanha para ${n} contato(s)?\n\nOs envios começam no próximo ciclo (janela seg-sex 9h-18h), respondem na thread original e param sozinhos se o contato responder.`)) return;
  try {
    const opts = confirmacao ? {json: {confirmacao}} : {method: "POST"};
    const r = await api(`/api/campaigns/${id}/activate`, opts);
    toast(`Campanha ativa ✔ ${r.agendados} contato(s) agendados`); vCampanha(id);
  } catch (e) {
    if (e.data && e.data.requer_confirmacao) {
      const d = e.data;
      const aviso = `⚠ ATENÇÃO: esta campanha inclui ${d.clientes} CLIENTE(S) 4Unik\n` +
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
async function campExcluir(id, nome) {
  if (!confirm(`Excluir a campanha "${nome}"?\n\nIsso remove a campanha, suas etapas e os vínculos com os contatos (os contatos em si permanecem no CRM). Não dá pra desfazer.`)) return;
  try { await api(`/api/campaigns/${id}`, {method: "DELETE"});
    toast("Campanha excluída ✔"); location.hash = "#/campanhas"; route();
  } catch (e) { toast("Erro ao excluir: " + e.message, 5000); }
}
async function aprovarTpl(ref) {
  if (!ref.startsWith("ai:")) return;
  await api(`/api/templates/${ref.slice(3)}/approve`, {method: "POST"});
  toast("Template aprovado ✔"); route();
}
async function previewTpl(ref, email, campaignId = 0, ordem = 0) {
  const html = await api(`/api/templates/preview?ref=${encodeURIComponent(ref)}&email=${encodeURIComponent(email || "")}`);
  const box = document.getElementById("tpl-preview-box");
  if (!box) return;
  const editavel = ref.startsWith("ai:");
  box.innerHTML = `
    <div style="margin-bottom:8px">
      ${editavel
        ? `<button onclick="editarTpl('${ref}','${esc(email || "")}')">✏️ Editar este e-mail</button>`
        : campaignId ? `<button onclick="tornarEmailEditavel(${campaignId},${ordem},'${esc(email || "")}')">✏️ Criar cópia editável</button>`
        : `<small class="muted">template fixo — abra-o dentro de uma campanha para criar uma cópia editável.</small>`}
    </div>
    <iframe class="preview" sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox" referrerpolicy="no-referrer"></iframe>`;
  box.querySelector("iframe").srcdoc = previewHtmlSeguro(html);
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
async function editarTpl(ref, email) {
  if (!ref.startsWith("ai:")) return;
  const t = await api(`/api/templates/${ref.slice(3)}/raw`);
  const box = document.getElementById("tpl-preview-box");
  if (!box) return;
  const f = (id, label, val) => `<label style="display:block;margin:8px 0 2px;font-size:13px;font-weight:600">${label}</label>
    <input id="ed-${id}" style="width:100%;box-sizing:border-box" value="${esc(val || "")}">`;
  box.innerHTML = `
    <div class="card" style="background:#fafbfc">
      <b>✏️ Editando o e-mail</b> <small class="muted">— ao salvar, volta a “pendente” para você reconferir antes de ativar.</small>
      ${f("nome", "Nome interno", t.nome)}
      ${f("assunto", "Assunto", t.assunto)}
      ${f("preheader", "Preheader (prévia que aparece na caixa de entrada)", t.preheader)}
      ${f("cta", "Botão (rótulo)", t.cta)}
      ${f("cta_url", "Botão (URL)", t.cta_url)}
      <label style="display:block;margin:8px 0 2px;font-size:13px;font-weight:600">Corpo do e-mail (HTML do miolo)</label>
      <textarea id="ed-inner_html" rows="16" style="width:100%;box-sizing:border-box;font-family:ui-monospace,Menlo,monospace;font-size:12px">${esc(t.inner_html || "")}</textarea>
      <label style="display:block;margin:8px 0 2px;font-size:13px;font-weight:600">Versão texto simples</label>
      <textarea id="ed-texto_plano" rows="6" style="width:100%;box-sizing:border-box">${esc(t.texto_plano || "")}</textarea>
      <div style="margin-top:10px">
        <button class="green" onclick="salvarTpl('${ref}','${esc(email || "")}')">💾 Salvar</button>
        <button onclick="previewTpl('${ref}','${esc(email || "")}')">Cancelar</button>
      </div>
    </div>`;
  box.scrollIntoView({behavior: "smooth"});
}

async function salvarTpl(ref, email) {
  const val = id => document.getElementById("ed-" + id)?.value;
  const payload = {nome: val("nome"), assunto: val("assunto"), preheader: val("preheader"), cta: val("cta"),
                   cta_url: val("cta_url"), inner_html: val("inner_html"), texto_plano: val("texto_plano")};
  try {
    await api(`/api/templates/${ref.slice(3)}`, {json: payload});
    toast("E-mail salvo ✔ (voltou a pendente para reconferir)", 3500);
    // re-renderiza a campanha primeiro (atualiza status na tabela), depois reabre o preview no box novo
    if (location.hash.startsWith("#/campanha/")) await vCampanha(location.hash.split("/")[2]);
    await previewTpl(ref, email);
  } catch (e) { toast("Erro ao salvar: " + e.message, 5000); }
}

/* ---------- TELA: Respostas (auto-resposta + histórico) ---------- */
async function vRespostas() {
  const d = await api("/api/replies");
  const cf = d.config, on = cf.auto_reply;
  const classHtml = Object.entries(cf.classes_seguras).map(([k, desc]) => {
    const ativa = cf.classes_ativas.includes(k);
    return `<label class="classrow ${ativa ? "on" : ""}">
      <span class="switch"><input type="checkbox" class="rc-class" value="${k}" ${ativa ? "checked" : ""} ${on ? "" : "disabled"}><span class="sl"></span></span>
      <span><b>${esc(k)}</b><div class="desc">${esc(desc)}</div></span></label>`;
  }).join("");
  view.innerHTML = `
    <h2>Respostas</h2>
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
      <div class="toolbar"><h3 style="margin:0">Histórico de respostas (${d.total})</h3>
        <span class="muted">✅ ${d.stats.auto_respondidas} auto-respondidas · 🙋 ${d.stats.escaladas} escaladas</span>
        <span class="sp"></span>
        <button onclick="api('/api/replies/scan',{method:'POST'}).then(r=>{toast('Varredura: '+r.novas+' nova(s)');vRespostas()})">🔄 Varrer agora</button>
      </div>
      <table><tr><th>Empresa / Contato</th><th>Classe</th><th>Conf.</th><th>Data</th><th>Ação</th><th>Resumo</th><th></th></tr>
      ${d.respostas.map(r => `<tr>
        <td><b>${esc(r.empresa || "—")}</b><br><small class="muted">${esc(r.nome || r.email)}</small></td>
        <td>${badge(r.classe)}</td><td>${Math.round((r.confianca || 0) * 100)}%</td>
        <td>${esc(r.data)}</td>
        <td>${r.acao === "auto-respondida" ? badge("auto-respondida") : badge("escalada")}</td>
        <td><small>${esc(r.resumo || "")}</small></td>
        <td><a href="${r.gmail}" target="_blank"><small>Gmail ↗</small></a></td></tr>`).join("")
        || '<tr><td colspan="7" class="muted">nenhuma resposta detectada ainda</td></tr>'}</table>
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
const spill = s => s.ativo === 0 ? '<span class="spill inativo">⚪ inativo</span>'
  : (s.conectado ? '<span class="spill on">🟢 conectado</span>'
                 : `<span class="spill off">🔴 ${esc(s.status_motivo || "precisa login")}</span>`);
const iniciais = e => (e || "?").slice(0, 2).toUpperCase();
const pct = (v, max) => `<div class="barwrap"><i style="width:${Math.min(100, v / Math.max(max, 1) * 100)}%"></i></div>`;

async function vRemetentes() {
  const d = await api("/api/senders");
  const ss = d.senders;
  const maxEnv = Math.max(1, ...ss.map(s => s.stats.enviados));
  view.innerHTML = `
    <h2>Remetentes</h2>
    <p class="muted" style="margin-top:0">Cada remetente é uma conta de e-mail própria (perfil gws isolado).
    Escolha o remetente certo na criação de campanha — você sempre sabe quem está enviando o quê.</p>

    <div class="card">
      <h3>📊 Performance global (todos os remetentes)</h3>
      <table class="perf-table"><tr><th>Remetente</th><th>Propósito</th><th>Status</th>
        <th>Enviados</th><th>Abertura</th><th>Clique</th><th>Resposta</th></tr>
      ${ss.map(s => `<tr class="click" onclick="location.hash='#/remetente/${s.id}'">
        <td><b>${esc(s.email)}</b></td><td><small class="muted">${esc(s.proposito)}</small></td>
        <td>${spill(s)}</td>
        <td>${s.stats.enviados} ${pct(s.stats.enviados, maxEnv)}</td>
        <td>${s.stats.taxa_abertura}%</td><td>${s.stats.taxa_clique}%</td><td>${s.stats.taxa_resposta}%</td>
      </tr>`).join("")}</table>
    </div>

    <div class="toolbar" style="margin-top:6px">
      <h3 style="margin:0">Gerenciar contas</h3>
      <button class="primary" id="novo-remetente-btn">＋ Novo remetente</button>
    </div>
    <div class="card" id="novo-remetente-form" style="display:none;margin-top:12px">
      <h3 style="margin-top:0">Cadastrar remetente</h3>
      <p class="muted">O cadastro não envia e-mail. A conta começa desconectada; depois, use <b>Conectar conta</b> para autorizar o Gmail nesse perfil isolado.</p>
      <form id="sender-create-form">
        <div class="row" style="gap:12px;align-items:end">
          <div class="field grow"><label for="sender-email">E-mail *</label><input id="sender-email" type="email" required placeholder="voce@empresa.com"></div>
          <div class="field grow"><label for="sender-nome">Nome</label><input id="sender-nome" placeholder="Ex.: Genau Junior"></div>
          <div class="field grow"><label for="sender-proposito">Propósito</label><input id="sender-proposito" placeholder="Ex.: Operação e administração"></div>
        </div>
        <div class="row" style="gap:8px"><button class="primary" type="submit" id="sender-create-submit">Cadastrar remetente</button><button type="button" id="cancelar-remetente">Cancelar</button></div>
        <p class="form-feedback" id="sender-create-feedback" role="status" aria-live="polite"></p>
      </form>
    </div>
    ${ss.map(s => senderCard(s, d.classes_seguras)).join("")}`;
  wireSenders(d.classes_seguras);
  document.getElementById("novo-remetente-btn").onclick = () => {
    const form = document.getElementById("novo-remetente-form");
    form.style.display = "block"; document.getElementById("sender-email").focus();
  };
  document.getElementById("cancelar-remetente").onclick = () => document.getElementById("novo-remetente-form").style.display = "none";
  document.getElementById("sender-create-form").onsubmit = senderCriar;
}

function senderCard(s, classesSeguras) {
  const conectar = s.slug && !s.conectado ? `
    <div class="connectbox" id="cb-${s.id}" style="display:none">
      <b>Conectar ${esc(s.email)}</b> — rode este comando no terminal e logue como essa conta no navegador:
      <code>! HOME=__HOME__ gws auth login -s gmail</code>
      <button class="primary" onclick="senderVerificar(${s.id})">Já rodei — Verificar</button>
    </div>` : "";
  const auto = s.auto_reply_on;
  return `<div class="sender-card">
    <div class="top">
      <div class="av">${iniciais(s.email)}</div>
      <div style="flex:1"><span class="em">${esc(s.email)}</span> ${spill(s)}
        <div class="pp">${esc(s.nome)} · ${esc(s.proposito)}</div></div>
      ${s.slug ? (s.conectado
        ? `<button onclick="location.hash='#/remetente/${s.id}'">Ver detalhes →</button>`
        : `<button class="primary" onclick="senderConectar(${s.id})">🔌 Conectar conta</button>`)
        : '<span class="muted">conta principal</span>'}
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
  try {
    const r = await api(`/api/senders/${id}/setup`, {method: "POST"});
    const cb = document.getElementById("cb-" + id);
    if (cb) { cb.style.display = "block"; cb.querySelector("code").textContent = "! " + r.comando; cb.scrollIntoView({behavior: "smooth"}); }
  } catch (e) {
    toast(`Não foi possível preparar a conexão: ${e.message}`, 6000);
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
      <h2 style="margin:0">${esc(s.email)}</h2> ${spill(s)}</div>
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
  const funis = ["", "novo", "contatado", "follow-up", "último-toque", "respondeu", "agendou", "encerrado"];
  const [td, ld] = await Promise.all([api("/api/tags"), api("/api/lead-segments")]);
  let crmPage = 1;
  view.innerHTML = `
    <div class="crm-head"><div><h2>CRM inteligente</h2>
      <p>Prioriza leads completos e com telefone. Busque por qualquer característica comercial.</p></div>
      <div class="toolbar"><button class="primary" onclick="document.getElementById('dlg-contato').showModal()">＋ Novo contato</button>
      <label class="btn">📥 Importar<input type="file" id="crm-up" accept=".csv,.xlsx,.xls" hidden></label>
      <button id="crm-export">⬇ Exportar CSV</button></div></div>
    <div class="card crm-filters">
      <input id="crm-q" class="crm-search" placeholder="🔎 Nome, empresa, e-mail, telefone, cargo, categoria, interesse, comentário…">
      <select id="crm-perfil"><option value="">🎯 Todos os perfis</option>${ld.segmentos.map(s => `<option value="${s.id}">${esc(s.nome)} (${s.contatos})</option>`).join("")}</select>
      <select id="crm-score"><option value="0">Qualquer score</option><option value="40">Score ≥ 40</option><option value="50">Score ≥ 50</option><option value="70">Score ≥ 70</option></select>
      <select id="crm-funil">${funis.map(f => `<option value="${f}">${f || "Todos os funis"}</option>`).join("")}</select>
      <select id="crm-resp"><option value="">Qualquer resposta</option><option>sim</option><option value="não">Não respondeu</option></select>
      <select id="crm-tag"><option value="">🏷️ Todas as categorias</option>${td.tags.map(t => `<option value="${esc(t.tag)}">${esc(t.tag)} (${t.contatos})</option>`).join("")}</select>
      <select id="crm-ordem"><option value="prioridade">🔥 Mais quentes primeiro</option><option value="recentes">Mais recentes</option><option value="nome">Nome A–Z</option><option value="empresa">Empresa A–Z</option></select>
      <select id="crm-size"><option value="20">20 por página</option><option value="50" selected>50 por página</option><option value="100">100 por página</option><option value="200">200 por página</option></select>
      <button id="crm-limpar">Limpar filtros</button>
    </div><div id="crm-tbl">carregando…</div>`;
  const inicial = ld.segmentos.find(s => s.id === "com_telefone" && s.contatos > 0);
  if (inicial) document.getElementById("crm-perfil").value = "com_telefone";
  const params = (page = crmPage) => new URLSearchParams({
    q: document.getElementById("crm-q").value, funil: document.getElementById("crm-funil").value,
    respondeu: document.getElementById("crm-resp").value, tag: document.getElementById("crm-tag").value,
    perfil: document.getElementById("crm-perfil").value, score_min: document.getElementById("crm-score").value,
    ordenar: document.getElementById("crm-ordem").value, page, page_size: document.getElementById("crm-size").value});
  const load = async () => {
    const d = await api("/api/contacts?" + params());
    const r = d.resumo || {};
    document.getElementById("crm-tbl").innerHTML = `
      <div class="crm-summary">
        <div><b>${d.total}</b><span>contatos filtrados</span></div><div><b>${r.telefones || 0}</b><span>com telefone</span></div>
        <div><b>${r.historicos || 0}</b><span>com histórico</span></div><div><b>${r.quentes || 0}</b><span>leads quentes</span></div>
      </div>
      <div class="lead-grid">${d.contatos.map(x => `<article class="lead-card" onclick="location.hash='#/contato/${encodeURIComponent(x.email)}'">
        <div class="lead-top"><div><h3>${esc(x.nome || x.email.split("@")[0])}</h3><div class="lead-company">${esc(x.empresa || "Empresa não informada")}</div></div>
          <div class="score ${x.lead_score >= 70 ? "hot" : x.lead_score >= 50 ? "warm" : ""}"><b>${x.lead_score || 0}</b><small>/100</small></div></div>
        <div class="lead-contact"><span>✉ ${esc(x.email)}</span><span class="${x.telefone ? "has-phone" : "muted"}">☎ ${esc(x.telefone || "sem telefone")}</span></div>
        <div class="lead-profile"><b>${esc(x.cargo || "Cargo não informado")}</b><span>${esc(x.porte_assinantes || x.porte_funcionarios || x.erp || "")}</span></div>
        <div class="lead-tags">${badge(x.segmento || "sem segmento", x.segmento)} ${badge(x.funil)} ${(x.tags || []).slice(0, 3).map(tagChip).join(" ")}</div>
        <div class="lead-history">${esc(x.qualificacao || x.resumo_historico || x.enriquecimento || "Sem histórico comercial")}</div>
      </article>`).join("") || '<div class="card muted">Nenhum lead encontrado.</div>'}</div>
      <div class="pager"><button id="crm-prev" ${d.page <= 1 ? "disabled" : ""}>← Anterior</button><span>Página <b>${d.page}</b> de <b>${d.pages}</b> · ${d.total} resultado(s)</span><button id="crm-next" ${d.page >= d.pages ? "disabled" : ""}>Próxima →</button></div>`;
    document.getElementById("crm-prev").onclick = () => { crmPage--; load(); };
    document.getElementById("crm-next").onclick = () => { crmPage++; load(); };
  };
  let timer;
  document.getElementById("crm-q").oninput = () => { clearTimeout(timer); timer = setTimeout(() => { crmPage = 1; load(); }, 220); };
  ["crm-funil", "crm-resp", "crm-tag", "crm-perfil", "crm-score", "crm-ordem", "crm-size"].forEach(id => document.getElementById(id).onchange = () => { crmPage = 1; load(); });
  document.getElementById("crm-limpar").onclick = () => { ["crm-q", "crm-funil", "crm-resp", "crm-tag", "crm-perfil"].forEach(id => document.getElementById(id).value = ""); document.getElementById("crm-score").value = "0"; crmPage = 1; load(); };
  document.getElementById("crm-export").onclick = async () => {
    const res = await fetch("/api/contacts/export.csv?" + params(1), {headers: {"X-API-Key": window.API_KEY}});
    if (!res.ok) return toast("Não foi possível exportar os leads.");
    const blob = await res.blob(), a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = "leads-4unik.csv"; a.click(); URL.revokeObjectURL(a.href); toast("CSV exportado ✔");
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
      return `<tr class="${it.is_contato ? "click" : ""}" ${ir}>
        <td><b>${titulo}</b><br><small class="muted">${esc(it.email)}</small></td>
        <td class="det-meta">${esc(it.detalhe || "")}</td>
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
      <button class="primary" onclick="contactApplyEnrichment('${esc(email)}')">Revisar e aplicar selecionados</button>` : '<p class="muted">Nenhum dado novo confiável foi encontrado.</p>'}
      ${(r.fontes || []).length ? `<details><summary>Fontes consultadas (${r.fontes.length})</summary>${r.fontes.map(f => `<p><a href="${esc(f.url)}" target="_blank">${esc(f.titulo || f.url)} ↗</a><br><small>${esc(f.evidencia || "")}</small></p>`).join("")}</details>` : ""}</div>`;
  } catch(e) { box.innerHTML = `<div class="enrich-box error"><b>Enriquecimento não concluído</b><p>${esc(e.message)}</p></div>`; }
}
function contactApplyEnrichment(email) {
  const picks = [...document.querySelectorAll(".enrich-pick:checked")];
  if (!picks.length) return toast("Selecione pelo menos uma sugestão.");
  contactToggleEdit(true);
  picks.forEach(p => { const el = document.getElementById("ce-" + p.dataset.field); if (el) el.value = p.dataset.value; });
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
    view.innerHTML = `<div class="toolbar"><div><a href="#/inicio">← Voltar ao início</a><h2 style="margin:6px 0 0">Loop contínuo SEO + GEO</h2></div><span class="sp"></span><button class="primary" onclick="rodarSeoGeo()">↻ Atualizar análise</button></div>
      <div class="card"><p>${esc(r.guardrail || "")}</p><div class="strategy-signals"><span>👀 ${r.metricas?.sessoes ?? 0} sessões</span><span>🌿 ${r.metricas?.sessoes_organicas ?? 0} orgânicas</span><span>🎯 ${r.metricas?.leads ?? 0} eventos de lead</span></div>
      ${(r.erros || []).length ? `<p class="microcopy">⚠ ${esc(r.erros.join(" · "))}</p>` : ""}</div>
      <section class="seo-geo-grid"><div class="card"><h3>Prioridades com evidência</h3>${pri}</div><div class="card"><h3>Pauta GEO para revisão</h3>${pauta || '<p class="muted">Pauta disponível no próximo ciclo.</p>'}</div></section>`;
  } catch (e) { toast("Não foi possível abrir o loop SEO/GEO: " + e.message, 6000); }
}
async function rodarSeoGeo() {
  try { const r = await api("/api/seo-geo/run", {method:"POST"}); toast(`SEO/GEO atualizado · ${(r.prioridades || []).length} prioridade(s)`, 4000); abrirSeoGeo(); }
  catch (e) { toast("Não foi possível atualizar: " + e.message, 6000); }
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
      const t = prompt(`Esta campanha inclui ${e.data.clientes} cliente(s) 4Unik. Para disparar, digite QUERO_ENVIAR.`, "");
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
  const html = await api(`/api/templates/preview?ref=${encodeURIComponent(ref)}`);
  document.getElementById("tpl-frame").srcdoc = previewHtmlSeguro(html);
}

async function editarTplGeral(ref) {
  if (!ref.startsWith("ai:")) return;
  const t = await api(`/api/templates/${ref.slice(3)}/raw`);
  const head = document.getElementById("tpl-head");
  if (!head) return;
  const f = (id, label, value) => `<label style="display:block;margin:8px 0 2px;font-size:13px;font-weight:700">${label}</label><input id="tg-${id}" value="${esc(value || "")}" style="width:100%">`;
  head.innerHTML = `<b>✏️ Editando template</b> <small class="muted">Salvar devolve o template para pendente; aprove-o novamente antes de usar em uma campanha.</small>
    ${f("nome", "Nome interno", t.nome)}${f("assunto", "Assunto", t.assunto)}${f("preheader", "Preheader", t.preheader)}${f("cta", "CTA", t.cta)}${f("cta_url", "URL do CTA", t.cta_url)}
    <label style="display:block;margin:8px 0 2px;font-size:13px;font-weight:700">HTML do e-mail</label><textarea id="tg-inner_html" rows="12" style="width:100%;font-family:ui-monospace,Menlo,monospace;font-size:12px">${esc(t.inner_html || "")}</textarea>
    <label style="display:block;margin:8px 0 2px;font-size:13px;font-weight:700">Texto simples</label><textarea id="tg-texto_plano" rows="4" style="width:100%">${esc(t.texto_plano || "")}</textarea>
    <div style="margin-top:10px"><button class="green" id="tg-save">💾 Salvar template</button><button id="tg-cancel">Cancelar</button></div>`;
  document.getElementById("tg-cancel").onclick = () => tplOpen(ref);
  document.getElementById("tg-save").onclick = async () => {
    const v = k => document.getElementById("tg-" + k).value;
    try {
      await api(`/api/templates/${ref.slice(3)}`, {json: {nome:v("nome"), assunto:v("assunto"), preheader:v("preheader"), cta:v("cta"), cta_url:v("cta_url"), inner_html:v("inner_html"), texto_plano:v("texto_plano")}});
      toast("Template salvo — revise e aprove antes de enviar ✔", 4000); vTemplates();
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
      <button id="gm-discover">⌕ Encontrar GA4</button><button class="primary" id="gm-sync">↻ Sincronizar métricas</button></div>
    <p class="muted">Consolida dados de Analytics, Tag Manager e Ads para acompanhar campanhas. A integração é <b>somente leitura</b>: não publica tags, nem cria ou altera anúncios.</p>
    <div class="media-health">${pill(s.ga4_pronto, "GA4")} ${pill(s.gtm_pronto, "Tag Manager")} ${pill(s.ads_pronto, "Google Ads")}</div>
    <div class="card" id="gm-result"><div class="media-connected">
      <div><b>GA4 conectado</b><br><small class="muted">Conta ${esc(s.ga4?.account_id || "—")} · Property ${esc(s.ga4?.property_id || "—")} · Landing ${esc(s.ga4?.measurement_id || "a selecionar")} · Site ${esc(s.ga4?.site_measurement_id || "não informado")}</small></div>
      <div><button id="gm-goals">🎯 Configurar eventos-chave de aquisição</button> <button class="primary" id="gm-sync-top">↻ Atualizar agora</button></div></div>
      <p class="muted">Eventos-chave oficiais: <code>generate_lead</code>, <code>schedule_demo</code>, <code>contact_whatsapp</code> (+55 11 2684-4724) e <code>contact_email</code> (comercial@4unik.com.br).</p>
      <div id="gm-metrics"><span class="muted">Carregando métricas salvas…</span></div></div>
    <div class="row">
      <div class="card grow"><h3>Configuração</h3>
        <p class="muted">Credenciais já gravadas não são exibidas. Deixe um campo vazio para mantê-la.</p>
        <div class="field"><label>GA4 Account ID</label><input id="gm-ga4-account" value="${esc(s.ga4?.account_id || "")}" placeholder="ex.: 66932658" style="width:100%"></div>
        <div class="field"><label>GA4 Property ID</label><input id="gm-ga4" value="${esc(s.ga4?.property_id || "")}" placeholder="ex.: 327916606" style="width:100%"></div>
        <div class="field"><label>GA4 Measurement ID</label><input id="gm-measurement" value="${esc(s.ga4?.measurement_id || "")}" placeholder="ex.: G-XXXXXXXXXX" style="width:100%"><small class="muted">Stream da Plataforma Landing. O dashboard não injeta tags no site; configure a tag no GTM.</small></div>
        <div class="field"><label>GA4 Measurement ID — site 4unik.com.br</label><input id="gm-site-measurement" value="${esc(s.ga4?.site_measurement_id || "")}" placeholder="ex.: G-XXXXXXXXXX" style="width:100%"></div>
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
        <div class="field"><label>URL de destino</label><input id="gm-url" placeholder="https://plataforma.4unik.com.br/..." style="width:100%"></div>
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
      <button class="${sel("plataforma", site)}" onclick="vSite('plataforma',${days})">plataforma.4unik.com.br</button>
      <button class="${sel("site", site)}" onclick="vSite('site',${days})">4unik.com.br</button>
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
      (Studio → Leads e audiência) e um email para <b>comercial@4unik.com.br</b>. A coluna Origem é o
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
      ${(cf.ga4_sem_campanha_local || []).length ? `<p class="muted">Também no GA4 com utm_source=4unik-mail, sem campanha local correspondente: ${cf.ga4_sem_campanha_local.map(x => esc(x.utm_campaign)).join(", ")}</p>` : ""}
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
    KBs (markdown) viram conhecimento e as imagens ficam disponíveis para a IA citar nos emails.</p>
    <div class="row">
      <div class="card grow"><h3>KBs (dashboard/context/kb/)</h3>
        ${d.kb.map(k => `<div class="card"><b>${esc(k.nome)}</b> <small class="muted">${k.tamanho} bytes</small>
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

/* ---------- TELA: Qualidade (normalização de nomes) ---------- */
let nmStatus = "todos";   // todos | pendente | aprovado | aplicado | rejeitado
function nomesFiltrar(s) { nmStatus = s; vNomes(); }
async function vNomes() {
  const prog = await api("/api/nomes/analise");
  const d = await api(`/api/nomes/propostas?status=${nmStatus === "todos" ? "" : nmStatus}`);
  const counts = d.counts || {};
  const analisando = prog.status === "analisando";
  const aprovados = counts.aprovado || 0;
  const filtros = ["todos", "pendente", "aprovado", "aplicado", "rejeitado"];
  const banner = analisando
    ? `<div class="card" style="border-left:4px solid #45bbd8;background:#f2fbfd">
        <div class="spinner" style="width:18px;height:18px;display:inline-block;vertical-align:middle"></div>
        <b style="margin-left:8px">🧠 Analisando os nomes com IA…</b>
        <span class="muted"> ${prog.feitos || 0}/${prog.total || "?"} — ${esc(prog.etapa || "")}</span></div>`
    : prog.status === "erro"
    ? `<div class="card" style="border-left:4px solid #ff6b6b;background:#fff5f5"><b>⚠️ A análise falhou:</b> ${esc(prog.erro || "")}</div>`
    : "";
  view.innerHTML = `
    <div class="toolbar"><h2 style="margin:0">Qualidade — Nomes</h2>
      <span class="sp"></span>
      <button class="primary" ${analisando ? "disabled" : ""} onclick="nomesAnalisar()">🔍 Analisar nomes da base</button>
    </div>
    ${banner}
    <div class="card">
      <b>Propostas:</b> ${counts.pendente || 0} pendente(s) · ${aprovados} aprovada(s) · ${counts.aplicado || 0} já aplicada(s) · ${counts.rejeitado || 0} rejeitada(s)
      <div style="margin-top:10px">
        Aprovar todas pendentes com confiança ≥
        <input id="nm-conf" type="number" step="0.05" min="0" max="1" value="0.90" style="width:70px">
        <button onclick="nomesAprovarConf()">✓ Aprovar em lote</button>
        <span class="sp"></span>
        <button class="green" ${aprovados ? "" : "disabled"} onclick="nomesAplicar(${aprovados})">💾 Aplicar aprovados (${aprovados})</button>
      </div>
      <div style="margin-top:10px">Filtrar: ${filtros.map(s =>
        `<button onclick="nomesFiltrar('${s}')" ${s === nmStatus ? 'class="primary"' : ""}>${s}${counts[s] != null ? " (" + counts[s] + ")" : ""}</button>`).join(" ")}</div>
    </div>
    <div class="card">
      <h3>Propostas — ${nmStatus} (${d.total})</h3>
      ${d.total === 0 ? '<span class="muted">Nada aqui. Troque o filtro acima ou rode a análise.</span>' : `
      <table><tr><th>Empresa</th><th>Nome atual</th><th>→ Sugerido (editável)</th><th>Ação</th><th>Conf.</th><th>Status</th><th>Motivo</th><th></th></tr>
      ${d.propostas.map(p => `<tr ${p.confianca < 0.7 ? 'style="background:#fff8e8"' : ""}>
        <td><b>${esc(p.empresa || "—")}</b><br><small class="muted">${esc(p.email)}</small></td>
        <td>${esc(p.nome_atual || "—")}</td>
        <td>${p.acao === "limpar"
              ? '<span class="muted">(limpar — não é pessoa)</span>'
              : `<input id="nm-${esc(p.email)}" value="${esc(p.nome_sugerido || "")}" style="width:100%;box-sizing:border-box">`}</td>
        <td>${badge(p.acao)}</td>
        <td>${Math.round((p.confianca || 0) * 100)}%</td>
        <td>${badge(p.status)}</td>
        <td><small>${esc(p.motivo || "")}</small></td>
        <td style="white-space:nowrap">
          ${p.status !== "aplicado" ? `
          <button class="green" title="aprovar" onclick="nomesAprovarUm('${esc(p.email)}','${p.acao}')">✓</button>
          <button class="warn" title="rejeitar" onclick="nomesRejeitarUm('${esc(p.email)}')">✗</button>
          ${p.status === "aprovado" ? `<button title="voltar a pendente" onclick="nomesPendente('${esc(p.email)}')">↩</button>` : ""}` : "—"}</td>
      </tr>`).join("")}</table>`}
    </div>`;
  if (analisando) { const myhash = location.hash;
    setTimeout(() => { if (location.hash === myhash) vNomes(); }, 3000); }
}
async function nomesAnalisar() {
  try { await api("/api/nomes/analisar", {json: {}}); toast("Análise iniciada 🧠", 3000); vNomes(); }
  catch (e) { toast("Erro: " + e.message, 5000); }
}
async function nomesAprovarConf() {
  const min = parseFloat(document.getElementById("nm-conf").value || "0.9");
  const r = await api("/api/nomes/aprovar-lote", {json: {min_conf: min}});
  toast(`${r.aprovados} proposta(s) aprovada(s) ✔`, 3000); vNomes();
}
async function nomesAprovarUm(email, acao) {
  const inp = document.getElementById("nm-" + email);
  const body = {status: "aprovado"};
  if (acao !== "limpar" && inp) body.nome_sugerido = inp.value;
  await api(`/api/nomes/propostas/${encodeURIComponent(email)}`, {json: body});
  toast("Aprovada ✔", 1500); vNomes();
}
async function nomesRejeitarUm(email) {
  await api(`/api/nomes/propostas/${encodeURIComponent(email)}`, {json: {status: "rejeitado"}});
  toast("Rejeitada", 1500); vNomes();
}
async function nomesPendente(email) {
  await api(`/api/nomes/propostas/${encodeURIComponent(email)}`, {json: {status: "pendente"}});
  toast("Voltou a pendente", 1500); vNomes();
}
async function nomesAplicar(n) {
  if (!confirm(`Aplicar ${n} correção(ões) de nome aos contatos? Isso altera o campo nome no CRM (não envia nada).`)) return;
  const r = await api("/api/nomes/aplicar", {method: "POST"});
  toast(`${r.aplicados} nome(s) corrigido(s) no CRM ✔`, 4000); vNomes();
}

/* ---------- router ---------- */
async function route() {
  const h = location.hash || "#/inicio";
  const [, r, arg] = h.split("/");
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
    else if (r === "site") await vSite();
    else if (r === "midia") await vMidia();
    else if (r === "contexto") await vContexto();
    else await vInicio();
  } catch (e) { view.innerHTML = `<div class="card">⚠️ ${esc(e.message)}</div>`; }
}
window.addEventListener("hashchange", route);
route(); pollStatus();
