import pytest

from safechange.backends.fake import sample_deployment
from safechange.models import Status, Target
from safechange.policy import normalize_image, path_matches

from conftest import WEB, make_env

C0 = "/spec/template/spec/containers/0"


def failed(p):
    return {r.name for r in p.policy_results if not r.passed}


@pytest.mark.parametrize("ops,expected", [
    # path allowlist
    ([{"op": "add", "path": f"{C0}/securityContext", "value": {"privileged": True}}],
     {"paths_allowlisted", "no_privileged_or_host_access"}),
    ([{"op": "add", "path": "/spec/template/spec/hostNetwork", "value": True}],
     {"paths_allowlisted", "no_privileged_or_host_access"}),
    ([{"op": "add", "path": "/spec/template/spec/hostPID", "value": True}],
     {"paths_allowlisted", "no_privileged_or_host_access"}),
    ([{"op": "add", "path": "/spec/template/spec/securityContext", "value": {"runAsUser": 0}}],
     {"paths_allowlisted", "no_privileged_or_host_access"}),
    ([{"op": "replace", "path": "/spec/selector/matchLabels/app", "value": "x"}], {"paths_allowlisted"}),
    ([{"op": "add", "path": "/spec/template/spec/serviceAccountName", "value": "admin"}], {"paths_allowlisted"}),
    # replicas
    ([{"op": "replace", "path": "/spec/replicas", "value": 0}], {"replica_bounds"}),
    ([{"op": "replace", "path": "/spec/replicas", "value": 11}], {"replica_bounds", "replica_change_pct"}),
    ([{"op": "replace", "path": "/spec/replicas", "value": 5}], {"replica_change_pct"}),
    ([{"op": "replace", "path": "/spec/replicas", "value": "3"}], {"replica_bounds", "replica_change_pct"}),
    # images
    ([{"op": "replace", "path": f"{C0}/image", "value": "evil.io/miner:1.0"}], {"image_registry_allowlist"}),
    ([{"op": "replace", "path": f"{C0}/image", "value": "ghcr.io/organization/web:1"}],
     {"image_registry_allowlist"}),
    ([{"op": "replace", "path": f"{C0}/image", "value": "ghcr.io/org/web:latest"}], {"image_not_latest"}),
    ([{"op": "replace", "path": f"{C0}/image", "value": "ghcr.io/org/web"}], {"image_not_latest"}),
    ([{"op": "replace", "path": f"{C0}/image", "value": "nginx:latest"}], {"image_not_latest"}),
    # limits
    ([{"op": "remove", "path": f"{C0}/resources/limits"}], {"resource_limits_set"}),
    ([{"op": "remove", "path": f"{C0}/resources/limits/memory"}], {"resource_limits_set"}),
    # op kinds / structure
    ([{"op": "move", "from": "/metadata/labels/app", "path": "/metadata/labels/x"}],
     {"ops_allowed"}),
    ([{"op": "replace", "path": "/spec/doesnotexist", "value": 1}],
     {"patch_well_formed", "patch_changes_object_and_is_reversible", "paths_allowlisted"}),
    ([], {"patch_size", "patch_changes_object_and_is_reversible"}),
    ([{"op": "replace", "path": "/spec/replicas", "value": 2}], {"patch_changes_object_and_is_reversible"}),
])
def test_guardrail_denials(env, ops, expected):
    p = env.propose_ops(ops)
    assert p.status == Status.POLICY_FAILED
    got = failed(p) - {"server_dry_run"}
    assert expected <= got, (expected, got)
    # server dry-run is never attempted once a static check fails
    dry = next(r for r in p.policy_results if r.name == "server_dry_run")
    assert not dry.passed and "not attempted" in dry.detail


def test_max_patch_ops(env):
    ops = [{"op": "add", "path": f"/metadata/labels/k{i}", "value": "v"} for i in range(6)]
    assert "patch_size" in failed(env.propose_ops(ops))
    assert env.propose_ops(ops[:5]).status == Status.PENDING_APPROVAL


def test_namespace_and_kind(tmp_path, monkeypatch):
    monkeypatch.delenv("SAFECHANGE_KILL", raising=False)
    e = make_env(tmp_path)
    e.cluster.put(sample_deployment(namespace="prod"))
    p = e.propose_ops([{"op": "replace", "path": "/spec/replicas", "value": 3}],
                      Target("Deployment", "prod", "web"))
    assert "namespace_allowed" in failed(p)

    e2 = make_env(tmp_path / "2", policy={"allowed_kinds": ["StatefulSet"]})
    p = e2.propose_ops([{"op": "replace", "path": "/spec/replicas", "value": 3}])
    assert "kind_allowed" in failed(p)


def test_server_dry_run_rejection(env):
    env.cluster.set_fault("dry_run_reject", True)
    p = env.propose("scale web to 3")
    assert p.status == Status.POLICY_FAILED and failed(p) == {"server_dry_run"}


def test_policy_failed_can_never_be_approved(env):
    p = env.propose("set image web=nginx:latest")
    assert p.status == Status.POLICY_FAILED
    from safechange.approval import ApprovalError
    with pytest.raises(ApprovalError):
        env.approve(p)


def test_allowed_changes_pass(env):
    for task in ["scale web to 3", "set image web=ghcr.io/org/web:1.4.2",
                 "set env web LOG_LEVEL=debug", "set label web tier=frontend",
                 "set limits web cpu=300m memory=256Mi"]:
        p = env.propose(task)
        assert p.status == Status.PENDING_APPROVAL, (task, failed(p))


def test_path_matching():
    assert path_matches("/spec/replicas", "/spec/replicas")
    assert not path_matches("/spec/replicas", "/spec/replicas/x")
    assert path_matches("/a/*/image", "/a/3/image")
    assert not path_matches("/a/*/image", "/a/3/4/image")
    assert path_matches("/a/*/env/**", "/a/0/env")
    assert path_matches("/a/*/env/**", "/a/0/env/-")
    assert path_matches("/a/*/env/**", "/a/0/env/2/value")
    assert not path_matches("/a/*/env/**", "/a/0/envFrom")


def test_normalize_image():
    assert normalize_image("nginx:1.2") == ("docker.io/library/nginx", "1.2", None)
    assert normalize_image("org/app") == ("docker.io/org/app", None, None)
    assert normalize_image("ghcr.io/org/web:1.4.2") == ("ghcr.io/org/web", "1.4.2", None)
    assert normalize_image("localhost:5000/x:1") == ("localhost:5000/x", "1", None)
    assert normalize_image("ghcr.io/org/web@sha256:ab") == ("ghcr.io/org/web", None, "sha256:ab")
