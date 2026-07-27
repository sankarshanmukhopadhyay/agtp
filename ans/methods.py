"""
ANS method handlers: RESOLVE / REGISTER / DEREGISTER.

Dispatched by the AGTP server only when a :class:`ans.store.NameStore` is
attached (ANS mode). :func:`maybe_handle_ans` is the single entry point the
server's connection handler calls; it returns an ``AGTPResponse`` when it
owns the request and ``None`` otherwise (so an ANS still answers DISCOVER
/population, PROBE, etc. through the presence hook).

An ANS is a presence coordinator plus a name store, so REGISTER both records
a name binding and announces a :class:`presence.records.PresenceRecord` into
the presence store — which means ANS-brokered DISCOVER reuses the whole
presence ranking + signing pipeline for free.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from client.core_client import send_method
from core import wire
from presence import scopes as _scopes
from presence.envelope import sign_result_set
from presence.records import PresenceRecord, Visibility
from server.methods import error_response, json_response, parse_body


ANS_METHODS = frozenset({"RESOLVE", "REGISTER", "DEREGISTER"})


def maybe_handle_ans(
    request: wire.AGTPRequest,
    registry: Any,
) -> Optional[wire.AGTPResponse]:
    """Route RESOLVE / REGISTER / DEREGISTER when this server is an ANS."""
    store = getattr(registry, "ans_store", None)
    if store is None:
        return None
    method = request.method.upper()
    if method == "RESOLVE":
        return _handle_resolve(request, registry, store)
    if method == "REGISTER":
        return _handle_register(request, registry, store)
    if method == "DEREGISTER":
        return _handle_deregister(request, registry, store)
    return None


def _params(request: wire.AGTPRequest) -> Dict[str, Any]:
    merged: Dict[str, Any] = dict(getattr(request, "query", {}) or {})
    try:
        body = parse_body(request)
    except ValueError:
        body = {}
    if isinstance(body, dict):
        merged.update(body)
    return merged


def _sign_payload(registry: Any, payload: Dict[str, Any], key: str) -> None:
    """Attach an ans_signature over ``payload[key]`` when a governance key
    is configured. RESOLVE signs the binding; DISCOVER signs the results."""
    signing = getattr(registry, "signing_service", None)
    if signing is None:
        return
    try:
        payload["ans_signature"] = sign_result_set(signing, payload[key])
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# RESOLVE — name -> Agent-ID + manifest.
# ---------------------------------------------------------------------------


def _handle_resolve(request, registry, store) -> wire.AGTPResponse:
    params = _params(request)
    name = params.get("name")
    if not isinstance(name, str) or not name.strip():
        return error_response(
            400, "Bad Request", "resolve-missing-name",
            "RESOLVE requires a 'name' parameter.",
        )
    binding = store.resolve(name)
    if binding is not None:
        payload: Dict[str, Any] = {
            "method": "RESOLVE",
            "name": binding.name,
            "binding": binding.to_dict(),
        }
        _sign_payload(registry, payload, "binding")
        return json_response(200, "OK", payload, method_name="RESOLVE")

    # Local miss. Try cross-ANS federation, unless this request is itself a
    # forwarded (federated) query — federation is single-hop, which bounds
    # fan-out and prevents loops.
    federated = _truthy(params.get("federated"))
    trust = getattr(registry, "federation_trust", None)
    if not federated and trust is not None and len(trust) > 0:
        fed = _federated_resolve(request, registry, name.strip())
        if fed is not None:
            remote_binding, path = fed
            payload = {
                "method": "RESOLVE",
                "name": remote_binding.get("name", name.strip()),
                "binding": remote_binding,
                "federation_path": path,
            }
            # The local ANS re-signs the merged result with its own key.
            _sign_payload(registry, payload, "binding")
            return json_response(200, "OK", payload, method_name="RESOLVE")

    return error_response(
        404, "Not Found", "name-not-found",
        f"no active binding for name {name!r} in this or any federated authority.",
    )


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _federated_resolve(request, registry, name):
    """
    Forward a RESOLVE to trusted peer ANS servers, carrying the original
    requester unchanged and marked as federated (so the peer does not
    re-forward). Verify the peer's ans_signature against its pinned key
    before trusting the binding. Returns ``(binding_dict, [peer_endpoint])``
    on the first verified hit, else None.
    """
    from presence.envelope import verify_result_set

    requester = wire.read_agent_id(request) or wire.header(request, "Agent-Identity") or ""
    use_tls = getattr(registry, "federation_use_tls", True)
    body = json.dumps({"name": name, "federated": True}).encode("utf-8")
    extra = {"Agent-Identity": requester} if requester else None

    for peer in registry.federation_trust.peers():
        host, _, port_s = peer.endpoint.rpartition(":")
        if not host or not port_s.isdigit():
            continue
        try:
            resp = send_method(
                None, host, int(port_s), "RESOLVE",
                body=body, body_content_type="application/json",
                extra_headers=extra, use_tls=use_tls, insecure_skip_verify=True,
            )
        except (OSError, wire.WireFormatError):
            continue
        if resp.status_code != 200 or not resp.body_bytes:
            continue
        try:
            data = json.loads(resp.body_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        remote_binding = data.get("binding")
        signature = data.get("ans_signature")
        # A federated result MUST be signed by the peer we trust; verify
        # against the pinned key before accepting it.
        if not isinstance(remote_binding, dict):
            continue
        if not verify_result_set(peer.public_key, remote_binding, signature):
            continue
        return remote_binding, [peer.endpoint]
    return None


# ---------------------------------------------------------------------------
# REGISTER — record a binding and announce the agent into presence.
# ---------------------------------------------------------------------------


def _handle_register(request, registry, store) -> wire.AGTPResponse:
    params = _params(request)
    agent_id = params.get("agent_id") or wire.read_agent_id(request)
    name = params.get("name")
    manifest = params.get("manifest")
    if not agent_id:
        return error_response(
            400, "Bad Request", "register-missing-agent-id",
            "REGISTER requires an agent_id.",
        )
    if not isinstance(name, str) or not name.strip():
        return error_response(
            400, "Bad Request", "register-missing-name",
            "REGISTER requires a 'name'.",
        )
    if not isinstance(manifest, dict):
        manifest = {}
    # Fall back to the binding name for display when the manifest omits it.
    if not manifest.get("name"):
        manifest = {**manifest, "name": name}

    binding = store.register(name, agent_id, manifest)

    # Announce into the presence store so ANS-brokered DISCOVER can rank
    # and return this agent through the normal presence pipeline.
    presence_store = getattr(registry, "presence_store", None)
    if presence_store is not None:
        presence_store.announce(_record_from_manifest(agent_id, manifest))

    return json_response(
        200, "OK",
        {
            "method": "REGISTER",
            "registered": True,
            "binding": binding.to_dict(),
            "naming_authority_size": store.count(),
        },
        method_name="REGISTER",
    )


def _record_from_manifest(agent_id: str, manifest: Dict[str, Any]) -> PresenceRecord:
    """Build a presence record from a submitted manifest (ANS has no local
    AgentDocument for the agent)."""
    methods = manifest.get("supported_methods") or []
    caps = manifest.get("capabilities")
    if not caps:
        caps = sorted(_scopes.derive_capabilities_from_methods(methods))
    entry: Dict[str, Any] = {
        "agent_id": agent_id,
        "manifest_uri": f"agtp://{agent_id}",
        "name": manifest.get("name"),
        "supported_methods": list(methods),
        "capabilities": list(caps),
        "trust_tier": manifest.get("trust_tier"),
        "verification_path": manifest.get("verification_path"),
    }
    score = manifest.get("behavioral_trust_score", manifest.get("trust_score"))
    if isinstance(score, (int, float)):
        entry["behavioral_trust_score"] = score
    owner_id = manifest.get("owner_id")
    if owner_id:
        entry["owner_id"] = owner_id
    return PresenceRecord(
        agent_id=agent_id,
        result_entry=entry,
        owner_domain=owner_id or None,
        visibility=Visibility(),
    )


# ---------------------------------------------------------------------------
# DEREGISTER — urgent removal on lifecycle transition.
# ---------------------------------------------------------------------------


def _handle_deregister(request, registry, store) -> wire.AGTPResponse:
    params = _params(request)
    agent_id = params.get("agent_id") or wire.read_agent_id(request)
    name = params.get("name")
    if not agent_id and not name:
        return error_response(
            400, "Bad Request", "deregister-missing-target",
            "DEREGISTER requires an agent_id or name.",
        )
    removed = store.deregister(agent_id=agent_id, name=name)

    # Mirror the removal into presence so a Revoked agent vanishes from
    # discovery too (the PDD names a Revoked-agent-in-results a governance
    # failure).
    presence_store = getattr(registry, "presence_store", None)
    if presence_store is not None and agent_id:
        presence_store.withdraw(agent_id)

    return json_response(
        200, "OK",
        {
            "method": "DEREGISTER",
            "deregistered": removed,
            "agent_id": agent_id,
            "naming_authority_size": store.count(),
        },
        method_name="DEREGISTER",
    )
