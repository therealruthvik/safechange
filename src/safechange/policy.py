"""Guardrails. Every check is independent and fails closed: an exception inside a check
is a failed check, never a pass. A proposal with any failed check is `policy_failed`
and can never be approved.
"""
from __future__ import annotations

from typing import Any, Callable

from . import patch as jp
from .backends.base import ClusterApplier
from .config import PolicyConfig
from .models import CheckResult, Target


# --------------------------------------------------------------------------- helpers
def path_matches(pattern: str, path: str) -> bool:
    """'*' matches exactly one segment; a trailing '**' matches zero or more segments."""
    pt, pp = jp.parse_pointer(pattern), jp.parse_pointer(path)
    if pt and pt[-1] == "**":
        head = pt[:-1]
        return len(pp) >= len(head) and all(a in ("*", b) for a, b in zip(head, pp))
    return len(pt) == len(pp) and all(a in ("*", b) for a, b in zip(pt, pp))


def normalize_image(image: str) -> tuple[str, str | None, str | None]:
    """Return (repository-with-registry, tag, digest). Docker Hub shorthand is expanded."""
    ref, digest = image, None
    if "@" in image:
        ref, digest = image.split("@", 1)
    last = ref.rsplit("/", 1)[-1]
    tag = None
    if ":" in last:
        ref, tag = ref.rsplit(":", 1)
    parts = ref.split("/")
    if len(parts) == 1:
        ref = f"docker.io/library/{ref}"
    elif not ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        ref = f"docker.io/{ref}"
    return ref, tag, digest


def pod_spec(obj: dict[str, Any]) -> dict[str, Any]:
    return obj["spec"]["template"]["spec"]


def all_containers(obj: dict[str, Any]) -> list[dict[str, Any]]:
    spec = pod_spec(obj)
    return list(spec.get("containers") or []) + list(spec.get("initContainers") or [])


# --------------------------------------------------------------------------- checks
Check = Callable[[], tuple[bool, str]]


def evaluate(cfg: PolicyConfig, target: Target, before: dict[str, Any], after: dict[str, Any],
             ops: list[dict[str, Any]], applier: ClusterApplier | None,
             prior_ok: bool = True) -> list[CheckResult]:
    """Run every guardrail. `prior_ok=False` (an earlier structural check failed) also
    suppresses the server dry-run."""
    def kind_allowed() -> tuple[bool, str]:
        return target.kind in cfg.allowed_kinds, f"kind={target.kind} allowed={list(cfg.allowed_kinds)}"

    def namespace_allowed() -> tuple[bool, str]:
        return (target.namespace in cfg.allowed_namespaces,
                f"namespace={target.namespace} allowed={list(cfg.allowed_namespaces)}")

    def identity_unchanged() -> tuple[bool, str]:
        ok = all(o.get("kind") == target.kind and o["metadata"].get("name") == target.name
                 and o["metadata"].get("namespace") == target.namespace for o in (before, after))
        return ok, "object kind/name/namespace match target before and after"

    def ops_allowed() -> tuple[bool, str]:
        bad = [o.get("op") for o in ops if o.get("op") not in cfg.allowed_ops]
        return not bad, f"disallowed ops: {bad}" if bad else f"ops in {list(cfg.allowed_ops)}"

    def patch_size() -> tuple[bool, str]:
        n = len(ops)
        return 1 <= n <= cfg.max_patch_ops, f"{n} ops (allowed 1..{cfg.max_patch_ops})"

    def paths_allowlisted() -> tuple[bool, str]:
        denied = [o.get("path") for o in ops
                  if not any(path_matches(p, str(o.get("path"))) for p in cfg.allowed_paths)]
        return not denied, f"denied paths: {denied}" if denied else "all paths allowlisted"

    def replica_bounds() -> tuple[bool, str]:
        r = after["spec"].get("replicas", 1)
        ok = isinstance(r, int) and not isinstance(r, bool) and cfg.min_replicas <= r <= cfg.max_replicas
        return ok, f"replicas={r} bounds=[{cfg.min_replicas},{cfg.max_replicas}]"

    def replica_change() -> tuple[bool, str]:
        a, b = before["spec"].get("replicas", 1), after["spec"].get("replicas", 1)
        pct = abs(b - a) / max(a, 1) * 100
        return pct <= cfg.max_replica_change_pct, \
            f"{a} -> {b} = {pct:.0f}% (max {cfg.max_replica_change_pct:.0f}%)"

    def registry_allowlist() -> tuple[bool, str]:
        bad = []
        for c in all_containers(after):
            repo, _, _ = normalize_image(str(c["image"]))
            if not any(repo == r or repo.startswith(r.rstrip("/") + "/") for r in cfg.allowed_registries):
                bad.append(c["image"])
        return not bad, f"images outside registry allowlist: {bad}" if bad else "registries ok"

    def no_latest() -> tuple[bool, str]:
        bad = []
        for c in all_containers(after):
            _, tag, digest = normalize_image(str(c["image"]))
            if digest is None and (tag is None or tag == "latest"):
                bad.append(c["image"])
        return not bad, f"unpinned/latest images: {bad}" if bad else "all images pinned"

    def limits_set() -> tuple[bool, str]:
        bad = []
        for c in pod_spec(after).get("containers") or []:
            limits = (c.get("resources") or {}).get("limits") or {}
            missing = [r for r in cfg.require_resource_limits if not limits.get(r)]
            if missing:
                bad.append(f"{c.get('name')}:{missing}")
        return not bad, f"missing limits: {bad}" if bad else "limits present"

    def no_privilege() -> tuple[bool, str]:
        spec = pod_spec(after)
        problems = []
        for flag in ("hostNetwork", "hostPID", "hostIPC"):
            if spec.get(flag):
                problems.append(flag)
        psc = spec.get("securityContext") or {}
        if psc.get("runAsUser") == 0 or psc.get("runAsNonRoot") is False:
            problems.append("pod runs as root")
        for c in all_containers(after):
            sc = c.get("securityContext") or {}
            if sc.get("privileged"):
                problems.append(f"{c.get('name')}: privileged")
            if sc.get("allowPrivilegeEscalation") is True:
                problems.append(f"{c.get('name')}: allowPrivilegeEscalation")
            if sc.get("runAsUser") == 0 or sc.get("runAsNonRoot") is False:
                problems.append(f"{c.get('name')}: runs as root")
        return not problems, f"violations: {problems}" if problems else "no privileged settings"

    def server_dry_run() -> tuple[bool, str]:
        if not cfg.server_dry_run:
            return True, "skipped by config"
        if applier is None:
            return False, "no applier available for dry-run (fail closed)"
        return applier.dry_run_apply(dry_run_body(after))

    checks: list[tuple[str, Check]] = [
        ("kind_allowed", kind_allowed),
        ("namespace_allowed", namespace_allowed),
        ("target_identity_unchanged", identity_unchanged),
        ("ops_allowed", ops_allowed),
        ("patch_size", patch_size),
        ("paths_allowlisted", paths_allowlisted),
        ("replica_bounds", replica_bounds),
        ("replica_change_pct", replica_change),
        ("image_registry_allowlist", registry_allowlist),
        ("image_not_latest", no_latest),
        ("resource_limits_set", limits_set),
        ("no_privileged_or_host_access", no_privilege),
    ]
    results = [_run(name, fn) for name, fn in checks]
    # Only ask the API server once every static check passes: never send a known-bad object.
    if prior_ok and all(r.passed for r in results):
        results.append(_run("server_dry_run", server_dry_run))
    else:
        results.append(CheckResult("server_dry_run", False, "not attempted: static checks failed"))
    return results


def dry_run_body(obj: dict[str, Any]) -> dict[str, Any]:
    body = jp.normalize(obj)
    body.setdefault("apiVersion", "apps/v1")
    return body


def _run(name: str, fn: Check) -> CheckResult:
    try:
        ok, detail = fn()
        return CheckResult(name, bool(ok), str(detail))
    except Exception as e:  # fail closed: any error is a failed check
        return CheckResult(name, False, f"check error (fail closed): {type(e).__name__}: {e}")


def passed(results: list[CheckResult]) -> bool:
    return bool(results) and all(r.passed for r in results)
