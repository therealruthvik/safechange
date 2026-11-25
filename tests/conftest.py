from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from safechange import config as config_mod
from safechange.audit import AuditLog
from safechange.backends.base import Backends
from safechange.backends.fake import FakeCluster, make_fake_backends
from safechange.models import Proposal, Status, Target
from safechange.pipeline import Clock, Pipeline
from safechange.proposer import ManualProposer, RulesProposer
from safechange.store import Store

WEB = Target("Deployment", "demo", "web")


class FakeClock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.t = start
        self.on_sleep: list[Any] = []

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s
        for hook in list(self.on_sleep):
            hook(self)

    def advance(self, s: float) -> None:
        self.t += s


@dataclass
class Env:
    cfg: config_mod.Config
    cluster: FakeCluster
    backends: Backends
    store: Store
    audit: AuditLog
    clock: FakeClock
    pipeline: Pipeline
    logs: list[str]

    def propose(self, task: str) -> Proposal:
        return self.pipeline.propose(task, RulesProposer(), "demo")

    def propose_ops(self, ops: list[dict[str, Any]], target: Target = WEB) -> Proposal:
        return self.pipeline.propose("manual", ManualProposer(target, ops), target.namespace)

    def approve(self, p: Proposal, who: str = "alice") -> Proposal:
        return self.pipeline.approve(p.id, who, p.plan_hash[:8])

    def approved(self, task: str) -> Proposal:
        p = self.propose(task)
        assert p.status == Status.PENDING_APPROVAL, p.policy_results
        return self.approve(p)

    def live(self) -> dict[str, Any]:
        return self.cluster.get(WEB)


def make_env(tmp_path: Path, **overrides: Any) -> Env:
    base: dict[str, Any] = {
        "state_dir": str(tmp_path / "state"),
        "backend": "fake",
        "rollout": {"timeout_seconds": 10, "poll_seconds": 1},
    }
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k] = {**base[k], **v}
        else:
            base[k] = v
    cfg = config_mod.from_dict(base)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cluster = FakeCluster(None, ready_after_polls=2)
    backends = make_fake_backends(cluster)
    store = Store(cfg.db_path)
    audit = AuditLog(cfg.audit_path)
    clock = FakeClock()
    logs: list[str] = []
    pipe = Pipeline(cfg, backends, store, audit, clock=Clock(clock.now, clock.sleep), log=logs.append)
    return Env(cfg, cluster, backends, store, audit, clock, pipe, logs)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    monkeypatch.delenv("SAFECHANGE_KILL", raising=False)
    return make_env(tmp_path)
