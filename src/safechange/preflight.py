"""Rollback-readiness / preflight checks run immediately before apply.

All checks run (so the operator sees the full picture) and each fails closed. If any
fails, the proposal is `aborted` and the cluster is untouched.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import approval, killswitch, policy
from . import patch as jp
from .backends.base import Backends
from .config import Config
from .models import CheckResult, Proposal


@dataclass
class PreflightOutcome:
    results: list[CheckResult]
    live: dict[str, Any] | None
    snapshot_path: Path | None

    @property
    def ok(self) -> bool:
        return bool(self.results) and all(r.passed for r in self.results)


def write_snapshot(directory: Path, pid: str, obj: dict[str, Any]) -> tuple[Path, bool, str]:
    """Write the live object to disk, fsync, read it back and compare byte-for-byte + value."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{pid}.json"
    data = json.dumps(obj, indent=2, sort_keys=True)
    with open(path, "w") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    back = path.read_text()
    if back != data or json.loads(back) != obj:
        return path, False, "snapshot read-back differs from live object"
    return path, True, f"snapshot written and verified: {path}"


def run(cfg: Config, b: Backends, p: Proposal, now: float,
        snapshot_writer: Callable[[Path, str, dict[str, Any]], tuple[Path, bool, str]] = write_snapshot,
        ) -> PreflightOutcome:
    results: list[CheckResult] = []
    live: dict[str, Any] | None = None
    snap: Path | None = None

    def check(name: str, fn: Callable[[], tuple[bool, str]]) -> None:
        try:
            ok, detail = fn()
        except Exception as e:  # fail closed
            ok, detail = False, f"check error (fail closed): {type(e).__name__}: {e}"
        results.append(CheckResult(name, bool(ok), str(detail)))

    def kill() -> tuple[bool, str]:
        st = killswitch.soft_state(cfg, b.reader)
        return not st.engaged, "; ".join(st.reasons) or "kill switch disengaged"

    def quorum() -> tuple[bool, str]:
        ok, problems = approval.valid_approvals(p, cfg.approval, cfg.agent_identity, now)
        need = cfg.approval.required_approvals
        detail = f"{len(ok)}/{need} valid approvals ({', '.join(a.approver for a in ok) or 'none'})"
        if problems:
            detail += "; invalid: " + "; ".join(problems)
        return len(ok) >= need, detail

    def plan_integrity() -> tuple[bool, str]:
        h = approval.plan_hash(p.target, p.before, p.after)
        if h != p.plan_hash:
            return False, "stored plan does not match its plan hash (tampered?)"
        if jp.apply_patch(p.before, p.patch) != p.after:
            return False, "stored patch does not produce stored 'after'"
        return True, f"plan hash {h[:12]} verified"

    def guardrails() -> tuple[bool, str]:
        res = policy.evaluate(cfg.policy, p.target, p.before, p.after, p.patch, b.applier)
        bad = [r.name for r in res if not r.passed]
        return not bad, f"failed: {bad}" if bad else f"{len(res)} guardrails re-checked"

    def drift() -> tuple[bool, str]:
        nonlocal live
        live = b.reader.get(p.target)
        live_hash, before_hash = jp.sha256_json(jp.normalize(live)), jp.sha256_json(p.before)
        if live_hash != before_hash:
            changed = [o["path"] for o in jp.diff(p.before, jp.normalize(live))]
            return False, f"live object drifted since proposal; changed paths: {changed}"
        return True, f"live matches proposal 'before' (sha256 {live_hash[:12]})"

    def snapshot() -> tuple[bool, str]:
        nonlocal snap
        if live is None:
            return False, "no live object read"
        snap, ok, detail = snapshot_writer(cfg.snapshot_dir, p.id, live)
        if ok and jp.normalize(json.loads(snap.read_text())) != p.before:
            return False, "snapshot does not normalize to proposal 'before'"
        return ok, detail

    def rollback_ready() -> tuple[bool, str]:
        if not p.rollback_patch:
            return False, "empty rollback patch"
        if jp.apply_patch(p.after, p.rollback_patch) != p.before:
            return False, "rollback patch does not reconstruct 'before' from 'after'"
        allowed = cfg.policy.allowed_paths
        outside = [o["path"] for o in p.rollback_patch
                   if not any(policy.path_matches(pat, o["path"]) for pat in allowed)]
        if outside:
            return False, f"rollback would touch non-allowlisted paths {outside}"
        return True, f"rollback ({len(p.rollback_patch)} ops) reconstructs 'before' exactly"

    def can_patch() -> tuple[bool, str]:
        ok = b.applier.can_i_patch(p.target)
        return ok, f"{b.applier.identity} can-i patch {p.target}: {'yes' if ok else 'no'}"

    check("kill_switch_disengaged", kill)
    check("approval_quorum_and_ttl", quorum)
    check("plan_hash_integrity", plan_integrity)
    check("guardrails_recheck", guardrails)
    check("no_drift_since_proposal", drift)
    check("snapshot_roundtrip", snapshot)
    check("rollback_reconstructs_before", rollback_ready)
    check("applier_can_patch", can_patch)
    return PreflightOutcome(results, live, snap)
