"""Human approval gate: plan-hash binding, separation of duties, expiry, N-of-M."""
from __future__ import annotations

from typing import Any

from .config import ApprovalConfig
from .models import Approval, Proposal, Status, Target
from .patch import normalize, sha256_json


class ApprovalError(Exception):
    pass


def plan_hash(target: Target, before: dict[str, Any], after: dict[str, Any]) -> str:
    """sha256 over the exact change: target + normalized before + normalized after."""
    return sha256_json({"target": target.to_dict(), "before": normalize(before),
                        "after": normalize(after)})


def check_approve(p: Proposal, approver: str, typed_hash: str, cfg: ApprovalConfig,
                  agent_identity: str) -> None:
    """Raise ApprovalError unless `approver` may approve `p` right now."""
    approver = approver.strip()
    if not approver:
        raise ApprovalError("approver identity is required (--as)")
    if p.status != Status.PENDING_APPROVAL:
        raise ApprovalError(f"proposal is {p.status}, only pending_approval can be approved")
    if approver in (agent_identity, p.agent_identity):
        raise ApprovalError("self-approval blocked: the agent identity cannot approve its own proposals")
    if approver not in cfg.approvers:
        raise ApprovalError(f"{approver!r} is not in the approvers list")
    if any(a.approver == approver for a in p.approvals):
        raise ApprovalError(f"{approver!r} already approved this proposal (N-of-M needs distinct people)")
    typed = typed_hash.strip().lower()
    if len(typed) < cfg.min_plan_hash_prefix:
        raise ApprovalError(f"type at least {cfg.min_plan_hash_prefix} characters of the plan hash")
    if not p.plan_hash.startswith(typed):
        raise ApprovalError("plan hash mismatch: you did not review this exact diff")


def valid_approvals(p: Proposal, cfg: ApprovalConfig, agent_identity: str,
                    now: float) -> tuple[list[Approval], list[str]]:
    """Approvals that still count, plus reasons for the ones that do not."""
    ok: list[Approval] = []
    problems: list[str] = []
    ttl = cfg.ttl_minutes * 60
    seen: set[str] = set()
    for a in p.approvals:
        if a.approver in seen:
            problems.append(f"{a.approver}: duplicate")
        elif a.approver in (agent_identity, p.agent_identity):
            problems.append(f"{a.approver}: agent identity cannot approve")
        elif a.approver not in cfg.approvers:
            problems.append(f"{a.approver}: not an approver")
        elif a.plan_hash != p.plan_hash:
            problems.append(f"{a.approver}: approved a different plan hash")
        elif now - a.approved_at > ttl:
            problems.append(f"{a.approver}: approval expired ({(now - a.approved_at) / 60:.1f} min old, "
                            f"ttl {cfg.ttl_minutes:g} min)")
        else:
            ok.append(a)
        seen.add(a.approver)
    return ok, problems


def has_quorum(p: Proposal, cfg: ApprovalConfig, agent_identity: str, now: float) -> bool:
    ok, _ = valid_approvals(p, cfg, agent_identity, now)
    return len(ok) >= cfg.required_approvals
