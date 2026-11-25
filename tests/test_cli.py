import json

import pytest

from safechange.cli import main


@pytest.fixture
def cli(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("SAFECHANGE_KILL", raising=False)
    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"state_dir: {tmp_path / 'state'}\nbackend: fake\n"
                   "rollout: {timeout_seconds: 1, poll_seconds: 0.01}\n")

    def run(*args):
        code = main(["--config", str(cfg), *args])
        out = capsys.readouterr()
        return code, out.out, out.err
    return run


def test_cli_end_to_end(cli):
    code, out, _ = cli("propose", "scale web to 4", "--id-only")
    assert code == 0
    pid = out.strip()
    code, out, _ = cli("show", pid, "--json")
    ph = json.loads(out)["plan_hash"]
    assert cli("approve", pid, "--plan-hash", ph[:8], "--as", "safechange-agent")[0] == 2
    assert cli("approve", pid, "--plan-hash", "deadbeef", "--as", "alice")[0] == 2
    assert cli("approve", pid, "--plan-hash", ph[:8], "--as", "alice")[0] == 0
    code, out, _ = cli("apply", pid, "--as", "alice")
    assert code == 0 and "applied" in out
    code, out, _ = cli("show", pid)
    assert "plan hash" in out and "+  replicas: 4" in out
    assert cli("audit", "verify")[0] == 0
    assert "applied=1" in cli("status")[1]


def test_cli_kill_and_unkill(cli):
    assert cli("kill", "--hard", "--as", "alice")[0] == 0
    code, _, err = cli("propose", "scale web to 3")
    assert code == 2 and "REFUSED" in err
    _, out, _ = cli("status")
    assert "ENGAGED" in out and "can-i patch: no" in out
    assert cli("unkill", "--as", "safechange-agent")[0] == 2
    assert cli("unkill", "--as", "alice")[0] == 0
    assert cli("propose", "scale web to 3")[0] == 0


def test_cli_policy_denial_exit_code(cli, tmp_path):
    patch = tmp_path / "p.json"
    patch.write_text(json.dumps([{"op": "add", "path": "/spec/template/spec/containers/0/securityContext",
                                  "value": {"privileged": True}}]))
    code, out, _ = cli("propose", "--patch-file", str(patch), "--target", "deployment/web")
    assert code == 2 and "policy_failed" in out and "FAIL" in out
