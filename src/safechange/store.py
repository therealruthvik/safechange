"""SQLite proposal store. Status changes are compare-and-set against the state machine,
so two concurrent `apply` runs cannot both claim the same proposal."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from .models import TRANSITIONS, Approval, CheckResult, Proposal, Status, Target

SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
  id TEXT PRIMARY KEY,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  status TEXT NOT NULL,
  task TEXT NOT NULL,
  proposer TEXT NOT NULL,
  agent_identity TEXT NOT NULL,
  target TEXT NOT NULL,
  before TEXT NOT NULL,
  after TEXT NOT NULL,
  before_rv TEXT NOT NULL,
  patch TEXT NOT NULL,
  rollback_patch TEXT NOT NULL,
  rationale TEXT NOT NULL,
  plan_hash TEXT NOT NULL,
  policy_results TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS approvals (
  proposal_id TEXT NOT NULL REFERENCES proposals(id),
  approver TEXT NOT NULL,
  plan_hash TEXT NOT NULL,
  approved_at REAL NOT NULL,
  PRIMARY KEY (proposal_id, approver)
);
"""


class StoreError(Exception):
    pass


class TransitionError(StoreError):
    pass


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex[:10]

    def insert(self, p: Proposal) -> None:
        self.db.execute(
            "INSERT INTO proposals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (p.id, p.created_at, p.created_at, p.status.value, p.task, p.proposer,
             p.agent_identity, json.dumps(p.target.to_dict()), json.dumps(p.before),
             json.dumps(p.after), p.before_resource_version, json.dumps(p.patch),
             json.dumps(p.rollback_patch), p.rationale, p.plan_hash,
             json.dumps([r.to_dict() for r in p.policy_results]), p.note))

    def get(self, pid: str) -> Proposal:
        row = self.db.execute("SELECT * FROM proposals WHERE id = ?", (pid,)).fetchone()
        if row is None:
            raise StoreError(f"proposal {pid!r} not found")
        return self._row(row)

    def list(self, limit: int = 50) -> list[Proposal]:
        rows = self.db.execute(
            "SELECT * FROM proposals ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(r) for r in rows]

    def _row(self, r: sqlite3.Row) -> Proposal:
        approvals = [Approval(a["approver"], a["plan_hash"], a["approved_at"]) for a in
                     self.db.execute("SELECT * FROM approvals WHERE proposal_id = ? "
                                     "ORDER BY approved_at", (r["id"],)).fetchall()]
        return Proposal(
            id=r["id"], created_at=r["created_at"], updated_at=r["updated_at"],
            status=Status(r["status"]), task=r["task"], proposer=r["proposer"],
            agent_identity=r["agent_identity"], target=Target.from_dict(json.loads(r["target"])),
            before=json.loads(r["before"]), after=json.loads(r["after"]),
            before_resource_version=r["before_rv"], patch=json.loads(r["patch"]),
            rollback_patch=json.loads(r["rollback_patch"]), rationale=r["rationale"],
            plan_hash=r["plan_hash"],
            policy_results=[CheckResult(**d) for d in json.loads(r["policy_results"])],
            approvals=approvals, note=r["note"])

    def transition(self, pid: str, frm: Status, to: Status, note: str | None = None) -> None:
        if to not in TRANSITIONS[frm]:
            raise TransitionError(f"illegal transition {frm} -> {to}")
        sets, args = "status = ?, updated_at = ?", [to.value, time.time()]
        if note is not None:
            sets += ", note = ?"
            args.append(note)
        cur = self.db.execute(f"UPDATE proposals SET {sets} WHERE id = ? AND status = ?",
                              (*args, pid, frm.value))
        if cur.rowcount != 1:
            actual = self.get(pid).status
            raise TransitionError(f"proposal {pid} is {actual}, expected {frm}")

    def add_approval(self, pid: str, approver: str, plan_hash: str, at: float) -> None:
        try:
            self.db.execute("INSERT INTO approvals VALUES (?,?,?,?)", (pid, approver, plan_hash, at))
        except sqlite3.IntegrityError as e:
            raise StoreError(f"{approver} has already approved {pid}") from e

    # test hook: simulate tampering with stored data
    def _raw_update(self, pid: str, column: str, value: Any) -> None:
        self.db.execute(f"UPDATE proposals SET {column} = ? WHERE id = ?", (value, pid))
