"""Two-layer kill switch.

Soft (cooperative) layer - checked by safechange code before propose, before apply and on
every rollout poll. Engaged if ANY of:
  * the file <state_dir>/KILL exists
  * env var SAFECHANGE_KILL is set to a truthy value
  * ConfigMap safechange-system/safechange-control has killed: "true"
  * the ConfigMap cannot be read (fail closed)
The agent's ServiceAccounts can only GET that ConfigMap, so they cannot un-kill.

Hard layer - delete the applier RoleBinding. The API server then refuses every write by
the agent regardless of what its code does. Requires admin credentials.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from .backends.base import ClusterAdmin, ClusterApplier, ClusterReader
from .config import Config
from .models import Target

TRUTHY = {"1", "true", "yes", "on", "killed"}


@dataclass(frozen=True)
class KillState:
    engaged: bool
    reasons: list[str] = field(default_factory=list)


def soft_state(cfg: Config, reader: ClusterReader) -> KillState:
    ks = cfg.killswitch
    reasons: list[str] = []
    try:
        if ks.file.exists():
            reasons.append(f"kill file present: {ks.file}")
    except OSError as e:
        reasons.append(f"kill file unreadable (fail closed): {e}")
    if os.environ.get(ks.env_var, "").strip().lower() in TRUTHY:
        reasons.append(f"env {ks.env_var}={os.environ[ks.env_var]}")
    try:
        cm = reader.get_configmap(ks.configmap_namespace, ks.configmap_name)
        val = str((cm.get("data") or {}).get(ks.configmap_key, "")).strip().lower()
        if val in TRUTHY:
            reasons.append(f"configmap {ks.configmap_namespace}/{ks.configmap_name} {ks.configmap_key}={val}")
    except Exception as e:  # unreadable control plane = engaged
        reasons.append(f"control configmap unreadable (fail closed): {type(e).__name__}: {e}")
    return KillState(engaged=bool(reasons), reasons=reasons)


def engage(cfg: Config, admin: ClusterAdmin, *, hard: bool) -> list[str]:
    """Engage every layer we can. Each layer is attempted even if another fails."""
    done: list[str] = []
    errors: list[str] = []
    ks = cfg.killswitch
    try:
        ks.file.parent.mkdir(parents=True, exist_ok=True)
        ks.file.write_text("killed\n")
        done.append(f"soft: wrote {ks.file}")
    except OSError as e:
        errors.append(f"soft file: {e}")
    try:
        admin.set_control_killed(ks.configmap_namespace, ks.configmap_name, ks.configmap_key, True)
        done.append(f"soft: configmap {ks.configmap_namespace}/{ks.configmap_name} killed=true")
    except Exception as e:
        errors.append(f"soft configmap: {e}")
    if hard:
        k = cfg.kube
        try:
            admin.delete_rolebinding(k.applier_rolebinding_namespace, k.applier_rolebinding_name)
            done.append(f"hard: deleted rolebinding {k.applier_rolebinding_namespace}/{k.applier_rolebinding_name}")
        except Exception as e:
            errors.append(f"hard rolebinding: {e}")
    if errors:
        raise KillSwitchError("partially engaged: " + "; ".join(done + ["ERRORS: " + "; ".join(errors)]))
    return done


def disengage(cfg: Config, admin: ClusterAdmin) -> list[str]:
    ks, k = cfg.killswitch, cfg.kube
    done: list[str] = []
    admin.restore_applier_rolebinding(k.applier_rolebinding_namespace, k.applier_rolebinding_name)
    done.append("hard: applier rolebinding restored")
    admin.set_control_killed(ks.configmap_namespace, ks.configmap_name, ks.configmap_key, False)
    done.append("soft: configmap killed=false")
    if ks.file.exists():
        ks.file.unlink()
        done.append(f"soft: removed {ks.file}")
    if os.environ.get(ks.env_var, "").strip().lower() in TRUTHY:
        done.append(f"WARNING: env {ks.env_var} is still set in this shell; unset it yourself")
    return done


def hard_state(cfg: Config, applier: ClusterApplier, admin: ClusterAdmin | None,
               probe: Target) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        out["applier can-i patch"] = "yes" if applier.can_i_patch(probe) else "no"
    except Exception as e:
        out["applier can-i patch"] = f"unknown ({e})"
    if admin is not None:
        k = cfg.kube
        try:
            out["applier rolebinding"] = ("present" if admin.rolebinding_exists(
                k.applier_rolebinding_namespace, k.applier_rolebinding_name) else "DELETED")
        except Exception as e:
            out["applier rolebinding"] = f"unknown ({e})"
    return out


class KillSwitchError(Exception):
    pass
