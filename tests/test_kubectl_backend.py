"""kubectl backend command construction and error mapping, without a cluster."""
import subprocess

import pytest

from safechange.backends import kubectl as kb
from safechange.backends.base import BackendError, ConflictError, ForbiddenError, NotFoundError
from safechange.models import Target

T = Target("Deployment", "demo", "web")


class Recorder:
    def __init__(self, rc=0, out="{}", err=""):
        self.rc, self.out, self.err, self.cmds = rc, out, err, []

    def __call__(self, cmd, **kw):
        self.cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, self.rc, self.out, self.err)


@pytest.fixture
def rec(monkeypatch):
    r = Recorder()
    monkeypatch.setattr(kb.subprocess, "run", r)
    return r


def test_every_call_pins_kubeconfig_and_context(rec):
    reader = kb.KubectlReader("/r.kubeconfig", "safechange-reader", "r")
    reader.get(T)
    applier = kb.KubectlApplier("/a.kubeconfig", "safechange-applier", "a")
    applier.patch_json(T, [{"op": "replace", "path": "/spec/replicas", "value": 3}])
    assert rec.cmds[0][:5] == ["kubectl", "--kubeconfig", "/r.kubeconfig", "--context", "safechange-reader"]
    assert rec.cmds[1][:5] == ["kubectl", "--kubeconfig", "/a.kubeconfig", "--context", "safechange-applier"]
    assert "--type=json" in rec.cmds[1]


@pytest.mark.parametrize("err,exc", [
    ('Error from server (Forbidden): deployments.apps "web" is forbidden', ForbiddenError),
    ('Error from server (NotFound): deployments.apps "web" not found', NotFoundError),
    ("The request is invalid: the server rejected our request due to an error in our request",
     ConflictError),
    ("something else broke", BackendError),
])
def test_patch_error_mapping(rec, err, exc):
    rec.rc, rec.out, rec.err = 1, "", err
    with pytest.raises(exc):
        kb.KubectlApplier("", "ctx", "a").patch_json(T, [])


def test_rollout_status_parsing(rec):
    r = kb.KubectlReader("", "ctx", "r")
    rec.out = 'deployment "web" successfully rolled out\n'
    assert r.rollout_status(T).ready
    rec.out = 'Waiting for deployment "web" rollout to finish: 1 of 2 updated replicas are available...\n'
    st = r.rollout_status(T)
    assert not st.ready and not st.failed
    rec.rc, rec.out, rec.err = 1, "", 'error: deployment "web" exceeded its progress deadline'
    assert r.rollout_status(T).failed


def test_can_i(rec):
    a = kb.KubectlApplier("", "ctx", "a")
    rec.out = "yes\n"
    assert a.can_i_patch(T)
    rec.rc, rec.out = 1, "no\n"
    assert not a.can_i_patch(T)


def test_kubectl_missing_is_backend_error(monkeypatch):
    def boom(*a, **kw):
        raise FileNotFoundError("kubectl")
    monkeypatch.setattr(kb.subprocess, "run", boom)
    with pytest.raises(BackendError):
        kb.KubectlReader("", "ctx", "r").get(T)
