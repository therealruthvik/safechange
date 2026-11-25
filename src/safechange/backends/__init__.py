"""Cluster backends. `build_backends` is the single place identities are wired up."""
from __future__ import annotations

from ..config import Config
from .base import (Backends, BackendError, ClusterAdmin, ClusterApplier, ClusterReader,
                   ConflictError, ForbiddenError, NotFoundError, RolloutStatus)


def build_backends(cfg: Config, kind: str | None = None) -> Backends:
    kind = kind or cfg.backend
    if kind == "fake":
        from .fake import FakeCluster, make_fake_backends
        return make_fake_backends(FakeCluster(cfg.fake_cluster_file, cfg.fake_ready_after_polls))
    if kind == "kubectl":
        from .kubectl import make_kubectl_backends
        return make_kubectl_backends(cfg)
    raise ValueError(f"unknown backend {kind!r}")


__all__ = ["Backends", "BackendError", "ClusterAdmin", "ClusterApplier", "ClusterReader",
           "ConflictError", "ForbiddenError", "NotFoundError", "RolloutStatus", "build_backends"]
