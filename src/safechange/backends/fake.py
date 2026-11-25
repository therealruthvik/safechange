"""In-memory / JSON-file cluster for tests, CI and offline demos.

Simulates: resourceVersion bumps, JSON-patch semantics (incl. `test` ops), RBAC for the
applier RoleBinding, the control ConfigMap, and rollouts. Any container image whose
reference contains "bad" never becomes ready. Fault injection lives under `faults`.
"""
from __future__ import annotations

import copy
import fcntl
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .. import patch as jp
from ..models import Target
from .base import (Backends, BackendError, ConflictError, ForbiddenError, NotFoundError,
                   RolloutStatus)


def sample_deployment(name: str = "web", namespace: str = "demo", replicas: int = 2,
                      image: str = "docker.io/library/nginx:1.27-alpine") -> dict[str, Any]:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": namespace, "labels": {"app": name},
                     "resourceVersion": "1", "generation": 1, "uid": f"uid-{name}"},
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {"labels": {"app": name}},
                "spec": {"containers": [{
                    "name": name,
                    "image": image,
                    "ports": [{"containerPort": 80}],
                    "resources": {"requests": {"cpu": "50m", "memory": "64Mi"},
                                  "limits": {"cpu": "200m", "memory": "128Mi"}},
                }]},
            },
        },
        "status": {"replicas": replicas, "readyReplicas": replicas,
                   "updatedReplicas": replicas, "observedGeneration": 1},
    }


def default_state() -> dict[str, Any]:
    return {
        "rv": 1,
        "objects": {"Deployment/demo/web": sample_deployment()},
        "configmaps": {"safechange-system/safechange-control": {"data": {"killed": "false"}}},
        "rolebindings": {"demo/safechange-applier": True},
        "rollouts": {},
        "faults": {},
        "calls": {"patch": 0},
    }


class FakeCluster:
    def __init__(self, path: Path | None = None, ready_after_polls: int = 2,
                 state: dict[str, Any] | None = None) -> None:
        self.path = path
        self.ready_after_polls = ready_after_polls
        self._mem = state if state is not None else default_state()
        if path is not None and not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self._mem, indent=2))

    # -- persistence ---------------------------------------------------------------
    @contextmanager
    def _state(self, write: bool = False) -> Iterator[dict[str, Any]]:
        if self.path is None:
            yield self._mem
            return
        with open(self.path, "r+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                state = json.load(fh)
                yield state
                if write:
                    fh.seek(0)
                    fh.truncate()
                    json.dump(state, fh, indent=2)
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def snapshot_state(self) -> dict[str, Any]:
        with self._state() as s:
            return copy.deepcopy(s)

    def set_fault(self, name: str, value: Any) -> None:
        with self._state(write=True) as s:
            s["faults"][name] = value

    def put(self, obj: dict[str, Any]) -> None:
        with self._state(write=True) as s:
            m = obj["metadata"]
            s["objects"][f"{obj['kind']}/{m['namespace']}/{m['name']}"] = copy.deepcopy(obj)

    # -- helpers -------------------------------------------------------------------
    @staticmethod
    def _key(t: Target) -> str:
        return f"{t.kind}/{t.namespace}/{t.name}"

    def _write_object(self, s: dict[str, Any], key: str, new: dict[str, Any],
                      old: dict[str, Any]) -> dict[str, Any]:
        s["rv"] += 1
        new["metadata"]["resourceVersion"] = str(s["rv"])
        if new.get("spec") != old.get("spec"):
            new["metadata"]["generation"] = int(old["metadata"].get("generation", 1)) + 1
            s["rollouts"][key] = {"polls": 0}
        # Status is controller-owned: keep the old one; rollout_status() advances it.
        new["status"] = copy.deepcopy(old.get("status", {}))
        s["objects"][key] = new
        return copy.deepcopy(new)

    # -- reader ops ----------------------------------------------------------------
    def get(self, t: Target) -> dict[str, Any]:
        with self._state() as s:
            obj = s["objects"].get(self._key(t))
            if obj is None:
                raise NotFoundError(f"{t} not found")
            return copy.deepcopy(obj)

    def list_names(self, kind: str, namespace: str) -> list[str]:
        with self._state() as s:
            return sorted(k.split("/")[2] for k in s["objects"]
                          if k.startswith(f"{kind}/{namespace}/"))

    def get_configmap(self, namespace: str, name: str) -> dict[str, Any]:
        with self._state() as s:
            if s["faults"].get("configmap_unreadable"):
                raise BackendError("simulated: configmap read failed")
            cm = s["configmaps"].get(f"{namespace}/{name}")
            if cm is None:
                raise NotFoundError(f"configmap {namespace}/{name} not found")
            return copy.deepcopy(cm)

    def rollout_status(self, t: Target) -> RolloutStatus:
        with self._state(write=True) as s:
            key = self._key(t)
            obj = s["objects"].get(key)
            if obj is None:
                raise NotFoundError(f"{t} not found")
            ro = s["rollouts"].setdefault(key, {"polls": self.ready_after_polls})
            ro["polls"] += 1
            containers = obj["spec"]["template"]["spec"]["containers"]
            bad = any("bad" in str(c.get("image", "")) for c in containers)
            want = int(obj["spec"].get("replicas", 1))
            st = obj.setdefault("status", {})
            if bad or ro["polls"] < self.ready_after_polls:
                st["observedGeneration"] = obj["metadata"].get("generation", 1)
                msg = ("Waiting for rollout: 0 of %d updated replicas are available "
                       "(simulated ImagePullBackOff)" % want) if bad else \
                      "Waiting for rollout to finish (simulated)"
                return RolloutStatus(ready=False, failed=False, message=msg)
            st.update({"replicas": want, "readyReplicas": want, "updatedReplicas": want,
                       "observedGeneration": obj["metadata"].get("generation", 1)})
            return RolloutStatus(ready=True, failed=False,
                                 message=f'deployment "{t.name}" successfully rolled out')

    # -- applier ops ---------------------------------------------------------------
    def _require_applier_binding(self, s: dict[str, Any]) -> None:
        if not s["rolebindings"].get("demo/safechange-applier"):
            raise ForbiddenError("simulated RBAC: applier has no RoleBinding (hard kill)")

    def can_i_patch(self, t: Target) -> bool:
        with self._state() as s:
            if s["faults"].get("can_i_denied"):
                return False
            return bool(s["rolebindings"].get("demo/safechange-applier")) and t.namespace == "demo"

    def dry_run_apply(self, obj: dict[str, Any]) -> tuple[bool, str]:
        with self._state() as s:
            self._require_applier_binding(s)
            if s["faults"].get("dry_run_reject"):
                return False, "simulated admission rejection"
        try:
            containers = obj["spec"]["template"]["spec"]["containers"]
            assert isinstance(containers, list) and containers, "containers must be non-empty"
            for c in containers:
                assert isinstance(c.get("image"), str) and c["image"], "image must be a string"
            reps = obj["spec"].get("replicas", 1)
            assert isinstance(reps, int) and reps >= 0, "replicas must be a non-negative int"
        except (KeyError, TypeError, AssertionError) as e:
            return False, f"simulated validation error: {e}"
        return True, "dry-run ok (fake)"

    def patch_json(self, t: Target, ops: list[dict[str, Any]]) -> dict[str, Any]:
        with self._state(write=True) as s:
            self._require_applier_binding(s)
            s["calls"]["patch"] += 1
            fail_on = s["faults"].get("patch_fail_on_call")
            if fail_on is not None and s["calls"]["patch"] >= int(fail_on):
                raise BackendError(f"simulated: patch call #{s['calls']['patch']} failed")
            key = self._key(t)
            old = s["objects"].get(key)
            if old is None:
                raise NotFoundError(f"{t} not found")
            try:
                new = jp.apply_patch(old, ops)
            except jp.PatchTestFailed as e:
                raise ConflictError(str(e)) from e
            except jp.PatchError as e:
                raise BackendError(f"patch rejected: {e}") from e
            return self._write_object(s, key, new, old)

    # -- admin ops -----------------------------------------------------------------
    def set_control_killed(self, namespace: str, name: str, key: str, killed: bool) -> None:
        with self._state(write=True) as s:
            cm = s["configmaps"].setdefault(f"{namespace}/{name}", {"data": {}})
            cm.setdefault("data", {})[key] = "true" if killed else "false"

    def delete_rolebinding(self, namespace: str, name: str) -> None:
        with self._state(write=True) as s:
            s["rolebindings"][f"{namespace}/{name}"] = False

    def restore_applier_rolebinding(self, namespace: str, name: str) -> None:
        with self._state(write=True) as s:
            s["rolebindings"][f"{namespace}/{name}"] = True

    def rolebinding_exists(self, namespace: str, name: str) -> bool:
        with self._state() as s:
            return bool(s["rolebindings"].get(f"{namespace}/{name}"))

    def scale(self, t: Target, replicas: int) -> None:
        """Out-of-band human change (used to demonstrate drift)."""
        with self._state(write=True) as s:
            key = self._key(t)
            old = s["objects"][key]
            new = copy.deepcopy(old)
            new["spec"]["replicas"] = replicas
            self._write_object(s, key, new, old)


class _View:
    """Expose only one identity's methods of the shared FakeCluster."""
    _methods: tuple[str, ...] = ()

    def __init__(self, cluster: FakeCluster, identity: str) -> None:
        self._cluster = cluster
        self.identity = identity

    def __getattr__(self, name: str) -> Any:
        if name in self._methods:
            return getattr(self._cluster, name)
        raise AttributeError(f"{type(self).__name__} ({self.identity}) has no permission '{name}'")


class FakeReader(_View):
    _methods = ("get", "list_names", "get_configmap", "rollout_status")


class FakeApplier(_View):
    _methods = ("can_i_patch", "dry_run_apply", "patch_json")


class FakeAdmin(_View):
    _methods = ("set_control_killed", "delete_rolebinding", "restore_applier_rolebinding",
                "rolebinding_exists", "scale")


def make_fake_backends(cluster: FakeCluster) -> Backends:
    return Backends(
        reader=FakeReader(cluster, "system:serviceaccount:safechange-system:safechange-reader"),  # type: ignore[arg-type]
        applier=FakeApplier(cluster, "system:serviceaccount:safechange-system:safechange-applier"),  # type: ignore[arg-type]
        admin=FakeAdmin(cluster, "fake-admin"),  # type: ignore[arg-type]
    )
