"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const els = {
  form:       $("#address-form"),
  uri:        $("#uri"),
  format:     $("#format"),
  go:         $("#go"),
  menuBtn:    $("#menu-btn"),
  menu:       $("#menu"),
  advanced:   $("#advanced"),
  registry:   $("#registry"),
  insecure:   $("#insecure"),
  skip:       $("#skip-verify"),
  status:     $("#status"),
  prettyPre:  $("#pretty-pre"),
  prettyIfr:  $("#pretty-iframe"),
  raw:        $("#raw"),
  headers:    $("#headers"),
  agentTabs:  $("#agent-tabs"),
  newTab:     $("#new-tab"),
  histPanel:  $("#history-panel"),
  histList:   $("#history-list"),
  histEmpty:  $("#history-empty"),
  histClose:  $("#hist-close"),
  // methods explorer
  mTabBadge:  $("#methods-tab-badge"),
  mEmpty:     $("#methods-empty"),
  mError:     $("#methods-error"),
  mContent:   $("#methods-content"),
  actionsGrid:    $("#actions-grid"),
  actionsSummary: $("#actions-summary"),
  embeddedSection: $("#embedded-section"),
  customSection:   $("#custom-section"),
  embeddedList:    $("#embedded-list"),
  customList:      $("#custom-list"),
  embeddedCount:   $("#embedded-count"),
  customCount:     $("#custom-count"),
  // invocations
  invList:    $("#invocations-list"),
  invEmpty:   $("#invocations-empty"),
  invClear:   $("#inv-clear"),
};

// Per-host:port DISCOVER cache, keyed across tabs in this session.
const methodsCacheByEndpoint = new Map();
// localStorage key for invocation history.
const INV_KEY = "elemen.invocations.v1";
const INV_LIMIT = 100;

const state = {
  tabs: [],         // [{ id, uri, format, registry, insecure, skip, result, status }]
  activeId: null,
  history: [],
  respPane: "pretty",
};

let tabCounter = 0;
const newId = () => `t${++tabCounter}`;

// ---------- API readiness ----------
function whenApiReady() {
  return new Promise((resolve) => {
    if (window.pywebview && window.pywebview.api) return resolve();
    window.addEventListener("pywebviewready", () => resolve(), { once: true });
  });
}

// ---------- helpers ----------
function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function shortUri(uri) {
  if (!uri) return "(new tab)";
  const m = uri.match(/^agtp:\/\/([0-9a-f]{1,64})(.*)$/i);
  if (m) return `agtp://${m[1].slice(0, 10)}…${m[2]}`;
  return uri.length > 32 ? uri.slice(0, 30) + "…" : uri;
}

// Pull a human-readable agent name from a successful response body. Falls
// back to null if the format isn't easily parseable or the field is absent.
function nameFromBody(body, format) {
  if (!body) return null;
  try {
    if (format === "json") {
      const obj = JSON.parse(body);
      return typeof obj?.name === "string" ? obj.name.trim() : null;
    }
    if (format === "yaml") {
      // First top-level `name: ...` line. Strips optional quotes.
      const m = body.match(/^name:\s*"?([^"\n#]+?)"?\s*(?:#.*)?$/m);
      return m ? m[1].trim() : null;
    }
    if (format === "html") {
      // Use <title> minus any " — ..." / " - ..." suffix.
      const m = body.match(/<title>([^<]+)<\/title>/i);
      if (!m) return null;
      const raw = m[1].trim();
      return raw.split(/\s+[—-]\s+/)[0].trim() || null;
    }
  } catch {
    /* ignore */
  }
  return null;
}

function highlightJson(str) {
  const re = /("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false)\b|\bnull\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|[{}\[\],]/g;
  return str.replace(re, (match, strLit, colon) => {
    if (strLit !== undefined) {
      const cls = colon ? "j-key" : "j-str";
      const tail = colon ? `<span class="j-punct">${colon}</span>` : "";
      return `<span class="${cls}">${escapeHtml(strLit)}</span>${tail}`;
    }
    if (match === "true" || match === "false") return `<span class="j-bool">${match}</span>`;
    if (match === "null") return `<span class="j-null">${match}</span>`;
    if (/^[{}\[\],]$/.test(match)) return `<span class="j-punct">${match}</span>`;
    return `<span class="j-num">${match}</span>`;
  });
}

// ---------- tab state ----------
function getActive() {
  return state.tabs.find((t) => t.id === state.activeId);
}

function snapshotFormToTab(tab) {
  if (!tab) return;
  tab.uri = els.uri.value;
  tab.format = els.format.value;
  tab.registry = els.registry.value;
  tab.insecure = els.insecure.checked;
  tab.skip = els.skip.checked;
}

function loadTabIntoForm(tab) {
  els.uri.value = tab.uri || "";
  els.format.value = tab.format || "json";
  els.registry.value = tab.registry || "";
  els.insecure.checked = !!tab.insecure;
  els.skip.checked = !!tab.skip;
  renderResponse(tab);
  renderMethods(tab);
  setStatus(tab.status?.text ?? "Ready.", tab.status?.kind ?? "idle");
}

function newTab(opts = {}) {
  const tab = {
    id: newId(),
    uri: opts.uri ?? "",
    format: opts.format ?? "json",
    registry: opts.registry ?? "",
    insecure: false,
    skip: false,
    result: null,
    status: null,
  };
  state.tabs.push(tab);
  switchTab(tab.id, { skipSnapshot: true });
  renderTabStrip();
  return tab;
}

function closeTab(id) {
  const i = state.tabs.findIndex((t) => t.id === id);
  if (i < 0) return;
  state.tabs.splice(i, 1);

  if (state.tabs.length === 0) {
    newTab();
    return;
  }
  if (state.activeId === id) {
    const next = state.tabs[Math.min(i, state.tabs.length - 1)];
    switchTab(next.id, { skipSnapshot: true });
  }
  renderTabStrip();
}

function switchTab(id, { skipSnapshot = false } = {}) {
  if (!skipSnapshot) snapshotFormToTab(getActive());
  state.activeId = id;
  const tab = getActive();
  if (tab) loadTabIntoForm(tab);
  renderTabStrip();
}

function renderTabStrip() {
  els.agentTabs.innerHTML = "";
  for (const tab of state.tabs) {
    const div = document.createElement("div");
    div.className = "agent-tab" + (tab.id === state.activeId ? " active" : "");
    div.title = tab.uri || "(new tab)";

    const label = document.createElement("span");
    label.className = "label";
    label.textContent = tab.name || shortUri(tab.uri);

    const close = document.createElement("button");
    close.className = "close";
    close.type = "button";
    close.textContent = "×";
    close.addEventListener("click", (e) => {
      e.stopPropagation();
      closeTab(tab.id);
    });

    div.appendChild(label);
    div.appendChild(close);
    div.addEventListener("click", () => switchTab(tab.id));

    els.agentTabs.appendChild(div);
  }
}

// ---------- response panes ----------
function showPrettyAs(mode) {
  if (mode === "iframe") {
    els.prettyPre.classList.add("hidden");
    els.prettyIfr.classList.remove("hidden");
  } else {
    els.prettyIfr.classList.add("hidden");
    els.prettyPre.classList.remove("hidden");
  }
}

function clearResponsePanes() {
  els.prettyPre.textContent = "";
  els.prettyIfr.srcdoc = "";
  els.raw.textContent = "";
  els.headers.textContent = "";
  showPrettyAs("pre");
}

function renderResponse(tab) {
  clearResponsePanes();
  const r = tab?.result;
  if (!r) return;

  if (!r.ok) {
    els.prettyPre.textContent = r.error || "(error)";
    els.raw.textContent = r.error || "";
    els.headers.textContent = `error during ${r.stage || "?"}`;
    showPrettyAs("pre");
    return;
  }

  els.raw.textContent = r.body;

  // Headers pane
  const hLines = [];
  hLines.push(`AGTP/1.0 ${r.status_code} ${r.status_text}`);
  for (const [k, v] of Object.entries(r.headers || {})) {
    hLines.push(`${k}: ${v}`);
  }
  hLines.push("");
  hLines.push(`# resolved: ${r.host}:${r.port}`);
  hLines.push(`# agent_id: ${r.agent_id}`);
  els.headers.textContent = hLines.join("\n");

  // Pretty pane: rendered HTML for html, syntax-highlighted for json, plain for yaml
  if (r.format === "html") {
    els.prettyIfr.srcdoc = r.body;
    showPrettyAs("iframe");
  } else if (r.format === "json") {
    try {
      const obj = JSON.parse(r.body);
      els.prettyPre.innerHTML = highlightJson(JSON.stringify(obj, null, 2));
    } catch {
      els.prettyPre.textContent = r.body;
    }
    showPrettyAs("pre");
  } else {
    els.prettyPre.textContent = r.body;
    showPrettyAs("pre");
  }
}

// response-tab switching
$$(".rtab").forEach((btn) => {
  btn.addEventListener("click", () => {
    if (btn.disabled || btn.classList.contains("disabled")) return;
    $$(".rtab").forEach((b) => b.classList.remove("active"));
    $$(".pane").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    $(`#pane-${btn.dataset.tab}`).classList.add("active");
    state.respPane = btn.dataset.tab;
  });
});

// ---------- status ----------
function setStatus(text, kind = "idle") {
  els.status.textContent = text;
  els.status.className = `status ${kind}`;
  const tab = getActive();
  if (tab) tab.status = { text, kind };
}

// ---------- history ----------
async function refreshHistory() {
  state.history = await window.pywebview.api.history_load();
  renderHistory();
}

function renderHistory() {
  els.histList.innerHTML = "";
  if (!state.history.length) {
    els.histEmpty.classList.remove("hidden");
    return;
  }
  els.histEmpty.classList.add("hidden");

  for (const h of state.history) {
    const li = document.createElement("li");
    const klass = h.ok ? "h-ok" : "h-err";
    const status = h.ok ? `${h.status_code}` : "ERR";
    const when = h.ts ? new Date(h.ts * 1000).toLocaleString() : "";
    li.innerHTML =
      `<div>${escapeHtml(h.uri)}</div>` +
      `<span class="h-meta">` +
      `<span class="${klass}">${status}</span> · ${escapeHtml(h.format || "")}` +
      (h.host ? ` · ${escapeHtml(h.host)}:${h.port}` : "") +
      (when ? ` · ${escapeHtml(when)}` : "") +
      `</span>`;
    li.addEventListener("click", () => {
      const tab = getActive();
      if (tab) {
        tab.uri = h.uri;
        tab.format = h.format || "json";
        loadTabIntoForm(tab);
        renderTabStrip();
        doFetch();
      }
    });
    els.histList.appendChild(li);
  }
}

async function pushHistory(entry) {
  state.history = await window.pywebview.api.history_add(entry);
  renderHistory();
}

async function clearHistory() {
  state.history = await window.pywebview.api.history_clear();
  renderHistory();
}

function toggleHistory(force) {
  const show = force === undefined
    ? els.histPanel.classList.contains("hidden")
    : !!force;
  els.histPanel.classList.toggle("hidden", !show);
}

// ---------- menu ----------
function toggleMenu(force) {
  const show = force === undefined
    ? els.menu.classList.contains("hidden")
    : !!force;
  els.menu.classList.toggle("hidden", !show);
}

els.menuBtn.addEventListener("click", (e) => {
  e.stopPropagation();
  toggleMenu();
});

document.addEventListener("click", (e) => {
  if (!els.menu.contains(e.target) && e.target !== els.menuBtn) {
    toggleMenu(false);
  }
});

$$(".menu-item").forEach((item) => {
  item.addEventListener("click", () => {
    const action = item.dataset.action;
    toggleMenu(false);
    if (action === "toggle-history") toggleHistory();
    if (action === "clear-history") clearHistory();
    if (action === "toggle-advanced") els.advanced.classList.toggle("hidden");
  });
});

els.histClose.addEventListener("click", () => toggleHistory(false));

// ---------- new tab ----------
els.newTab.addEventListener("click", () => newTab());

// ---------- methods explorer ----------

const KNOWN_CATEGORIES = new Set(["cognitive", "mechanics", "transact"]);

function categoryClass(cat) {
  return KNOWN_CATEGORIES.has(cat) ? `cat-${cat}` : "cat-other";
}

function badgeForSource(source) {
  if (source === "agtp/1.0") return { cls: "src-agtp", label: "AGTP standard" };
  if (source === "amg/1.0")  return { cls: "src-amg", label: "AMG validated" };
  return { cls: "src-experimental", label: "Experimental" };
}

function shouldUseTextarea(paramName) {
  return /^(schema|context|criteria|payload|parameters|filter|constraints)$/i
    .test(paramName);
}

function setMethodsView(state) {
  // state: "empty" | "error" | "content"
  els.mEmpty.classList.toggle("hidden", state !== "empty");
  els.mError.classList.toggle("hidden", state !== "error");
  els.mContent.classList.toggle("hidden", state !== "content");
}

function clearMethodsBadge() {
  els.mTabBadge.classList.add("hidden");
  els.mTabBadge.textContent = "0";
}

function setMethodsBadge(n) {
  if (n > 0) {
    els.mTabBadge.textContent = String(n);
    els.mTabBadge.classList.remove("hidden");
  } else {
    clearMethodsBadge();
  }
}

function endpointKey(host, port) {
  return `${host}:${port}`;
}

async function doDiscoverMethods(tab) {
  if (!tab || !tab.uri) return;

  // Cache by host:port if we have it from a recent fetch.
  const r = tab.result;
  let cacheKey = null;
  if (r && r.ok && r.host && r.port) {
    cacheKey = endpointKey(r.host, r.port);
    if (methodsCacheByEndpoint.has(cacheKey)) {
      tab.methods = methodsCacheByEndpoint.get(cacheKey);
      renderMethods(tab);
      return;
    }
  }

  let result;
  try {
    result = await window.pywebview.api.discover(
      tab.uri,
      tab.registry || "",
      !!tab.insecure,
      !!tab.skip,
    );
  } catch (e) {
    tab.methods = { ok: false, error: `bridge error: ${e}` };
    renderMethods(tab);
    return;
  }

  tab.methods = result;
  if (result.ok && cacheKey) {
    methodsCacheByEndpoint.set(cacheKey, result);
  }
  if (state.activeId === tab.id) renderMethods(tab);
}

function renderMethods(tab) {
  const m = tab && tab.methods;
  if (!tab || !m) {
    setMethodsView("empty");
    clearMethodsBadge();
    return;
  }

  if (!m.ok) {
    els.mError.textContent = m.error || "DISCOVER failed.";
    setMethodsView("error");
    clearMethodsBadge();
    return;
  }

  setMethodsView("content");
  const summary = m.summary || {};
  const total = summary.total || 0;
  setMethodsBadge(total);

  // ---- Available Actions: agent.capabilities ∩ method universe.
  const caps = capabilitiesForTab(tab);
  const universe = new Map();
  for (const e of m.embedded || []) universe.set(e.name, e);
  for (const e of m.custom   || []) universe.set(e.name, e);

  els.actionsGrid.innerHTML = "";
  if (caps.length === 0) {
    const note = document.createElement("div");
    note.className = "section-sub";
    note.textContent = "No capabilities reported by this agent.";
    els.actionsGrid.appendChild(note);
  } else {
    for (const cap of caps) {
      const spec = universe.get(cap);
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = `action-btn ${spec ? categoryClass(spec.category) : "cat-other"}`;
      if (!spec) {
        btn.classList.add("unavailable");
        btn.title = "Declared by the agent but not advertised by the server.";
      }
      btn.innerHTML = `
        <span class="action-name">${escapeHtml(cap)}</span>
        ${spec ? "" : '<span class="badge muts">unavailable</span>'}
      `;
      btn.addEventListener("click", () => {
        if (spec) expandMethod(tab, cap);
      });
      els.actionsGrid.appendChild(btn);
    }
  }
  els.actionsSummary.textContent =
    `${caps.length} capabilities · ${total} methods`;

  // ---- Standard Methods bucket
  els.embeddedCount.textContent =
    `${(m.embedded || []).length} method${(m.embedded || []).length === 1 ? "" : "s"}`;
  els.embeddedList.innerHTML = "";
  for (const spec of m.embedded || []) {
    els.embeddedList.appendChild(renderMethodRow(tab, spec));
  }

  // ---- Custom Methods bucket (hidden when empty)
  const customCount = (m.custom || []).length;
  els.customSection.classList.toggle("hidden", customCount === 0);
  els.customCount.textContent = `${customCount} method${customCount === 1 ? "" : "s"}`;
  els.customList.innerHTML = "";
  for (const spec of m.custom || []) {
    els.customList.appendChild(renderMethodRow(tab, spec));
  }

  // Re-expand whichever method the user had open before re-render.
  if (tab.openMethod) expandMethod(tab, tab.openMethod, { skipScroll: true });
}

function capabilitiesForTab(tab) {
  // The Pretty pane shows the identity card; the agent's accepted
  // method set comes from the v2 ``requires.methods`` array. We still
  // fall back to the legacy ``capabilities`` field for documents that
  // were served without migration. YAML parsing is best-effort.
  if (!tab.result || !tab.result.ok || !tab.result.body) return [];
  try {
    if (tab.result.format === "json") {
      const obj = JSON.parse(tab.result.body);
      if (obj && obj.requires && Array.isArray(obj.requires.methods)) {
        return obj.requires.methods;
      }
      return Array.isArray(obj.capabilities) ? obj.capabilities : [];
    }
    if (tab.result.format === "yaml") {
      // v2: methods are nested inside `requires:`. Match that block first.
      const reqBlock = tab.result.body.match(
        /^requires:\s*\n((?:\s+\S.*\n?)+)/m,
      );
      if (reqBlock) {
        // Inline list form: `methods: [A, B, C]`
        const inline = reqBlock[1].match(/methods:\s*\[([^\]]*)\]/);
        if (inline) {
          return inline[1].split(",")
            .map((s) => s.trim().replace(/^["']|["']$/g, ""))
            .filter(Boolean);
        }
        // Block list form (rare for our emitter but still supported).
        const block = reqBlock[1].match(
          /methods:\s*\n((?:\s+-\s+\S+\s*\n)+)/,
        );
        if (block) {
          return block[1].split("\n")
            .map((line) => line.match(/^\s+-\s+(.+?)\s*$/))
            .filter(Boolean)
            .map((mm) => mm[1].replace(/^["']|["']$/g, ""));
        }
      }
      // Legacy fallback: top-level `capabilities:` list.
      const legacy = tab.result.body.match(
        /^capabilities:\s*\n((?:\s+-\s+\S+\s*\n)+)/m,
      );
      if (!legacy) return [];
      return legacy[1].split("\n")
        .map((line) => line.match(/^\s+-\s+(.+?)\s*$/))
        .filter(Boolean)
        .map((mm) => mm[1].replace(/^["']|["']$/g, ""));
    }
  } catch { /* ignore */ }
  return [];
}

function renderMethodRow(tab, spec) {
  const row = document.createElement("div");
  row.className = "method-row";
  row.dataset.method = spec.name;
  if (tab.openMethod === spec.name) row.classList.add("expanded");

  const head = document.createElement("div");
  head.className = "method-head";

  const name = document.createElement("span");
  name.className = "method-name";
  name.textContent = spec.name;

  const blurb = document.createElement("span");
  blurb.className = "method-blurb";
  blurb.textContent = spec.description || "";

  const badges = document.createElement("span");
  badges.className = "method-badges";

  const catBadge = document.createElement("span");
  catBadge.className = `badge ${categoryClass(spec.category)}`;
  catBadge.textContent = spec.category || "other";
  badges.appendChild(catBadge);

  const srcInfo = badgeForSource(spec.source);
  const srcBadge = document.createElement("span");
  srcBadge.className = `badge ${srcInfo.cls}`;
  srcBadge.textContent = srcInfo.label;
  srcBadge.title = spec.namespace ? `namespace: ${spec.namespace}` : spec.source;
  badges.appendChild(srcBadge);

  if (spec.idempotent) {
    const b = document.createElement("span");
    b.className = "badge idemp";
    b.textContent = "idempotent";
    badges.appendChild(b);
  }
  if (spec.state_modifying) {
    const b = document.createElement("span");
    b.className = "badge muts";
    b.textContent = "mutates";
    badges.appendChild(b);
  }

  head.appendChild(name);
  head.appendChild(blurb);
  head.appendChild(badges);
  head.addEventListener("click", () => {
    if (tab.openMethod === spec.name) {
      tab.openMethod = null;
      row.classList.remove("expanded");
    } else {
      expandMethod(tab, spec.name);
    }
  });
  row.appendChild(head);

  const detail = document.createElement("div");
  detail.className = "method-detail";
  detail.appendChild(buildMethodDetail(tab, spec));
  row.appendChild(detail);

  return row;
}

function buildMethodDetail(tab, spec) {
  const wrap = document.createElement("div");

  const desc = document.createElement("div");
  desc.className = "detail-desc";
  desc.textContent = spec.description || "";
  wrap.appendChild(desc);

  const grid = document.createElement("dl");
  grid.className = "detail-grid";
  const rows = [
    ["semantic", spec.semantic_class || ""],
    ["source", spec.namespace ? `${spec.source} · ${spec.namespace}` : spec.source],
    ["idempotent", spec.idempotent ? "yes" : "no"],
    ["state-modifying", spec.state_modifying ? "yes" : "no"],
    ["required", (spec.required_params || []).join(", ") || "(none)"],
    ["optional", (spec.optional_params || []).join(", ") || "(none)"],
    ["error codes", (spec.error_codes || []).join(", ") || "(none)"],
  ];
  for (const [k, v] of rows) {
    const dt = document.createElement("dt");
    dt.textContent = k;
    const dd = document.createElement("dd");
    dd.textContent = v;
    grid.appendChild(dt);
    grid.appendChild(dd);
  }
  wrap.appendChild(grid);

  wrap.appendChild(buildTryItForm(tab, spec));
  return wrap;
}

function buildTryItForm(tab, spec) {
  const form = document.createElement("div");
  form.className = "try-it";
  form.innerHTML = "<h4>Try it</h4>";

  const fields = {};

  function addField(paramName, required) {
    const wrap = document.createElement("div");
    wrap.className = "try-field";
    const label = document.createElement("label");
    label.innerHTML =
      `<span>${escapeHtml(paramName)}</span>` +
      (required ? '<span class="req-mark">*</span>' : "");
    wrap.appendChild(label);
    const input = shouldUseTextarea(paramName)
      ? document.createElement("textarea")
      : document.createElement("input");
    if (input.tagName === "INPUT") input.type = "text";
    if (input.tagName === "TEXTAREA")
      input.placeholder = "JSON value (string is also fine)";
    input.dataset.param = paramName;
    label.htmlFor = `field-${spec.name}-${paramName}`;
    input.id = label.htmlFor;
    wrap.appendChild(input);
    fields[paramName] = input;
    return wrap;
  }

  for (const p of spec.required_params || []) form.appendChild(addField(p, true));

  const optionals = spec.optional_params || [];
  let optionalContainer = null;
  if (optionals.length) {
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "try-optional-toggle";
    toggle.textContent = `+ ${optionals.length} optional parameter${optionals.length === 1 ? "" : "s"}`;
    optionalContainer = document.createElement("div");
    optionalContainer.style.display = "none";
    optionalContainer.style.flexDirection = "column";
    optionalContainer.style.gap = "8px";
    toggle.addEventListener("click", () => {
      const open = optionalContainer.style.display === "none";
      optionalContainer.style.display = open ? "flex" : "none";
      toggle.textContent = open
        ? `- hide optional parameters`
        : `+ ${optionals.length} optional parameter${optionals.length === 1 ? "" : "s"}`;
    });
    form.appendChild(toggle);
    form.appendChild(optionalContainer);
    for (const p of optionals) optionalContainer.appendChild(addField(p, false));
  }

  if (spec.state_modifying) {
    const warn = document.createElement("div");
    warn.className = "try-warn";
    warn.textContent =
      "This method is state-modifying. You will be asked to confirm before invoking.";
    form.appendChild(warn);
  }

  const actions = document.createElement("div");
  actions.className = "try-actions";
  const invokeBtn = document.createElement("button");
  invokeBtn.type = "button";
  invokeBtn.className = "try-invoke";
  invokeBtn.textContent = `Invoke ${spec.name}`;
  actions.appendChild(invokeBtn);
  form.appendChild(actions);

  const respArea = document.createElement("div");
  respArea.className = "try-response hidden";
  form.appendChild(respArea);

  invokeBtn.addEventListener("click", async () => {
    if (spec.state_modifying) {
      const ok = confirm(
        `${spec.name} is state-modifying. This action may change agent state. Continue?`,
      );
      if (!ok) return;
    }

    const body = collectFormBody(fields, spec);
    if (body.error) {
      respArea.classList.remove("hidden");
      respArea.innerHTML = "";
      const status = document.createElement("div");
      status.className = "resp-status err";
      status.textContent = `error: ${body.error}`;
      respArea.appendChild(status);
      return;
    }

    invokeBtn.disabled = true;
    invokeBtn.textContent = "Invoking...";
    let result;
    try {
      result = await window.pywebview.api.invoke(
        tab.uri,
        spec.name,
        body.payload,
        tab.registry || "",
        !!tab.insecure,
        !!tab.skip,
      );
    } catch (e) {
      result = { ok: false, error: `bridge error: ${e}` };
    }
    invokeBtn.disabled = false;
    invokeBtn.textContent = `Invoke ${spec.name}`;
    renderTryItResponse(respArea, result, spec.name);
    pushInvocation(tab, spec, result);
  });

  return form;
}

function collectFormBody(fields, spec) {
  const payload = {};
  for (const [name, input] of Object.entries(fields)) {
    const raw = input.value;
    if (raw === "" || raw === null || raw === undefined) {
      // Skip empty optionals; required fields are checked below.
      continue;
    }
    let value = raw;
    // Try to JSON-parse so numbers/booleans/objects come through typed.
    try {
      value = JSON.parse(raw);
    } catch {
      // Plain string is fine for textboxes; textareas should be JSON.
      if (input.tagName === "TEXTAREA" && raw.trim().startsWith("{")) {
        return { error: `${name}: invalid JSON in textarea` };
      }
    }
    payload[name] = value;
  }
  for (const req of spec.required_params || []) {
    if (!(req in payload) || payload[req] === "") {
      return { error: `missing required parameter: ${req}` };
    }
  }
  return { payload };
}

function renderTryItResponse(area, result, methodName) {
  area.classList.remove("hidden");
  area.innerHTML = "";

  const status = document.createElement("div");
  if (!result.ok) {
    status.className = "resp-status err";
    status.textContent = `${methodName} failed at ${result.stage || "?"}: ${result.error || ""}`;
    area.appendChild(status);
    return;
  }
  const kind = result.status_code === 200 ? "ok" : "err";
  status.className = `resp-status ${kind}`;
  status.textContent =
    `AGTP/1.0 ${result.status_code} ${result.status_text}  ·  ${result.host}:${result.port}`;
  area.appendChild(status);

  const pre = document.createElement("pre");
  if (result.body) {
    try {
      pre.innerHTML = highlightJson(JSON.stringify(JSON.parse(result.body), null, 2));
    } catch {
      pre.textContent = result.body;
    }
  }
  area.appendChild(pre);
}

function expandMethod(tab, methodName, { skipScroll = false } = {}) {
  tab.openMethod = methodName;
  $$(".method-row").forEach((r) => {
    r.classList.toggle("expanded", r.dataset.method === methodName);
  });
  if (skipScroll) return;
  const row = $(`.method-row[data-method="${CSS.escape(methodName)}"]`);
  if (row && row.scrollIntoView) {
    row.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
}

// ---------- collapse/expand sections ----------
$$(".section-head.expandable").forEach((btn) => {
  btn.addEventListener("click", () => {
    btn.parentElement.classList.toggle("collapsed");
  });
});

// ---------- invocation history (localStorage) ----------
function loadInvocations() {
  try {
    const raw = localStorage.getItem(INV_KEY);
    if (!raw) return [];
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? arr : [];
  } catch {
    return [];
  }
}

function saveInvocations(arr) {
  try { localStorage.setItem(INV_KEY, JSON.stringify(arr)); }
  catch { /* quota or disabled storage */ }
}

function pushInvocation(tab, spec, result) {
  const entry = {
    method: spec.name,
    uri: tab.uri,
    host: result.host,
    port: result.port,
    ok: !!result.ok,
    status_code: result.ok ? result.status_code : null,
    error: result.ok ? null : (result.error || ""),
    ts: Date.now(),
  };
  const arr = loadInvocations();
  arr.unshift(entry);
  arr.length = Math.min(arr.length, INV_LIMIT);
  saveInvocations(arr);
  renderInvocations();
}

function renderInvocations() {
  const arr = loadInvocations();
  els.invList.innerHTML = "";
  if (!arr.length) {
    els.invEmpty.classList.remove("hidden");
    return;
  }
  els.invEmpty.classList.add("hidden");
  for (const e of arr) {
    const li = document.createElement("li");
    const status = e.ok ? `${e.status_code}` : "ERR";
    const cls = e.ok && e.status_code === 200 ? "ok" : "err";
    const when = new Date(e.ts).toLocaleString();
    const line = document.createElement("div");
    line.className = "inv-line";
    line.innerHTML =
      `<span class="inv-status ${cls}">${escapeHtml(status)}</span>` +
      `<span class="inv-method">${escapeHtml(e.method)}</span>` +
      `<span class="inv-when">${escapeHtml(when)}</span>`;
    const target = document.createElement("div");
    target.className = "inv-target";
    target.innerHTML =
      `<a data-uri="${escapeHtml(e.uri || "")}">${escapeHtml(shortUri(e.uri || ""))}</a>` +
      (e.host ? ` · ${escapeHtml(e.host)}:${e.port}` : "") +
      (e.error ? ` · ${escapeHtml(e.error)}` : "");
    li.appendChild(line);
    li.appendChild(target);
    li.addEventListener("click", () => {
      const tab = getActive();
      if (!tab) return;
      tab.uri = e.uri;
      loadTabIntoForm(tab);
      doFetch();
    });
    els.invList.appendChild(li);
  }
}

if (els.invClear) {
  els.invClear.addEventListener("click", () => {
    saveInvocations([]);
    renderInvocations();
  });
}

// ---------- main fetch ----------
async function doFetch() {
  const tab = getActive();
  if (!tab) return;
  snapshotFormToTab(tab);

  const uri = tab.uri.trim();
  if (!uri) {
    setStatus("Enter an agtp:// URI.", "err");
    return;
  }

  tab.name = null;
  els.go.disabled = true;
  setStatus(`Resolving ${uri} …`, "working");

  let result;
  try {
    result = await window.pywebview.api.fetch(
      uri,
      tab.format,
      tab.registry,
      tab.insecure,
      tab.skip,
    );
  } catch (e) {
    setStatus(`bridge error: ${e}`, "err");
    els.go.disabled = false;
    return;
  } finally {
    els.go.disabled = false;
  }

  // Stash on the tab — but only if the user hasn't already switched away.
  // (We keyed by tab.id at the time of the call.)
  tab.result = result;
  tab.uri = uri;
  // Reset the methods view; auto-DISCOVER will repopulate it on success.
  tab.methods = null;
  tab.openMethod = null;
  if (result.ok) {
    tab.name = nameFromBody(result.body, result.format);
  }
  renderTabStrip();
  if (state.activeId === tab.id) renderMethods(tab);

  if (state.activeId === tab.id) {
    if (!result.ok) {
      setStatus(`[${result.stage}] ${result.error}`, "err");
    } else {
      const kind = result.status_code === 200 ? "ok" : "err";
      setStatus(
        `${result.status_code} ${result.status_text} · ${result.host}:${result.port} · ${result.content_type || "no content-type"}`,
        kind,
      );
    }
    renderResponse(tab);
  }

  await pushHistory({
    uri,
    format: tab.format,
    ok: !!result.ok,
    status_code: result.status_code,
    host: result.host,
    port: result.port,
    agent_id: result.agent_id,
    error: result.ok ? null : result.error,
  });

  // Auto-DISCOVER /methods on a successful identity fetch. If the
  // server doesn't support it, renderMethods shows the error state and
  // the rest of the UI continues to function.
  if (result.ok && result.status_code === 200) {
    doDiscoverMethods(tab);
  }
}

els.form.addEventListener("submit", (e) => {
  e.preventDefault();
  doFetch();
});

// ---------- boot ----------
(async function init() {
  await whenApiReady();
  const [initialUri, defaultRegistry] = await Promise.all([
    window.pywebview.api.get_initial_uri(),
    window.pywebview.api.get_default_registry(),
  ]);
  els.registry.placeholder = defaultRegistry || "https://registry.agtp.io";

  await refreshHistory();
  renderInvocations();

  newTab({ uri: initialUri || "" });

  if (initialUri) {
    doFetch();
  } else {
    setStatus("Ready. Enter an agtp:// URI and press Go.", "idle");
    els.uri.focus();
  }
})();
