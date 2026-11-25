"""Backend interfaces, split by identity so least privilege is visible in the types.

* ClusterReader  - read-only ServiceAccount. The only thing a proposer ever receives.
* ClusterApplier - patch-only ServiceAccount. Used by the pipeline after every gate.
* ClusterAdmin   - a human operator's credentials. Used only by the kill switch CLI.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ..models import Target


class BackendError(Exception):
    """Any backend failure. Callers treat it as a failed (closed) check."""


class ConflictError(BackendError):
    """The server rejected a patch because a `test` op failed (stale resourceVersion)."""


class ForbiddenError(BackendError):
    """RBAC denied the request."""


class NotFoundError(BackendError):
    pass


@dataclass(frozen=True)
class RolloutStatus:
    ready: bool
    failed: bool
    message: str


class ClusterReader(Protocol):
    identity: str

    def get(self, target: Target) -> dict[str, Any]: ...

    def list_names(self, kind: str, namespace: str) -> list[str]: ...

    def get_configmap(self, namespace: str, name: str) -> dict[str, Any]: ...

    def rollout_status(self, target: Target) -> RolloutStatus: ...


class ClusterApplier(Protocol):
    identity: str

    def can_i_patch(self, target: Target) -> bool: ...

    def dry_run_apply(self, obj: dict[str, Any]) -> tuple[bool, str]: ...

    def patch_json(self, target: Target, ops: list[dict[str, Any]]) -> dict[str, Any]: ...


class ClusterAdmin(Protocol):
    identity: str

    def set_control_killed(self, namespace: str, name: str, key: str, killed: bool) -> None: ...

    def delete_rolebinding(self, namespace: str, name: str) -> None: ...

    def restore_applier_rolebinding(self, namespace: str, name: str) -> None: ...

    def rolebinding_exists(self, namespace: str, name: str) -> bool: ...

    def scale(self, target: Target, replicas: int) -> None: ...


@dataclass
class Backends:
    reader: ClusterReader
    applier: ClusterApplier
    admin: ClusterAdmin
