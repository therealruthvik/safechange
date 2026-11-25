import json

from safechange.audit import AuditLog


def build(tmp_path, n=5):
    log = AuditLog(tmp_path / "audit.jsonl")
    for i in range(n):
        log.append("stage", "actor", "ok", proposal_id=f"p{i}", plan_hash="h" * 64, i=i)
    return log


def lines(log):
    return log.path.read_text().splitlines()


def test_chain_ok(tmp_path):
    log = build(tmp_path)
    r = log.verify()
    assert r.ok and r.entries == 5
    e = log.entries()
    assert e[0]["prev_hash"] == "0" * 64
    assert all(e[i]["prev_hash"] == e[i - 1]["hash"] for i in range(1, 5))


def test_modified_entry_detected(tmp_path):
    log = build(tmp_path)
    ls = lines(log)
    e = json.loads(ls[2])
    e["result"] = "approved"
    ls[2] = json.dumps(e)
    log.path.write_text("\n".join(ls) + "\n")
    r = log.verify()
    assert not r.ok and any("line 3" in p and "hash mismatch" in p for p in r.problems)


def test_rehashed_entry_breaks_link(tmp_path):
    from safechange.patch import sha256_json
    log = build(tmp_path)
    ls = lines(log)
    e = json.loads(ls[1])
    e["actor"] = "mallory"
    e.pop("hash")
    e["hash"] = sha256_json(e)  # attacker recomputes this entry's hash...
    ls[1] = json.dumps(e)
    log.path.write_text("\n".join(ls) + "\n")
    r = log.verify()
    assert not r.ok and any("line 3" in p and "prev_hash" in p for p in r.problems)


def test_deleted_entry_detected(tmp_path):
    log = build(tmp_path)
    ls = lines(log)
    del ls[1]
    log.path.write_text("\n".join(ls) + "\n")
    assert not log.verify().ok


def test_reordered_entries_detected(tmp_path):
    log = build(tmp_path)
    ls = lines(log)
    ls[1], ls[2] = ls[2], ls[1]
    log.path.write_text("\n".join(ls) + "\n")
    assert not log.verify().ok


def test_truncated_tail_detected(tmp_path):
    log = build(tmp_path)
    ls = lines(log)
    log.path.write_text("\n".join(ls[:-1]) + "\n")
    r = log.verify()
    assert not r.ok and any("anchor" in p for p in r.problems)


def test_garbage_line_detected(tmp_path):
    log = build(tmp_path)
    with open(log.path, "a") as fh:
        fh.write("{not json\n")
    assert not log.verify().ok


def test_pipeline_writes_verifiable_chain(env):
    p = env.approved("scale web to 3")
    env.pipeline.apply(p.id, "ops")
    assert env.audit.verify().ok
    for e in env.audit.entries():
        assert {"stage", "actor", "result", "prev_hash", "hash", "ts"} <= e.keys()
    assert {e["plan_hash"] for e in env.audit.entries() if e["proposal_id"]} == {p.plan_hash}
