"""
Tests for the mod_cache operational module: the backend, the hook,
and the install() boot path.
"""

from __future__ import annotations

import time

import pytest

from agtp.handlers import EndpointContext, EndpointError, EndpointResponse
from core.endpoint import EndpointSpec, ParamSpec, SemanticBlock
from mod_cache.backend import InMemoryCache
from mod_cache.hook import CacheHook


# ---------------------------------------------------------------------------
# InMemoryCache.
# ---------------------------------------------------------------------------


def test_cache_hit_after_set() -> None:
    cache = InMemoryCache(max_entries=10, default_ttl_seconds=60)
    cache.set("k", "v")
    assert cache.get("k") == "v"


def test_cache_miss_when_empty() -> None:
    cache = InMemoryCache()
    assert cache.get("k") is None


def test_cache_evicts_lru_at_capacity() -> None:
    cache = InMemoryCache(max_entries=2, default_ttl_seconds=60)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.set("c", 3)  # evicts a
    assert cache.get("a") is None
    assert cache.get("b") == 2
    assert cache.get("c") == 3


def test_cache_ttl_expires() -> None:
    cache = InMemoryCache(default_ttl_seconds=0.05)
    cache.set("k", "v")
    assert cache.get("k") == "v"
    time.sleep(0.1)
    assert cache.get("k") is None


def test_cache_get_promotes_to_mru() -> None:
    cache = InMemoryCache(max_entries=2, default_ttl_seconds=60)
    cache.set("a", 1)
    cache.set("b", 2)
    _ = cache.get("a")  # promote a
    cache.set("c", 3)   # evicts b, not a
    assert cache.get("a") == 1
    assert cache.get("b") is None
    assert cache.get("c") == 3


def test_cache_stats() -> None:
    cache = InMemoryCache(max_entries=2, default_ttl_seconds=60)
    cache.set("a", 1)
    cache.get("a")
    cache.get("missing")
    stats = cache.stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 1


# ---------------------------------------------------------------------------
# Helpers for building specs and contexts.
# ---------------------------------------------------------------------------


def _spec(
    impact: str = "informational",
    is_idempotent: bool = True,
) -> EndpointSpec:
    return EndpointSpec(
        name="QUERY",
        path="/echo",
        description="Echo.",
        required_params=[ParamSpec(name="value", type="string", description="x")],
        semantic=SemanticBlock(
            intent="Echo.",
            actor="agent",
            outcome="Echoed.",
            capability="retrieval",
            confidence=0.9,
            impact=impact,
            is_idempotent=is_idempotent,
        ),
    )


def _ctx(value: str = "x") -> EndpointContext:
    return EndpointContext(
        input={"value": value},
        agent_id="agent-1",
        method="QUERY",
        path="/echo",
        request_id="req-1",
    )


# ---------------------------------------------------------------------------
# CacheHook.
# ---------------------------------------------------------------------------


def test_cache_hook_miss_then_hit() -> None:
    hook = CacheHook(InMemoryCache())
    spec = _spec(impact="informational")

    # First call: miss.
    assert hook.before_dispatch(spec, _ctx("v1"), None) is None
    # Handler ran, populated cache.
    hook.after_dispatch(spec, _ctx("v1"), EndpointResponse(body={"echo": "v1"}), None)
    # Second call same input: hit.
    cached = hook.before_dispatch(spec, _ctx("v1"), None)
    assert isinstance(cached, EndpointResponse)
    assert cached.body == {"echo": "v1"}


def test_cache_hook_keyed_by_input() -> None:
    hook = CacheHook(InMemoryCache())
    spec = _spec()
    hook.after_dispatch(spec, _ctx("a"), EndpointResponse(body={"echo": "a"}), None)
    hook.after_dispatch(spec, _ctx("b"), EndpointResponse(body={"echo": "b"}), None)
    assert hook.before_dispatch(spec, _ctx("a"), None).body == {"echo": "a"}
    assert hook.before_dispatch(spec, _ctx("b"), None).body == {"echo": "b"}


def test_cache_hook_skips_irreversible() -> None:
    hook = CacheHook(InMemoryCache())
    spec = _spec(impact="irreversible", is_idempotent=False)
    hook.after_dispatch(spec, _ctx(), EndpointResponse(body={"ok": True}), None)
    assert hook.before_dispatch(spec, _ctx(), None) is None


def test_cache_hook_skips_reversible_unless_idempotent() -> None:
    hook = CacheHook(InMemoryCache())
    not_idem = _spec(impact="reversible", is_idempotent=False)
    hook.after_dispatch(not_idem, _ctx(), EndpointResponse(body={"r": 1}), None)
    assert hook.before_dispatch(not_idem, _ctx(), None) is None

    idem = _spec(impact="reversible", is_idempotent=True)
    hook.after_dispatch(idem, _ctx(), EndpointResponse(body={"r": 2}), None)
    cached = hook.before_dispatch(idem, _ctx(), None)
    assert isinstance(cached, EndpointResponse)
    assert cached.body == {"r": 2}


def test_cache_hook_does_not_cache_errors() -> None:
    hook = CacheHook(InMemoryCache())
    spec = _spec()
    hook.after_dispatch(spec, _ctx(), EndpointError(code="x", message="y"), None)
    assert hook.before_dispatch(spec, _ctx(), None) is None


def test_cache_hook_skips_endpoints_without_semantic() -> None:
    hook = CacheHook(InMemoryCache())
    spec = EndpointSpec(name="QUERY", path="/x")  # no semantic block
    hook.after_dispatch(spec, _ctx(), EndpointResponse(body={}), None)
    assert hook.before_dispatch(spec, _ctx(), None) is None


# ---------------------------------------------------------------------------
# install() boot path.
# ---------------------------------------------------------------------------


def test_install_registers_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGTP_CACHE_MAX_ENTRIES", "5")
    monkeypatch.setenv("AGTP_CACHE_DEFAULT_TTL", "10")
    monkeypatch.setenv("AGTP_CACHE_ENABLED", "1")

    from server.hooks import HookRegistry
    from mod_cache import install

    class _FakeState:
        hook_registry = HookRegistry()

    state = _FakeState()
    install(state)
    assert state.hook_registry.count() == 1
    hook = state.hook_registry.all()[0]
    assert isinstance(hook, CacheHook)
    assert hook.backend.max_entries == 5
    assert hook.backend.default_ttl_seconds == 10.0


def test_install_respects_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGTP_CACHE_ENABLED", "0")

    from server.hooks import HookRegistry
    from mod_cache import install

    class _FakeState:
        hook_registry = HookRegistry()

    state = _FakeState()
    install(state)
    assert state.hook_registry.count() == 0
