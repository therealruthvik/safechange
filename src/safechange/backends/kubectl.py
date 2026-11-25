"""Real-cluster backend: shells out to kubectl with one kubeconfig/context per identity.

Every call passes --kubeconfig and --context explicitly; nothing relies on the user's
current-context, so the reader can never accidentally act with admin credentials.
"""
from __future__ import annotations

import json
import subprocess
from typing import Any

import yaml

from ..models import Target
from .base import (Backends, BackendError, ConflictError, ForbiddenError, NotFoundError,
                   RolloutStatus)

_PLURAL = {"Deployment": "deployments.apps"}


def _resource(kind: str) -> str:
    try:
        return _PLURAL[kind]
    except KeyError:
        raise BackendError(f"unsupported kind {kind!r}") from None


class _Kubectl:
    def __init__(self, kubeconfig: str, context: str, identity: str, timeout: float = 30) -> None:
        self.kubeconfig = kubeconfig
        self.context = context
        self.identity = identity
        self.timeout = timeout

    def run(self, args: list[str], stdin: str | None = None,
            check: bool = True) -> subprocess.CompletedProcess[str]:
        cmd = ["kubectl"]
        if self.kubeconfig:
            cmd += ["--kubeconfig", self.kubeconfig]
        cmd += ["--context", self.context, *args]
        try:
            proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True,
                                  timeout=self.timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise BackendError(f"kubectl failed to run: {e}") from e
        if check and proc.returncode != 0:
            err = (proc.stderr or proc.stdout).strip()
            low = err.lower()
            if "testing value" in low or "test operation" in low or "test failed" in low \
                    or "conflict" in low:
                raise ConflictError(err)
            if "forbidden" in low:
                raise ForbiddenError(err)
            if "notfound" in low.replace(" ", "") or "not found" in low:
                raise NotFoundError(err)
            raise BackendError(err)
        return proc

    def json(self, args: list[str]) -> dict[str, Any]:
        out = self.run(args).stdout
        try:
            return json.loads(out)
        except json.JSONDecodeError as e:
            raise BackendError(f"invalid JSON from kubectl: {e}") from e


class KubectlReader(_Kubectl):
    def get(self, target: Target) -> dict[str, Any]:
        return self.json(["get", _resource(target.kind), target.name, "-n", target.namespace,
                          "-o", "json"])

    def list_names(self, kind: str, namespace: str) -> list[str]:
        data = self.json(["get", _resource(kind), "-n", namespace, "-o", "json"])
        return sorted(i["metadata"]["name"] for i in data.get("items", []))

    def get_configmap(self, namespace: str, name: str) -> dict[str, Any]:
        return self.json(["get", "configmap", name, "-n", namespace, "-o", "json"])

    def rollout_status(self, target: Target) -> RolloutStatus:
        proc = self.run(["rollout", "status", f"deployment/{target.name}", "-n",
                         target.namespace, "--watch=false"], check=False)
        msg = (proc.stdout or proc.stderr).strip().splitlines()
        text = msg[-1] if msg else ""
        if proc.returncode != 0:
            failed = "exceeded its progress deadline" in (proc.stderr or "")
            if not failed:
                raise BackendError(text or "rollout status failed")
            return RolloutStatus(ready=False, failed=True, message=text)
        return RolloutStatus(ready="successfully rolled out" in text, failed=False, message=text)


class KubectlApplier(_Kubectl):
    def can_i_patch(self, target: Target) -> bool:
        proc = self.run(["auth", "can-i", "patch", f"{_resource(target.kind)}/{target.name}",
                         "-n", target.namespace], check=False)
        return proc.returncode == 0 and proc.stdout.strip() == "yes"

    def dry_run_apply(self, obj: dict[str, Any]) -> tuple[bool, str]:
        proc = self.run(["apply", "--dry-run=server", "-f", "-", "-o", "name"],
                        stdin=yaml.safe_dump(obj), check=False)
        out = (proc.stdout + proc.stderr).strip()
        return proc.returncode == 0, out

    def patch_json(self, target: Target, ops: list[dict[str, Any]]) -> dict[str, Any]:
        try:
            return self.json(["patch", _resource(target.kind), target.name, "-n", target.namespace,
                              "--type=json", "-p", json.dumps(ops), "-o", "json"])
        except ConflictError:
            raise
        except BackendError as e:
            # The API server answers a failed JSON-patch `test` op with a generic 422
            # ("the server rejected our request due to an error in our request"). JSON
            # patches are atomic server-side, so this always means nothing was changed.
            if "rejected our request" in str(e).lower():
                raise ConflictError(f"JSON patch rejected (resourceVersion test failed or "
                                    f"invalid path); nothing changed: {e}") from e
            raise


class KubectlAdmin(_Kubectl):
    def set_control_killed(self, namespace: str, name: str, key: str, killed: bool) -> None:
        body = {"apiVersion": "v1", "kind": "ConfigMap",
                "metadata": {"name": name, "namespace": namespace},
                "data": {key: "true" if killed else "false"}}
        self.run(["apply", "-f", "-"], stdin=yaml.safe_dump(body))

    def delete_rolebinding(self, namespace: str, name: str) -> None:
        self.run(["delete", "rolebinding", name, "-n", namespace, "--ignore-not-found"])

    def restore_applier_rolebinding(self, namespace: str, name: str) -> None:
        body = {
            "apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
            "metadata": {"name": name, "namespace": namespace,
                         "labels": {"app.kubernetes.io/part-of": "safechange"}},
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role",
                        "name": "safechange-applier"},
            "subjects": [{"kind": "ServiceAccount", "name": "safechange-applier",
                          "namespace": "safechange-system"}],
        }
        self.run(["apply", "-f", "-"], stdin=yaml.safe_dump(body))

    def rolebinding_exists(self, namespace: str, name: str) -> bool:
        proc = self.run(["get", "rolebinding", name, "-n", namespace, "-o", "name"], check=False)
        return proc.returncode == 0

    def scale(self, target: Target, replicas: int) -> None:
        self.run(["scale", f"deployment/{target.name}", "-n", target.namespace,
                  f"--replicas={replicas}"])


def make_kubectl_backends(cfg: Any) -> Backends:
    k = cfg.kube
    return Backends(
        reader=KubectlReader(k.reader_kubeconfig, k.reader_context,
                             "system:serviceaccount:safechange-system:safechange-reader"),
        applier=KubectlApplier(k.applier_kubeconfig, k.applier_context,
                               "system:serviceaccount:safechange-system:safechange-applier"),
        admin=KubectlAdmin(k.admin_kubeconfig, k.admin_context, f"admin:{k.admin_context}"),
    )
