import functools
from pathlib import Path

import pytest

from safechange import patch as jp
from safechange import pipeline as pipeline_mod
from safechange import preflight
from safechange.models import Status
from safechange.pipeline import PipelineError

from conftest import WEB


def preflight_entry(env):
    return [e for e in env.audit.entries() if e["stage"] == "preflight"][-1]


def test_happy_path(env):
    p = env.approved("scale web to 4")
    p = env.pipeline.apply(p.id, "ops")
    assert p.status == Status.APPLIED
    assert env.live()["spec"]["replicas"] == 4
    assert jp.normalize(env.live()) == p.after
    stages = [e["stage"] for e in env.audit.entries()]
    assert stages == ["propose", "guardrails", "approve", "preflight", "apply", "verify", "verify"]
    snap = Path(preflight_entry(env)["details"]["snapshot"])
    assert snap.exists()


def test_apply_requires_approved(env):
    p = env.propose("scale web to 4")
    with pytest.raises(PipelineError):
        env.pipeline.apply(p.id, "ops")
    assert env.live()["spec"]["replicas"] == 2


def test_cannot_apply_twice(env):
    p = env.approved("scale web to 4")
    env.pipeline.apply(p.id, "ops")
    with pytest.raises(PipelineError):
        env.pipeline.apply(p.id, "ops")


def test_drift_aborts(env):
    p = env.approved("scale web to 4")
    env.backends.admin.scale(WEB, 3)  # a human scales manually after the proposal
    p = env.pipeline.apply(p.id, "ops")
    assert p.status == Status.ABORTED
    assert "no_drift_since_proposal" in preflight_entry(env)["details"]["failed"]
    assert env.live()["spec"]["replicas"] == 3, "drifted value must be left alone"


def test_status_only_change_is_not_drift(env):
    p = env.approved("scale web to 4")
    env.backends.reader.rollout_status(WEB)  # controller writes status / rv unchanged spec
    assert env.pipeline.apply(p.id, "ops").status == Status.APPLIED


def test_resource_version_conflict_aborts(env, monkeypatch):
    p = env.approved("scale web to 4")
    real = preflight.run

    def racing_preflight(*a, **kw):
        out = real(*a, **kw)
        # Someone writes between our preflight read and our patch.
        env.backends.admin.scale(WEB, 2)  # same spec, but resourceVersion bumps
        return out

    monkeypatch.setattr(pipeline_mod.preflight, "run", racing_preflight)
    p = env.pipeline.apply(p.id, "ops")
    assert p.status == Status.ABORTED and "resourceVersion conflict" in p.note
    assert env.live()["spec"]["replicas"] == 2


def test_snapshot_failure_aborts(env, monkeypatch):
    p = env.approved("scale web to 4")

    def bad_writer(directory, pid, obj):
        path = directory / f"{pid}.json"
        directory.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
        return path, False, "read-back mismatch (simulated)"

    monkeypatch.setattr(pipeline_mod.preflight, "run",
                        functools.partial(preflight.run, snapshot_writer=bad_writer))
    assert env.pipeline.apply(p.id, "ops").status == Status.ABORTED
    assert "snapshot_roundtrip" in preflight_entry(env)["details"]["failed"]
    assert env.live()["spec"]["replicas"] == 2


def test_snapshot_writer_exception_fails_closed(env, monkeypatch):
    p = env.approved("scale web to 4")

    def boom(*a):
        raise OSError("disk full")

    monkeypatch.setattr(pipeline_mod.preflight, "run",
                        functools.partial(preflight.run, snapshot_writer=boom))
    assert env.pipeline.apply(p.id, "ops").status == Status.ABORTED


def test_broken_rollback_patch_aborts(env):
    p = env.approved("scale web to 4")
    env.store._raw_update(p.id, "rollback_patch",
                          '[{"op": "replace", "path": "/spec/replicas", "value": 3}]')
    assert env.pipeline.apply(p.id, "ops").status == Status.ABORTED
    assert "rollback_reconstructs_before" in preflight_entry(env)["details"]["failed"]


def test_can_i_patch_denied_aborts(env):
    p = env.approved("scale web to 4")
    env.cluster.set_fault("can_i_denied", True)
    assert env.pipeline.apply(p.id, "ops").status == Status.ABORTED
    assert "applier_can_patch" in preflight_entry(env)["details"]["failed"]


def test_guardrails_rechecked_at_preflight(env):
    p = env.approved("scale web to 4")
    env.cluster.set_fault("dry_run_reject", True)  # e.g. admission policy changed since approval
    assert env.pipeline.apply(p.id, "ops").status == Status.ABORTED
    assert "guardrails_recheck" in preflight_entry(env)["details"]["failed"]


def test_apply_error_with_no_change_is_aborted(env):
    p = env.approved("scale web to 4")
    env.cluster.set_fault("patch_fail_on_call", 1)
    p = env.pipeline.apply(p.id, "ops")
    assert p.status == Status.ABORTED
    assert env.live()["spec"]["replicas"] == 2


def test_preflight_runs_all_checks(env):
    p = env.approved("scale web to 4")
    env.cluster.set_fault("can_i_denied", True)
    env.backends.admin.scale(WEB, 3)
    env.pipeline.apply(p.id, "ops")
    names = [r["name"] for r in preflight_entry(env)["details"]["results"]]
    assert names == ["kill_switch_disengaged", "approval_quorum_and_ttl", "plan_hash_integrity",
                     "guardrails_recheck", "no_drift_since_proposal", "snapshot_roundtrip",
                     "rollback_reconstructs_before", "applier_can_patch"]
    assert set(preflight_entry(env)["details"]["failed"]) >= {"no_drift_since_proposal",
                                                              "applier_can_patch"}
