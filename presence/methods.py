"""
Coordinator-level presence method handlers.

These are dispatched by the AGTP server *only* when a
:class:`presence.store.PresenceStore` is attached to the server state
(coordinator mode). :func:`maybe_handle_presence` is the single entry
point the server's connection handler calls; it returns an
``AGTPResponse`` when it owns the request and ``None`` to fall through to
the ordinary agent dispatch (so a coordinator can still host agents and
answer DESCRIBE for them).

Methods handled (PDD §6.1 lifecycle + §14 Q1 population path):

  * ``ANNOUNCE``            — publish/refresh presence (idempotent).
  * ``WITHDRAW``            — remove presence (graceful exit).
  * ``PROBE``               — liveness + posture for one Agent-ID.
  * ``DISCOVER /population`` — the visible-population query, distinct from
                              ``DISCOVER /agents`` (a single server's
                              hosted inventory).

M1 constraints, kept honest:
  * Only agents *hosted on this coordinator* may ANNOUNCE — the record is
    built from the coordinator's own trusted AgentDocument. Foreign,
    self-signed announcements need JWS verification, which lands in M3.
  * PROBE returns a plain 404 for an unknown Agent-ID; the
    invisible-vs-nonexistent indistinguishability guarantee is M2.
  * The population response is unsigned; the ``ans_signature`` envelope
    and ranking land in M3.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from core import wire
from presence import envelope as _envelope
from presence import gossip as _gossip
from presence import ranking as _ranking
from presence import recordsig as _recordsig
from presence import scopes as _scopes
from presence import visibility as _vis
from presence.records import DEFAULT_TTL_SECONDS, Visibility
from presence.visibility import RequesterContext
from server.methods import error_response, json_response, parse_body


PRESENCE_METHODS = frozenset(
    {"ANNOUNCE", "WITHDRAW", "PROBE", "PUBLISH", _gossip.GOSSIP_VERB}
)
_POPULATION_PATH = "/population"
_PROVIDERS_PATH = "/providers"


def maybe_handle_presence(
    request: wire.AGTPRequest,
    registry: Any,
    config: Any = None,
) -> Optional[wire.AGTPResponse]:
    """
    Route a request to a presence handler if this server is a coordinator
    and the request is a presence operation; otherwise return ``None``.
    """
    store = getattr(registry, "presence_store", None)
    if store is None:
        return None  # not a coordinator — ordinary dispatch owns this.

    method = request.method.upper()
    path = (getattr(request, "path", "/") or "/").lower()

    if method == "ANNOUNCE":
        return _handle_announce(request, registry, store)
    if method == "WITHDRAW":
        return _handle_withdraw(request, store)
    if method == "PROBE":
        return _handle_probe(request, registry, store)
    if method == "DISCOVER" and path == _POPULATION_PATH:
        return _handle_population(request, registry, store)
    if method == "DISCOVER" and path == _PROVIDERS_PATH:
        return _handle_providers(request, registry)
    if method == "PUBLISH":
        return _handle_publish(request, registry)
    if method == _gossip.GOSSIP_VERB:
        return _handle_replicate(request, registry, store)
    return None


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _subject_agent_id(request: wire.AGTPRequest, params: Dict[str, Any]) -> str:
    """
    Resolve the Agent-ID a presence op targets: the ``agent_id`` (or
    legacy ``target``) body/query parameter, else the ``Agent-ID`` wire
    header.
    """
    for key in ("agent_id", "target"):
        val = params.get(key)
        if isinstance(val, str) and val:
            return val
    return wire.read_agent_id(request) or ""


def _merged_params(request: wire.AGTPRequest) -> Dict[str, Any]:
    """Query params merged with the JSON body (body wins on conflicts)."""
    merged: Dict[str, Any] = dict(getattr(request, "query", {}) or {})
    try:
        body = parse_body(request)
    except ValueError:
        body = {}
    if isinstance(body, dict):
        # Unwrap a ``parameters`` envelope if present (the PDD DISCOVER
        # request nests filters under ``parameters``).
        inner = body.get("parameters")
        if isinstance(inner, dict):
            merged.update(inner)
        merged.update({k: v for k, v in body.items() if k != "parameters"})
    return merged


def _requester_context(request: wire.AGTPRequest, registry: Any) -> RequesterContext:
    """
    Build the :class:`RequesterContext` the visibility model evaluates
    against. The *caller's* identity is the verified certificate (mTLS),
    else the ``Agent-Identity`` header — distinct from ``Agent-ID``, which
    names the *target* of a request. Attributes (tier, owner-domain,
    capabilities) come from the caller's cert extensions when present,
    else from a hosted-agent lookup, else unknown (anonymous).
    """
    cert = getattr(request, "verified_cert", None)
    caller_id = ""
    if cert is not None:
        caller_id = getattr(cert, "agent_id", "") or ""
    if not caller_id:
        caller_id = wire.header(request, "Agent-Identity") or ""

    if not caller_id:
        return _vis.ANONYMOUS

    doc = registry.lookup(caller_id) if hasattr(registry, "lookup") else None
    if doc is not None:
        return RequesterContext(
            agent_id=caller_id,
            tier=doc.trust_tier,
            owner_domain=doc.owner_id or None,
            capabilities=frozenset(_scopes.derive_capabilities(doc)),
        )

    # Known caller id but not hosted here. Pull what the cert declares;
    # otherwise the id alone is enough for agent-id / explicit-only checks.
    tier = getattr(cert, "trust_tier", None) if cert is not None else None
    owner_domain = getattr(cert, "governance_zone", None) if cert is not None else None
    return RequesterContext(
        agent_id=caller_id,
        tier=tier if isinstance(tier, int) else None,
        owner_domain=owner_domain or None,
    )


def _cert_visibility_envelope(request: wire.AGTPRequest) -> Optional[Visibility]:
    """The max visibility envelope declared in the caller's certificate
    (``presence-visibility`` extension), or None when absent."""
    cert = getattr(request, "verified_cert", None)
    if cert is None:
        return None
    try:
        from server.agent_cert_ext import visibility_envelope_from_cert
    except ImportError:
        return None
    return visibility_envelope_from_cert(cert)


def _read_requested_visibility(
    request: wire.AGTPRequest, params: Dict[str, Any]
) -> Visibility:
    """
    The runtime-requested visibility for an ANNOUNCE: the ``visibility``
    body object, with a ``Presence-Mode`` header overriding the presence
    axis. Bounded against the cert envelope by the caller.
    """
    raw = params.get("visibility")
    requested = Visibility.from_dict(raw) if isinstance(raw, dict) else Visibility()
    header_mode = wire.header(request, "Presence-Mode")
    if header_mode:
        requested = Visibility(
            presence_mode=header_mode.strip(),
            disclosure_mode=requested.disclosure_mode,
            audience_scope=requested.audience_scope,
        )
    return requested


# ---------------------------------------------------------------------------
# Handlers.
# ---------------------------------------------------------------------------


def _handle_announce(
    request: wire.AGTPRequest,
    registry: Any,
    store: Any,
) -> wire.AGTPResponse:
    params = _merged_params(request)

    # Signed foreign announce (M3): a non-hosted agent may announce by
    # submitting a full, self-signed record. The coordinator verifies the
    # signature (integrity) and, when it can resolve the agent's Genesis,
    # the key→Agent-ID binding — so a relay can neither forge nor mutate
    # the announcement.
    raw_record = params.get("record")
    if isinstance(raw_record, dict):
        return _announce_foreign_record(registry, store, raw_record)

    agent_id = _subject_agent_id(request, params)
    if not agent_id:
        return error_response(
            400, "Bad Request", "announce-missing-agent-id",
            "ANNOUNCE requires an agent_id (body/query param or Agent-ID header).",
        )

    doc = registry.lookup(agent_id)
    if doc is None:
        return error_response(
            422, "Unprocessable Entity", "announce-agent-not-hosted",
            (
                f"agent {agent_id} is not hosted on this coordinator. A "
                f"non-hosted agent may announce by submitting a signed "
                f"'record' in the body (M3 signed foreign announce)."
            ),
        )

    # Effective visibility = runtime request bounded by the cert-declared
    # envelope (runtime may only reduce). Presence-Mode header overrides
    # the presence axis within that envelope.
    requested = _read_requested_visibility(request, params)
    envelope = _cert_visibility_envelope(request)
    effective = _vis.bound_visibility(envelope, requested)

    ttl_seconds = DEFAULT_TTL_SECONDS
    raw_ttl = params.get("ttl_seconds")
    if raw_ttl is not None:
        try:
            ttl_seconds = int(raw_ttl)
        except (TypeError, ValueError):
            return error_response(
                400, "Bad Request", "announce-bad-ttl",
                f"ttl_seconds must be an integer (got {raw_ttl!r}).",
            )

    relay = getattr(registry, "presence_relay_endpoint", None)
    record = store.build_record(
        doc, relay_endpoint=relay, visibility=effective, ttl_seconds=ttl_seconds,
    )
    # A coordinator holding a governance key signs its hosted records
    # (relay attestation) so they carry integrity protection as they
    # propagate through gossip.
    signing = getattr(registry, "signing_service", None)
    if signing is not None:
        try:
            _recordsig.sign_record(record, signing)
        except Exception:  # noqa: BLE001 - never fail announce on a signing error
            pass
    store.announce(record)
    return json_response(
        200, "OK",
        {
            "method": "ANNOUNCE",
            "presence": "announced",
            "announcement": record.to_announcement_dict(),
            "population_size": store.count(),
        },
        method_name="ANNOUNCE",
    )


def _announce_foreign_record(registry, store, raw_record) -> wire.AGTPResponse:
    """Accept a signed record from a non-hosted agent after verifying it."""
    from presence.records import PresenceRecord
    try:
        record = PresenceRecord.from_gossip_dict(raw_record)
    except (KeyError, TypeError, ValueError) as exc:
        return error_response(
            400, "Bad Request", "announce-bad-record",
            f"malformed record: {exc}",
        )
    if not _recordsig.verify_record(record):
        return error_response(
            403, "Forbidden", "announce-signature-invalid",
            "a foreign ANNOUNCE record must carry a valid signature.",
        )
    # Identity binding when a Genesis is resolvable; else integrity-only
    # (trust-on-first-use for the key).
    resolver = getattr(registry, "lookup_genesis", None)
    genesis = resolver(record.agent_id) if callable(resolver) else None
    if genesis is not None and not _recordsig.binds_to_genesis(record, genesis):
        return error_response(
            403, "Forbidden", "announce-key-binding-invalid",
            "record signing key is not bound to this agent's Genesis.",
        )
    store.announce(record)
    return json_response(
        200, "OK",
        {
            "method": "ANNOUNCE",
            "presence": "announced",
            "verified": True,
            "announcement": record.to_announcement_dict(),
            "population_size": store.count(),
        },
        method_name="ANNOUNCE",
    )


def _handle_withdraw(
    request: wire.AGTPRequest,
    store: Any,
) -> wire.AGTPResponse:
    params = _merged_params(request)
    agent_id = _subject_agent_id(request, params)
    if not agent_id:
        return error_response(
            400, "Bad Request", "withdraw-missing-agent-id",
            "WITHDRAW requires an agent_id (body/query param or Agent-ID header).",
        )
    withdrawn = store.withdraw(agent_id)
    return json_response(
        200, "OK",
        {
            "method": "WITHDRAW",
            "presence": "withdrawn" if withdrawn else "absent",
            "agent_id": agent_id,
            "population_size": store.count(),
        },
        method_name="WITHDRAW",
    )


def _probe_404(agent_id: str) -> wire.AGTPResponse:
    """
    The single 404 shape a PROBE returns for BOTH "not present" and
    "present but invisible to you". The two MUST be byte-indistinguishable
    (PDD §11.1 PROBE-404 side channel) so a caller cannot tell an
    invisible agent from a nonexistent one. Keep this the only 404 path.
    """
    return error_response(
        404, "Not Found", "probe-not-present",
        f"agent {agent_id} is not currently present in this scope.",
    )


def _handle_probe(
    request: wire.AGTPRequest,
    registry: Any,
    store: Any,
) -> wire.AGTPResponse:
    params = _merged_params(request)
    agent_id = _subject_agent_id(request, params)
    if not agent_id:
        return error_response(
            400, "Bad Request", "probe-missing-agent-id",
            "PROBE requires an agent_id (body/query param or Agent-ID header).",
        )
    record = store.probe(agent_id)
    requester = _requester_context(request, registry)
    # Absent, aged out, or invisible-to-this-requester all collapse to the
    # same 404 — an out-of-scope PROBE is indistinguishable from nonexistence.
    if record is None or not _vis.is_visible(record, requester):
        return _probe_404(agent_id)
    return json_response(
        200, "OK",
        {
            "method": "PROBE",
            "agent_id": agent_id,
            "present": True,
            "posture": record.visibility.to_dict(),
            "announced_at": record.announced_at,
            "ttl_seconds": record.ttl_seconds,
            "entry": _vis.shape_entry(record, requester),
        },
        method_name="PROBE",
    )


_DISCOVERY_SCOPE = "discovery:query"


def _has_discovery_scope(request: wire.AGTPRequest) -> bool:
    """True if the request's Authority-Scope header carries
    ``discovery:query`` (space-separated token list)."""
    raw = wire.header(request, "Authority-Scope") or ""
    return _DISCOVERY_SCOPE in raw.split()


def _int_param(params, key):
    raw = params.get(key)
    if raw is None:
        return None, None
    try:
        return int(raw), None
    except (TypeError, ValueError):
        return None, f"{key} must be an integer (got {raw!r})."


def _float_param(params, key):
    raw = params.get(key)
    if raw is None:
        return None, None
    try:
        return float(raw), None
    except (TypeError, ValueError):
        return None, f"{key} must be a number (got {raw!r})."


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _handle_population(
    request: wire.AGTPRequest,
    registry: Any,
    store: Any,
) -> wire.AGTPResponse:
    params = _merged_params(request)

    # discovery:query Authority-Scope gate (PDD §6.3). Off by default for
    # back-compat; a coordinator opts in via presence_require_discovery_scope.
    if getattr(registry, "presence_require_discovery_scope", False):
        if not _has_discovery_scope(request):
            return error_response(
                262, "Authorization Required", "authorization-required",
                "DISCOVER requires the discovery:query scope in Authority-Scope.",
                extra={"type": "scope-required", "required_scope": _DISCOVERY_SCOPE},
            )

    def _str(key):
        v = params.get(key)
        if isinstance(v, str):
            return v.strip() or None
        return str(v) if v is not None else None

    capability = _str("capability")
    intent = _str("intent")
    governance_zone = _str("governance_zone")
    org_domain = _str("org_domain")
    capability_domains = params.get("capability_domains")
    scope_negotiate = _truthy(params.get("scope_negotiate"))

    # Multi-overlay partition filters (tier / owner-domain). org_domain is
    # the DISCOVER-level alias for the owner-domain partition.
    tier, err = _int_param(params, "tier")
    if err:
        return error_response(400, "Bad Request", "population-bad-tier", err)
    owner_domain = _str("owner_domain") or _str("owner-domain") or org_domain

    trust_tier_min, err = _int_param(params, "trust_tier_min")
    if err:
        return error_response(400, "Bad Request", "population-bad-trust-tier-min", err)
    behavioral_trust_min, err = _float_param(params, "behavioral_trust_min")
    if err:
        return error_response(400, "Bad Request", "population-bad-behavioral-min", err)

    limit, err = _int_param(params, "limit")
    if err:
        return error_response(400, "Bad Request", "population-bad-limit", err)

    requester = _requester_context(request, registry)

    # Base partition query, then trust + visibility filters.
    records = store.query_population(
        capability=capability, tier=tier, owner_domain=owner_domain,
    )
    filtered = []
    for rec in records:
        rtier = rec.result_entry.get("trust_tier")
        # trust_tier_min is the least-trusted tier accepted; lower number
        # is more trusted, so require rec tier at least as trusted.
        if trust_tier_min is not None and (rtier is None or rtier > trust_tier_min):
            continue
        if behavioral_trust_min is not None:
            beh = rec.result_entry.get("behavioral_trust_score")
            if not isinstance(beh, (int, float)) or beh < behavioral_trust_min:
                continue
        if not _vis.is_visible(rec, requester):
            continue
        filtered.append(rec)

    scored = _ranking.rank_records(
        filtered,
        lambda r: set(r.result_entry.get("capabilities", [])),
        capability=capability,
        intent=intent,
    )

    results = []
    for sc in scored:
        if limit is not None and limit >= 0 and len(results) >= limit:
            break
        entry = _vis.shape_entry(sc.record, requester)
        entry["rank"] = len(results) + 1
        entry["capability_match_score"] = round(sc.capability_match, 4)
        entry["score"] = round(sc.score, 4)
        if scope_negotiate:
            # Informational: the scope a requester will need to interact
            # with this agent. The requester MUST evaluate it against its
            # own authorization before granting anything.
            entry["required_scope"] = _required_scope_for(sc.record)
        results.append(entry)

    payload: Dict[str, Any] = {
        "method": "DISCOVER",
        "target": "population",
        "query": {
            "capability": capability,
            "intent": intent,
            "capability_domains": capability_domains,
            "tier": tier,
            "owner_domain": owner_domain,
            "governance_zone": governance_zone,
            "trust_tier_min": trust_tier_min,
            "behavioral_trust_min": behavioral_trust_min,
            "scope_negotiate": scope_negotiate,
            "limit": limit,
        },
        "total_matches": len(scored),
        "returned": len(results),
        "results": results,
    }

    # Sign the result set when the coordinator holds a governance key.
    # Unsigned responses are emitted only when no key is configured; a
    # conforming requester rejects unsigned results in a signed deployment.
    signing = getattr(registry, "signing_service", None)
    if signing is not None:
        try:
            payload["ans_signature"] = _envelope.sign_result_set(signing, results)
        except Exception:  # noqa: BLE001 - never fail discovery on a signing error
            pass

    return json_response(200, "OK", payload, method_name="DISCOVER")


def _required_scope_for(record) -> str:
    """The Authority-Scope a caller needs to interact with a discovered
    agent. Derived from the agent's methods as ``<method>:invoke`` tokens
    (M3 placeholder; a full scope-negotiation contract is future work)."""
    methods = record.result_entry.get("supported_methods") or []
    return " ".join(sorted(f"{m.lower()}:invoke" for m in methods))


def _handle_replicate(
    request: wire.AGTPRequest,
    registry: Any,
    store: Any,
) -> wire.AGTPResponse:
    """
    Coordinator-to-coordinator gossip anti-entropy (REPLICATE). Merges the
    peer's records and returns the delta the peer is missing. This is a
    full-state sync between full nodes, NOT visibility-filtered: records
    carry their posture and are filtered per-requester at query time.

    When ``registry.presence_verify_signatures`` is set, records that carry
    an invalid/absent signature are dropped, so a peer cannot inject a
    forged or mutated announcement.
    """
    try:
        body = parse_body(request)
    except ValueError as exc:
        return error_response(400, "Bad Request", "replicate-bad-body", str(exc))
    if not isinstance(body, dict):
        return error_response(
            400, "Bad Request", "replicate-bad-body",
            "REPLICATE body must be a JSON object.",
        )
    verify = (
        _recordsig.verify_record
        if getattr(registry, "presence_verify_signatures", False)
        else None
    )
    reply = _gossip.apply_replicate(store, body, verify=verify)
    return json_response(200, "OK", reply, method_name=_gossip.GOSSIP_VERB)


def _handle_publish(request: wire.AGTPRequest, registry: Any) -> wire.AGTPResponse:
    """
    Rendezvous PUBLISH: a coordinator advertises that it serves a scope. The
    rendezvous node (this coordinator, being DHT-closest to the scope key)
    records the provider so cross-scope queries can find it.
    """
    index = getattr(registry, "rendezvous_index", None)
    if index is None:
        return error_response(
            404, "Not Found", "rendezvous-unavailable",
            "this coordinator does not hold a rendezvous index.",
        )
    params = _merged_params(request)
    scope_key = params.get("scope_key")
    endpoint = params.get("endpoint")
    if not isinstance(scope_key, str) or not scope_key.strip():
        return error_response(
            400, "Bad Request", "publish-missing-scope-key",
            "PUBLISH requires a 'scope_key'.",
        )
    if not isinstance(endpoint, str) or ":" not in endpoint:
        return error_response(
            400, "Bad Request", "publish-missing-endpoint",
            "PUBLISH requires a provider 'endpoint' (host:port).",
        )
    label = params.get("label") if isinstance(params.get("label"), str) else ""
    index.register_provider(scope_key.strip().lower(), endpoint.strip(), label=label or "")
    return json_response(
        200, "OK",
        {
            "method": "PUBLISH",
            "published": True,
            "scope_key": scope_key.strip().lower(),
            "scopes_indexed": index.scope_count(),
        },
        method_name="PUBLISH",
    )


def _handle_providers(request: wire.AGTPRequest, registry: Any) -> wire.AGTPResponse:
    """DISCOVER /providers?scope_key=... — the provider coordinators the
    rendezvous node knows for a scope."""
    index = getattr(registry, "rendezvous_index", None)
    if index is None:
        return error_response(
            404, "Not Found", "rendezvous-unavailable",
            "this coordinator does not hold a rendezvous index.",
        )
    params = _merged_params(request)
    scope_key = params.get("scope_key")
    if not isinstance(scope_key, str) or not scope_key.strip():
        return error_response(
            400, "Bad Request", "providers-missing-scope-key",
            "DISCOVER /providers requires a 'scope_key'.",
        )
    key = scope_key.strip().lower()
    providers = index.providers(key)
    return json_response(
        200, "OK",
        {
            "method": "DISCOVER",
            "target": "providers",
            "scope_key": key,
            "label": index.label(key),
            "providers": providers,
        },
        method_name="DISCOVER",
    )
