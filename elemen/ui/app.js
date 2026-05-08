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
  // navigation
  navBack:    $("#nav-back"),
  navFwd:     $("#nav-forward"),
  // agent view (user profile)
  agentView:          $("#agent-view"),
  migrationBanner:    $("#migration-banner"),
  matchBadge:         $("#match-badge"),
  matchDetail:        $("#match-detail"),
  agentHeader:        $("#agent-header"),
  agentIdentity:      $("#agent-identity"),
  agentGoals:         $("#agent-goals"),
  agentSkills:        $("#agent-skills"),
  agentPermissions:   $("#agent-permissions"),
  agentCredentials:   $("#agent-credentials"),
  agentFooter:        $("#agent-footer"),
  // manifest view (workplace dashboard)
  manifestView:           $("#manifest-view"),
  manifestHeader:         $("#manifest-header"),
  manifestServer:         $("#manifest-server"),
  manifestMethodsSection: $("#manifest-methods-section"),
  manifestApisPreview:    $("#manifest-apis-preview"),
  manifestAgents:         $("#manifest-agents"),
  manifestProtocols:      $("#manifest-protocols"),
  manifestPolicy:         $("#manifest-policy"),
  // APIs tab
  apisTabBadge: $("#apis-tab-badge"),
  apisEmpty:    $("#apis-empty"),
  apisContent:  $("#apis-content"),
  // dynamic protocol tabs
  protocolTabsHost:  $("#protocol-tabs-host"),
  protocolPanesHost: $("#protocol-panes-host"),
  // invocations
  invList:    $("#invocations-list"),
  invEmpty:   $("#invocations-empty"),
  invClear:   $("#inv-clear"),
};

// Per-host:port DISCOVER cache, keyed across tabs in this session.
const methodsCacheByEndpoint = new Map();
// Per-host:port manifest cache (used by the matching handshake on
// agent loads to avoid re-fetching).
const manifestCacheByEndpoint = new Map();
// Per-tab synthesis state: {methodName: synthesisId}
// Stored on the tab itself so each tab keeps its own session.
// localStorage key for invocation history.
const INV_KEY = "elemen.invocations.v1";
const INV_LIMIT = 100;
// localStorage key for URI bar history.
const URI_HISTORY_KEY = "elemen.uri_history.v1";
const URI_HISTORY_LIMIT = 200;

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
  applyTabVisibility(tab);
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
  // Remove any status banners left from a prior render.
  document.querySelectorAll(".resp-banner").forEach((b) => {
    if (!b.closest("#pane-pretty")) return;
    b.remove();
  });
  document.querySelectorAll(".resp-banner-wrap").forEach((w) => w.remove());
  if (els.agentView) els.agentView.classList.add("hidden");
  if (els.manifestView) els.manifestView.classList.add("hidden");
  if (els.matchBadge) els.matchBadge.classList.add("hidden");
  if (els.matchDetail) els.matchDetail.classList.add("hidden");
  if (els.migrationBanner) els.migrationBanner.classList.add("hidden");
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
  if (r.agent_id) {
    hLines.push(`# agent_id: ${r.agent_id}`);
  } else if (r.kind === "manifest") {
    hLines.push(`# server: ${r.host}:${r.port}`);
  }
  els.headers.textContent = hLines.join("\n");

  // Status-specific banner (451 / 452 / 460 / 461 / 462). Rendered
  // in the Pretty pane above the structured/raw content. For 461
  // the Accept Counter button is wired via the try-it pathway.
  const banner = renderStatusBanner(r);

  // Pretty pane variants:
  //   * Manifest -> structured manifest view.
  //   * Agent doc (status 200) -> structured agent view.
  //   * HTML format -> iframe.
  //   * Otherwise -> syntax-highlighted JSON or plain text.
  if (r.kind === "manifest") {
    if (renderManifestView(tab)) {
      if (banner) {
        els.manifestView.insertBefore(
          banner,
          els.manifestView.firstChild,
        );
      }
      return;
    }
  }

  if (r.kind === "agent" && r.format === "html") {
    els.prettyIfr.srcdoc = r.body;
    showPrettyAs("iframe");
    return;
  }

  if (r.kind === "agent" && r.status_code === 200 && r.format === "json") {
    if (renderAgentView(tab)) {
      if (banner) {
        els.agentView.insertBefore(banner, els.agentView.firstChild);
      }
      return;
    }
  }

  if (r.format === "html") {
    els.prettyIfr.srcdoc = r.body;
    showPrettyAs("iframe");
    return;
  }

  if (r.format === "json") {
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

  if (banner) {
    // For non-structured renders, prepend the banner to the Pretty pane.
    const wrap = document.createElement("div");
    wrap.className = "resp-banner-wrap";
    wrap.appendChild(banner);
    els.prettyPre.parentNode.insertBefore(wrap, els.prettyPre);
  }
}

// Response-tab switching. Hidden tabs (display:none via the .hidden
// class) cannot fire clicks, so the visibility logic in
// applyTabVisibility is sufficient. Disabled tabs (Cert) ignore.
$$(".rtab").forEach((btn) => {
  btn.addEventListener("click", () => {
    if (btn.disabled || btn.classList.contains("disabled")) return;
    if (btn.classList.contains("hidden")) return;
    $$(".rtab").forEach((b) => b.classList.remove("active"));
    $$(".pane").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    const target = $(`#pane-${btn.dataset.tab}`);
    if (target) target.classList.add("active");
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

// The legacy Methods tab is removed in this revision; methods are
// now surfaced in the Server Overview (workplace) and as Permission
// tags on the Agent Overview (user profile). The helpers below are
// kept as safe no-ops so older callers do not break.
function setMethodsView(_state) { /* no-op (methods tab removed) */ }
function clearMethodsBadge() { /* no-op */ }
function setMethodsBadge(_n) { /* no-op */ }

function endpointKey(host, port) {
  return `${host}:${port}`;
}

async function doDiscoverMethods(tab) {
  // Methods inventory now lives in the Server Overview (when the URI
  // is a manifest) and as Permission tags on the Agent Overview. We
  // keep this function for backward compatibility with older call
  // sites, but it caches the agent's per-method DISCOVER for the
  // matching handshake without painting a Methods tab.
  if (!tab || !tab.uri) return;
  const r = tab.result;
  let cacheKey = null;
  if (r && r.ok && r.host && r.port) {
    cacheKey = endpointKey(r.host, r.port);
    if (methodsCacheByEndpoint.has(cacheKey)) {
      tab.methods = methodsCacheByEndpoint.get(cacheKey);
      return;
    }
  }
  try {
    const result = await window.pywebview.api.discover(
      tab.uri, tab.registry || "", !!tab.insecure, !!tab.skip,
    );
    tab.methods = result;
    if (result.ok && cacheKey) {
      methodsCacheByEndpoint.set(cacheKey, result);
    }
  } catch (e) {
    tab.methods = { ok: false, error: `bridge error: ${e}` };
  }
}

function renderMethods(_tab) {
  // Methods tab is removed. The bucketed inventory now appears under
  // the Server Overview's "Methods" section; the per-agent
  // intersection (matched / missing) drives the Permission tags on
  // the Agent Overview. This stub keeps older call sites safe.
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
      // If a prior PROPOSE established a synthesis for this method,
      // reuse it so the server rewrites the request to the underlying
      // verb instead of soft-denying.
      const synthId = (tab.syntheses || {})[spec.name] || "";
      result = await window.pywebview.api.invoke(
        tab.uri,
        spec.name,
        body.payload,
        tab.registry || "",
        !!tab.insecure,
        !!tab.skip,
        synthId,
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

// ---------- negotiation flow ----------

async function promptNegotiationForMissing(tab, methodName) {
  // Best-effort confirm; the prompt API is fine for pywebview's
  // chromeless window. Future polish: render a dedicated modal.
  const proceed = window.confirm(
    `Server does not expose ${methodName}. Negotiate?\n\n` +
    `OK runs PROPOSE for the missing method; Cancel returns to the page.`,
  );
  if (!proceed) return;

  const proposal = {
    name: methodName,
    parameters: {},
    outcome: "auto-generated proposal from elemen",
    description: `client-driven proposal for ${methodName}`,
  };

  let result;
  try {
    result = await window.pywebview.api.invoke(
      tab.uri,
      "PROPOSE",
      proposal,
      tab.registry || "",
      !!tab.insecure,
      !!tab.skip,
    );
  } catch (e) {
    setStatus(`negotiation bridge error: ${e}`, "err");
    return;
  }

  if (!result || !result.ok) {
    setStatus(`PROPOSE failed: ${result?.error || "(unknown)"}`, "err");
    return;
  }

  // Reuse the renderResponse path by stuffing the response into the
  // tab as the active result. This produces the right banner.
  const renderable = {
    ok: true,
    kind: "agent",
    agent_id: result.agent_id,
    host: result.host,
    port: result.port,
    status_code: result.status_code,
    status_text: result.status_text,
    headers: result.headers,
    body: result.body,
    content_type: result.content_type,
    format: "json",
  };
  tab.result = renderable;
  renderResponse(tab);

  // If accepted, store synthesis_id so subsequent Try-it invocations
  // of the proposed method can use it.
  if (result.status_code === 200) {
    try {
      const payload = JSON.parse(result.body);
      const synth = payload.synthesis;
      if (synth && synth.synthesis_id) {
        tab.syntheses = tab.syntheses || {};
        tab.syntheses[methodName] = synth.synthesis_id;
        setStatus(
          `synthesis ${synth.synthesis_id.slice(0, 16)}… established for ${methodName}`,
          "ok",
        );
      }
    } catch { /* fall through */ }
  } else if (result.status_code === 461) {
    // Wire the Accept Counter button (if the banner rendered).
    const btn = document.querySelector(".resp-banner .accept-counter");
    if (btn) {
      btn.addEventListener("click", () => {
        try {
          const payload = JSON.parse(result.body);
          const counter = payload.counter_proposal || {};
          if (counter.name) expandMethod(tab, counter.name);
        } catch { /* ignore */ }
      });
    } else {
      // Banner is rendered without the button by default; add one
      // pointing to the counter method.
      try {
        const payload = JSON.parse(result.body);
        const counter = payload.counter_proposal || {};
        if (counter.name) {
          const banner = document.querySelector(".resp-banner.counter-proposal");
          if (banner) {
            const accept = document.createElement("button");
            accept.className = "accept-counter";
            accept.textContent = `Accept counter and open ${counter.name}`;
            accept.addEventListener("click", () => {
              expandMethod(tab, counter.name);
            });
            banner.appendChild(accept);
          }
        }
      } catch { /* ignore */ }
    }
  }
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

// ---------- URI bar history ----------

function loadUriHistory() {
  try {
    const raw = localStorage.getItem(URI_HISTORY_KEY);
    if (!raw) return { entries: [], current_index: -1 };
    const parsed = JSON.parse(raw);
    if (!parsed || !Array.isArray(parsed.entries)) {
      return { entries: [], current_index: -1 };
    }
    return parsed;
  } catch {
    return { entries: [], current_index: -1 };
  }
}

function saveUriHistory(h) {
  try { localStorage.setItem(URI_HISTORY_KEY, JSON.stringify(h)); }
  catch { /* quota or disabled */ }
}

function pushUriHistory(uri, title) {
  if (!uri) return;
  const h = loadUriHistory();
  // If we're currently at an older entry, truncate the forward stack
  // (matches browser back/forward semantics).
  if (h.current_index >= 0 && h.current_index < h.entries.length - 1) {
    h.entries.length = h.current_index + 1;
  }
  // Avoid pushing exact duplicates of the latest entry.
  const last = h.entries[h.entries.length - 1];
  if (last && last.uri === uri) {
    last.timestamp = new Date().toISOString();
    if (title) last.title = title;
    h.current_index = h.entries.length - 1;
  } else {
    h.entries.push({
      uri,
      timestamp: new Date().toISOString(),
      title: title || null,
    });
    if (h.entries.length > URI_HISTORY_LIMIT) {
      h.entries.shift();
    }
    h.current_index = h.entries.length - 1;
  }
  saveUriHistory(h);
  updateNavButtons();
}

function updateUriHistoryTitle(title) {
  const h = loadUriHistory();
  if (h.current_index < 0 || h.current_index >= h.entries.length) return;
  h.entries[h.current_index].title = title;
  saveUriHistory(h);
}

function updateNavButtons() {
  const h = loadUriHistory();
  els.navBack.disabled = h.current_index <= 0;
  els.navFwd.disabled = h.current_index >= h.entries.length - 1;
}

function navigateHistory(delta) {
  const h = loadUriHistory();
  const newIndex = h.current_index + delta;
  if (newIndex < 0 || newIndex >= h.entries.length) return;
  h.current_index = newIndex;
  saveUriHistory(h);
  const entry = h.entries[newIndex];
  const tab = getActive();
  if (!tab) return;
  tab.uri = entry.uri;
  tab.format = tab.format || "json";
  loadTabIntoForm(tab);
  // Use the silent fetch path to avoid re-pushing onto history.
  doFetch({ silentHistory: true });
  updateNavButtons();
}

els.navBack.addEventListener("click", () => navigateHistory(-1));
els.navFwd.addEventListener("click", () => navigateHistory(1));

document.addEventListener("keydown", (e) => {
  // Alt+Left / Alt+Right for back/forward navigation.
  if (e.altKey && !e.ctrlKey && !e.metaKey) {
    if (e.key === "ArrowLeft") {
      e.preventDefault();
      navigateHistory(-1);
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      navigateHistory(1);
    }
  }
});

// ---------- agent / manifest view rendering ----------

function showPaneVariant(variant) {
  // variant: "agent" | "manifest" | "raw" | "iframe"
  els.agentView.classList.toggle("hidden", variant !== "agent");
  els.manifestView.classList.toggle("hidden", variant !== "manifest");
  els.prettyPre.classList.toggle("hidden", variant !== "raw");
  els.prettyIfr.classList.toggle("hidden", variant !== "iframe");
}

// ---------- agent view as user profile ----------
//
// Conceptual frame: agents are users, not APIs. The view treats the
// Agent Document as a user profile (Identity / Goals / Skills /
// Permissions / Credentials), not a method directory. Methods are a
// server concept; agents only have permissions to invoke them.

function renderAgentView(tab) {
  const r = tab.result;
  if (!r || !r.ok || r.status_code !== 200) return false;
  let doc;
  try {
    doc = JSON.parse(r.body);
  } catch {
    return false;
  }
  if (!doc || !doc.agent_id || !Array.isArray(doc.skills)) {
    return false;
  }

  // Migration banner.
  els.migrationBanner.classList.toggle(
    "hidden",
    doc.document_version !== "v1-migrated",
  );

  // Hero header (name + principal + status badge).
  const status = (doc.status || "active").toLowerCase();
  els.agentHeader.innerHTML =
    `<div>` +
    `<h1 class="name">${escapeHtml(doc.name || "")}</h1>` +
    `<p class="principal">acting on behalf of ${escapeHtml(doc.principal || "")}</p>` +
    `</div>` +
    `<span class="status-badge status-${escapeHtml(status)}">` +
    `${escapeHtml(status)}</span>`;

  // Identity section: provenance metadata in a compact key/value grid.
  els.agentIdentity.innerHTML =
    `<h3 class="profile-section-title"><span>Identity</span></h3>` +
    `<dl class="identity-grid">` +
    `<dt>Name</dt><dd>${escapeHtml(doc.name || "")}</dd>` +
    `<dt>Principal</dt><dd>${escapeHtml(doc.principal || "")}</dd>` +
    `<dt>Principal ID</dt><dd>${escapeHtml(doc.principal_id || "")}</dd>` +
    `<dt>Issuer</dt><dd>${escapeHtml(doc.issuer || "")}</dd>` +
    `<dt>Issued at</dt><dd>${escapeHtml(doc.issued_at || "")}</dd>` +
    `<dt>Status</dt><dd>${escapeHtml(status)}</dd>` +
    `</dl>`;

  // Goals: derived from description until a structured goals field
  // lands in v07. Single paragraph for now.
  if (doc.description) {
    els.agentGoals.innerHTML =
      `<h3 class="profile-section-title"><span>Goals</span></h3>` +
      `<div class="goals-text">${escapeHtml(doc.description)}</div>`;
    els.agentGoals.classList.remove("hidden");
  } else {
    els.agentGoals.innerHTML = "";
    els.agentGoals.classList.add("hidden");
  }

  // Skills section.
  const skills = (doc.skills || [])
    .map((s) => `<li>${escapeHtml(s)}</li>`)
    .join("");
  els.agentSkills.innerHTML =
    `<h3 class="profile-section-title"><span>Skills</span></h3>` +
    (skills
      ? `<ul class="skill-card-list">${skills}</ul>`
      : `<div class="profile-empty">No skills declared.</div>`);

  // Permissions and Credentials are filled in by their own helpers
  // so they can re-render when match-handshake info arrives later.
  renderPermissionsSection(tab, doc);
  renderCredentialsSection(doc);

  // Footer (kept terse; full identity is in the Identity section).
  els.agentFooter.innerHTML =
    `<dt>Document version</dt><dd>${escapeHtml(doc.document_version || "v2")}</dd>` +
    `<dt>AGTP version</dt><dd>${escapeHtml(doc.agtp_version || "")}</dd>` +
    `<dd class="agent-id-cell">${escapeHtml(doc.agent_id)}</dd>`;

  showPaneVariant("agent");
  return true;
}

function renderPermissionsSection(tab, doc) {
  const req = doc.requires || {};
  const methods = req.methods || [];
  const wildcards = !!req.wildcards;
  const matchInfo = tab.matchOutcome || null;
  const matchedSet = matchInfo ? new Set(matchInfo.matched) : null;

  // Wildcards label moves to the section header so it's the first
  // thing the operator sees on a profile.
  const wildcardsBadge =
    `<span class="wildcards-prominent ${wildcards ? "open" : "strict"}">` +
    (wildcards
      ? "Open (any method permitted)"
      : "Strict (declared methods only)") +
    `</span>`;

  let body;
  if (!methods.length) {
    body = wildcards
      ? `<div class="profile-empty">No specific methods declared. ` +
        `Wildcards is open, so any server-exposed method is permitted.</div>`
      : `<div class="profile-empty">No permissions granted.</div>`;
  } else {
    const tags = methods.map((m) => {
      let cls = "permission-tag";
      let title = `Method: ${m}`;
      if (matchedSet) {
        if (matchedSet.has(m)) {
          cls += " matched";
          title = `${m} is available on this server.`;
        } else {
          cls += " missing";
          title = `${m} is not advertised by this server.`;
        }
      }
      return (
        `<span class="${cls}" title="${escapeHtml(title)}" ` +
        `data-method="${escapeHtml(m)}">` +
        `<span class="avail-dot"></span>${escapeHtml(m)}` +
        `</span>`
      );
    }).join("");
    body = `<div class="permission-tags">${tags}</div>`;
  }

  els.agentPermissions.innerHTML =
    `<h3 class="profile-section-title">` +
    `<span>Permissions (${methods.length})</span>${wildcardsBadge}` +
    `</h3>` +
    body;

  // Clicking a permission tag prompts negotiation when the method is
  // missing; otherwise it's a no-op (info-only on the profile view).
  els.agentPermissions.querySelectorAll(".permission-tag").forEach((tag) => {
    tag.addEventListener("click", () => {
      const m = tag.getAttribute("data-method");
      if (!m) return;
      if (tag.classList.contains("missing")) {
        promptNegotiationForMissing(tab, m);
      }
    });
  });
}

function renderCredentialsSection(doc) {
  const req = doc.requires || {};
  const reqScopes = req.scopes || [];
  const acceptScopes = doc.scopes_accepted || [];

  const cards = [];
  for (const s of reqScopes) {
    cards.push(makeCredentialCard(s, "scope (presents)", doc.issuer));
  }
  for (const s of acceptScopes) {
    cards.push(makeCredentialCard(s, "scope (accepts)", doc.issuer));
  }

  let body;
  if (!cards.length) {
    body = `<div class="profile-empty">No credentials declared.</div>`;
  } else {
    body = `<div class="credential-cards">${cards.join("")}</div>`;
  }
  els.agentCredentials.innerHTML =
    `<h3 class="profile-section-title"><span>Credentials</span></h3>` +
    body;
}

function makeCredentialCard(name, kind, issuer) {
  return (
    `<div class="credential-card">` +
    `<span class="kind">${escapeHtml(kind)}</span>` +
    `<span class="name">${escapeHtml(name)}</span>` +
    (issuer
      ? `<span class="meta">issuer: ${escapeHtml(issuer)}</span>`
      : "") +
    `</div>`
  );
}

// Backward compat for callers that still reference the old name.
function renderRequiresSection(tab, doc) {
  renderPermissionsSection(tab, doc);
  renderCredentialsSection(doc);
}

function renderManifestView(tab) {
  const r = tab.result;
  if (!r || !r.ok) return false;
  const m = r.manifest;
  if (!m) return false;

  els.manifestHeader.innerHTML =
    `<h2>${escapeHtml(m.server?.issuer || "(server)")}</h2>` +
    `<span class="endpoint">agtp://${escapeHtml(r.host)}:${escapeHtml(String(r.port))}</span>`;

  // Server section.
  const sv = m.server || {};
  const features = (sv.supported_features || [])
    .map((f) => `<span class="feature-pill">${escapeHtml(f)}</span>`)
    .join("");
  els.manifestServer.innerHTML =
    `<h3>Server</h3>` +
    `<div class="body">` +
    `<dl class="kv-grid">` +
    `<dt>Operator</dt><dd>${escapeHtml(sv.operator || "")}</dd>` +
    `<dt>Contact</dt><dd>${escapeHtml(sv.contact || "")}</dd>` +
    `<dt>AGTP version</dt><dd>${escapeHtml(m.agtp_version || "")}</dd>` +
    `<dt>AMG version</dt><dd>${escapeHtml(sv.amg_version || "")}</dd>` +
    `<dt>Document version</dt><dd>${escapeHtml(m.document_version || "v2")}</dd>` +
    `<dt>Issued at</dt><dd>${escapeHtml(m.issued_at || "")}</dd>` +
    `</dl>` +
    (features
      ? `<div style="margin-top:10px">${features}</div>`
      : "") +
    `</div>`;

  // Methods section.
  const meth = m.methods || {};
  const summary = meth.summary || {};
  const embedded = meth.embedded || [];
  const custom = meth.custom || [];
  els.manifestMethodsSection.innerHTML =
    `<h3>Methods (${summary.total ?? embedded.length + custom.length})</h3>` +
    `<div class="body">` +
    `<div style="font-size:11.5px;color:var(--text-dim);margin-bottom:8px">` +
    `Embedded: ${summary.embedded_count ?? embedded.length} &nbsp;·&nbsp; ` +
    `Custom: ${summary.custom_count ?? custom.length}` +
    `</div>` +
    renderManifestMethodsList(embedded, "Standard Methods") +
    (custom.length ? renderManifestMethodsList(custom, "Custom Methods") : "") +
    `</div>`;

  // APIs preview: when populated, hint that the dedicated tab has
  // resource-level details. Empty manifests skip this section
  // entirely so the dashboard stays terse.
  const apis = m.apis || [];
  if (apis.length) {
    els.manifestApisPreview.innerHTML =
      `<h3>APIs</h3>` +
      `<div class="body">` +
      `<div style="font-size:12px;color:var(--text-dim);margin-bottom:6px">` +
      `${apis.length} endpoint${apis.length === 1 ? "" : "s"} declared. ` +
      `See the APIs tab for resource-scoped details.` +
      `</div>` +
      `<ul style="list-style:none;padding:0;margin:0;` +
      `display:flex;flex-direction:column;gap:4px">` +
      apis.slice(0, 5).map((api) =>
        `<li style="font-family:var(--mono);font-size:12px">` +
        `<code style="color:var(--text)">${escapeHtml(api.path)}</code> ` +
        `<span style="color:var(--text-dim)">` +
        `(${(api.methods || []).join(", ")})` +
        `</span>` +
        `</li>`,
      ).join("") +
      (apis.length > 5
        ? `<li style="color:var(--text-dim);font-size:11px;margin-top:4px">` +
          `…and ${apis.length - 5} more</li>`
        : "") +
      `</ul></div>`;
    els.manifestApisPreview.classList.remove("hidden");
  } else {
    els.manifestApisPreview.classList.add("hidden");
    els.manifestApisPreview.innerHTML = "";
  }

  // Agents section.
  const agents = m.agents || {};
  const list = agents.list || [];
  let agentsHtml = `<h3>Hosted agents (${list.length})</h3>`;
  if (agents.notice) {
    agentsHtml += `<div class="disclosure-notice">${escapeHtml(agents.notice)}</div>`;
  }
  if (list.length === 0) {
    agentsHtml += `<div class="agents-empty">No agents disclosed at this server.</div>`;
  } else {
    agentsHtml += `<div class="agent-cards">${
      list.map((a) => renderManifestAgentCard(a, r.host, r.port)).join("")
    }</div>`;
  }
  els.manifestAgents.innerHTML = agentsHtml;

  // Hosted protocols section.
  const protocols = m.hosts_protocols || [];
  if (protocols.length) {
    els.manifestProtocols.innerHTML =
      `<h3>Hosted protocols (${protocols.length})</h3>` +
      `<div class="body" style="display:flex;flex-direction:column;gap:6px">` +
      protocols.map((p) =>
        `<div style="display:flex;justify-content:space-between;` +
        `gap:10px;font-family:var(--mono);font-size:12px">` +
        `<span><strong>${escapeHtml(p.protocol)}</strong> ` +
        `<span style="color:var(--text-dim)">v${escapeHtml(p.version)}</span></span>` +
        `<span style="color:var(--text-dim)">${escapeHtml(p.endpoint)}</span>` +
        `</div>`,
      ).join("") +
      `</div>`;
    els.manifestProtocols.classList.remove("hidden");
  } else {
    els.manifestProtocols.classList.add("hidden");
    els.manifestProtocols.innerHTML = "";
  }

  // Policy section.
  const pol = m.policy || {};
  els.manifestPolicy.innerHTML =
    `<h3>Policy</h3>` +
    `<div class="policy-row">` +
    pillFor("Wildcards accepted", pol.wildcards_accepted) +
    pillFor("Anonymous discovery", pol.anonymous_discovery) +
    pillFor("Scope required for invocation", pol.scope_required_for_invocation) +
    `</div>`;

  // Wire up agent-card open buttons (delegated).
  els.manifestAgents.querySelectorAll(".open-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const target = btn.getAttribute("data-target");
      if (!target) return;
      const tab = getActive();
      if (!tab) return;
      tab.uri = target;
      loadTabIntoForm(tab);
      doFetch();
    });
  });

  // Render the APIs tab + protocol tabs from the same manifest.
  // The split is product-shaped: MCP gets its own tab (because we
  // render its tool catalog interactively); every other bridged
  // protocol (OpenAPI, GraphQL, ...) is informational and folds into
  // the APIs tab as a "Bridged services" section above the resource
  // endpoints.
  const mcpProtocols = protocols.filter(
    (p) => (p.protocol || "").toLowerCase() === "mcp",
  );
  const otherProtocols = protocols.filter(
    (p) => (p.protocol || "").toLowerCase() !== "mcp",
  );
  renderApisTab(tab, apis, otherProtocols);
  renderProtocolTabs(tab, mcpProtocols);

  showPaneVariant("manifest");
  return true;
}

// ---------- APIs tab ----------
//
// Layout (top-to-bottom):
//
//   Bridged services    non-MCP hosts_protocols entries (OpenAPI,
//                       GraphQL, ...). Read-only metadata cards.
//                       Catalog rendering for these protocols is
//                       future work; the manifest entry is shown
//                       so deployments can declare them today.
//
//   Endpoints           the manifest's apis[] resource paths with
//                       per-method Try-It tags.
//
// Either section can be empty; the tab is shown when at least one
// section has content. The tab badge counts the total.

function renderApisTab(tab, apis, bridgedServices) {
  apis = apis || [];
  bridgedServices = bridgedServices || [];

  const total = apis.length + bridgedServices.length;
  if (els.apisTabBadge) {
    if (total) {
      els.apisTabBadge.textContent = String(total);
      els.apisTabBadge.classList.remove("hidden");
    } else {
      els.apisTabBadge.classList.add("hidden");
    }
  }

  if (!total) {
    els.apisEmpty.classList.remove("hidden");
    els.apisContent.classList.add("hidden");
    els.apisContent.innerHTML = "";
    return;
  }

  els.apisEmpty.classList.add("hidden");
  els.apisContent.classList.remove("hidden");

  // ---- Bridged services (top section) ----------------------------
  let html = "";
  if (bridgedServices.length) {
    html += `<h3 class="apis-section-title">Bridged services</h3>`;
    html += bridgedServices.map((p) => {
      const proto = (p.protocol || "").toLowerCase();
      const label = proto === "openapi" ? "OpenAPI"
                  : proto === "graphql" ? "GraphQL"
                  : capitalize(p.protocol || "service");
      return (
        `<div class="api-card bridge-card">` +
        `<div class="api-head">` +
        `<span class="api-path">${escapeHtml(label)}</span>` +
        `<span class="api-method-count">v${escapeHtml(p.version || "?")}</span>` +
        `</div>` +
        `<div class="api-desc">` +
        `<div><strong>Endpoint:</strong> <code>${escapeHtml(p.endpoint || "")}</code></div>` +
        (p.catalog
          ? `<div><strong>Catalog:</strong> <code>${escapeHtml(p.catalog)}</code></div>`
          : "") +
        `</div>` +
        `<div class="bridge-note">` +
        `Catalog rendering for this protocol is future work; the manifest ` +
        `entry is shown so deployments can declare it today.` +
        `</div>` +
        `</div>`
      );
    }).join("");
  }

  // ---- Resource endpoints (bottom section) -----------------------
  if (apis.length) {
    html += `<h3 class="apis-section-title">Endpoints</h3>`;
    html += apis.map((api) => {
      const tags = (api.methods || []).map((m) =>
        `<button type="button" class="method-tag" data-path="${escapeHtml(api.path)}" ` +
        `data-method="${escapeHtml(m)}">${escapeHtml(m)}</button>`,
      ).join("");
      return (
        `<div class="api-card">` +
        `<div class="api-head">` +
        `<span class="api-path">${escapeHtml(api.path)}</span>` +
        `<span class="api-method-count">${(api.methods || []).length} methods</span>` +
        `</div>` +
        (api.description
          ? `<div class="api-desc">${escapeHtml(api.description)}</div>`
          : "") +
        `<div class="method-tags">${tags}</div>` +
        `</div>`
      );
    }).join("");
  }

  els.apisContent.innerHTML = html;

  // Wire method-tag clicks to a Try-It prompt scoped to the path.
  els.apisContent.querySelectorAll(".method-tag").forEach((btn) => {
    btn.addEventListener("click", () => {
      const path = btn.getAttribute("data-path");
      const method = btn.getAttribute("data-method");
      promptApiTryIt(tab, path, method);
    });
  });
}

function promptApiTryIt(tab, path, method) {
  // Minimal Try-It: confirm intent, then issue the method on the
  // server's first agent (best-effort) with an empty body. The user
  // can refine after seeing the response. Future polish: in-pane
  // form scoped to the resource path's parameter schema.
  const agents = tab?.result?.manifest?.agents?.list || [];
  if (!agents.length) {
    window.alert(
      `${method} on ${path}: this server lists no public agents to ` +
      `target. Open an agent URL first, then invoke from there.`,
    );
    return;
  }
  const targetAgent = agents[0];
  const proceed = window.confirm(
    `Try ${method} on ${path}\n\n` +
    `Will be sent to agent: ${targetAgent.name} (${targetAgent.agent_id.slice(0, 16)}…)\n\n` +
    `Continue?`,
  );
  if (!proceed) return;
  const r = tab.result;
  const targetUri = `agtp://${targetAgent.agent_id}@${r.host}:${r.port}`;
  // Use the existing invoke API path. APIs Try-It is intentionally
  // bare-bones; the rich form lives in the Methods explorer (kept
  // for power users) but is not surfaced as a top-level tab.
  window.pywebview.api.invoke(
    targetUri, method, {}, tab.registry || "",
    !!tab.insecure, !!tab.skip, "",
  ).then((result) => {
    const body = result && result.body ? result.body : "(no body)";
    window.alert(
      `${method} ${path} -> ${result?.status_code || "?"}\n\n${body}`,
    );
  }).catch((e) => {
    window.alert(`Try-It error: ${e}`);
  });
}

// ---------- Protocol tabs (MCP only) ----------
//
// We dedicate a tab per MCP entry so its tool catalog can be fetched
// and rendered interactively. Other bridged protocols (OpenAPI,
// GraphQL, ...) live in the APIs tab as informational cards because
// catalog rendering for those is future work and they have no
// interactive surface today.

function renderProtocolTabs(tab, mcpEntries) {
  mcpEntries = mcpEntries || [];

  // Tear down any previous MCP tabs first; they are server-specific
  // and must not leak between fetches.
  els.protocolTabsHost.innerHTML = "";
  els.protocolPanesHost.innerHTML = "";

  mcpEntries.forEach((p, idx) => {
    const tabId = `proto-${idx}`;
    // Multiple MCP bridges on one server label as "MCP", "MCP 2", ...
    const label = idx === 0 ? "MCP" : `MCP ${idx + 1}`;

    const btn = document.createElement("button");
    btn.className = "rtab";
    btn.dataset.tab = tabId;
    btn.textContent = label;
    btn.addEventListener("click", () => activateRtab(tabId));
    els.protocolTabsHost.appendChild(btn);

    const pane = document.createElement("div");
    pane.id = `pane-${tabId}`;
    pane.className = "pane protocol-pane";
    pane.innerHTML = renderMcpPane(p);
    els.protocolPanesHost.appendChild(pane);

    if (p.catalog) {
      const fetchBtn = pane.querySelector(".fetch-btn");
      if (fetchBtn) {
        fetchBtn.addEventListener("click", () =>
          fetchAndRenderMcpCatalog(pane, p.catalog, !!tab.skip),
        );
      }
    }
  });
}

function renderMcpPane(p) {
  // Section 1 (server info) -> Section 2 (exposed connectors / tools).
  // Mirrors the APIs tab: server-level metadata up top, the
  // interactive surface (tool catalog) below.
  const head =
    `<h3 class="apis-section-title">MCP server</h3>` +
    `<div class="info">` +
    `<div><strong>Protocol:</strong> ${escapeHtml(p.protocol)}</div>` +
    `<div><strong>Version:</strong> ${escapeHtml(p.version)}</div>` +
    `<div><strong>Endpoint:</strong> ${escapeHtml(p.endpoint)}</div>` +
    (p.catalog
      ? `<div><strong>Catalog:</strong> ${escapeHtml(p.catalog)}</div>`
      : "") +
    `</div>`;

  if (!p.catalog) {
    return (
      head +
      `<h3 class="apis-section-title">Tools</h3>` +
      `<div class="stub-note">` +
      `This MCP entry has no catalog URL declared; tool listing ` +
      `requires a <code>catalog</code> field on the manifest entry.` +
      `</div>`
    );
  }

  return (
    head +
    `<h3 class="apis-section-title">Tools</h3>` +
    `<button class="fetch-btn" type="button">Fetch tool catalog</button>` +
    `<div class="tool-catalog"></div>`
  );
}

async function fetchAndRenderMcpCatalog(pane, catalogUrl, skipVerify) {
  const target = pane.querySelector(".tool-catalog");
  target.innerHTML =
    `<div class="info" style="margin-top:6px">Fetching ${escapeHtml(catalogUrl)}…</div>`;

  let result;
  try {
    result = await window.pywebview.api.fetch_mcp_catalog(
      catalogUrl, !!skipVerify,
    );
  } catch (e) {
    target.innerHTML = `<div class="fetch-error">bridge error: ${escapeHtml(String(e))}</div>`;
    return;
  }

  if (!result || !result.ok) {
    target.innerHTML =
      `<div class="fetch-error">${escapeHtml(result?.error || "fetch failed")}</div>`;
    return;
  }

  const tools = result.tools || [];
  if (!tools.length) {
    target.innerHTML =
      `<div class="info">Catalog returned, but no tools were listed.</div>`;
    return;
  }

  target.innerHTML =
    `<div class="tool-cards">` +
    tools.map((t) => {
      const name = t.name || t.id || "(unnamed)";
      const desc = t.description || t.summary || "";
      const params = t.parameters || t.input_schema || t.inputSchema || null;
      return (
        `<div class="tool-card">` +
        `<span class="name">${escapeHtml(name)}</span>` +
        (desc ? `<div class="desc">${escapeHtml(desc)}</div>` : "") +
        (params
          ? `<div class="params">${escapeHtml(JSON.stringify(params))}</div>`
          : "") +
        `</div>`
      );
    }).join("") +
    `</div>`;
}

function capitalize(s) {
  if (!s) return "";
  return s.charAt(0).toUpperCase() + s.slice(1).toLowerCase();
}

// ---------- tab visibility by URI type ----------
//
// Servers expose APIs and (when populated) hosted-protocol tabs.
// Agents are users; they have no Methods or APIs surface. Tabs are
// fully removed from the bar (display: none) rather than disabled,
// so the bar stays clean per URI type.

function applyTabVisibility(tab) {
  const isAgent = tab?.kind === "agent";
  const isManifest = tab?.kind === "manifest";
  const protocols = tab?.result?.manifest?.hosts_protocols || [];
  const apis = tab?.result?.manifest?.apis || [];

  // The APIs tab now subsumes non-MCP bridged protocols (OpenAPI,
  // GraphQL, ...). Show it whenever the manifest carries either
  // apis[] or any non-MCP protocol entry.
  const nonMcp = protocols.filter(
    (p) => (p.protocol || "").toLowerCase() !== "mcp",
  );
  const mcp = protocols.filter(
    (p) => (p.protocol || "").toLowerCase() === "mcp",
  );

  const apisBtn = document.querySelector('.rtab[data-tab="apis"]');
  if (apisBtn) {
    apisBtn.classList.toggle(
      "hidden",
      !(isManifest && (apis.length || nonMcp.length)),
    );
  }

  // MCP entries get dedicated tabs (rendered by renderProtocolTabs).
  // Hide the host element when no MCP entry is present so the gap
  // collapses cleanly.
  els.protocolTabsHost.classList.toggle(
    "hidden",
    !(isManifest && mcp.length),
  );

  // If the currently active rtab has been hidden by the URI type
  // change, fall back to "pretty".
  const activeBtn = document.querySelector(".rtab.active");
  if (activeBtn && activeBtn.classList.contains("hidden")) {
    activateRtab("pretty");
  }
}

function activateRtab(name) {
  $$(".rtab").forEach((b) => b.classList.remove("active"));
  $$(".pane").forEach((p) => p.classList.remove("active"));
  const btn = document.querySelector(`.rtab[data-tab="${name}"]`);
  const pane = document.querySelector(`#pane-${name}`);
  if (btn) btn.classList.add("active");
  if (pane) pane.classList.add("active");
  state.respPane = name;
}

function renderManifestMethodsList(items, title) {
  const rows = items.map((entry) =>
    `<li><code>${escapeHtml(entry.name)}</code> ` +
    `<span style="color:var(--text-dim);font-size:11px">` +
    `${escapeHtml(entry.category || "")}` +
    (entry.namespace ? ` · ${escapeHtml(entry.namespace)}` : "") +
    `</span></li>`,
  ).join("");
  return (
    `<div style="margin-top:6px"><strong style="font-size:11px;color:var(--text-dim)">` +
    `${escapeHtml(title)}</strong></div>` +
    `<ul style="list-style:none;margin:6px 0 0;padding:0;` +
    `display:flex;flex-direction:column;gap:2px;font-family:var(--mono);font-size:12px">` +
    rows +
    `</ul>`
  );
}

function renderManifestAgentCard(a, host, port) {
  const target = `agtp://${a.agent_id}@${host}:${port}`;
  return (
    `<div class="agent-card">` +
    `<span class="name">${escapeHtml(a.name || "")}</span>` +
    (a.skills_summary
      ? `<span class="skills-summary">${escapeHtml(a.skills_summary)}</span>`
      : "") +
    `<span class="meta">` +
    `<span>${escapeHtml(a.agent_id.slice(0, 12))}…</span>` +
    `<span>${a.methods_count} methods</span>` +
    `</span>` +
    `<button class="open-btn" data-target="${escapeHtml(target)}">Open</button>` +
    `</div>`
  );
}

function pillFor(label, value) {
  const cls = value ? "on" : "off";
  const symbol = value ? "✓" : "✗";
  return `<span class="policy-pill ${cls}">${symbol} ${escapeHtml(label)}</span>`;
}

// ---------- match handshake ----------

function endpointKeyFor(host, port) {
  return `${host}:${port}`;
}

async function fetchManifestForHandshake(tab, { force = false } = {}) {
  if (!tab.result || !tab.result.ok) return null;
  const host = tab.result.host;
  const port = tab.result.port;
  const key = endpointKeyFor(host, port);
  if (!force && manifestCacheByEndpoint.has(key)) {
    return manifestCacheByEndpoint.get(key);
  }
  let result;
  try {
    result = await window.pywebview.api.fetch_manifest(
      host, port, !!tab.insecure, !!tab.skip,
    );
  } catch (e) {
    return null;
  }
  if (result && result.ok && result.manifest) {
    manifestCacheByEndpoint.set(key, result.manifest);
    return result.manifest;
  }
  return null;
}

function computeMatchOutcome(agentDoc, manifest) {
  const requires = agentDoc.requires || {};
  const needs = (requires.methods || []).slice();
  const wildcards = !!requires.wildcards;
  const m = manifest || {};
  const meth = m.methods || {};
  const policy = m.policy || {};
  const universeSet = new Set();
  for (const e of meth.embedded || []) universeSet.add(e.name);
  for (const e of meth.custom || []) universeSet.add(e.name);
  const universe = Array.from(universeSet).sort();
  const serverWild = policy.wildcards_accepted !== false;

  if (wildcards && serverWild) {
    return {
      kind: "full",
      matched: universe.slice(),
      missing: [],
      universe,
      agentWantsWildcards: true,
      serverAcceptsWildcards: true,
    };
  }

  const matched = needs.filter((n) => universeSet.has(n)).sort();
  const missing = needs.filter((n) => !universeSet.has(n)).sort();
  let kind;
  if (!needs.length) {
    kind = universe.length ? "full" : "none";
  } else if (!missing.length) {
    kind = "full";
  } else if (matched.length === 0) {
    kind = "none";
  } else {
    kind = "partial";
  }
  return {
    kind, matched, missing, universe,
    agentWantsWildcards: wildcards,
    serverAcceptsWildcards: serverWild,
  };
}

function renderMatchBadge(tab) {
  const outcome = tab.matchOutcome;
  const badge = els.matchBadge;
  const detail = els.matchDetail;
  badge.classList.remove("full", "partial", "none");
  if (!outcome) {
    badge.classList.add("hidden");
    detail.classList.add("hidden");
    return;
  }
  badge.classList.add(outcome.kind);
  badge.classList.remove("hidden");

  const totalNeed = outcome.matched.length + outcome.missing.length;
  let desc;
  if (outcome.kind === "full") {
    desc = totalNeed
      ? `All ${totalNeed} required methods are available on this server.`
      : `Server exposes ${outcome.universe.length} methods.`;
  } else if (outcome.kind === "partial") {
    desc = `${outcome.matched.length} of ${totalNeed} required methods are available. ` +
      `Missing: ${outcome.missing.join(", ")}.`;
  } else {
    desc = `${outcome.matched.length} of ${totalNeed} required methods are available.`;
  }
  badge.innerHTML =
    `<span class="label">Match: ${outcome.kind}</span>` +
    `<span class="desc">${escapeHtml(desc)}</span>` +
    `<a class="refresh" data-action="refresh-manifest">↻ refresh manifest</a>`;
  badge.querySelector(".refresh").addEventListener("click", async (e) => {
    e.stopPropagation();
    await refreshManifestAndMatch(tab);
  });
  badge.onclick = (e) => {
    if (e.target && e.target.classList.contains("refresh")) return;
    detail.classList.toggle("hidden");
  };

  detail.innerHTML =
    `<div>Matched (${outcome.matched.length}): ${
      outcome.matched.length ? escapeHtml(outcome.matched.join(", ")) : "(none)"
    }</div>` +
    `<div>Missing (${outcome.missing.length}): ${
      outcome.missing.length ? escapeHtml(outcome.missing.join(", ")) : "(none)"
    }</div>` +
    `<div>Server has (${outcome.universe.length}): ${
      escapeHtml(outcome.universe.join(", "))
    }</div>` +
    (outcome.agentWantsWildcards && !outcome.serverAcceptsWildcards
      ? `<div style="color:var(--warn)">Note: agent declares wildcards but server policy refuses; ` +
        `non-embedded calls will return 462.</div>`
      : "");
}

async function refreshManifestAndMatch(tab) {
  if (!tab || !tab.result || !tab.result.ok) return;
  const manifest = await fetchManifestForHandshake(tab, { force: true });
  if (!manifest) return;
  const docText = tab.result.body;
  let doc = null;
  try { doc = JSON.parse(docText); } catch {}
  if (!doc) return;
  tab.matchOutcome = computeMatchOutcome(doc, manifest);
  if (state.activeId === tab.id) {
    renderMatchBadge(tab);
    renderRequiresSection(tab, doc);
    if (tab.methods) renderMethods(tab);
  }
}

// ---------- response banners (45x / 46x) ----------

const STATUS_BANNER_KINDS = {
  451: { cls: "scope-violation",     head: "451 Scope Violation" },
  452: { cls: "method-outside-need", head: "452 Method Outside Agent's Declared Need" },
  460: { cls: "negotiation-refused", head: "460 Negotiation Refused" },
  461: { cls: "counter-proposal",    head: "461 Counter-Proposal" },
  462: { cls: "wildcards-refused",   head: "462 Wildcards Refused" },
};

function renderStatusBanner(result) {
  if (!result || !result.ok) return null;
  const meta = STATUS_BANNER_KINDS[result.status_code];
  if (!meta) return null;

  let payload;
  try { payload = JSON.parse(result.body); }
  catch { payload = {}; }

  const div = document.createElement("div");
  div.className = `resp-banner ${meta.cls}`;

  const head = document.createElement("div");
  head.className = "head";
  head.textContent = meta.head;
  div.appendChild(head);

  const detail = document.createElement("div");
  detail.className = "detail";
  if (meta.cls === "counter-proposal") {
    const counter = payload.counter_proposal || {};
    detail.textContent =
      `Server suggests ${counter.name || "(unknown)"}: ${counter.description || ""}`;
    div.appendChild(detail);
    const spec = document.createElement("div");
    spec.className = "counter-spec";
    spec.textContent = JSON.stringify(counter, null, 2);
    div.appendChild(spec);
  } else if (meta.cls === "negotiation-refused") {
    const err = payload.error || {};
    detail.textContent =
      `${err.reason || "unknown"}: ${err.explanation || ""}`;
    div.appendChild(detail);
  } else {
    const err = payload.error || {};
    detail.textContent = err.explanation || result.status_text || "";
    div.appendChild(detail);
  }

  const meta2 = document.createElement("div");
  meta2.className = "meta";
  meta2.textContent = `${result.host}:${result.port}`;
  div.appendChild(meta2);
  return div;
}

// ---------- main fetch ----------
async function doFetch({ silentHistory = false } = {}) {
  const tab = getActive();
  if (!tab) return;
  snapshotFormToTab(tab);

  const uri = tab.uri.trim();
  if (!uri) {
    setStatus("Enter an agtp:// URI.", "err");
    return;
  }

  tab.name = null;
  // Reset per-load state.
  tab.matchOutcome = null;
  tab.serverManifest = null;
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
  tab.kind = result.kind || (result.ok ? (result.agent_id ? "agent" : "manifest") : null);
  // Reset the methods view; auto-DISCOVER will repopulate it on success.
  tab.methods = null;
  tab.openMethod = null;
  if (result.ok) {
    tab.name = nameFromBody(result.body, result.format) ||
      (result.kind === "manifest" ? `server: ${result.host}` : null);
    // Server-level manifest fetch: also seed the manifest cache and
    // load it into tab.serverManifest so the Methods tab uses it.
    if (result.kind === "manifest" && result.manifest) {
      manifestCacheByEndpoint.set(
        endpointKeyFor(result.host, result.port),
        result.manifest,
      );
      tab.serverManifest = result.manifest;
    }
  }
  renderTabStrip();

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
    applyTabVisibility(tab);
  }

  if (result.ok && !silentHistory) {
    pushUriHistory(uri, tab.name);
  } else {
    updateNavButtons();
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

  // Auto-DISCOVER /methods on a successful agent fetch. Manifest URIs
  // already carry the methods inventory, so no follow-up is needed.
  if (
    result.ok
    && result.status_code === 200
    && result.kind === "agent"
  ) {
    doDiscoverMethods(tab);
    // Matching handshake: fetch (or reuse) the server manifest and
    // compute the outcome. If anything fails we leave the badge empty.
    fetchManifestForHandshake(tab).then((manifest) => {
      if (!manifest) return;
      tab.serverManifest = manifest;
      let doc = null;
      try { doc = JSON.parse(result.body); } catch {}
      if (!doc) return;
      tab.matchOutcome = computeMatchOutcome(doc, manifest);
      if (state.activeId === tab.id) {
        renderMatchBadge(tab);
        renderRequiresSection(tab, doc);
        if (tab.methods) renderMethods(tab);
      }
    });
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
