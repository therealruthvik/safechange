import json

import pytest

from safechange.approval import ApprovalError, plan_hash
from safechange.models import Status

from conftest import make_env


def test_plan_hash_binds_target_before_after(env):
    p = env.propose("scale web to 3")
    assert p.plan_hash == plan_hash(p.target, p.before, p.after)
    q = env.propose("scale web to 4")
    assert q.plan_hash != p.plan_hash


def test_wrong_plan_hash_rejected(env):
    p = env.propose("scale web to 3")
    other = env.propose("scale web to 4")
    with pytest.raises(ApprovalError, match="plan hash mismatch"):
        env.pipeline.approve(p.id, "alice", other.plan_hash[:8])
    assert env.store.get(p.id).status == Status.PENDING_APPROVAL


def test_plan_hash_prefix_too_short(env):
    p = env.propose("scale web to 3")
    with pytest.raises(ApprovalError, match="at least 8"):
        env.pipeline.approve(p.id, "alice", p.plan_hash[:7])


def test_full_hash_and_case_insensitive_prefix_accepted(env):
    p = env.propose("scale web to 3")
    assert env.pipeline.approve(p.id, "alice", p.plan_hash[:12].upper()).status == Status.APPROVED


@pytest.mark.parametrize("who", ["safechange-agent", " safechange-agent "])
def test_self_approval_blocked(env, who):
    p = env.propose("scale web to 3")
    with pytest.raises(ApprovalError, match="self-approval"):
        env.pipeline.approve(p.id, who, p.plan_hash[:8])
    assert any(e["stage"] == "approve" and e["result"] == "denied" for e in env.audit.entries())


def test_non_approver_blocked(env):
    p = env.propose("scale web to 3")
    with pytest.raises(ApprovalError, match="not in the approvers"):
        env.pipeline.approve(p.id, "mallory", p.plan_hash[:8])


def test_agent_identity_cannot_be_configured_as_approver(tmp_path):
    from safechange.config import ConfigError, from_dict
    with pytest.raises(ConfigError):
        from_dict({"state_dir": str(tmp_path), "approval": {"approvers": ["safechange-agent"]}})


def test_approval_expiry_aborts_apply(env):
    p = env.approved("scale web to 3")
    env.clock.advance(31 * 60)
    p = env.pipeline.apply(p.id, "ops")
    assert p.status == Status.ABORTED
    assert env.live()["spec"]["replicas"] == 2, "nothing may change"
    pre = [e for e in env.audit.entries() if e["stage"] == "preflight"][-1]
    assert "approval_quorum_and_ttl" in pre["details"]["failed"]


def test_approval_within_ttl_applies(env):
    p = env.approved("scale web to 3")
    env.clock.advance(29 * 60)
    assert env.pipeline.apply(p.id, "ops").status == Status.APPLIED


def test_n_of_m_approvals(tmp_path, monkeypatch):
    monkeypatch.delenv("SAFECHANGE_KILL", raising=False)
    e = make_env(tmp_path, approval={"required_approvals": 2})
    p = e.propose("scale web to 3")
    p = e.approve(p, "alice")
    assert p.status == Status.PENDING_APPROVAL and len(p.approvals) == 1
    with pytest.raises(ApprovalError, match="already approved"):
        e.approve(p, "alice")
    from safechange.pipeline import PipelineError
    with pytest.raises(PipelineError):
        e.pipeline.apply(p.id, "ops")
    p = e.approve(p, "bob")
    assert p.status == Status.APPROVED
    assert e.pipeline.apply(p.id, "ops").status == Status.APPLIED


def test_n_of_m_one_expired_approval_aborts(tmp_path, monkeypatch):
    monkeypatch.delenv("SAFECHANGE_KILL", raising=False)
    e = make_env(tmp_path, approval={"required_approvals": 2})
    p = e.propose("scale web to 3")
    e.approve(p, "alice")
    e.clock.advance(20 * 60)
    e.approve(p, "bob")
    e.clock.advance(15 * 60)  # alice's approval is now 35 min old
    assert e.pipeline.apply(p.id, "ops").status == Status.ABORTED


def test_tampered_stored_plan_aborts(env):
    p = env.approved("scale web to 3")
    after = dict(p.after)
    after["spec"] = {**after["spec"], "replicas": 9}
    env.store._raw_update(p.id, "after", json.dumps(after))
    assert env.pipeline.apply(p.id, "ops").status == Status.ABORTED
    assert env.live()["spec"]["replicas"] == 2


def test_reject(env):
    p = env.propose("scale web to 3")
    p = env.pipeline.reject(p.id, "bob", "not now")
    assert p.status == Status.REJECTED
    with pytest.raises(ApprovalError):
        env.approve(p)
