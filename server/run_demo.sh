#!/usr/bin/env bash
# AGTP demo runner.
#
# Exercises the full 12-method set against two demo agents:
#   * Lauren (8 methods: 6 cognitive + CONFIRM + NOTIFY)
#   * Orchestrator (all 12 methods)
#
# Starts the registry (HTTP, dev mode), starts the agent server
# (plaintext, dev mode), registers both agents, then walks through
# twenty-nine scenarios covering every method plus the 405, 459, and 460 error
# paths.
#
# Output goes to server/transcripts/methods-demo.txt and a few sidecar
# files (registry log, server log, registry data store).
#
# Usage:
#   ./run_demo.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# We cd to the repo root so the core/server/client packages are on
# Python's default sys.path without needing PYTHONPATH. PYTHONPATH
# separator clashes with the drive letter on Windows + Git Bash.
#
# All paths handed to Python are kept relative to the repo root, since
# Git Bash returns posix-style paths from `pwd` (e.g. /x/agtp/server)
# and Python on Windows mis-interprets those as paths on the current
# drive.
cd "$REPO_ROOT"

AGENTS_DIR="server/agents"
TRANSCRIPT_DIR="server/transcripts"
REGISTRY_STORE="$TRANSCRIPT_DIR/registry_data.json"

# Pick whatever Python the host actually has on PATH. We probe each
# candidate by running --version, since on Windows the App Execution
# Alias for `python3` lives on PATH but errors out unless the user has
# installed Python from the Microsoft Store.
PY=""
for candidate in python3 python py; do
    if command -v "$candidate" >/dev/null 2>&1 \
        && "$candidate" --version >/dev/null 2>&1; then
        PY="$candidate"
        break
    fi
done
if [ -z "$PY" ]; then
    echo "error: no working python interpreter on PATH (tried python3, python, py)" >&2
    exit 1
fi

mkdir -p "$TRANSCRIPT_DIR"
TRANSCRIPT="$TRANSCRIPT_DIR/methods-demo.txt"
: > "$TRANSCRIPT"
: > "$TRANSCRIPT_DIR/registry.log"
: > "$TRANSCRIPT_DIR/server.log"

LAUREN_ID="d8dc6f0df55d66c7b30100db3cffbe383c5f814e6e58a08521fb7636c3bcc230"
ORCH_ID="9fe1dfc552a64c8bbec8dd2fe8cbe1a275f1a3405f7c5c20acca6453fd479709"
REGISTRY_URL="http://127.0.0.1:8080"

CLIENT="$PY -m client"
CLIENT_ARGS=(--registry "$REGISTRY_URL" --insecure --insecure-skip-verify)

run_scenario() {
    local n="$1"; shift
    local title="$1"; shift
    {
        echo
        echo "=================================================================="
        echo "SCENARIO $n  $title"
        echo "=================================================================="
        echo "\$ $*"
        "$@" 2>&1 || true
    } | tee -a "$TRANSCRIPT"
}

cleanup() {
    if [ -n "${REGISTRY_PID:-}" ] && kill -0 "$REGISTRY_PID" 2>/dev/null; then
        kill "$REGISTRY_PID" 2>/dev/null || true
        wait "$REGISTRY_PID" 2>/dev/null || true
    fi
    if [ -n "${SERVER_PID:-}" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
        kill "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT

{
    echo "=================================================================="
    echo "AGTP 12-Method Demo"
    echo "Run at:        $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "Lauren:        $LAUREN_ID"
    echo "Orchestrator:  $ORCH_ID"
    echo "=================================================================="
} | tee -a "$TRANSCRIPT"

# Reset registry state so each run is reproducible.
rm -f "$REGISTRY_STORE"

echo                                                                          | tee -a "$TRANSCRIPT"
echo "[runner] starting registry on $REGISTRY_URL"                            | tee -a "$TRANSCRIPT"
$PY -m registry 8080 \
    --host 127.0.0.1 \
    --store "$REGISTRY_STORE" \
    >> "$TRANSCRIPT_DIR/registry.log" 2>&1 &
REGISTRY_PID=$!
sleep 0.6

echo "[runner] registering Lauren and Orchestrator at 127.0.0.1:4480"         | tee -a "$TRANSCRIPT"
$PY -c "
from pathlib import Path
from registry.main import RegistryStore
store = RegistryStore(Path(r'$REGISTRY_STORE'))
store.register('$LAUREN_ID', '127.0.0.1', 4480)
store.register('$ORCH_ID',   '127.0.0.1', 4480)
print(f'[runner] registry now contains: {store.list_all()}')
" | tee -a "$TRANSCRIPT"

echo "[runner] starting agent server on agtp://127.0.0.1:4480 (plaintext)"    | tee -a "$TRANSCRIPT"
# Loopback bind defaults to plaintext, so --insecure is omitted here.
# Positional port matches the python -m http.server idiom.
# --config points the manifest identity at server/agtp-server.toml so the
# Server Manifest scenarios produce stable, demo-flavored output.
$PY -m server 4480 \
    --host 127.0.0.1 \
    --agents-dir "$AGENTS_DIR" \
    --config "$SCRIPT_DIR/agtp-server.toml" \
    --load-module server.examples.custom_methods \
    >> "$TRANSCRIPT_DIR/server.log" 2>&1 &
SERVER_PID=$!
sleep 0.6

# ---------------------------------------------------------------------------
# Scenarios.
# ---------------------------------------------------------------------------

run_scenario 1  "DESCRIBE Lauren (default method, JSON)" \
    $CLIENT "agtp://$LAUREN_ID" "${CLIENT_ARGS[@]}"

run_scenario 2  "DISCOVER methods on Lauren" \
    $CLIENT "agtp://$LAUREN_ID" DISCOVER \
    --param target=methods "${CLIENT_ARGS[@]}"

run_scenario 3  "QUERY Lauren with an intent" \
    $CLIENT "agtp://$LAUREN_ID" QUERY \
    --param "intent=what is the weather in San Francisco today" \
    --param scope=public "${CLIENT_ARGS[@]}"

run_scenario 4  "SUMMARIZE a sample input" \
    $CLIENT "agtp://$LAUREN_ID" SUMMARIZE \
    -d '{"source":"AGTP is a dedicated application-layer protocol for AI agent traffic. The reference implementation in this repo demonstrates canonical Agent IDs, registry lookup, and content-negotiated identity documents over a custom wire format on port 4480.","max_length":80}' \
    "${CLIENT_ARGS[@]}"

run_scenario 5  "PLAN a multi-step task" \
    $CLIENT "agtp://$LAUREN_ID" PLAN \
    --param "goal=draft a release note for AGTP v0.2" \
    "${CLIENT_ARGS[@]}"

run_scenario 6  "EXECUTE a stub plan on Lauren" \
    $CLIENT "agtp://$LAUREN_ID" EXECUTE \
    --param plan_id=plan-demo-001 \
    "${CLIENT_ARGS[@]}"

run_scenario 7  "NOTIFY Lauren" \
    $CLIENT "agtp://$LAUREN_ID" NOTIFY \
    --param event=demo.started \
    --param priority=normal \
    "${CLIENT_ARGS[@]}"

run_scenario 8  "CONFIRM a prior action on Lauren" \
    $CLIENT "agtp://$LAUREN_ID" CONFIRM \
    --param attestation_target=esc-fake-001 \
    --param decision=confirmed \
    "${CLIENT_ARGS[@]}"

run_scenario 9  "DELEGATE to Orchestrator" \
    $CLIENT "agtp://$ORCH_ID" DELEGATE \
    --param "task=run nightly summarization" \
    --param "sub_agent=$LAUREN_ID" \
    --param scope=read-only \
    "${CLIENT_ARGS[@]}"

run_scenario 10 "ESCALATE to Orchestrator" \
    $CLIENT "agtp://$ORCH_ID" ESCALATE \
    --param "decision_point=approve high-cost action" \
    --param target_authority=human \
    "${CLIENT_ARGS[@]}"

run_scenario 11 "SUSPEND on Orchestrator (returns resumption nonce)" \
    $CLIENT "agtp://$ORCH_ID" SUSPEND \
    --param reason=demo-pause \
    --param ttl_seconds=600 \
    "${CLIENT_ARGS[@]}"

run_scenario 12 "PROPOSE to Orchestrator (out-of-scope name, returns 422 negotiation-refused)" \
    $CLIENT "agtp://$ORCH_ID" PROPOSE \
    -d '{"name":"ZBLARGON","parameters":{"input":"string"},"outcome":"object","description":"verb unrelated to anything this server hosts; expected refusal"}' \
    "${CLIENT_ARGS[@]}"

run_scenario 13 "DELEGATE on Lauren (not in requires.methods, returns 405)" \
    $CLIENT "agtp://$LAUREN_ID" DELEGATE \
    --param task=anything \
    --param "sub_agent=$ORCH_ID" \
    "${CLIENT_ARGS[@]}"

run_scenario 14 "FAKEMETHOD on Lauren (unknown method, returns 501)" \
    $CLIENT "agtp://$LAUREN_ID" FAKEMETHOD \
    --param x=1 \
    "${CLIENT_ARGS[@]}"

run_scenario 15 "Server-level DISCOVER (Form 2 URI returns Server Manifest)" \
    $CLIENT "agtp://127.0.0.1:4480" "${CLIENT_ARGS[@]}"

run_scenario 16 "Soft-deny: RECONCILE on Lauren (custom method, returns 403 method-not-permitted-for-agent)" \
    $CLIENT "agtp://$LAUREN_ID" RECONCILE \
    --param account_id=acct-001 \
    --param period=Q1 \
    "${CLIENT_ARGS[@]}"

run_scenario 17 "PROPOSE counter-proposal (PROPOSEX -> 422 with counter_proposal body)" \
    $CLIENT "agtp://$ORCH_ID" PROPOSE \
    -d '{"name":"PROPOSEX","parameters":{"x":"string"},"outcome":"object","description":"variant of PROPOSE that the policy should counter against"}' \
    "${CLIENT_ARGS[@]}"

run_scenario 18 "PROPOSE accept (QUERY -> 200 with synthesis_id)" \
    $CLIENT "agtp://$ORCH_ID" PROPOSE \
    -d '{"name":"QUERY","parameters":{"intent":"string"},"outcome":"results","description":"alias for QUERY suitable for synthesis acceptance"}' \
    "${CLIENT_ARGS[@]}"

run_scenario 19 "--match-check on Lauren (full match against demo server)" \
    $CLIENT "agtp://$LAUREN_ID" --match-check "${CLIENT_ARGS[@]}"

# Catalog probe via --grammar-check: ask whether a verb is in the
# AGTP catalog and admissible on the server, without committing to
# a real invocation. The Method-Grammar header pathway was retired
# in the catalog-collapse refactor; the catalog gate at the top of
# dispatch carries the same job for ordinary requests.
CURL="$PY -m client.cli.curl"
CURL_ARGS=(--insecure --insecure-skip-verify)

run_scenario 20 "Catalog probe: RECONCILE on Orchestrator (catalog admits; 405 method-not-implemented)" \
    $CLIENT "agtp://$ORCH_ID" RECONCILE --grammar-check "${CLIENT_ARGS[@]}"

run_scenario 21 "Catalog probe: FROBNICATE on Orchestrator (refused locally; no network call)" \
    $CLIENT "agtp://$ORCH_ID" FROBNICATE --grammar-check "${CLIENT_ARGS[@]}"

# Synthesis runtime: PROPOSE matches a recipe in agtp-recipes.toml
# (AUDIT = QUERY + SUMMARIZE), the runtime instantiates a multi-step
# plan and returns a synthesis_id. A subsequent invocation with
# Synthesis-Id walks the plan; SUSPEND clears it.
PROPOSE_AUDIT_OUT="$($CLIENT "agtp://$ORCH_ID" PROPOSE \
    -d '{"name":"AUDIT","parameters":{"subject":"string","length":"string"},"outcome":"summary","description":"audit subject by querying then summarizing"}' \
    "${CLIENT_ARGS[@]}" 2>/dev/null || true)"
SYN_ID=$(printf '%s' "$PROPOSE_AUDIT_OUT" | "$PY" -c "import json,sys; d=json.loads(sys.stdin.read()); print(d.get('synthesis',{}).get('synthesis_id',''))" 2>/dev/null || true)

run_scenario 22 "PROPOSE AUDIT (recipe-based composition; 200 with multi-step plan)" \
    $CLIENT "agtp://$ORCH_ID" PROPOSE \
    -d '{"name":"AUDIT","parameters":{"subject":"string","length":"string"},"outcome":"summary","description":"audit subject via QUERY + SUMMARIZE"}' \
    "${CLIENT_ARGS[@]}"

if [ -n "$SYN_ID" ]; then
    run_scenario 23 "Invoke AUDIT via Synthesis-Id (runtime walks the plan)" \
        $CURL -X AUDIT "agtp://$ORCH_ID" \
        -H "Synthesis-Id: $SYN_ID" \
        -d '{"subject":"lauren","length":"short"}' \
        "${CURL_ARGS[@]}"
else
    echo "[runner] skipping scenario 23: no synthesis_id captured" | tee -a "$TRANSCRIPT"
fi

run_scenario 24 "PROPOSE TELEPORT (no recipe, no exact match; 422 negotiation-refused)" \
    $CLIENT "agtp://$ORCH_ID" PROPOSE \
    -d '{"name":"TELEPORT","parameters":{},"outcome":"object","description":"verb the demo server does not host or compose"}' \
    "${CLIENT_ARGS[@]}"

if [ -n "$SYN_ID" ]; then
    run_scenario 25 "SUSPEND with synthesis_id clears the synthesis (200)" \
        $CLIENT "agtp://$ORCH_ID" SUSPEND \
        --param "synthesis_id=$SYN_ID" \
        --param reason=demo-cleanup \
        "${CLIENT_ARGS[@]}"

    run_scenario 26 "Invoke AUDIT after SUSPEND (404 synthesis-not-found)" \
        $CURL -X AUDIT "agtp://$ORCH_ID" \
        -H "Synthesis-Id: $SYN_ID" \
        -d '{"subject":"lauren","length":"short"}' \
        "${CURL_ARGS[@]}"
else
    echo "[runner] skipping scenarios 25-26: no synthesis_id captured" | tee -a "$TRANSCRIPT"
fi

# Catalog-based dispatcher gates: 459 (verb not in catalog) and
# 460 (path violates path grammar). These exercise the front-line
# validators that gate method dispatch.

run_scenario 27 "Unknown verb FROBNICATE on Orchestrator (459 method-grammar-violation, suggestions in body)" \
    $CLIENT "agtp://$ORCH_ID" FROBNICATE \
    "${CLIENT_ARGS[@]}"

run_scenario 28 "Legacy verb GET without Legacy: opt-in (459, suggestion leads with FETCH)" \
    $CURL -X GET "agtp://$ORCH_ID" \
    "${CURL_ARGS[@]}"

run_scenario 29 "Typo PROPOSEX on Orchestrator (459 with PROPOSE in suggestions)" \
    $CLIENT "agtp://$ORCH_ID" PROPOSEX \
    "${CLIENT_ARGS[@]}"

# Note: 460 path grammar violations require path-aware wire shape,
# which AGTP does not yet carry on the request line. The validator
# is in place at core/path_grammar; once the endpoint registry
# lands, paths flow through dispatch and 460 fires for verb-in-path.

{
    echo
    echo "=================================================================="
    echo "Demo complete."
    echo "Transcript:    $TRANSCRIPT"
    echo "Server log:    $TRANSCRIPT_DIR/server.log"
    echo "Registry log:  $TRANSCRIPT_DIR/registry.log"
    echo "=================================================================="
} | tee -a "$TRANSCRIPT"
