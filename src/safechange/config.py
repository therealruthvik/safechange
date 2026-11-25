"""Typed configuration loaded from YAML, layered over packaged defaults."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class PolicyConfig:
    allowed_kinds: tuple[str, ...]
    allowed_namespaces: tuple[str, ...]
    allowed_ops: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    max_patch_ops: int
    min_replicas: int
    max_replicas: int
    max_replica_change_pct: float
    allowed_registries: tuple[str, ...]
    require_resource_limits: tuple[str, ...]
    server_dry_run: bool


@dataclass(frozen=True)
class ApprovalConfig:
    approvers: tuple[str, ...]
    required_approvals: int
    ttl_minutes: float
    min_plan_hash_prefix: int


@dataclass(frozen=True)
class KillSwitchConfig:
    file: Path
    env_var: str
    configmap_namespace: str
    configmap_name: str
    configmap_key: str
    on_engage_during_apply: str


@dataclass(frozen=True)
class KubeConfig:
    reader_kubeconfig: str
    reader_context: str
    applier_kubeconfig: str
    applier_context: str
    admin_kubeconfig: str
    admin_context: str
    applier_rolebinding_namespace: str
    applier_rolebinding_name: str


@dataclass(frozen=True)
class Config:
    agent_identity: str
    state_dir: Path
    backend: str
    kube: KubeConfig
    fake_cluster_file: Path
    fake_ready_after_polls: int
    proposer_default: str
    llm_model: str
    llm_effort: str
    policy: PolicyConfig
    approval: ApprovalConfig
    killswitch: KillSwitchConfig
    rollout_timeout_seconds: float
    rollout_poll_seconds: float
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def db_path(self) -> Path:
        return self.state_dir / "safechange.db"

    @property
    def audit_path(self) -> Path:
        return self.state_dir / "audit.jsonl"

    @property
    def snapshot_dir(self) -> Path:
        return self.state_dir / "snapshots"


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def default_dict() -> dict[str, Any]:
    text = resources.files("safechange").joinpath("default_config.yaml").read_text()
    return yaml.safe_load(text)


def _under(base: Path, p: str) -> Path:
    path = Path(p).expanduser()
    return path if path.is_absolute() else base / path


def from_dict(over: dict[str, Any] | None = None) -> Config:
    d = _deep_merge(default_dict(), over or {})
    try:
        state_dir = Path(str(d["state_dir"])).expanduser().resolve()
        k, ks, p, a = d["kube"], d["killswitch"], d["policy"], d["approval"]
        cfg = Config(
            agent_identity=str(d["agent_identity"]),
            state_dir=state_dir,
            backend=str(d["backend"]),
            kube=KubeConfig(
                reader_kubeconfig=str(k["reader_kubeconfig"]),
                reader_context=str(k["reader_context"]),
                applier_kubeconfig=str(k["applier_kubeconfig"]),
                applier_context=str(k["applier_context"]),
                admin_kubeconfig=str(k.get("admin_kubeconfig") or ""),
                admin_context=str(k["admin_context"]),
                applier_rolebinding_namespace=str(k["applier_rolebinding"]["namespace"]),
                applier_rolebinding_name=str(k["applier_rolebinding"]["name"]),
            ),
            fake_cluster_file=_under(state_dir, str(d["fake"]["cluster_file"])),
            fake_ready_after_polls=int(d["fake"]["ready_after_polls"]),
            proposer_default=str(d["proposer"]["default"]),
            llm_model=str(d["proposer"]["llm_model"]),
            llm_effort=str(d["proposer"]["llm_effort"]),
            policy=PolicyConfig(
                allowed_kinds=tuple(p["allowed_kinds"]),
                allowed_namespaces=tuple(p["allowed_namespaces"]),
                allowed_ops=tuple(p["allowed_ops"]),
                allowed_paths=tuple(p["allowed_paths"]),
                max_patch_ops=int(p["max_patch_ops"]),
                min_replicas=int(p["min_replicas"]),
                max_replicas=int(p["max_replicas"]),
                max_replica_change_pct=float(p["max_replica_change_pct"]),
                allowed_registries=tuple(p["allowed_registries"]),
                require_resource_limits=tuple(p["require_resource_limits"]),
                server_dry_run=bool(p["server_dry_run"]),
            ),
            approval=ApprovalConfig(
                approvers=tuple(a["approvers"]),
                required_approvals=int(a["required_approvals"]),
                ttl_minutes=float(a["ttl_minutes"]),
                min_plan_hash_prefix=int(a["min_plan_hash_prefix"]),
            ),
            killswitch=KillSwitchConfig(
                file=_under(state_dir, str(ks["file"])),
                env_var=str(ks["env_var"]),
                configmap_namespace=str(ks["configmap"]["namespace"]),
                configmap_name=str(ks["configmap"]["name"]),
                configmap_key=str(ks["configmap"]["key"]),
                on_engage_during_apply=str(ks["on_engage_during_apply"]),
            ),
            rollout_timeout_seconds=float(d["rollout"]["timeout_seconds"]),
            rollout_poll_seconds=float(d["rollout"]["poll_seconds"]),
            raw=d,
        )
    except (KeyError, TypeError, ValueError) as e:
        raise ConfigError(f"invalid config: {e!r}") from e
    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    if cfg.backend not in {"kubectl", "fake"}:
        raise ConfigError(f"backend must be kubectl|fake, got {cfg.backend!r}")
    if cfg.killswitch.on_engage_during_apply not in {"freeze", "rollback"}:
        raise ConfigError("killswitch.on_engage_during_apply must be freeze|rollback")
    if cfg.approval.required_approvals < 1:
        raise ConfigError("approval.required_approvals must be >= 1")
    if cfg.approval.min_plan_hash_prefix < 8:
        raise ConfigError("approval.min_plan_hash_prefix must be >= 8")
    if cfg.agent_identity in cfg.approval.approvers:
        raise ConfigError("agent_identity must not appear in approval.approvers")
    if cfg.approval.required_approvals > len(cfg.approval.approvers):
        raise ConfigError("required_approvals exceeds number of approvers")
    if not set(cfg.policy.allowed_ops) <= {"add", "remove", "replace"}:
        raise ConfigError("policy.allowed_ops may only contain add/remove/replace")


def load(path: str | Path | None = None, overrides: dict[str, Any] | None = None) -> Config:
    over: dict[str, Any] = {}
    if path is not None:
        p = Path(path)
        if not p.exists():
            raise ConfigError(f"config file not found: {p}")
        over = yaml.safe_load(p.read_text()) or {}
        if not isinstance(over, dict):
            raise ConfigError("config root must be a mapping")
    if overrides:
        over = _deep_merge(over, overrides)
    return from_dict(over)
