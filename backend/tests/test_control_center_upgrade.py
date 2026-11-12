from app.verification_layers import ActionEvidence, verify_layers
from app.mcp.registry import Registry, Tool
import pytest

def test_mutation_needs_authoritative_evidence():
    evidence = ActionEvidence(target_confidence=.95, dispatch_acknowledged=True, goal_aligned=True)
    assert not verify_layers(evidence, mutating=True)['verified']
    assert verify_layers(evidence)['verified']

def test_unknown_layers_are_not_claimed_passed():
    assert set(verify_layers(ActionEvidence())['layers'].values()) == {'unknown'}

def test_tool_approval_is_bound_and_single_use():
    calls=[]
    registry=Registry()
    registry.register(Tool('send', {'type':'object','properties':{'text':{'type':'string'}},'required':['text'],'additionalProperties':False}, lambda a: calls.append(a) or {'ok':True}))
    with pytest.raises(PermissionError): registry.execute('send', {'text':'hello'})
    token=registry.approve('send', {'text':'hello'})
    with pytest.raises(PermissionError): registry.execute('send', {'text':'changed'}, token)
    assert calls == []
    token=registry.approve('send', {'text':'hello'})
    assert registry.execute('send', {'text':'hello'}, token)['trust']=='untrusted_tool_content'
    with pytest.raises(PermissionError): registry.execute('send', {'text':'hello'}, token)
    assert len(calls)==1

def test_monitor_baseline_changes_and_dedup(tmp_path, monkeypatch):
    monkeypatch.setenv('PRIVAGENT_DB_PATH', str(tmp_path/'monitor.db'))
    from app.control_center import initialize, record_observation
    from app import db
    initialize()
    with db.get_connection() as conn:
        conn.execute("INSERT INTO monitoring_missions(id,title,target_url,interval_seconds,created_at) VALUES('m','Price','https://example.com',60,0)")
    assert record_observation('m','100',0) is False
    assert record_observation('m','90',60) is True
    assert record_observation('m','90',120) is False
    with db.get_connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM monitoring_events').fetchone()[0]==1
