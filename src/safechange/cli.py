"""safechange command-line interface."""
from __future__ import annotations

import argparse
import difflib
import getpass
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from . import approval, config as config_mod, killswitch
from .audit import AuditLog
from .backends import build_backends
from .models import Proposal, Status, Target
from .pipeline import KillSwitchEngaged, Pipeline, PipelineError
from .proposer import LLMProposer, ManualProposer, Proposer, ProposerError, RulesProposer
from .store import Store, StoreError

EXIT_OK, EXIT_ERROR, EXIT_REFUSED = 0, 1, 2

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None or bool(os.environ.get("FORCE_COLOR"))


def c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


RED, GREEN, YELLOW, CYAN, BOLD, DIM = "31", "32", "33", "36", "1", "2"

STATUS_COLOR = {
    Status.PENDING_APPROVAL: YELLOW, Status.APPROVED: CYAN, Status.APPLYING: CYAN,
    Status.APPLIED: GREEN, Status.ROLLED_BACK: YELLOW, Status.FROZEN: YELLOW,
    Status.POLICY_FAILED: RED, Status.REJECTED: RED, Status.ABORTED: RED, Status.FAILED: RED,
}


def fmt_status(s: Status) -> str:
    return c(s.value, STATUS_COLOR.get(s, BOLD))


# --------------------------------------------------------------------------- context
class Ctx:
    def __init__(self, args: argparse.Namespace) -> None:
        path = args.config or os.environ.get("SAFECHANGE_CONFIG")
        if path is None and Path("safechange.yaml").exists():
            path = "safechange.yaml"
        overrides: dict[str, Any] = {}
        if args.backend:
            overrides["backend"] = args.backend
        self.cfg = config_mod.load(path, overrides)
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        self.backends = build_backends(self.cfg)
        self.store = Store(self.cfg.db_path)
        self.audit = AuditLog(self.cfg.audit_path)
        self.pipeline = Pipeline(self.cfg, self.backends, self.store, self.audit, log=print)


def default_actor() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


# --------------------------------------------------------------------------- rendering
def colored_diff(before: dict[str, Any], after: dict[str, Any], name: str) -> str:
    a = yaml.safe_dump(before, sort_keys=True).splitlines(keepends=True)
    b = yaml.safe_dump(after, sort_keys=True).splitlines(keepends=True)
    out = []
    for line in difflib.unified_diff(a, b, f"{name} (live, before)", f"{name} (proposed, after)", n=3):
        line = line.rstrip("\n")
        if line.startswith(("+++", "---")):
            out.append(c(line, BOLD))
        elif line.startswith("+"):
            out.append(c(line, GREEN))
        elif line.startswith("-"):
            out.append(c(line, RED))
        elif line.startswith("@@"):
            out.append(c(line, CYAN))
        else:
            out.append(line)
    return "\n".join(out)


def render(p: Proposal, cfg: config_mod.Config) -> str:
    lines = [
        f"{c('Proposal', BOLD)} {p.id}   status: {fmt_status(p.status)}",
        f"  target:     {p.target}",
        f"  task:       {p.task}",
        f"  proposer:   {p.proposer} (identity {p.agent_identity})",
        f"  created:    {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(p.created_at))}",
        f"  plan hash:  {c(p.plan_hash[:8], BOLD + ';' + YELLOW)}{p.plan_hash[8:]}",
        f"  rationale:  {p.rationale}",
    ]
    if p.note:
        lines.append(f"  note:       {p.note}")
    lines += ["", c("JSON Patch:", BOLD), json.dumps(p.patch, indent=2),
              "", c("Rollback patch:", BOLD), json.dumps(p.rollback_patch, indent=2),
              "", c("Diff:", BOLD), colored_diff(p.before, p.after, str(p.target)) or "(no change)",
              "", c("Policy checks:", BOLD)]
    for r in p.policy_results:
        mark = c("PASS", GREEN) if r.passed else c("FAIL", RED)
        lines.append(f"  [{mark}] {r.name:42s} {c(r.detail, DIM)}")
    lines += ["", c("Approvals:", BOLD)]
    valid, problems = approval.valid_approvals(p, cfg.approval, cfg.agent_identity, time.time())
    for a in p.approvals:
        age = (time.time() - a.approved_at) / 60
        lines.append(f"  {a.approver:10s} {age:5.1f} min ago  {'valid' if a in valid else 'INVALID'}")
    for prob in problems:
        lines.append(f"  {c('!', RED)} {prob}")
    lines.append(f"  {len(valid)}/{cfg.approval.required_approvals} required, ttl {cfg.approval.ttl_minutes:g} min")
    if p.status == Status.PENDING_APPROVAL:
        lines += ["", c("To approve, review the diff above, then run:", BOLD),
                  f"  safechange approve {p.id} --plan-hash <first 8 chars of plan hash> --as <you>"]
    return "\n".join(lines)


# --------------------------------------------------------------------------- commands
def cmd_propose(ctx: Ctx, a: argparse.Namespace) -> int:
    proposer: Proposer
    kind = a.proposer or ("manual" if a.patch_file else ctx.cfg.proposer_default)
    if kind == "manual":
        if not (a.patch_file and a.target):
            print("manual proposer needs --patch-file and --target deployment/<name>", file=sys.stderr)
            return EXIT_ERROR
        ops = json.loads(Path(a.patch_file).read_text())
        name = a.target.split("/", 1)[-1]
        proposer = ManualProposer(Target("Deployment", a.namespace, name), ops,
                                  a.rationale or f"manual patch from {a.patch_file}")
    elif kind == "llm":
        proposer = LLMProposer(ctx.cfg.llm_model, ctx.cfg.llm_effort)
    else:
        proposer = RulesProposer()
    task = a.task or f"apply {a.patch_file}"
    try:
        p = ctx.pipeline.propose(task, proposer, a.namespace)
    except KillSwitchEngaged as e:
        print(c(f"REFUSED: {e}", RED), file=sys.stderr)
        return EXIT_REFUSED
    except (PipelineError, ProposerError) as e:
        print(c(f"error: {e}", RED), file=sys.stderr)
        return EXIT_ERROR
    if a.id_only:
        print(p.id)
    else:
        print(render(p, ctx.cfg))
    return EXIT_OK if p.status == Status.PENDING_APPROVAL else EXIT_REFUSED


def cmd_list(ctx: Ctx, a: argparse.Namespace) -> int:
    rows = ctx.store.list(a.limit)
    if not rows:
        print("no proposals")
    for p in rows:
        ts = time.strftime("%m-%d %H:%M:%S", time.localtime(p.created_at))
        print(f"{p.id}  {ts}  {fmt_status(p.status):28s} {p.plan_hash[:8]}  {str(p.target):28s} {p.task}")
    return EXIT_OK


def cmd_show(ctx: Ctx, a: argparse.Namespace) -> int:
    p = ctx.store.get(a.id)
    if a.json:
        d = {"id": p.id, "status": p.status.value, "plan_hash": p.plan_hash, "target": p.target.to_dict(),
             "task": p.task, "patch": p.patch, "rollback_patch": p.rollback_patch, "note": p.note,
             "policy_results": [r.to_dict() for r in p.policy_results],
             "approvals": [vars(x) for x in p.approvals]}
        print(json.dumps(d, indent=2))
    else:
        print(render(p, ctx.cfg))
    return EXIT_OK


def cmd_approve(ctx: Ctx, a: argparse.Namespace) -> int:
    try:
        p = ctx.pipeline.approve(a.id, a.as_, a.plan_hash)
    except (approval.ApprovalError, StoreError) as e:
        print(c(f"APPROVAL DENIED: {e}", RED), file=sys.stderr)
        return EXIT_REFUSED
    print(f"{a.as_} approved {p.id}; status now {fmt_status(p.status)} "
          f"({len(p.approvals)}/{ctx.cfg.approval.required_approvals}; expires in "
          f"{ctx.cfg.approval.ttl_minutes:g} min)")
    if p.status == Status.APPROVED:
        backend = " --backend fake" if getattr(a, "backend", None) == "fake" else ""
        print(c(f"Nothing has changed in the cluster yet. To apply, run:\n"
                f"  safechange{backend} apply {p.id} --as {a.as_}", YELLOW))
    else:
        missing = ctx.cfg.approval.required_approvals - len(p.approvals)
        print(f"Needs {missing} more approval(s) from a different approver before it can be applied.")
    return EXIT_OK


def cmd_reject(ctx: Ctx, a: argparse.Namespace) -> int:
    try:
        p = ctx.pipeline.reject(a.id, a.as_, a.reason)
    except StoreError as e:
        print(c(f"error: {e}", RED), file=sys.stderr)
        return EXIT_ERROR
    print(f"{p.id} {fmt_status(p.status)}")
    return EXIT_OK


def _report_terminal(p: Proposal) -> int:
    print(f"{p.id}: {fmt_status(p.status)}")
    if p.note:
        print(f"  {p.note}")
    return EXIT_OK if p.status in (Status.APPLIED, Status.ROLLED_BACK) else EXIT_REFUSED


def cmd_apply(ctx: Ctx, a: argparse.Namespace) -> int:
    try:
        print(f"preflight + apply {a.id} as {a.as_} ...")
        p = ctx.pipeline.apply(a.id, a.as_)
    except PipelineError as e:
        print(c(f"REFUSED: {e}", RED), file=sys.stderr)
        return EXIT_REFUSED
    if p.status == Status.ABORTED:
        for r in ctx.audit.entries()[::-1]:
            if r.get("proposal_id") == p.id and r["stage"] == "preflight":
                for res in r["details"].get("results", []):
                    mark = c("PASS", GREEN) if res["passed"] else c("FAIL", RED)
                    print(f"  [{mark}] {res['name']:32s} {c(res['detail'], DIM)}")
                break
    if p.status == Status.FAILED:
        print(c("=" * 72 + "\n!!! SAFECHANGE FAILURE: HUMAN ATTENTION REQUIRED !!!\n" + "=" * 72, RED))
    return _report_terminal(p)


def cmd_rollback(ctx: Ctx, a: argparse.Namespace) -> int:
    try:
        p = ctx.pipeline.rollback(a.id, a.as_)
    except PipelineError as e:
        print(c(f"REFUSED: {e}", RED), file=sys.stderr)
        return EXIT_REFUSED
    return _report_terminal(p)


def cmd_kill(ctx: Ctx, a: argparse.Namespace) -> int:
    try:
        done = killswitch.engage(ctx.cfg, ctx.backends.admin, hard=a.hard)
        result = "engaged"
    except killswitch.KillSwitchError as e:
        done, result = [str(e)], "partial"
    ctx.audit.append("killswitch", a.as_, result, hard=a.hard, actions=done)
    for d in done:
        print(c(f"KILL: {d}", RED))
    return EXIT_OK if result == "engaged" else EXIT_ERROR


def cmd_unkill(ctx: Ctx, a: argparse.Namespace) -> int:
    if a.as_ not in ctx.cfg.approval.approvers:
        print(c(f"REFUSED: {a.as_!r} is not an approver", RED), file=sys.stderr)
        return EXIT_REFUSED
    done = killswitch.disengage(ctx.cfg, ctx.backends.admin)
    ctx.audit.append("killswitch", a.as_, "disengaged", actions=done)
    for d in done:
        print(c(f"UNKILL: {d}", GREEN))
    return EXIT_OK


def cmd_status(ctx: Ctx, a: argparse.Namespace) -> int:
    st = killswitch.soft_state(ctx.cfg, ctx.backends.reader)
    print(c("Kill switch", BOLD))
    print(f"  soft layer: {c('ENGAGED', RED) if st.engaged else c('disengaged', GREEN)}")
    for r in st.reasons:
        print(f"    - {r}")
    probe = Target("Deployment", ctx.cfg.policy.allowed_namespaces[0], a.probe)
    for k, v in killswitch.hard_state(ctx.cfg, ctx.backends.applier, ctx.backends.admin, probe).items():
        print(f"  hard layer: {k}: {c(v, GREEN if v in ('yes', 'present') else RED)}")
    print(c("Config", BOLD))
    print(f"  backend={ctx.cfg.backend} state_dir={ctx.cfg.state_dir} "
          f"on_engage_during_apply={ctx.cfg.killswitch.on_engage_during_apply}")
    counts: dict[str, int] = {}
    for p in ctx.store.list(1000):
        counts[p.status.value] = counts.get(p.status.value, 0) + 1
    print(c("Proposals", BOLD))
    print("  " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none"))
    return EXIT_OK


def cmd_audit(ctx: Ctx, a: argparse.Namespace) -> int:
    if a.audit_cmd == "verify":
        res = ctx.audit.verify()
        if res.ok:
            print(c(f"audit chain OK: {res.entries} entries, hash-linked, head anchored", GREEN))
            return EXIT_OK
        print(c(f"AUDIT CHAIN BROKEN ({res.entries} entries read):", RED))
        for prob in res.problems:
            print(f"  - {prob}")
        return EXIT_REFUSED
    for e in ctx.audit.entries()[-a.limit:]:
        print(f"{e['seq']:4d} {e['ts']} {e['stage']:10s} {e['actor']:18s} {e['result']:22s} "
              f"{e.get('proposal_id') or '-':10s} {(e.get('plan_hash') or '')[:8]}")
    return EXIT_OK


# --------------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="safechange",
                                 description="Propose Kubernetes changes behind guardrails, "
                                             "human approval and rollback checks.")
    ap.add_argument("--config", help="config YAML (default: ./safechange.yaml or $SAFECHANGE_CONFIG)")
    ap.add_argument("--backend", choices=["fake", "kubectl"], help="override config backend")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("propose", help="propose a change (runs guardrails)")
    p.add_argument("task", nargs="?", help='e.g. "scale web to 5", "set image web=ghcr.io/org/web:1.4.2"')
    p.add_argument("-n", "--namespace", default="demo")
    p.add_argument("--proposer", choices=["rules", "llm", "manual"])
    p.add_argument("--patch-file", help="raw JSON Patch file (manual proposer)")
    p.add_argument("--target", help="deployment/<name> for --patch-file")
    p.add_argument("--rationale")
    p.add_argument("--id-only", action="store_true", help="print only the proposal id")
    p.set_defaults(fn=cmd_propose)

    p = sub.add_parser("list", help="list proposals")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("show", help="show diff, policy results and approvals")
    p.add_argument("id")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("approve", help="approve a proposal (type the plan hash)")
    p.add_argument("id")
    p.add_argument("--plan-hash", required=True, help="first >= 8 chars of the plan hash")
    p.add_argument("--as", dest="as_", required=True, help="your identity")
    p.set_defaults(fn=cmd_approve)

    p = sub.add_parser("reject", help="reject a proposal")
    p.add_argument("id")
    p.add_argument("--as", dest="as_", required=True)
    p.add_argument("--reason", default="")
    p.set_defaults(fn=cmd_reject)

    p = sub.add_parser("apply", help="preflight, apply and verify an approved proposal")
    p.add_argument("id")
    p.add_argument("--as", dest="as_", default=default_actor())
    p.set_defaults(fn=cmd_apply)

    p = sub.add_parser("rollback", help="roll back a frozen/applied proposal (approver only)")
    p.add_argument("id")
    p.add_argument("--as", dest="as_", required=True)
    p.set_defaults(fn=cmd_rollback)

    p = sub.add_parser("kill", help="engage the kill switch")
    p.add_argument("--hard", action="store_true", help="also delete the applier RoleBinding")
    p.add_argument("--as", dest="as_", default=default_actor())
    p.set_defaults(fn=cmd_kill)

    p = sub.add_parser("unkill", help="disengage all kill switch layers (approver only)")
    p.add_argument("--as", dest="as_", required=True)
    p.set_defaults(fn=cmd_unkill)

    p = sub.add_parser("status", help="kill switch layers and proposal counts")
    p.add_argument("--probe", default="web", help="deployment used for the can-i probe")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("audit", help="audit log")
    p.add_argument("audit_cmd", choices=["verify", "list"])
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(fn=cmd_audit)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        ctx = Ctx(args)
    except config_mod.ConfigError as e:
        print(c(f"config error: {e}", RED), file=sys.stderr)
        return EXIT_ERROR
    try:
        return int(args.fn(ctx, args))
    except StoreError as e:
        print(c(f"error: {e}", RED), file=sys.stderr)
        return EXIT_ERROR
    finally:
        ctx.store.close()


def run() -> int:
    """Console entry point: load ./.env (never overriding the real environment), then run."""
    from . import envfile
    envfile.load()
    return main()


if __name__ == "__main__":
    sys.exit(run())
