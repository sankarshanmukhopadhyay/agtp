from pathlib import Path
from server.rcns_state import SQLiteRcnsStateStore
from mod_merchant.replay_store import SQLiteSeenJtiStore
from tools.chain_integrity_check import repair_heads
from server.audit_chain import AuditChainStore
from server.audit_records import AuditRecordStore
from server.signing import SigningService
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

def test_sqlite_rcns_rate_limit_shared(tmp_path):
    a=SQLiteRcnsStateStore(tmp_path/'rcns.db'); b=SQLiteRcnsStateStore(tmp_path/'rcns.db')
    assert a.consume('agent', limit=2, now=100.0) is False
    assert b.consume('agent', limit=2, now=101.0) is False
    assert a.consume('agent', limit=2, now=102.0) is True

def test_sqlite_rcns_idempotency_shared(tmp_path):
    a=SQLiteRcnsStateStore(tmp_path/'rcns.db'); b=SQLiteRcnsStateStore(tmp_path/'rcns.db')
    a.put_idempotency('agent','key','sid',expires_at=200.0)
    assert b.get_idempotency('agent','key',now=150.0)=='sid'
    assert b.get_idempotency('agent','key',now=201.0) is None

def test_sqlite_jti_store_shared(tmp_path):
    a=SQLiteSeenJtiStore(str(tmp_path/'jti.db')); b=SQLiteSeenJtiStore(str(tmp_path/'jti.db'))
    assert a.check_and_record('jti', ttl_seconds=60) is False
    assert b.check_and_record('jti', ttl_seconds=60) is True

def test_repair_heads_from_unambiguous_records(tmp_path):
    heads=tmp_path/'heads'; records=tmp_path/'records'; agent='a'*64
    signer=SigningService(private_key=Ed25519PrivateKey.generate())
    store=AuditRecordStore(records); previous=''
    last=None
    for i in range(3):
        rec=signer.build_attribution_record(agent_id=agent, server_id='s', issued_at=f'2026-01-0{i+1}T00:00:00Z', status=200, previous_audit_id=previous)
        store.write(rec.audit_id, rec.jws); previous=rec.audit_id; last=rec.audit_id
    repaired=repair_heads(chain_head_root=heads, records_root=records)
    assert repaired[agent]==last
    assert AuditChainStore(heads).head(agent).audit_id==last
