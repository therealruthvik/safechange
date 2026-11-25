"""Orchestrates propose -> guardrails -> approval -> preflight -> apply -> verify.

Each stage writes an audit entry. Stages are only reachable through the store's state
machine, so no stage can be skipped: apply requires `approved`, which requires a passing
guardrail run and a valid human approval, and apply itself re-runs every check.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from . import approval, killswitch, policy, preflight
from . import patch as jp
from .audit import AuditLog
from .backends.base import Backends, BackendError, ConflictError
from .config import Config
from .models import CheckResult, Proposal, Status
from .proposer import Proposer
from .store import Store


class PipelineError(Exception):
    pass


class KillSwitchEngaged(PipelineError):
    pass


@dataclass
class Clock:
    now: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep


class Pipeline:
    def __init__(self, cfg: Config, backends: Backends, store: Store, audit: AuditLog,
                 clock: Clock | None = None, log: Callable[[str], None] = lambda s: None) -> None:
        self.cfg, self.b, self.store, self.audit = cfg, backends, store, audit
        self.clock = clock or Clock()
        self.log = log

    # ------------------------------------------------------------------ propose + guardrails
    def propose(self, task: str, proposer: Proposer, namespace: str) -> Proposal:
        agent = self.cfg.agent_identity
        ks = killswitch.soft_state(self.cfg, self.b.reader)
        if ks.engaged:
            self.audit.append("propose", agent, "refused_kill_switch", task=task, reasons=ks.reasons)
            raise KillSwitchEngaged("kill switch engaged, refusing to propose: " + "; ".join(ks.reasons))
        try:
            change = proposer.propose(task, namespace, self.b.reader)
        except Exception as e:
            self.audit.append("propose", agent, "error", task=task, proposer=proposer.name,
                              error=f"{type(e).__name__}: {e}")
            raise PipelineError(f"proposer failed: {e}") from e

        # Re-read the object ourselves; never trust a proposer's view of the cluster.
        live = self.b.reader.get(change.target)
        before = jp.normalize(live)
        rv = str(live.get("metadata", {}).get("resourceVersion", ""))
        pre: list[CheckResult] = []
        try:
            jp.validate_patch(change.ops)
            after = jp.normalize(jp.apply_patch(before, change.ops))
            pre.append(CheckResult("patch_well_formed", True, f"{len(change.ops)} ops apply cleanly"))
        except jp.PatchError as e:
            after = before
            pre.append(CheckResult("patch_well_formed", False, str(e)))
        rollback: list[dict[str, Any]] = []
        try:
            rollback = jp.diff(after, before)
            ok = after != before and jp.apply_patch(after, rollback) == before
            pre.append(CheckResult("patch_changes_object_and_is_reversible", ok,
                                   f"rollback has {len(rollback)} ops" if ok else
                                   "patch is a no-op or not reversible"))
        except jp.PatchError as e:
            pre.append(CheckResult("patch_changes_object_and_is_reversible", False, str(e)))

        results = pre + policy.evaluate(self.cfg.policy, change.target, before, after,
                                        change.ops, self.b.applier,
                                        prior_ok=all(r.passed for r in pre))
        status = Status.PENDING_APPROVAL if policy.passed(results) else Status.POLICY_FAILED
        p = Proposal(
            id=self.store.new_id(), created_at=self.clock.now(), status=status, task=task,
            proposer=change.proposer, agent_identity=agent, target=change.target,
            before=before, after=after, before_resource_version=rv, patch=change.ops,
            rollback_patch=rollback, rationale=change.rationale,
            plan_hash=approval.plan_hash(change.target, before, after), policy_results=results)
        self.store.insert(p)
        self.audit.append("propose", agent, "proposed", proposal_id=p.id, plan_hash=p.plan_hash,
                          task=task, proposer=change.proposer, target=str(p.target),
                          patch=p.patch, rationale=p.rationale, reader=self.b.reader.identity)
        failed = [r.name for r in results if not r.passed]
        self.audit.append("guardrails", "policy-engine", status.value, proposal_id=p.id,
                          plan_hash=p.plan_hash, failed=failed, checks=len(results))
        return self.store.get(p.id)

    # ------------------------------------------------------------------ approval gate
    def approve(self, pid: str, approver: str, typed_hash: str) -> Proposal:
        p = self.store.get(pid)
        try:
            approval.check_approve(p, approver, typed_hash, self.cfg.approval, self.cfg.agent_identity)
        except approval.ApprovalError as e:
            self.audit.append("approve", approver, "denied", proposal_id=pid,
                              plan_hash=p.plan_hash, reason=str(e))
            raise
        now = self.clock.now()
        self.store.add_approval(pid, approver, p.plan_hash, now)
        p = self.store.get(pid)
        valid, _ = approval.valid_approvals(p, self.cfg.approval, self.cfg.agent_identity, now)
        need = self.cfg.approval.required_approvals
        result = f"approval {len(valid)}/{need}"
        if len(valid) >= need:
            self.store.transition(pid, Status.PENDING_APPROVAL, Status.APPROVED)
            result = "approved"
        self.audit.append("approve", approver, result, proposal_id=pid, plan_hash=p.plan_hash,
                          expires_in_min=self.cfg.approval.ttl_minutes)
        return self.store.get(pid)

    def reject(self, pid: str, actor: str, reason: str) -> Proposal:
        p = self.store.get(pid)
        self.store.transition(pid, p.status, Status.REJECTED, note=f"rejected by {actor}: {reason}")
        self.audit.append("reject", actor, "rejected", proposal_id=pid, plan_hash=p.plan_hash,
                          reason=reason)
        return self.store.get(pid)

    # ------------------------------------------------------------------ preflight + apply + verify
    def apply(self, pid: str, actor: str) -> Proposal:
        p = self.store.get(pid)
        if p.status != Status.APPROVED:
            self.audit.append("apply", actor, "refused", proposal_id=pid, plan_hash=p.plan_hash,
                              reason=f"status is {p.status}")
            raise PipelineError(f"proposal {pid} is {p.status}; only approved proposals can be applied")
        self.store.transition(pid, Status.APPROVED, Status.APPLYING)  # claim (CAS)

        pf = preflight.run(self.cfg, self.b, p, self.clock.now())
        failed = [r.name for r in pf.results if not r.passed]
        self.audit.append("preflight", actor, "passed" if pf.ok else "failed", proposal_id=pid,
                          plan_hash=p.plan_hash, failed=failed,
                          results=[r.to_dict() for r in pf.results],
                          snapshot=str(pf.snapshot_path) if pf.snapshot_path else None)
        if not pf.ok or pf.live is None:
            return self._finish(p, Status.APPLYING, Status.ABORTED, actor,
                                f"preflight failed: {failed}; nothing changed", stage="abort")

        live_rv = str(pf.live["metadata"]["resourceVersion"])
        ops = [{"op": "test", "path": "/metadata/resourceVersion", "value": live_rv}, *p.patch]
        try:
            applied = self.b.applier.patch_json(p.target, ops)
        except ConflictError as e:
            return self._finish(p, Status.APPLYING, Status.ABORTED, actor,
                                f"resourceVersion conflict, nothing changed: {e}", stage="apply")
        except BackendError as e:
            return self._after_ambiguous_apply_error(p, actor, e)
        post_matches = jp.normalize(applied) == p.after
        self.audit.append("apply", actor, "patched", proposal_id=pid, plan_hash=p.plan_hash,
                          applier=self.b.applier.identity, resource_version_test=live_rv,
                          new_resource_version=applied.get("metadata", {}).get("resourceVersion"),
                          live_matches_after=post_matches)
        return self._verify(p, actor, applied)

    def _after_ambiguous_apply_error(self, p: Proposal, actor: str, err: Exception) -> Proposal:
        """The patch call failed: determine from the live object whether it took effect."""
        try:
            now_live = jp.normalize(self.b.reader.get(p.target))
        except Exception as e2:
            return self._finish(p, Status.APPLYING, Status.FAILED, actor,
                                f"apply error ({err}) and live state unknown ({e2}); INVESTIGATE",
                                stage="apply", loud=True)
        if now_live == p.before:
            return self._finish(p, Status.APPLYING, Status.ABORTED, actor,
                                f"apply rejected, nothing changed: {err}", stage="apply")
        if now_live == p.after:
            self.audit.append("apply", actor, "patched_despite_error", proposal_id=p.id,
                              plan_hash=p.plan_hash, error=str(err))
            return self._verify(p, actor, now_live)
        return self._finish(p, Status.APPLYING, Status.FAILED, actor,
                            f"apply error ({err}) and live matches neither before nor after; INVESTIGATE",
                            stage="apply", loud=True)

    def _verify(self, p: Proposal, actor: str, applied: dict[str, Any]) -> Proposal:
        outcome, msg = self._wait_rollout(p, watch_kill=True)
        self.audit.append("verify", actor, outcome, proposal_id=p.id, plan_hash=p.plan_hash, message=msg)
        if outcome == "healthy":
            return self._finish(p, Status.APPLYING, Status.APPLIED, actor, f"rollout healthy: {msg}",
                                stage="verify")
        if outcome == "killed" and self.cfg.killswitch.on_engage_during_apply == "freeze":
            return self._finish(p, Status.APPLYING, Status.FROZEN, actor,
                                f"kill switch engaged mid-rollout; frozen as-is ({msg}). "
                                f"Run `safechange rollback {p.id}` or investigate.", stage="verify")
        return self._rollback(p, Status.APPLYING, actor, reason=f"{outcome}: {msg}")

    def _wait_rollout(self, p: Proposal, watch_kill: bool) -> tuple[str, str]:
        deadline = self.clock.now() + self.cfg.rollout_timeout_seconds
        last = "no status yet"
        while True:
            if watch_kill:
                ks = killswitch.soft_state(self.cfg, self.b.reader)
                if ks.engaged:
                    return "killed", "; ".join(ks.reasons)
            try:
                st = self.b.reader.rollout_status(p.target)
                if st.message != last:
                    self.log(f"  rollout: {st.message}")
                last = st.message
                if st.ready:
                    return "healthy", st.message
                if st.failed:
                    return "unhealthy", st.message
            except BackendError as e:
                if f"status error: {e}" != last:
                    self.log(f"  rollout: status error: {e}")
                last = f"status error: {e}"
            if self.clock.now() >= deadline:
                return "unhealthy", f"timed out after {self.cfg.rollout_timeout_seconds:g}s: {last}"
            self.clock.sleep(self.cfg.rollout_poll_seconds)

    # ------------------------------------------------------------------ rollback
    def rollback(self, pid: str, actor: str) -> Proposal:
        """Human-initiated rollback of a frozen or applied proposal."""
        p = self.store.get(pid)
        if actor not in self.cfg.approval.approvers:
            raise PipelineError(f"{actor!r} is not in the approvers list; cannot roll back")
        if p.status not in (Status.FROZEN, Status.APPLIED):
            raise PipelineError(f"proposal is {p.status}; only frozen/applied can be rolled back")
        return self._rollback(p, p.status, actor, reason=f"manual rollback by {actor}")

    def _rollback(self, p: Proposal, frm: Status, actor: str, reason: str) -> Proposal:
        self.log(f"  rolling back: {reason}")
        last_err = ""
        for _attempt in range(5):
            try:
                live = self.b.reader.get(p.target)
                if jp.normalize(live) == p.before:
                    break  # already at 'before'
                if jp.normalize(live) != p.after:
                    changed = [o["path"] for o in jp.diff(p.after, jp.normalize(live))]
                    return self._finish(p, frm, Status.FAILED, actor,
                                        f"ROLLBACK REFUSED: live object no longer matches the applied "
                                        f"state (changed: {changed}); manual intervention required. "
                                        f"Snapshot: {self.cfg.snapshot_dir / (p.id + '.json')}",
                                        stage="rollback", loud=True)
                rv = str(live["metadata"]["resourceVersion"])
                self.b.applier.patch_json(
                    p.target, [{"op": "test", "path": "/metadata/resourceVersion", "value": rv},
                               *p.rollback_patch])
                break
            except ConflictError as e:  # status updates bump resourceVersion; re-read and retry
                last_err = str(e)
                continue
            except Exception as e:
                return self._finish(p, frm, Status.FAILED, actor,
                                    f"ROLLBACK FAILED: {type(e).__name__}: {e}. Cluster may be in the "
                                    f"NEW state. Snapshot: {self.cfg.snapshot_dir / (p.id + '.json')}",
                                    stage="rollback", loud=True)
        else:
            return self._finish(p, frm, Status.FAILED, actor,
                                f"ROLLBACK FAILED after retries (conflicts): {last_err}",
                                stage="rollback", loud=True)
        self.audit.append("rollback", actor, "patched", proposal_id=p.id, plan_hash=p.plan_hash,
                          reason=reason, rollback_patch=p.rollback_patch)
        outcome, msg = self._wait_rollout(p, watch_kill=False)
        try:
            restored = jp.normalize(self.b.reader.get(p.target)) == p.before
        except Exception as e:
            restored, msg = False, f"{msg}; could not re-read: {e}"
        if outcome == "healthy" and restored:
            return self._finish(p, frm, Status.ROLLED_BACK, actor,
                                f"rolled back ({reason}); before-state restored and healthy",
                                stage="rollback")
        return self._finish(p, frm, Status.FAILED, actor,
                            f"ROLLBACK VERIFY FAILED: healthy={outcome == 'healthy'} restored={restored} "
                            f"({msg})", stage="rollback", loud=True)

    # ------------------------------------------------------------------ helpers
    def _finish(self, p: Proposal, frm: Status, to: Status, actor: str, note: str, *,
                stage: str, loud: bool = False) -> Proposal:
        self.store.transition(p.id, frm, to, note=note)
        self.audit.append(stage, actor, to.value, proposal_id=p.id, plan_hash=p.plan_hash,
                          note=note, severity="CRITICAL" if loud else "info")
        if loud:
            self.log(f"!!! {note}")
        return self.store.get(p.id)
