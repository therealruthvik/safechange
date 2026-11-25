"""Core value types shared across modules."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class Status(StrEnum):
    POLICY_FAILED = "policy_failed"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    APPLYING = "applying"
    APPLIED = "applied"
    ROLLED_BACK = "rolled_back"
    FROZEN = "frozen"
    ABORTED = "aborted"
    FAILED = "failed"


# The only legal edges of the state machine. Anything else is a bug and is refused.
TRANSITIONS: dict[Status, frozenset[Status]] = {
    Status.PENDING_APPROVAL: frozenset({Status.APPROVED, Status.REJECTED}),
    Status.APPROVED: frozenset({Status.APPLYING, Status.REJECTED}),
    Status.APPLYING: frozenset({Status.APPLIED, Status.ROLLED_BACK, Status.FAILED,
                                Status.FROZEN, Status.ABORTED}),
    Status.FROZEN: frozenset({Status.ROLLED_BACK, Status.FAILED}),
    Status.APPLIED: frozenset({Status.ROLLED_BACK, Status.FAILED}),
    Status.POLICY_FAILED: frozenset(),
    Status.REJECTED: frozenset(),
    Status.ROLLED_BACK: frozenset(),
    Status.ABORTED: frozenset(),
    Status.FAILED: frozenset(),
}


@dataclass(frozen=True)
class Target:
    kind: str
    namespace: str
    name: str

    def __str__(self) -> str:
        return f"{self.kind}/{self.namespace}/{self.name}"

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Target":
        return cls(kind=str(d["kind"]), namespace=str(d["namespace"]), name=str(d["name"]))


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProposedChange:
    """What a proposer returns. Proposers never see or produce credentials."""
    target: Target
    ops: list[dict[str, Any]]
    rationale: str
    proposer: str


@dataclass
class Approval:
    approver: str
    plan_hash: str
    approved_at: float


@dataclass
class Proposal:
    id: str
    created_at: float
    status: Status
    task: str
    proposer: str
    agent_identity: str
    target: Target
    before: dict[str, Any]
    after: dict[str, Any]
    before_resource_version: str
    patch: list[dict[str, Any]]
    rollback_patch: list[dict[str, Any]]
    rationale: str
    plan_hash: str
    policy_results: list[CheckResult] = field(default_factory=list)
    approvals: list[Approval] = field(default_factory=list)
    note: str = ""
    updated_at: float = 0.0
