"""Regression tests for governance/security evaluation remediations."""

from server.config import AuditConfig, MtlsConfig, ServerConfig, ServerInfo
from server.manifest import generate
from server.methods import dispatch
from tests.test_lifecycle import _req, _stage


def test_retired_agent_is_refused_before_dispatch(tmp_path):
    reg, aid, cfg, doc = _stage(tmp_path)
    doc.status = "retired"
    resp = dispatch(_req("INSPECT", aid, {"target": "lifecycle"}), reg, doc, config=cfg)
    assert resp.status_code == 410
    assert b"agent-revoked" in resp.body_bytes


def test_suspended_agent_is_temporarily_unavailable(tmp_path):
    reg, aid, cfg, doc = _stage(tmp_path)
    doc.status = "suspended"
    resp = dispatch(_req("INSPECT", aid, {"target": "lifecycle"}), reg, doc, config=cfg)
    assert resp.status_code == 503
    assert b"agent-suspended" in resp.body_bytes


def test_lifecycle_introspection_remains_available(tmp_path):
    reg, aid, cfg, doc = _stage(tmp_path)
    doc.status = "retired"
    resp = dispatch(_req("DESCRIBE", aid), reg, doc, config=cfg)
    assert resp.status_code not in {410, 503}


def test_reactivated_agent_regains_dispatch(tmp_path):
    reg, aid, cfg, doc = _stage(tmp_path)
    doc.status = "active"
    resp = dispatch(_req("INSPECT", aid, {"target": "lifecycle"}), reg, doc, config=cfg)
    assert resp.status_code not in {410, 503}


def test_manifest_discloses_demo_posture():
    cfg = ServerConfig(server=ServerInfo(server_id="s", operator="o", contact="c"))
    payload = generate(cfg, {}).to_dict()
    assert payload["security"]["identity_binding"] == "none"
    assert payload["assurance"] == {
        "identity_binding": "none",
        "attribution_records": False,
        "anonymous_discovery": True,
        "posture": "demo",
    }


def test_manifest_discloses_hardened_posture():
    cfg = ServerConfig(
        server=ServerInfo(server_id="s", operator="o", contact="c"),
        mtls=MtlsConfig(mode="required", ca_bundle_path="ca.pem"),
        audit=AuditConfig(attribution_records_enabled=True),
    )
    payload = generate(cfg, {}).to_dict()
    assert payload["security"]["identity_binding"] == "required"
    assert payload["assurance"]["posture"] == "hardened"
