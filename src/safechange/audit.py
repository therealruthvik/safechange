"""Append-only, hash-chained JSONL audit log.

Each entry stores `prev_hash` (the previous entry's `hash`) and its own `hash` =
sha256(canonical JSON of the entry without `hash`). Editing, deleting or reordering any
entry breaks the chain. A sidecar `.head` file records the last seq/hash so truncation of
the tail is detected too. (A determined attacker with write access can rewrite both; in
production ship entries to WORM storage / a remote log as they are written.)
"""
from __future__ import annotations

import fcntl
import getpass
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .patch import canonical_json, sha256_json

GENESIS = "0" * 64


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    entries: int
    problems: list[str]


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.head_path = path.with_suffix(path.suffix + ".head")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)

    def append(self, stage: str, actor: str, result: str, *, proposal_id: str | None = None,
               plan_hash: str | None = None, **details: Any) -> dict[str, Any]:
        with open(self.path, "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                fh.seek(0)
                last = None
                for line in fh:
                    if line.strip():
                        last = line
                prev = json.loads(last) if last else None
                entry: dict[str, Any] = {
                    "seq": (prev["seq"] + 1) if prev else 0,
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "stage": stage,
                    "actor": actor,
                    "os_user": _os_user(),
                    "proposal_id": proposal_id,
                    "plan_hash": plan_hash,
                    "result": result,
                    "details": details,
                    "prev_hash": prev["hash"] if prev else GENESIS,
                }
                entry["hash"] = sha256_json(entry)
                fh.seek(0, os.SEEK_END)
                fh.write(canonical_json(entry) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
                self.head_path.write_text(f"{entry['seq']} {entry['hash']}\n")
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
        return entry

    def entries(self) -> list[dict[str, Any]]:
        return [json.loads(l) for l in self.path.read_text().splitlines() if l.strip()]

    def verify(self) -> VerifyResult:
        problems: list[str] = []
        prev_hash, count = GENESIS, 0
        for lineno, line in enumerate(self.path.read_text().splitlines(), 1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                problems.append(f"line {lineno}: not valid JSON")
                break
            claimed = entry.pop("hash", None)
            if entry.get("seq") != count:
                problems.append(f"line {lineno}: seq {entry.get('seq')} != expected {count}")
            if entry.get("prev_hash") != prev_hash:
                problems.append(f"line {lineno}: prev_hash does not link to previous entry")
            if sha256_json(entry) != claimed:
                problems.append(f"line {lineno}: content hash mismatch (entry modified)")
            prev_hash, count = claimed or "", count + 1
        if self.head_path.exists():
            head = self.head_path.read_text().split()
            if len(head) != 2 or head != [str(count - 1), prev_hash]:
                problems.append("tail does not match .head anchor (entries truncated or appended out of band)")
        elif count:
            problems.append(".head anchor missing")
        return VerifyResult(ok=not problems, entries=count, problems=problems)


def _os_user() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"
