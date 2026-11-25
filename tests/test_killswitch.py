import pytest

from safechange import killswitch
from safechange.models import Status
from safechange.pipeline import KillSwitchEngaged

from conftest import WEB, make_env


def engage_file(env):
    env.cfg.killswitch.file.parent.mkdir(parents=True, exist_ok=True)
    env.cfg.killswitch.file.write_text("x")


def engage_env(env, monkeypatch):
    monkeypatch.setenv("SAFECHANGE_KILL", "1")


def engage_configmap(env):
    env.backends.admin.set_control_killed("safechange-system", "safechange-control", "killed", True)


def configmap_unreadable(env):
    env.cluster.set_fault("configmap_unreadable", True)


ENGAGERS = {
    "file": lambda env, mp: engage_file(env),
    "env": engage_env,
    "configmap": lambda env, mp: engage_configmap(env),
    "configmap_unreadable_fails_closed": lambda env, mp: configmap_unreadable(env),
}


@pytest.mark.parametrize("how", ENGAGERS)
def test_propose_refused(env, monkeypatch, how):
    ENGAGERS[how](env, monkeypatch)
    with pytest.raises(KillSwitchEngaged):
        env.propose("scale web to 3")
    assert env.store.list() == []
    assert env.audit.entries()[-1]["result"] == "refused_kill_switch"


@pytest.mark.parametrize("how", ENGAGERS)
def test_apply_refused(env, monkeypatch, how):
    p = env.approved("scale web to 3")
    ENGAGERS[how](env, monkeypatch)
    p = env.pipeline.apply(p.id, "ops")
    assert p.status == Status.ABORTED
    assert env.live()["spec"]["replicas"] == 2


def test_env_var_falsy_values_do_not_engage(env, monkeypatch):
    monkeypatch.setenv("SAFECHANGE_KILL", "0")
    assert not killswitch.soft_state(env.cfg, env.backends.reader).engaged


def _engage_after_polls(env, n):
    def hook(clock, _count=[0]):
        _count[0] += 1
        if _count[0] == n:
            engage_configmap(env)
    env.clock.on_sleep.append(hook)


def test_mid_rollout_freeze(env):
    p = env.approved("set image web=ghcr.io/org/web:1.4.2-bad")
    _engage_after_polls(env, 2)
    p = env.pipeline.apply(p.id, "ops")
    assert p.status == Status.FROZEN
    img = env.live()["spec"]["template"]["spec"]["containers"][0]["image"]
    assert img == "ghcr.io/org/web:1.4.2-bad", "freeze leaves the cluster exactly as-is"
    # A human can then roll back explicitly; rollback is allowed under the soft kill.
    p = env.pipeline.rollback(p.id, "alice")
    assert p.status == Status.ROLLED_BACK
    assert env.live()["spec"]["template"]["spec"]["containers"][0]["image"].endswith("1.27-alpine")


def test_mid_rollout_rollback_mode(tmp_path, monkeypatch):
    monkeypatch.delenv("SAFECHANGE_KILL", raising=False)
    e = make_env(tmp_path, killswitch={"on_engage_during_apply": "rollback"})
    p = e.approved("scale web to 4")
    _engage_after_polls(e, 1)
    e.cluster.ready_after_polls = 5
    p = e.pipeline.apply(p.id, "ops")
    assert p.status == Status.ROLLED_BACK
    assert e.live()["spec"]["replicas"] == 2


def test_hard_kill_removes_write_access(env):
    p = env.approved("scale web to 3")
    killswitch.engage(env.cfg, env.backends.admin, hard=True)
    assert not env.backends.applier.can_i_patch(WEB)
    # Even if the agent's code ignored the soft switch, the API server refuses the write.
    from safechange.backends.base import ForbiddenError
    with pytest.raises(ForbiddenError):
        env.backends.applier.patch_json(WEB, [{"op": "replace", "path": "/spec/replicas", "value": 9}])
    assert env.pipeline.apply(p.id, "ops").status == Status.ABORTED
    assert env.live()["spec"]["replicas"] == 2


def test_hard_kill_blocks_apply_even_with_soft_layer_bypassed(env, monkeypatch):
    p = env.approved("scale web to 3")
    env.backends.admin.delete_rolebinding("demo", "safechange-applier")  # hard only
    assert not killswitch.soft_state(env.cfg, env.backends.reader).engaged
    p = env.pipeline.apply(p.id, "ops")
    assert p.status == Status.ABORTED
    assert env.live()["spec"]["replicas"] == 2


def test_unkill_restores_everything(env):
    killswitch.engage(env.cfg, env.backends.admin, hard=True)
    assert killswitch.soft_state(env.cfg, env.backends.reader).engaged
    killswitch.disengage(env.cfg, env.backends.admin)
    assert not killswitch.soft_state(env.cfg, env.backends.reader).engaged
    assert env.backends.applier.can_i_patch(WEB)


def test_agent_identities_cannot_unkill(env):
    for ident in (env.backends.reader, env.backends.applier):
        with pytest.raises(AttributeError):
            ident.set_control_killed("safechange-system", "safechange-control", "killed", False)
        with pytest.raises(AttributeError):
            ident.restore_applier_rolebinding("demo", "safechange-applier")
