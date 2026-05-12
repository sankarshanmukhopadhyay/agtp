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

// ---------- document classification ----------
//
// What kind of document came back? Three signals, in priority order:
//
//   1. Headers (authoritative).
//      X-AGTP-Document-Type tells us the document kind:
//        agtp.agent.document    Agent Document (Form 1/1a)
//        agtp.server.manifest   AGTP Server Manifest (Form 2)
//        agtp.server.identity   Application-server identity doc
//                               (e.g., MCP-on-AGTP gateway)
//      X-AGTP-Application names the application kind on
//      application-typed servers ("mcp" today; "openapi", "graphql"
//      in the future).
//
//   2. URI form (back-compat).
//      Form 1/1a carries an agent_id → agent. Form 2 → manifest.
//      Used when a server pre-dates the header contract.
//
//   3. Body shape (last resort).
//      An identity doc labels itself with `application.type` at the
//      document root. Read directly off the parsed manifest.
//
// Output: { kind, application, source }. `kind` is one of "agent",
// "manifest", or "unknown". `application` is the lowercased
// application name when known, else null. `source` records which
// of the three layers won, useful for debugging "why did this
// server render as X?" without re-deriving the answer.
function classifyDocument(result) {
  if (!result || !result.ok) {
    return { kind: "unknown", application: null, source: "error" };
  }

  const docType = (result.document_type || "").toLowerCase();
  const application = (result.application || "").toLowerCase() || null;

  // 1. Headers.
  if (docType === "agtp.agent.document") {
    return { kind: "agent", application: null, source: "header" };
  }
  if (
    docType === "agtp.server.manifest"
    || docType === "agtp.server.identity"
  ) {
    return { kind: "manifest", application, source: "header" };
  }

  // 2. URI form (bridge's `kind`, derived from the URI before fetch).
  if (result.agent_id || result.kind === "agent") {
    return { kind: "agent", application: null, source: "uri" };
  }
  if (result.kind === "manifest") {
    // 3. Body shape — only if no header dispatched us. Identity-doc
    // shaped manifests label themselves via `application.type`.
    const bodyApp = (
      result.manifest?.application?.type || ""
    ).toLowerCase() || null;
    return {
      kind: "manifest",
      application: bodyApp,
      source: bodyApp ? "body" : "uri",
    };
  }

  return { kind: "unknown", application: null, source: "unknown" };
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
  // Protocol tabs/panes are URI-scoped and must not survive a fetch.
  // If renderManifestView throws partway, applyTabVisibility never
  // runs and old buttons would otherwise stay clickable with stale
  // contents underneath them.
  if (els.protocolTabsHost) els.protocolTabsHost.innerHTML = "";
  if (els.protocolPanesHost) els.protocolPanesHost.innerHTML = "";
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
  // Classification is the authoritative dispatcher — header-first,
  // URI fallback, body last. Use tab.classification when present
  // (doFetch populates it) and re-derive otherwise so callers that
  // build a synthetic tab (e.g., invocations replay) still work.
  const cls = tab.classification || classifyDocument(r);
  if (r.agent_id) {
    hLines.push(`# agent_id: ${r.agent_id}`);
  } else if (cls.kind === "manifest") {
    hLines.push(`# server: ${r.host}:${r.port}`);
  }
  els.headers.textContent = hLines.join("\n");

  // Status-specific banner (455 / 403 soft-deny / 422 negotiation). Rendered
  // in the Pretty pane above the structured/raw content. For 422
  // counter-proposal responses the Accept Counter button is wired
  // via the try-it pathway.
  const banner = renderStatusBanner(r);

  // Pretty pane variants:
  //   * Manifest -> structured manifest view.
  //   * Agent doc (status 200) -> structured agent view.
  //   * HTML format -> iframe.
  //   * Otherwise -> syntax-highlighted JSON or plain text.
  if (cls.kind === "manifest") {
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

  if (cls.kind === "agent" && r.format === "html") {
    els.prettyIfr.srcdoc = r.body;
    showPrettyAs("iframe");
    return;
  }

  if (cls.kind === "agent" && r.status_code === 200 && r.format === "json") {
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

function badgeForSpec(spec) {
  // The embedded-vs-custom distinction is signalled by namespace
  // presence: embedded primitives carry none; custom methods
  // require one. The badge label reflects which bucket the method
  // belongs to.
  if (spec && spec.namespace) {
    return { cls: "src-custom", label: "Custom" };
  }
  return { cls: "src-agtp", label: "AGTP standard" };
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

  const srcInfo = badgeForSpec(spec);
  const srcBadge = document.createElement("span");
  srcBadge.className = `badge ${srcInfo.cls}`;
  srcBadge.textContent = srcInfo.label;
  srcBadge.title = spec.namespace ? `namespace: ${spec.namespace}` : "AGTP-standard embedded method";
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
    ["kind", spec.namespace ? `custom · ${spec.namespace}` : "AGTP embedded"],
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

// ---------- active syntheses panel ----------
//
// Populated by the Compose drawer's accept handler. Each accepted
// PROPOSE pushes an entry onto ``tab.activeSyntheses`` keyed by
// synthesis_id; this renderer surfaces them in the Server Overview
// next to the manifest's methods inventory so the user can invoke
// the proposed method without leaving the workplace view.

function renderActiveSyntheses(tab) {
  // Append a panel to the manifest's Methods section. The section's
  // .innerHTML is rebuilt on each renderManifest call, so the panel
  // we append here is naturally cleared on re-render — calling this
  // function after the innerHTML assignment refreshes the panel.
  const host = els.manifestMethodsSection;
  if (!host) return;
  // Clear any previous panel.
  const prior = host.querySelector(".active-syntheses-panel");
  if (prior) prior.remove();
  const entries = tab.activeSyntheses || {};
  const ids = Object.keys(entries).sort(
    (a, b) => (entries[b].created_at || 0) - (entries[a].created_at || 0),
  );
  if (!ids.length) return;
  const panel = document.createElement("div");
  panel.className = "active-syntheses-panel";
  panel.innerHTML = `<h4 class="active-syntheses-head">Active syntheses</h4>`;
  const list = document.createElement("div");
  list.className = "active-syntheses-list";
  for (const id of ids) {
    const entry = entries[id];
    list.appendChild(_buildSynthesisPill(tab, id, entry));
  }
  panel.appendChild(list);
  host.appendChild(panel);
}

function _buildSynthesisPill(tab, synthesisId, entry) {
  const pill = document.createElement("div");
  pill.className = "synthesis-pill";

  const head = document.createElement("div");
  head.className = "synthesis-pill-head";
  const title = document.createElement("span");
  title.className = "synthesis-pill-title";
  title.textContent = entry.method;
  head.appendChild(title);
  const idBadge = document.createElement("code");
  idBadge.className = "synthesis-pill-id";
  idBadge.textContent = synthesisId;
  idBadge.title = synthesisId;
  head.appendChild(idBadge);
  if (entry.plan && entry.plan.steps && entry.plan.steps.length) {
    const planBadge = document.createElement("span");
    planBadge.className = "synthesis-pill-plan";
    planBadge.textContent = `${entry.plan.steps.length} step plan`;
    head.appendChild(planBadge);
  } else if (entry.target_method) {
    const planBadge = document.createElement("span");
    planBadge.className = "synthesis-pill-plan";
    planBadge.textContent = `→ ${entry.target_method}`;
    head.appendChild(planBadge);
  }
  const clearBtn = document.createElement("button");
  clearBtn.type = "button";
  clearBtn.className = "synthesis-pill-clear";
  clearBtn.title = "Clear (forget locally; server retains until SUSPEND)";
  clearBtn.textContent = "×";
  clearBtn.addEventListener("click", (ev) => {
    ev.stopPropagation();
    if (tab.activeSyntheses) delete tab.activeSyntheses[synthesisId];
    if (tab.syntheses) delete tab.syntheses[entry.method];
    renderActiveSyntheses(tab);
  });
  head.appendChild(clearBtn);
  pill.appendChild(head);

  // Inline try-it form re-using the existing builder. The form's
  // invocation handler reads tab.syntheses[spec.name] for the
  // Synthesis-Id header — the accept handler stashed the id under
  // entry.method, so the request fires through the runtime.
  const tryIt = buildTryItForm(tab, entry.spec);
  pill.appendChild(tryIt);
  return pill;
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
  } else if (result.status_code === 422 && result.body
             && result.body.includes("counter_proposal")) {
    // Counter-proposal landed at 422 with a counter_proposal body.
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

  // Server identity is exposed under `server_id` post-§5; older
  // manifests carry it as `issuer`. The MCP-on-AGTP gateway emits
  // `agtp.server.identity` docs that label themselves with
  // `server.name`. Fall back to the resolved host so identity docs
  // don't head as "(server)".
  const serverId = m.server?.server_id
    || m.server?.issuer
    || m.server?.name
    || r.host
    || "(server)";
  els.manifestHeader.innerHTML =
    `<h2>${escapeHtml(serverId)}</h2>` +
    `<span class="endpoint">agtp://${escapeHtml(r.host)}:${escapeHtml(String(r.port))}</span>`;

  // Server section.
  const sv = m.server || {};
  const features = (sv.supported_features || [])
    .map((f) => `<span class="feature-pill">${escapeHtml(f)}</span>`)
    .join("");
  const issued = sv.issued || m.issued_at || "";
  const updated = sv.updated || "";
  els.manifestServer.innerHTML =
    `<h3>Server</h3>` +
    `<div class="body">` +
    `<dl class="kv-grid">` +
    `<dt>Operator</dt><dd>${escapeHtml(sv.operator || "")}</dd>` +
    `<dt>Contact</dt><dd>${escapeHtml(sv.contact || "")}</dd>` +
    (sv.domain
      ? `<dt>Domain</dt><dd>${escapeHtml(sv.domain)}</dd>`
      : "") +
    `<dt>AGTP version</dt><dd>${escapeHtml(m.agtp_version || "")}</dd>` +
    `<dt>AGTP-API version</dt><dd>${escapeHtml(m.agtp_api_version || "")}</dd>` +
    `<dt>Document version</dt><dd>${escapeHtml(m.document_version || "")}</dd>` +
    `<dt>Issued</dt><dd>${escapeHtml(issued)}</dd>` +
    (updated
      ? `<dt>Updated</dt><dd>${escapeHtml(updated)}</dd>`
      : "") +
    `</dl>` +
    (features
      ? `<div style="margin-top:10px">${features}</div>`
      : "") +
    `</div>`;

  // Methods section. Three shapes show up in the wild:
  //   * post-§5: top-level `embedded_methods` / `custom_methods` as
  //     arrays of {name, category, namespace?} objects.
  //   * pre-§5:  nested `methods.{embedded,custom}` with the same
  //     entry shape.
  //   * server-identity (e.g., MCP-on-AGTP gateway): `methods.embedded`
  //     and `methods.custom` are counts (numbers), with the list living
  //     under `methods.standard_methods` as bare verb strings.
  // pickMethodList walks the candidates and returns the first array
  // it finds, skipping past number-shaped counts so the identity-doc
  // path falls through to `standard_methods`.
  const embedded = normalizeMethods(pickMethodList(
    m.embedded_methods,
    m.methods?.embedded,
    m.methods?.standard_methods,
  ));
  const custom = normalizeMethods(pickMethodList(
    m.custom_methods,
    m.methods?.custom,
    m.methods?.custom_methods,
  ));
  const embeddedCount = typeof m.methods?.embedded === "number"
    ? m.methods.embedded
    : embedded.length;
  const customCount = typeof m.methods?.custom === "number"
    ? m.methods.custom
    : custom.length;
  const totalMethods = embeddedCount + customCount;
  els.manifestMethodsSection.innerHTML =
    `<h3>Methods (${totalMethods})</h3>` +
    `<div class="body">` +
    `<div style="font-size:11.5px;color:var(--text-dim);margin-bottom:8px">` +
    `Embedded: ${embeddedCount} &nbsp;·&nbsp; ` +
    `Custom: ${customCount}` +
    `</div>` +
    renderManifestMethodsList(embedded, "Standard Methods") +
    (custom.length ? renderManifestMethodsList(custom, "Custom Methods") : "") +
    `</div>`;

  // Active syntheses panel — populated by the Compose drawer's
  // accept handler. Lives at the bottom of the Methods section so a
  // user can see (and invoke) syntheses they've negotiated this
  // session without leaving the workplace view.
  renderActiveSyntheses(tab);

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

  // Agents section. Post-§5: top-level hosted_agents /
  // agent_disclosure_notice. Pre-§5: nested agents.{list,notice}.
  const list = m.hosted_agents
    || (m.agents && m.agents.list)
    || [];
  const disclosureNotice = m.agent_disclosure_notice
    || (m.agents && m.agents.notice)
    || "";
  let agentsHtml = `<h3>Hosted agents (${list.length})</h3>`;
  if (disclosureNotice) {
    agentsHtml += `<div class="disclosure-notice">${escapeHtml(disclosureNotice)}</div>`;
  }
  if (list.length === 0) {
    agentsHtml += `<div class="agents-empty">No agents disclosed at this server.</div>`;
  } else {
    agentsHtml += `<div class="agent-cards">${
      list.map((a) => renderManifestAgentCard(a, r.host, r.port)).join("")
    }</div>`;
  }
  els.manifestAgents.innerHTML = agentsHtml;

  // Hosted protocols section. Post-§5: hosted_protocols. Pre-§5:
  // hosts_protocols.
  const protocols = m.hosted_protocols || m.hosts_protocols || [];
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
  const mcpProtocols = collectMcpEntries(m, protocols, r);
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
//   Bridged services    non-MCP hosted_protocols entries (OpenAPI,
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

  // Badge counts endpoints only. Bridged services are informational
  // metadata, not endpoints the user can invoke against, so they
  // shouldn't inflate the count. The tab itself is still shown when
  // either section has content (see applyTabVisibility).
  if (els.apisTabBadge) {
    if (apis.length) {
      els.apisTabBadge.textContent = String(apis.length);
      els.apisTabBadge.classList.remove("hidden");
    } else {
      els.apisTabBadge.classList.add("hidden");
    }
  }

  if (!apis.length && !bridgedServices.length) {
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

// Collect every MCP-flavored entry on a manifest into a uniform list
// for the protocol-tab renderer. Two sources contribute:
//
//   * `hosted_protocols[]` entries with protocol == "mcp" — the
//     standard manifest channel, used by servers that bridge an MCP
//     deployment as one of many hosted protocols.
//   * the document-root `application` block with `type == "mcp"` —
//     the shape served by MCP-on-AGTP gateways (see
//     mcp-on-agtp/gateway.py:374). The identity doc is itself the
//     MCP surface, so the tools list arrives inline and no catalog
//     fetch is needed.
//
// Both sources are normalized to the same entry shape so
// renderMcpPane stays agnostic about where the data came from.
function collectMcpEntries(manifest, hostedProtocols, fetchResult) {
  const out = [];
  const app = detectMcpApplication(manifest, fetchResult);
  if (app) out.push(app);
  for (const p of hostedProtocols || []) {
    if ((p.protocol || "").toLowerCase() === "mcp") out.push(p);
  }
  return out;
}

function detectMcpApplication(manifest, fetchResult) {
  const app = manifest?.application;
  if (!app || (app.type || "").toLowerCase() !== "mcp") return null;
  const endpoint = app.endpoint
    || (fetchResult?.host
      ? `agtp://${fetchResult.host}:${fetchResult.port}`
      : "");
  return {
    protocol: "mcp",
    version: app.protocol_version || app.version || "?",
    endpoint,
    catalog: app.catalog || "",
    inlineTools: Array.isArray(app.tools) ? app.tools : null,
    backendName: app.name || "",
    backendVersion: app.version || "",
    toolCount: typeof app.tool_count === "number" ? app.tool_count : null,
    capabilities: app.capabilities || null,
    transport: app.backend_transport || "",
    source: "application",
  };
}

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

    // Inline tools (from application.tools on an identity doc) are
    // already in hand — paint them directly. Otherwise the pane has
    // a Fetch button wired to the catalog URL.
    if (Array.isArray(p.inlineTools) && p.inlineTools.length) {
      const target = pane.querySelector(".tool-catalog");
      if (target) target.innerHTML = renderToolCards(p.inlineTools);
    } else if (p.catalog) {
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
  const hasInlineTools = Array.isArray(p.inlineTools) && p.inlineTools.length;
  const head =
    `<h3 class="apis-section-title">MCP server</h3>` +
    `<div class="info">` +
    `<div><strong>Protocol:</strong> ${escapeHtml(p.protocol)}</div>` +
    `<div><strong>Version:</strong> ${escapeHtml(p.version)}</div>` +
    `<div><strong>Endpoint:</strong> ${escapeHtml(p.endpoint)}</div>` +
    (p.backendName
      ? `<div><strong>Backend:</strong> ${escapeHtml(p.backendName)}` +
        (p.backendVersion ? ` <span style="color:var(--text-dim)">v${escapeHtml(p.backendVersion)}</span>` : "") +
        `</div>`
      : "") +
    (p.transport
      ? `<div><strong>Transport:</strong> ${escapeHtml(p.transport)}</div>`
      : "") +
    (p.catalog
      ? `<div><strong>Catalog:</strong> ${escapeHtml(p.catalog)}</div>`
      : "") +
    `</div>`;

  // Inline tools (identity-doc shape): tool list is already present
  // on the document, no fetch needed.
  if (hasInlineTools) {
    const count = p.toolCount ?? p.inlineTools.length;
    return (
      head +
      `<h3 class="apis-section-title">Tools (${count})</h3>` +
      `<div class="tool-catalog"></div>`
    );
  }

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

function renderToolCards(tools) {
  if (!Array.isArray(tools) || !tools.length) {
    return `<div class="info">No tools listed.</div>`;
  }
  return (
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
    `</div>`
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

  target.innerHTML = renderToolCards(tools);
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
  const cls = tab?.classification || classifyDocument(tab?.result);
  const isAgent = cls.kind === "agent";
  const isManifest = cls.kind === "manifest";
  const manifest = tab?.result?.manifest;
  const protocols =
    manifest?.hosted_protocols
    || manifest?.hosts_protocols
    || [];
  const apis = manifest?.apis || [];

  // The APIs tab now subsumes non-MCP bridged protocols (OpenAPI,
  // GraphQL, ...). Show it whenever the manifest carries either
  // apis[] or any non-MCP protocol entry.
  const nonMcp = protocols.filter(
    (p) => (p.protocol || "").toLowerCase() !== "mcp",
  );
  // MCP surface comes from three places, in priority order:
  //   1. X-AGTP-Application: mcp on the response (classification.application)
  //   2. hosted_protocols[].protocol == "mcp" on the manifest
  //   3. application.type == "mcp" in the body (identity-doc shape)
  // detectMcpApplication covers (3); classification covers (1).
  // Must stay aligned with collectMcpEntries.
  const hasMcp =
    cls.application === "mcp"
    || protocols.some((p) => (p.protocol || "").toLowerCase() === "mcp")
    || !!detectMcpApplication(manifest, tab?.result);

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
    !(isManifest && hasMcp),
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

function pickMethodList(...candidates) {
  for (const c of candidates) {
    if (Array.isArray(c)) return c;
  }
  return [];
}

function normalizeMethods(raw) {
  if (!Array.isArray(raw)) return [];
  return raw.map((entry) =>
    typeof entry === "string" ? { name: entry } : (entry || {}),
  );
}

function renderManifestMethodsList(items, title) {
  const rows = items.map((entry) =>
    `<li><code>${escapeHtml(entry.name || "")}</code> ` +
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
  // Method universe and policy block are top-level post-§5; pre-§5
  // shapes nest them under ``methods`` / ``policy``. Identity docs
  // expose only counts under ``methods.{embedded,custom}``, with the
  // verb list living at ``methods.standard_methods``. normalizeMethods
  // coerces all three into {name} objects.
  const embeddedMethods = normalizeMethods(pickMethodList(
    m.embedded_methods,
    m.methods?.embedded,
    m.methods?.standard_methods,
  ));
  const customMethods = normalizeMethods(pickMethodList(
    m.custom_methods,
    m.methods?.custom,
    m.methods?.custom_methods,
  ));
  const policies = m.policies || m.policy || {};
  const universeSet = new Set();
  for (const e of embeddedMethods) if (e.name) universeSet.add(e.name);
  for (const e of customMethods) if (e.name) universeSet.add(e.name);
  const universe = Array.from(universeSet).sort();
  const serverWild = policies.wildcards_accepted !== false;

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
        `non-embedded calls will return 403 wildcards-refused.</div>`
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

// ---------- response banners ----------
//
// AGTP refusals now ride existing HTTP-style status codes:
//   * 455                          → Scope Violation
//   * 403 method-not-permitted-..  → soft-deny banner
//   * 403 wildcards-refused        → wildcards-refused banner
//   * 422 + counter_proposal       → counter-proposal banner
//   * 422 negotiation-refused      → negotiation-refused banner
// We branch on (status_code, body shape) since the wire codes are
// re-used across multiple AGTP semantics.

function _statusBannerMeta(result, payload) {
  const code = result.status_code;
  const errCode = (payload.error && payload.error.code) || "";
  if (code === 455) {
    return { cls: "scope-violation", head: "455 Scope Violation" };
  }
  if (code === 403 && errCode === "method-not-permitted-for-agent") {
    return { cls: "method-outside-need",
             head: "403 Method Not Permitted for Agent" };
  }
  if (code === 403 && errCode === "wildcards-refused") {
    return { cls: "wildcards-refused", head: "403 Wildcards Refused" };
  }
  if (code === 422 && payload.counter_proposal) {
    return { cls: "counter-proposal", head: "422 Counter-Proposal" };
  }
  if (code === 422 && errCode === "negotiation-refused") {
    return { cls: "negotiation-refused", head: "422 Negotiation Refused" };
  }
  return null;
}

function renderStatusBanner(result) {
  if (!result || !result.ok) return null;

  let payload;
  try { payload = JSON.parse(result.body); }
  catch { payload = {}; }

  const meta = _statusBannerMeta(result, payload);
  if (!meta) return null;

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
  // classifyDocument prefers X-AGTP-Document-Type, falls back to URI
  // form, and only consults the body as a last resort. tab.kind is
  // derived from it so every existing `tab.kind === "..."` site
  // automatically picks up header-driven dispatch; tab.classification
  // also carries the application type ("mcp", ...) for the MCP tab.
  const classification = classifyDocument(result);
  tab.classification = classification;
  tab.kind = classification.kind === "unknown" ? null : classification.kind;
  // Reset the methods view; auto-DISCOVER will repopulate it on success.
  tab.methods = null;
  tab.openMethod = null;
  if (result.ok) {
    tab.name = nameFromBody(result.body, result.format) ||
      (tab.kind === "manifest" ? `server: ${result.host}` : null);
    // Server-level manifest fetch: also seed the manifest cache and
    // load it into tab.serverManifest so the Methods tab uses it.
    if (tab.kind === "manifest" && result.manifest) {
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
  // Use the classification (header-first) rather than result.kind so
  // the header contract is honored across the whole dispatch chain.
  if (
    result.ok
    && result.status_code === 200
    && classification.kind === "agent"
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

/* ============================================================
   Developer drawer + Compose tab.

   Self-contained module: the drawer's state, library persistence,
   form rendering, validation, YAML preview, and submission flow
   all live below. Hooks into the rest of the app only via:
     - the wrench button + F12 / Ctrl+Shift+I keyboard shortcuts
     - window.pywebview.api.{validate_compose, get_verb_catalog,
       save_method_yaml, export_library, import_library, invoke}
     - reading the current tab's URI for "Will submit to:" target

   Validation is catalog-driven: the verb name is checked against
   the AGTP curated catalog (server-side core/methods.json) and the
   path is checked against core/path_grammar.py.

   Future drawer tabs (Inspect, Storage, Network) drop into the same
   .dev-drawer-tabs bar with their own .dev-pane sibling.
   ============================================================ */
(function devDrawer() {
  "use strict";

  // ---- localStorage keys ----
  const DRAWER_KEY  = "elemen.drawer.v1";
  const LIBRARY_KEY = "elemen.method_library.v1";
  const LIBRARY_LIMIT = 50;

  // ---- DOM cache ----
  const D = {
    drawer:        $("#dev-drawer"),
    toggle:        $("#drawer-toggle"),
    closeBtn:      $("#dev-drawer-close"),
    resize:        $("#dev-drawer-resize"),
    composePane:   $("#dev-pane-compose"),
    composeForm:   $("#compose-form"),
    composeEmpty:  $("#compose-empty"),
    composeActive: $("#compose-active"),
    composeRO:     $("#compose-readonly-banner"),
    response:      $("#compose-response"),
    libList:       $("#lib-list"),
    libEmpty:      $("#lib-empty"),
    libNew:        $("#lib-new"),
    libMenuBtn:    $("#lib-menu-btn"),
    libMenu:       $("#lib-menu"),
    emptyNew:      $("#compose-empty-new"),
    name:          $("#cf-name"),
    nameAuto:      $("#cf-name-autocomplete"),
    path:          $("#cf-path"),
    description:   $("#cf-description"),
    intent:        $("#cf-intent"),
    outcome:       $("#cf-outcome"),
    capability:    $("#cf-capability"),
    confidence:    $("#cf-confidence"),
    confidenceVal: $("#cf-confidence-value"),
    idempotent:    $("#cf-idempotent"),
    namespace:     $("#cf-namespace"),
    errorCodes:    $("#cf-error-codes"),
    impact:        document.querySelector(".compose-impact"),
    submit:        $("#cf-submit"),
    saveFile:      $("#cf-save-file"),
    addLibrary:    $("#cf-add-library"),
    submitTarget:  $("#cf-submit-target"),
    yamlPane:      $("#compose-yaml"),
    yamlPre:       $("#cf-yaml-pre"),
    yamlCopy:      $("#cf-yaml-copy"),
    yamlToggle:    $("#cf-yaml-toggle"),
    warnings:      $("#compose-warnings"),
    toast:         $("#compose-toast"),
  };

  // ---- state ----
  const drawerState = loadDrawerState();
  let library = loadLibrary();
  let activeLibId = null;        // null = unsaved draft
  let readOnly = false;
  let yamlCollapsed = false;
  let verbCatalog = [];          // [{name, members, categories, description}, ...]
  let autocompleteIndex = -1;
  const debounceTimers = new Map();

  // ---- utilities ----
  function loadDrawerState() {
    try {
      const raw = localStorage.getItem(DRAWER_KEY);
      if (!raw) return { open: false, width: 0 };
      const parsed = JSON.parse(raw);
      return {
        open: !!parsed.open,
        width: Number(parsed.width) || 0,
      };
    } catch (e) {
      return { open: false, width: 0 };
    }
  }
  function saveDrawerState() {
    try {
      localStorage.setItem(DRAWER_KEY, JSON.stringify(drawerState));
    } catch (e) { /* ignore quota errors */ }
  }
  function loadLibrary() {
    try {
      const raw = localStorage.getItem(LIBRARY_KEY);
      if (!raw) return { version: 1, entries: [] };
      const parsed = JSON.parse(raw);
      if (!parsed || !Array.isArray(parsed.entries)) {
        return { version: 1, entries: [] };
      }
      return { version: 1, entries: parsed.entries.slice(0, LIBRARY_LIMIT) };
    } catch (e) {
      return { version: 1, entries: [] };
    }
  }
  function saveLibrary() {
    try {
      library.entries = library.entries.slice(0, LIBRARY_LIMIT);
      localStorage.setItem(LIBRARY_KEY, JSON.stringify(library));
    } catch (e) { /* ignore */ }
  }
  function newLibId(name) {
    const slug = (name || "draft").toLowerCase().replace(/[^a-z0-9]+/g, "_");
    const stamp = new Date().toISOString().replace(/[^0-9]/g, "").slice(0, 14);
    return `lib_${slug}_${stamp}`;
  }
  function nowIso() { return new Date().toISOString(); }
  function relTime(iso) {
    if (!iso) return "";
    const t = new Date(iso).getTime();
    if (isNaN(t)) return "";
    const diff = Date.now() - t;
    if (diff < 60_000) return "just now";
    if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`;
    if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h ago`;
    return `${Math.floor(diff / 86_400_000)}d ago`;
  }
  function debounce(key, fn, ms = 200) {
    if (debounceTimers.has(key)) clearTimeout(debounceTimers.get(key));
    debounceTimers.set(key, setTimeout(() => {
      debounceTimers.delete(key);
      fn();
    }, ms));
  }
  function showToast(text) {
    D.toast.textContent = text;
    D.toast.classList.remove("hidden");
    setTimeout(() => D.toast.classList.add("hidden"), 1800);
  }
  function currentUri() {
    if (typeof getActive === "function") {
      const tab = getActive();
      return (tab && tab.uri) || "";
    }
    return (els.uri && els.uri.value || "").trim();
  }

  // ---- drawer open/close + resize ----
  function openDrawer() {
    D.drawer.classList.remove("hidden");
    D.drawer.setAttribute("aria-hidden", "false");
    D.toggle.classList.add("active");
    if (drawerState.width) {
      D.drawer.style.width = drawerState.width + "px";
    }
    drawerState.open = true;
    saveDrawerState();
    if (!activeLibId && library.entries.length === 0) {
      // First-time or empty: show empty state.
      D.composeEmpty.classList.remove("hidden");
      D.composeActive.classList.add("hidden");
    }
  }
  function closeDrawer() {
    D.drawer.classList.add("hidden");
    D.drawer.setAttribute("aria-hidden", "true");
    D.toggle.classList.remove("active");
    drawerState.open = false;
    saveDrawerState();
  }
  function toggleDrawer() {
    if (D.drawer.classList.contains("hidden")) openDrawer();
    else closeDrawer();
  }

  // ---- form: read state into a draft dict matching the wire PROPOSE body shape ----
  function readDraft() {
    const draft = {
      name: (D.name.value || "").trim().toUpperCase(),
      path: (D.path.value || "").trim(),
      description: D.description.value.trim(),
      semantic: {
        intent: D.intent.value.trim(),
        actor: (document.querySelector('input[name="cf-actor"]:checked') || {}).value || "agent",
        outcome: D.outcome.value.trim(),
        capability: D.capability.value || null,
        impact: getActiveImpact(),
        confidence: D.confidence.value === "" || D.confidence.value === "0"
          ? null : Number(D.confidence.value),
        is_idempotent: D.idempotent.checked,
      },
      required_params: readParamRows("required_params"),
      optional_params: readParamRows("optional_params"),
      namespace: D.namespace.value.trim(),
      error_codes: getActiveCodes(),
    };
    if (!draft.path) delete draft.path;
    return draft;
  }

  function getActiveImpact() {
    const btn = document.querySelector(".impact-btn.active");
    return btn ? btn.dataset.impact : null;
  }
  function getActiveCodes() {
    return $$("#cf-error-codes .chip.active").map((c) => Number(c.dataset.code));
  }
  function readParamRows(which) {
    const tbl = document.querySelector(`.compose-params[data-params="${which}"] tbody`);
    if (!tbl) return [];
    const rows = [];
    Array.from(tbl.querySelectorAll("tr")).forEach((tr) => {
      const name = tr.querySelector('input[data-pf="name"]').value.trim();
      const type = tr.querySelector('select[data-pf="type"]').value;
      const desc = tr.querySelector('input[data-pf="description"]').value.trim();
      const schemaRaw = tr.querySelector('input[data-pf="schema"]').value.trim();
      if (!name && !type && !desc) return; // skip blank rows
      const row = { name, type, description: desc };
      if (schemaRaw) {
        try { row.schema = JSON.parse(schemaRaw); }
        catch (e) { row.schema = { _invalid: schemaRaw }; }
      } else if (type === "object" || type === "array") {
        // sentinel so validate_partial flags the missing schema
        row.schema = null;
      }
      rows.push(row);
    });
    return rows;
  }
  // ---- form: write a draft dict back into the DOM (used when loading library) ----
  function writeDraft(draft) {
    draft = draft || {};
    D.name.value = draft.name || "";
    D.path.value = draft.path || "";
    D.description.value = draft.description || "";
    const sb = draft.semantic || {};
    D.intent.value = sb.intent || "";
    D.outcome.value = sb.outcome || "";
    D.capability.value = sb.capability || "";
    const actor = sb.actor || "agent";
    const radio = document.querySelector(`input[name="cf-actor"][value="${actor}"]`);
    if (radio) radio.checked = true;
    // Back-compat: persisted drafts may still carry the pre-§4 keys
    // ``impact_tier`` / ``confidence_guidance``. Accept either shape
    // so older library entries keep loading.
    setActiveImpact(sb.impact || sb.impact_tier || null);
    const cgRaw = sb.confidence != null ? sb.confidence : sb.confidence_guidance;
    const cg = cgRaw == null ? 0 : Number(cgRaw);
    D.confidence.value = String(cg);
    D.confidenceVal.textContent = cg ? cg.toFixed(2) : "—";
    D.idempotent.checked = !!sb.is_idempotent;
    D.namespace.value = draft.namespace || "";
    setActiveCodes(Array.isArray(draft.error_codes) && draft.error_codes.length
      ? draft.error_codes : [400, 405, 422]);
    writeParamRows("required_params", draft.required_params || []);
    writeParamRows("optional_params", draft.optional_params || []);
  }
  function setActiveImpact(tier) {
    $$(".impact-btn").forEach((b) => {
      b.classList.toggle("active", b.dataset.impact === tier);
    });
  }
  function setActiveCodes(codes) {
    const set = new Set(codes.map(Number));
    $$("#cf-error-codes .chip").forEach((c) => {
      c.classList.toggle("active", set.has(Number(c.dataset.code)));
    });
  }
  function writeParamRows(which, rows) {
    const tbody = document.querySelector(`.compose-params[data-params="${which}"] tbody`);
    if (!tbody) return;
    tbody.innerHTML = "";
    rows.forEach((r) => addParamRow(which, r));
  }
  function addParamRow(which, row) {
    row = row || {};
    const tbody = document.querySelector(`.compose-params[data-params="${which}"] tbody`);
    if (!tbody) return;
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td><input data-pf="name" type="text" value="${escapeHtml(row.name || "")}" placeholder="lower_snake" /></td>` +
      `<td><select data-pf="type">${
        ["string", "integer", "number", "boolean", "object", "array"].map(
          (t) => `<option value="${t}"${row.type === t ? " selected" : ""}>${t}</option>`
        ).join("")
      }</select></td>` +
      `<td><input data-pf="description" type="text" value="${escapeHtml(row.description || "")}" placeholder="what this parameter is for" /></td>` +
      `<td><input data-pf="schema" type="text" value="${escapeHtml(row.schema ? JSON.stringify(row.schema) : "")}" placeholder='{"type":"object"}' /></td>` +
      `<td><button type="button" class="param-row-del" title="Remove">×</button></td>`;
    tr.querySelector(".param-row-del").addEventListener("click", () => {
      tr.remove(); onFormChange();
    });
    tr.querySelectorAll("input, select").forEach((el) => {
      el.addEventListener("input", () => onFormChange());
      el.addEventListener("blur", () => onFormChange());
    });
    tbody.appendChild(tr);
  }
  // ---- validation + UI feedback ----
  async function runValidation() {
    if (!window.pywebview || !window.pywebview.api) return;
    const draft = readDraft();
    let result;
    try {
      result = await window.pywebview.api.validate_compose(draft);
    } catch (e) { return; }
    renderFeedback(result);
    renderSectionIndicators(result.completion || {});
    renderWarnings(result.warnings || {});
    updateSubmitState(result);
  }
  function renderFeedback(result) {
    const errs = result.errors || {};
    const warns = result.warnings || {};
    // Clear all first.
    $$(".compose-feedback").forEach((el) => {
      el.textContent = "";
      el.className = "compose-feedback";
    });
    $$(".compose-field input.error, .compose-field input.warn, .compose-field textarea.error, .compose-field textarea.warn, .compose-field select.error, .compose-field select.warn")
      .forEach((el) => el.classList.remove("error", "warn"));
    Object.keys(errs).forEach((field) => {
      const fb = document.querySelector(`.compose-feedback[data-feedback="${cssEscape(field)}"]`);
      const input = document.querySelector(`[data-field="${cssEscape(field)}"]`);
      if (input) input.classList.add("error");
      if (fb) {
        fb.className = "compose-feedback error";
        renderFieldFeedback(fb, errs[field], field, "error");
      }
    });
    Object.keys(warns).forEach((field) => {
      if (errs[field]) return; // error wins
      const fb = document.querySelector(`.compose-feedback[data-feedback="${cssEscape(field)}"]`);
      const input = document.querySelector(`[data-field="${cssEscape(field)}"]`);
      if (input) input.classList.add("warn");
      if (fb) {
        fb.className = "compose-feedback warn";
        renderFieldFeedback(fb, warns[field], field, "warn");
      }
    });
    // Live "ok" feedback for the name field once it passes the
    // catalog check.
    if (!errs.name && !warns.name && D.name.value.trim().length >= 3) {
      const fb = document.querySelector('.compose-feedback[data-feedback="name"]');
      if (fb) {
        fb.className = "compose-feedback ok";
        fb.textContent = "✓ Verb is in the AGTP catalog.";
      }
    }
    // Same affirmation for the path field once it passes the path
    // grammar.
    if (!errs.path && !warns.path && (D.path.value || "").trim()) {
      const fb = document.querySelector('.compose-feedback[data-feedback="path"]');
      if (fb) {
        fb.className = "compose-feedback ok";
        fb.textContent = "✓ Path satisfies AGTP path grammar.";
      }
    }
    lastSuggestions = (result && result.suggestions) || {};
  }
  let lastSuggestions = {};
  function renderFieldFeedback(fb, message, field, level) {
    const text = document.createElement("span");
    text.textContent = (level === "error" ? "✗ " : "⚠ ") + message;
    fb.appendChild(text);
    if (field === "name") {
      injectNameSuggestions(fb);
    }
  }
  function injectNameSuggestions(fb) {
    // Prefer catalog-driven suggestions surfaced by the bridge (they
    // come from core.methods.find_close_matches, which is the same
    // Levenshtein-based ranker the CLI uses). Fall back to a local
    // prefix scan over the verb catalog when the bridge didn't
    // attach any.
    const typed = (D.name.value || "").trim().toUpperCase();
    let matches = (lastSuggestions && lastSuggestions.name) || [];
    if (!matches.length && verbCatalog.length && typed.length >= 2) {
      matches = verbCatalog
        .map((c) => c.name)
        .filter((n) => n !== typed && n.startsWith(typed.slice(0, 3)))
        .slice(0, 5);
    }
    if (!matches.length) return;
    const top = matches.slice(0, 3);
    fb.appendChild(document.createTextNode(" "));
    top.forEach((m) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "compose-feedback-suggest";
      btn.textContent = m;
      btn.addEventListener("click", () => {
        D.name.value = m;
        D.name.dispatchEvent(new Event("input"));
        D.name.focus();
      });
      fb.appendChild(btn);
    });
    if (matches.length > 3) {
      const more = document.createElement("button");
      more.type = "button";
      more.className = "compose-feedback-show-all";
      more.textContent = `Show all ${matches.length} matches →`;
      more.addEventListener("click", () => showCatalogPanel(fb, matches));
      fb.appendChild(more);
    }
  }
  function showCatalogPanel(fb, allMatches) {
    let panel = fb.parentElement.querySelector(".compose-catalog-panel");
    if (panel) { panel.remove(); return; }
    panel = document.createElement("div");
    panel.className = "compose-catalog-panel";
    panel.appendChild(makeText("From the AGTP verb catalog:", "compose-catalog-entry-name"));
    allMatches.forEach((m) => {
      const row = document.createElement("div");
      row.className = "compose-catalog-entry";
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "compose-feedback-suggest";
      btn.textContent = `Use ${m}`;
      btn.addEventListener("click", () => {
        D.name.value = m;
        D.name.dispatchEvent(new Event("input"));
        panel.remove();
      });
      row.appendChild(btn);
      panel.appendChild(row);
    });
    fb.parentElement.appendChild(panel);
  }
  function makeText(text, cls) {
    const s = document.createElement("span");
    s.className = cls || "";
    s.textContent = text;
    return s;
  }

  // ---- verb-name autocomplete dropdown ----
  //
  // Backed by the full AGTP catalog (currently 423 verbs). The
  // dropdown is anchored to the name input; it shows on focus or
  // when the user types, filters by prefix, and highlights the
  // primary category as a tag on each row. Up/Down + Enter pick a
  // match; Escape dismisses.
  function autocompleteCandidates(typed) {
    const upper = (typed || "").toUpperCase();
    if (!verbCatalog.length) return [];
    const exact = [];
    const prefix = [];
    const contains = [];
    verbCatalog.forEach((c) => {
      const n = c.name;
      if (!upper) { prefix.push(c); return; }
      if (n === upper) exact.push(c);
      else if (n.startsWith(upper)) prefix.push(c);
      else if (n.indexOf(upper) >= 0) contains.push(c);
    });
    return exact.concat(prefix, contains).slice(0, 24);
  }
  function showAutocomplete() {
    if (!D.nameAuto) return;
    const typed = (D.name.value || "").trim().toUpperCase();
    const candidates = autocompleteCandidates(typed);
    if (!candidates.length) {
      hideAutocomplete();
      return;
    }
    D.nameAuto.innerHTML = "";
    autocompleteIndex = -1;
    candidates.forEach((c, idx) => {
      const row = document.createElement("div");
      row.className = "compose-autocomplete-row";
      // Phase-6: deprecated verbs render with the .deprecated
      // marker (italics + tooltip explaining successor and
      // removed_in). The verb is still pickable; the visual
      // treatment is the migration prompt.
      if (c.deprecated) {
        row.classList.add("deprecated");
        const succ = c.successor ? ` Successor: ${c.successor}.` : "";
        const removed = c.removed_in ? ` Removed in: ${c.removed_in}.` : "";
        row.title = `Deprecated in ${c.deprecated_in || "?"}.${succ}${removed}`;
      }
      row.dataset.value = c.name;
      const cat = (c.categories || [])[0] || "verb";
      const depMark = c.deprecated ? `<span class="ac-deprecated">deprecated</span>` : "";
      row.innerHTML =
        `<span class="ac-name">${escapeHtml(c.name)}</span>` +
        `<span class="ac-cat">${escapeHtml(cat)}</span>` +
        depMark +
        (c.description ? `<span class="ac-desc">${escapeHtml(c.description)}</span>` : "");
      row.addEventListener("mousedown", (e) => {
        // mousedown beats blur, so the focus stays put after pick
        e.preventDefault();
        pickAutocomplete(c.name);
      });
      row.addEventListener("mouseenter", () => {
        autocompleteIndex = idx;
        highlightAutocomplete();
      });
      D.nameAuto.appendChild(row);
    });
    D.nameAuto.classList.remove("hidden");
  }
  function hideAutocomplete() {
    if (D.nameAuto) {
      D.nameAuto.classList.add("hidden");
      D.nameAuto.innerHTML = "";
      autocompleteIndex = -1;
    }
  }
  function highlightAutocomplete() {
    if (!D.nameAuto) return;
    const rows = D.nameAuto.querySelectorAll(".compose-autocomplete-row");
    rows.forEach((r, i) => {
      r.classList.toggle("active", i === autocompleteIndex);
      if (i === autocompleteIndex && typeof r.scrollIntoView === "function") {
        r.scrollIntoView({ block: "nearest" });
      }
    });
  }
  function pickAutocomplete(value) {
    if (!value) return;
    D.name.value = value;
    hideAutocomplete();
    D.name.dispatchEvent(new Event("input"));
    D.name.focus();
  }
  function renderSectionIndicators(completion) {
    Object.keys(completion).forEach((section) => {
      const ind = document.querySelector(`.compose-section-indicator[data-indicator="${section}"]`);
      if (!ind) return;
      const status = completion[section];
      ind.className = `compose-section-indicator ${status}`;
      const glyph =
        status === "complete" ? "●" :
        status === "partial"  ? "◐" :
                                "○";
      ind.textContent = glyph;
    });
  }
  function renderWarnings(warnings) {
    const items = Object.keys(warnings);
    if (!items.length) {
      D.warnings.classList.add("hidden");
      D.warnings.innerHTML = "";
      return;
    }
    D.warnings.classList.remove("hidden");
    D.warnings.innerHTML = "";
    items.forEach((field) => {
      const item = document.createElement("div");
      item.className = "compose-warning-item";
      item.textContent = `⚠ ${warnings[field]}`;
      item.addEventListener("click", () => {
        const target = document.querySelector(`[data-field="${cssEscape(field)}"]`);
        if (target && typeof target.scrollIntoView === "function") {
          target.scrollIntoView({ behavior: "smooth", block: "center" });
          if (typeof target.focus === "function") target.focus();
        }
      });
      D.warnings.appendChild(item);
    });
  }
  function cssEscape(s) {
    if (window.CSS && CSS.escape) return CSS.escape(s);
    return String(s).replace(/[^a-zA-Z0-9_-]/g, (c) => `\\${c}`);
  }
  function updateSubmitState(result) {
    const hasUri = !!currentUri();
    const valid = result && result.valid;
    D.submit.disabled = readOnly || !valid || !hasUri;
    D.submitTarget.textContent = hasUri
      ? `Will submit to: ${currentUri()}`
      : "Load a server URL to enable submission.";
  }

  // ---- YAML preview ----
  function renderYaml() {
    const draft = readDraft();
    D.yamlPre.textContent = toYaml(draft);
  }
  function toYaml(obj, indent = 0) {
    // Minimal hand-rolled YAML emitter — enough for spec dicts.
    const pad = "  ".repeat(indent);
    if (obj === null || obj === undefined) return "null";
    if (typeof obj === "boolean") return obj ? "true" : "false";
    if (typeof obj === "number") return String(obj);
    if (typeof obj === "string") {
      if (/^[\w\-./]+$/.test(obj) && obj.length < 120) return obj;
      return JSON.stringify(obj);
    }
    if (Array.isArray(obj)) {
      if (!obj.length) return "[]";
      return obj.map((v) =>
        `\n${pad}- ${toYamlValue(v, indent + 1)}`
      ).join("");
    }
    const keys = Object.keys(obj);
    if (!keys.length) return "{}";
    return keys.map((k) => {
      const val = obj[k];
      if (val && typeof val === "object" && (Array.isArray(val) ? val.length : Object.keys(val).length)) {
        return `${pad}${k}:${toYaml(val, indent + 1)}`;
      }
      return `${pad}${k}: ${toYamlValue(val, indent + 1)}`;
    }).join("\n");
  }
  function toYamlValue(v, indent) {
    if (v === null || v === undefined) return "null";
    if (typeof v === "object") {
      if (Array.isArray(v) && !v.length) return "[]";
      if (!Array.isArray(v) && !Object.keys(v).length) return "{}";
      // For inline-prefix lists/objects, emit on the next lines.
      const inner = toYaml(v, indent);
      // Strip first line indent so list-marker '- ' and key prefix line up.
      return inner.startsWith("\n") ? inner.replace(/^\n/, "") : inner;
    }
    if (typeof v === "boolean") return v ? "true" : "false";
    if (typeof v === "number") return String(v);
    if (typeof v === "string") {
      if (/^[\w\-./]+$/.test(v) && v.length < 120) return v;
      return JSON.stringify(v);
    }
    return JSON.stringify(v);
  }

  // ---- onFormChange — single entry point for reactivity ----
  function onFormChange(opts) {
    opts = opts || {};
    const ms = opts.immediate ? 0 : 200;
    debounce("validate", runValidation, ms);
    debounce("yaml", renderYaml, ms);
  }

  // ---- library rendering ----
  function renderLibrary() {
    D.libList.innerHTML = "";
    if (!library.entries.length) {
      D.libEmpty.classList.remove("hidden");
      return;
    }
    D.libEmpty.classList.add("hidden");
    library.entries.forEach((e) => {
      const li = document.createElement("li");
      li.className = "lib-card" + (e.id === activeLibId ? " active" : "");
      li.innerHTML =
        `<div class="lib-card-row">` +
          `<span class="lib-name">${escapeHtml(e.name || "(unnamed)")}</span>` +
          `<span class="lib-status-dot ${e.status || "draft"}" title="${e.status || "draft"}"></span>` +
        `</div>` +
        `<div class="lib-meta">${escapeHtml(e.status || "draft")} · ${escapeHtml(relTime(e.saved_at))}</div>` +
        (e.submitted_to ? `<div class="lib-server">${escapeHtml(serverFromUri(e.submitted_to))}</div>` : "");
      li.addEventListener("click", () => loadLibraryEntry(e.id));
      D.libList.appendChild(li);
    });
  }
  function serverFromUri(uri) {
    if (!uri) return "";
    const m = uri.match(/^agtp:\/\/(?:[0-9a-f]+@)?(.+)$/i);
    return m ? m[1] : uri;
  }
  function loadLibraryEntry(id) {
    const entry = library.entries.find((e) => e.id === id);
    if (!entry) return;
    activeLibId = id;
    readOnly = entry.status === "submitted" || entry.status === "accepted";
    writeDraft(entry.spec || {});
    D.composeEmpty.classList.add("hidden");
    D.composeActive.classList.remove("hidden");
    setReadOnly(readOnly, entry);
    renderLibrary();
    onFormChange({ immediate: true });
  }
  function setReadOnly(yes, entry) {
    const inputs = D.composeActive.querySelectorAll(
      "input, textarea, select, button.param-row-del, button.compose-params-add, button.impact-btn, button.chip"
    );
    inputs.forEach((el) => {
      if (el.type === "checkbox" || el.type === "radio") {
        el.disabled = yes;
      } else if (el.tagName === "BUTTON") {
        el.disabled = yes;
      } else {
        el.readOnly = yes;
      }
    });
    if (yes && entry) {
      D.composeRO.classList.remove("hidden");
      const when = entry.submitted_at ? new Date(entry.submitted_at).toLocaleString() : "(unknown date)";
      D.composeRO.innerHTML =
        `<span>This method was submitted on ${escapeHtml(when)}.</span>` +
        `<button type="button" id="cf-edit-as-new">Edit as new draft</button>`;
      D.composeRO.querySelector("#cf-edit-as-new").addEventListener("click", () => {
        forkAsNewDraft(entry);
      });
    } else {
      D.composeRO.classList.add("hidden");
      D.composeRO.innerHTML = "";
    }
    D.submit.disabled = yes;
    D.saveFile.disabled = false;     // saving is always allowed
    D.addLibrary.disabled = false;
  }
  function forkAsNewDraft(entry) {
    const spec = JSON.parse(JSON.stringify(entry.spec || {}));
    const id = newLibId(spec.name);
    const newEntry = {
      id,
      name: spec.name,
      spec,
      status: "draft",
      saved_at: nowIso(),
    };
    library.entries.unshift(newEntry);
    saveLibrary();
    activeLibId = id;
    readOnly = false;
    writeDraft(spec);
    setReadOnly(false, null);
    renderLibrary();
    onFormChange({ immediate: true });
    showToast("Forked as new draft.");
  }
  function newDraft() {
    activeLibId = null;
    readOnly = false;
    setReadOnly(false, null);
    writeDraft({
      name: "",
      semantic: { actor: "agent", is_idempotent: false },
      error_codes: [400, 405, 422],
    });
    D.response.classList.add("hidden");
    D.response.innerHTML = "";
    D.composeEmpty.classList.add("hidden");
    D.composeActive.classList.remove("hidden");
    renderLibrary();
    onFormChange({ immediate: true });
    setTimeout(() => D.name.focus(), 0);
  }
  function addToLibrary() {
    const draft = readDraft();
    const id = activeLibId || newLibId(draft.name);
    const existing = library.entries.find((e) => e.id === id);
    const entry = existing || { id, status: "draft", saved_at: nowIso() };
    entry.name = draft.name || "(unnamed)";
    entry.spec = draft;
    entry.saved_at = nowIso();
    if (!existing) library.entries.unshift(entry);
    library.entries = library.entries.slice(0, LIBRARY_LIMIT);
    activeLibId = id;
    saveLibrary();
    renderLibrary();
    showToast("Saved to library.");
  }

  // ---- submission flow ----
  async function submitPropose() {
    const uri = currentUri();
    if (!uri) {
      showToast("Load a server URL first.");
      return;
    }
    const draft = readDraft();
    D.submit.disabled = true;
    D.submit.textContent = "Submitting…";
    let result;
    try {
      result = await window.pywebview.api.invoke(uri, "PROPOSE", draft);
    } catch (e) {
      result = { ok: false, error: String(e) };
    }
    D.submit.textContent = "Submit PROPOSE";
    handleProposeResponse(result, draft);
  }
  function handleProposeResponse(result, draft) {
    D.response.classList.remove("hidden");
    D.response.className = "compose-response";
    D.response.innerHTML = "";
    if (!result || !result.ok) {
      D.response.classList.add("refused");
      D.response.innerHTML = `<div class="compose-response-title">✗ Bridge error</div>` +
        `<div>${escapeHtml(result && result.error || "(unknown)")}</div>`;
      D.submit.disabled = false;
      return;
    }
    const code = result.status_code;
    const payload = parseBody(result.body);
    if (code === 200) {
      const synth = (payload && payload.synthesis) || {};
      D.response.classList.add("accepted");
      const id = synth.synthesis_id || "(unknown)";
      const target = synth.target_method || "";
      const mapping = synth.parameter_mapping || {};
      const proposedName = (draft && draft.name) || target || "(unknown)";
      const mappingHtml = Object.keys(mapping).map((p) =>
        `<dd>${escapeHtml(target)} → ${escapeHtml(mapping[p])} (from ${escapeHtml(p)})</dd>`
      ).join("");
      D.response.innerHTML =
        `<div class="compose-response-title">✓ Server accepted. Synthesis instantiated.</div>` +
        `<dl>` +
          `<dt>Synthesis ID</dt><dd>${escapeHtml(id)}</dd>` +
          (target ? `<dt>Underlying method</dt><dd>${escapeHtml(target)}</dd>` : "") +
          mappingHtml +
        `</dl>` +
        `<div class="compose-response-actions">` +
          `<button type="button" id="cf-go-invoke">Invoke this synthesis</button>` +
        `</div>`;
      const btn = D.response.querySelector("#cf-go-invoke");
      if (btn) btn.addEventListener("click", () => {
        // Replaces the broken v1 toast handler:
        //   1. Record the synthesis under tab.syntheses so the
        //      Try-It form's invocation path picks up the
        //      Synthesis-Id header automatically.
        //   2. Stash a minimal spec on tab.activeSyntheses keyed by
        //      synthesis_id so the manifest-side renderer can build
        //      a Try-It form for the proposed method (which is not
        //      in the server's REGISTRY when the plan is recipe-
        //      based).
        //   3. Close the drawer.
        //   4. Re-render the manifest's Methods section so the
        //      "Active Syntheses" pill appears with the new entry.
        const tab = (typeof getActive === "function") ? getActive() : null;
        if (tab) {
          tab.syntheses = tab.syntheses || {};
          tab.syntheses[proposedName] = id;
          tab.activeSyntheses = tab.activeSyntheses || {};
          tab.activeSyntheses[id] = {
            method: proposedName,
            spec: {
              name: proposedName,
              description: (draft && draft.description) ||
                           (draft && draft.intent) || "",
              required_params: ((draft && draft.required_params) || [])
                .map((p) => p.name),
              optional_params: ((draft && draft.optional_params) || [])
                .map((p) => p.name),
              category: "synthesis",
              source: "agtp/1.0",
            },
            plan: synth.plan || null,
            target_method: target,
            created_at: Date.now(),
          };
          if (typeof renderActiveSyntheses === "function") {
            renderActiveSyntheses(tab);
          }
        }
        closeDrawer();
        showToast(
          `Synthesis ${id.slice(0, 16)}… ready — try invoking ${proposedName}.`
        );
      });
      markLibraryStatus("accepted", { synthesis_id: id });
    } else if (code === 422 && payload && payload.counter_proposal) {
      // PROPOSE counter-proposal: 422 with a counter_proposal body.
      const counter = payload.counter_proposal || {};
      D.response.classList.add("countered");
      const sName = counter.name || "(unknown)";
      D.response.innerHTML =
        `<div class="compose-response-title">↻ Server proposed an alternative.</div>` +
        `<dl>` +
          `<dt>Suggests</dt><dd>${escapeHtml(sName)} instead of ${escapeHtml(draft.name || "")}</dd>` +
          (counter.description ? `<dt>Reason</dt><dd>${escapeHtml(counter.description)}</dd>` : "") +
        `</dl>` +
        renderCounterCard(draft, counter) +
        `<div class="compose-response-actions">` +
          `<button type="button" id="cf-counter-accept">Accept counter and re-submit</button>` +
          `<button type="button" id="cf-counter-modify" class="secondary">Modify and re-submit</button>` +
          `<button type="button" id="cf-counter-decline" class="secondary">Decline</button>` +
        `</div>`;
      D.response.querySelector("#cf-counter-accept").addEventListener("click", () => {
        D.name.value = sName;
        D.name.dispatchEvent(new Event("input"));
        submitPropose();
      });
      D.response.querySelector("#cf-counter-modify").addEventListener("click", () => {
        D.response.classList.add("hidden");
      });
      D.response.querySelector("#cf-counter-decline").addEventListener("click", () => {
        D.response.classList.add("hidden");
      });
      markLibraryStatus("countered", { counter_proposal: counter });
    } else if (code === 422
               && payload && payload.error
               && payload.error.code === "negotiation-refused") {
      // PROPOSE refusal: 422 with structured negotiation-refused body.
      const err = payload.error || {};
      D.response.classList.add("refused");
      D.response.innerHTML =
        `<div class="compose-response-title">✗ Server refused negotiation.</div>` +
        `<dl>` +
          `<dt>Reason</dt><dd>${escapeHtml(err.reason || "(unknown)")}</dd>` +
          (err.explanation ? `<dt>Detail</dt><dd>${escapeHtml(err.explanation)}</dd>` : "") +
        `</dl>` +
        `<div>Try a different server, or modify and re-submit.</div>`;
      markLibraryStatus("refused");
    } else {
      D.response.classList.add("refused");
      D.response.innerHTML =
        `<div class="compose-response-title">${escapeHtml(`${code} ${result.status_text || ""}`)}</div>` +
        `<pre>${escapeHtml(result.body || "")}</pre>`;
    }
    D.submit.disabled = false;
    onFormChange({ immediate: true });
  }
  function parseBody(body) {
    if (!body) return null;
    try { return JSON.parse(body); } catch (e) { return null; }
  }
  function renderCounterCard(orig, counter) {
    const diffs = [];
    if (counter.name && counter.name !== orig.name) {
      diffs.push(`Name: ${escapeHtml(orig.name)} → <span class="counter-diff">${escapeHtml(counter.name)}</span>`);
    }
    if (Array.isArray(counter.required_params)) {
      const ourNames = (orig.required_params || []).map((p) => p.name).sort();
      const theirNames = counter.required_params.map((p) => p.name).sort();
      if (JSON.stringify(ourNames) === JSON.stringify(theirNames)) {
        diffs.push(`Required params: identical (${ourNames.join(", ") || "(none)"})`);
      } else {
        diffs.push(`Required params: <span class="counter-diff">${escapeHtml(ourNames.join(","))} → ${escapeHtml(theirNames.join(","))}</span>`);
      }
    }
    if (!diffs.length) return "";
    return `<div class="counter-card">Differences:<br>${diffs.map((d) => `· ${d}`).join("<br>")}</div>`;
  }
  function markLibraryStatus(status, extra) {
    if (!activeLibId) {
      // Auto-save submitted drafts into the library so they don't go missing.
      addToLibrary();
    }
    const entry = library.entries.find((e) => e.id === activeLibId);
    if (!entry) return;
    entry.status = status;
    entry.submitted_to = currentUri();
    entry.submitted_at = nowIso();
    Object.assign(entry, extra || {});
    saveLibrary();
    renderLibrary();
  }

  // ---- save-as-file flow ----
  async function saveAsFile() {
    const draft = readDraft();
    const filename = (draft.name || "method").toLowerCase() + ".method.yaml";
    let path = "";
    try {
      path = await window.pywebview.api.save_method_yaml(draft, filename);
    } catch (e) { /* ignore */ }
    if (path) {
      showToast(`Saved to ${path}`);
    } else {
      showToast("Save cancelled or pyyaml not installed.");
    }
  }

  async function exportLibrary() {
    let path = "";
    try {
      path = await window.pywebview.api.export_library(library);
    } catch (e) { /* ignore */ }
    if (path) showToast(`Exported to ${path}`);
  }
  async function importLibrary() {
    let data = {};
    try {
      data = await window.pywebview.api.import_library();
    } catch (e) { /* ignore */ }
    if (!data || !Array.isArray(data.entries)) {
      showToast("Import cancelled or invalid file.");
      return;
    }
    library = { version: 1, entries: data.entries.slice(0, LIBRARY_LIMIT) };
    saveLibrary();
    renderLibrary();
    showToast(`Imported ${library.entries.length} entries.`);
  }
  function clearLibrary() {
    if (!confirm("Clear all saved methods? This cannot be undone.")) return;
    library = { version: 1, entries: [] };
    saveLibrary();
    activeLibId = null;
    renderLibrary();
    D.composeEmpty.classList.remove("hidden");
    D.composeActive.classList.add("hidden");
  }

  // ---- wire everything up ----
  function wire() {
    D.toggle.addEventListener("click", toggleDrawer);
    D.closeBtn.addEventListener("click", closeDrawer);
    D.libNew.addEventListener("click", newDraft);
    D.emptyNew.addEventListener("click", newDraft);

    // Library menu
    D.libMenuBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      D.libMenu.classList.toggle("hidden");
    });
    document.addEventListener("click", (e) => {
      if (!D.libMenu.classList.contains("hidden") && !D.libMenu.contains(e.target) && e.target !== D.libMenuBtn) {
        D.libMenu.classList.add("hidden");
      }
    });
    D.libMenu.querySelectorAll("[data-lib-action]").forEach((btn) => {
      btn.addEventListener("click", () => {
        D.libMenu.classList.add("hidden");
        const action = btn.dataset.libAction;
        if (action === "export") exportLibrary();
        else if (action === "import") importLibrary();
        else if (action === "clear") clearLibrary();
      });
    });

    // Section collapse toggles
    $$(".compose-section-head").forEach((head) => {
      head.addEventListener("click", () => {
        const sec = head.closest(".compose-section");
        if (sec) sec.classList.toggle("collapsed");
      });
    });

    // Continuous validation on the name field — fires on every
    // keystroke after the second character, per spec.
    D.name.addEventListener("input", () => {
      // Force uppercase as the user types (the catalog is uppercase).
      const cur = D.name.value || "";
      const up = cur.toUpperCase();
      if (cur !== up) {
        D.name.value = up;
      }
      if (D.name.value.trim().length >= 2) {
        debounce("validate", runValidation, 60);
        showAutocomplete();
      } else {
        hideAutocomplete();
      }
      debounce("yaml", renderYaml, 120);
    });
    D.name.addEventListener("focus", () => {
      if ((D.name.value || "").trim().length >= 2) showAutocomplete();
    });
    D.name.addEventListener("blur", () => {
      // Defer hide so a click on a row can register first.
      setTimeout(hideAutocomplete, 120);
    });
    D.name.addEventListener("keydown", (e) => {
      if (D.nameAuto.classList.contains("hidden")) return;
      const rows = D.nameAuto.querySelectorAll(".compose-autocomplete-row");
      if (e.key === "ArrowDown") {
        e.preventDefault();
        autocompleteIndex = Math.min(autocompleteIndex + 1, rows.length - 1);
        highlightAutocomplete();
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        autocompleteIndex = Math.max(autocompleteIndex - 1, 0);
        highlightAutocomplete();
      } else if (e.key === "Enter") {
        if (autocompleteIndex >= 0 && rows[autocompleteIndex]) {
          e.preventDefault();
          pickAutocomplete(rows[autocompleteIndex].dataset.value);
        }
      } else if (e.key === "Escape") {
        e.preventDefault();
        hideAutocomplete();
      }
    });

    // Path field: validate on input + blur.
    if (D.path) {
      D.path.addEventListener("input", () => onFormChange());
      D.path.addEventListener("blur", () => onFormChange({ immediate: true }));
    }

    // On-blur validation for everything else.
    [D.description, D.intent, D.outcome, D.namespace].forEach((el) => {
      el.addEventListener("input", () => onFormChange());
      el.addEventListener("blur", () => onFormChange({ immediate: true }));
    });
    D.capability.addEventListener("change", () => onFormChange({ immediate: true }));
    D.idempotent.addEventListener("change", () => onFormChange({ immediate: true }));
    document.querySelectorAll('input[name="cf-actor"]').forEach((r) =>
      r.addEventListener("change", () => onFormChange({ immediate: true })));

    // Confidence slider
    D.confidence.addEventListener("input", () => {
      const v = Number(D.confidence.value);
      D.confidenceVal.textContent = v ? v.toFixed(2) : "—";
      onFormChange();
    });

    // Impact buttons + auto-snap to 0.85 when irreversible chosen low.
    $$(".impact-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const tier = btn.dataset.impact;
        const wasActive = btn.classList.contains("active");
        $$(".impact-btn").forEach((b) => b.classList.remove("active"));
        if (!wasActive) btn.classList.add("active");
        if (!wasActive && tier === "irreversible") {
          if (Number(D.confidence.value) < 0.85) {
            D.confidence.value = "0.85";
            D.confidenceVal.textContent = "0.85";
            showToast("AGTP guidance: confidence ≥ 0.85 for irreversible methods.");
          }
        }
        onFormChange({ immediate: true });
      });
    });

    // Error code chips
    $$("#cf-error-codes .chip").forEach((c) => {
      c.addEventListener("click", () => {
        c.classList.toggle("active");
        onFormChange({ immediate: true });
      });
    });

    // Parameter table "+ Add" buttons
    $$(".compose-params-add").forEach((btn) => {
      btn.addEventListener("click", () => {
        addParamRow(btn.dataset.add);
        onFormChange();
      });
    });

    // Submit / Save / Add to library
    D.submit.addEventListener("click", submitPropose);
    D.saveFile.addEventListener("click", saveAsFile);
    D.addLibrary.addEventListener("click", addToLibrary);

    // YAML preview
    D.yamlCopy.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(D.yamlPre.textContent); showToast("Copied YAML."); }
      catch (e) { showToast("Clipboard unavailable."); }
    });
    D.yamlToggle.addEventListener("click", () => {
      yamlCollapsed = !yamlCollapsed;
      D.yamlPane.classList.toggle("collapsed", yamlCollapsed);
      D.composePane.classList.toggle("yaml-collapsed", yamlCollapsed);
    });

    // Resize handle. Drawer is right-anchored, so dragging the
    // handle leftward grows the drawer (delta = startX - currentX).
    let resizeStartX = 0, resizeStartW = 0;
    D.resize.addEventListener("mousedown", (e) => {
      e.preventDefault();
      resizeStartX = e.clientX;
      resizeStartW = D.drawer.getBoundingClientRect().width;
      const onMove = (ev) => {
        const dx = resizeStartX - ev.clientX;
        const minW = 420;
        const maxW = Math.max(minW, window.innerWidth * 0.9);
        const w = Math.max(minW, Math.min(maxW, resizeStartW + dx));
        D.drawer.style.width = w + "px";
        drawerState.width = w;
      };
      const onUp = () => {
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        saveDrawerState();
      };
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    });

    // Keyboard shortcuts
    document.addEventListener("keydown", (e) => {
      if (e.key === "F12") {
        e.preventDefault();
        toggleDrawer();
        return;
      }
      if ((e.ctrlKey || e.metaKey) && e.shiftKey && (e.key === "I" || e.key === "i")) {
        e.preventDefault();
        toggleDrawer();
        return;
      }
      if (e.key === "Escape" && !D.drawer.classList.contains("hidden")) {
        if (D.drawer.contains(document.activeElement) || document.activeElement === document.body) {
          closeDrawer();
        }
      }
    });

    // Re-validate when the active main tab changes (URL changes).
    if (els.uri) {
      els.uri.addEventListener("input", () => onFormChange());
    }
  }

  async function bootstrap() {
    await whenApiReady();
    try {
      verbCatalog = await window.pywebview.api.get_verb_catalog();
    } catch (e) { verbCatalog = []; }
    wire();
    renderLibrary();
    if (drawerState.open) {
      openDrawer();
      // Fall through: empty state visible until user picks New or a card.
    }
    // Pre-render YAML so the pane isn't blank on first open.
    renderYaml();
  }

  bootstrap();
})();
