from safechange import patch as jp
from safechange.models import Status
from safechange.pipeline import PipelineError

import pytest

from conftest import WEB


def test_failed_rollout_rolls_back_to_exact_before(env):
    original = env.live()
    p = env.approved("set image web=ghcr.io/org/web:1.4.2-bad")
    p = env.pipeline.apply(p.id, "ops")
    assert p.status == Status.ROLLED_BACK
    live = env.live()
    assert jp.normalize(live) == p.before
    assert live["spec"] == original["spec"]
    assert live["metadata"]["labels"] == original["metadata"]["labels"]
    results = [e["result"] for e in env.audit.entries()]
    assert "unhealthy" in results and "rolled_back" in results


def test_multi_op_change_rolls_back_exactly(env):
    p = env.propose_ops([
        {"op": "replace", "path": "/spec/template/spec/containers/0/image", "value": "ghcr.io/org/web:bad"},
        {"op": "add", "path": "/spec/template/spec/containers/0/env", "value": [{"name": "A", "value": "1"}]},
        {"op": "add", "path": "/metadata/labels/release", "value": "canary"},
        {"op": "replace", "path": "/spec/replicas", "value": 3},
    ])
    assert p.status == Status.PENDING_APPROVAL, p.policy_results
    env.approve(p)
    assert env.pipeline.apply(p.id, "ops").status == Status.ROLLED_BACK
    assert jp.normalize(env.live()) == p.before


def test_rollback_failure_is_failed_and_loud(env):
    p = env.approved("set image web=ghcr.io/org/web:1.4.2-bad")
    env.cluster.set_fault("patch_fail_on_call", 2)  # forward patch ok, rollback patch fails
    p = env.pipeline.apply(p.id, "ops")
    assert p.status == Status.FAILED
    assert "ROLLBACK FAILED" in p.note
    assert any(l.startswith("!!!") for l in env.logs)
    last = env.audit.entries()[-1]
    assert last["result"] == "failed" and last["details"]["severity"] == "CRITICAL"


def test_rollback_refused_if_someone_else_changed_object(env):
    p = env.approved("set image web=ghcr.io/org/web:1.4.2-bad")

    def meddle(clock, _n=[0]):
        _n[0] += 1
        if _n[0] == 3:
            env.backends.admin.scale(WEB, 7)
    env.clock.on_sleep.append(meddle)
    p = env.pipeline.apply(p.id, "ops")
    assert p.status == Status.FAILED and "ROLLBACK REFUSED" in p.note
    assert env.live()["spec"]["replicas"] == 7, "never blindly overwrite someone else's change"


def test_rollback_with_hard_kill_fails_loudly(tmp_path, monkeypatch):
    from conftest import make_env
    monkeypatch.delenv("SAFECHANGE_KILL", raising=False)
    e = make_env(tmp_path)
    p = e.approved("set image web=ghcr.io/org/web:1.4.2-bad")

    def hard_kill(clock, _n=[0]):
        _n[0] += 1
        if _n[0] == 2:
            e.backends.admin.delete_rolebinding("demo", "safechange-applier")
    e.clock.on_sleep.append(hard_kill)
    assert e.pipeline.apply(p.id, "ops").status == Status.FAILED


def test_manual_rollback_requires_approver(env):
    p = env.approved("scale web to 4")
    env.pipeline.apply(p.id, "ops")
    with pytest.raises(PipelineError):
        env.pipeline.rollback(p.id, "safechange-agent")
    assert env.pipeline.rollback(p.id, "bob").status == Status.ROLLED_BACK
    assert env.live()["spec"]["replicas"] == 2
