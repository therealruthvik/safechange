# Kill switch

safechange has two independent layers. Use the soft layer to stop a well-behaved agent.
Use the hard layer when you no longer trust the agent's code.

## Soft layer (cooperative)

safechange's own code checks the soft layer at three checkpoints:

1. **before propose**: nothing is proposed or stored
2. **before apply**: preflight check `kill_switch_disengaged`; the proposal becomes `aborted` and the cluster is untouched
3. **every poll during rollout verification**: the reaction is set by `killswitch.on_engage_during_apply`:
   - `freeze` (default): stop immediately and leave the cluster exactly as it is.
     Status becomes `frozen`, and a human decides (`safechange rollback <id>` or investigate).
   - `rollback`: apply the verified inverse patch. This needs write access, so it does
     not work under the hard kill.

The soft layer counts as **engaged** if any of these is true:

| Signal | How to set | Who can set it |
|---|---|---|
| File `<state_dir>/KILL` (default `~/.safechange/KILL`) | `touch ~/.safechange/KILL` | anyone on the agent host |
| Env var `SAFECHANGE_KILL` ∈ {1,true,yes,on} | `export SAFECHANGE_KILL=1` (affects processes started afterwards) | whoever launches the agent |
| ConfigMap `safechange-system/safechange-control` `killed: "true"` | `safechange kill` or `kubectl patch` with admin creds | cluster admins only |
| ConfigMap unreadable (deleted, RBAC error, API down) | — | fails closed: treated as engaged |

The agent's ServiceAccounts can only `get` that one ConfigMap (`resourceNames`-scoped),
so the agent **cannot un-kill itself**.

**Limitation:** "cooperative" means a compromised or buggy agent process can ignore it.

## Hard layer (enforced by the API server)

`safechange kill --hard` deletes the RoleBinding `demo/safechange-applier`. From that
moment the API server refuses every write by the agent (`403 Forbidden`), whatever its
code does. Reads keep working, so `status` and verification still function, and so does
the freeze. `safechange status` shows `applier can-i patch: no`.

`safechange unkill --as <approver>` recreates the RoleBinding, sets the ConfigMap to
`killed: "false"` and removes the KILL file. It warns if `SAFECHANGE_KILL` is still set in
your shell. Only names in `approval.approvers` may unkill.

Both layers need admin credentials (`kube.admin_context`). The agent's kubeconfigs cannot
perform either one.

|  | Soft | Hard |
|---|---|---|
| Stops a healthy agent | yes | yes |
| Stops a compromised agent | no | **yes** |
| Mid-rollout behavior | freeze or auto-rollback | freeze only (rollback needs write) |
| Takes effect | next checkpoint (≤ `rollout.poll_seconds`) | next API call |
| Undo | `unkill` | `unkill` (re-applies RoleBinding) |

## Runbook: "the agent is misbehaving, what do I do in 60 seconds?"

**0–10 s: stop all writes.**

```bash
safechange kill --hard --as <you>
```

If the CLI is unavailable, do the same thing with raw kubectl and admin credentials:

```bash
kubectl --context <admin-ctx> -n demo delete rolebinding safechange-applier
kubectl --context <admin-ctx> -n safechange-system patch configmap safechange-control \
  --type merge -p '{"data":{"killed":"true"}}'
```

**10–20 s: confirm it worked.**

```bash
safechange status            # soft: ENGAGED, hard: applier can-i patch: no
kubectl --context <admin-ctx> auth can-i patch deployments -n demo \
  --as system:serviceaccount:safechange-system:safechange-applier   # -> no
```

**20–40 s: see what it touched.**

```bash
safechange list                       # look for applying / frozen / failed / applied
safechange audit list --limit 30      # who/what/when, plan hashes
safechange audit verify               # is the log intact?
kubectl --context <admin-ctx> -n demo get deploy -o wide
```

**40–60 s: decide on the workload.**

- A proposal is `frozen` or recently `applied` and the workload is unhealthy: restore
  access just long enough to roll back, then kill again.

  ```bash
  safechange unkill --as <you> && safechange rollback <id> --as <you> && safechange kill --hard --as <you>
  ```

  Or, without the agent at all, use the snapshot taken before apply
  (`<state_dir>/snapshots/<id>.json`) or `kubectl rollout undo deploy/<name> -n demo`
  with admin credentials.
- A proposal is `failed`: read its note (`safechange show <id>`). It names the snapshot
  path and says whether the cluster may be in the new state.

**Afterwards:** rotate the agent tokens (`make kubeconfigs`; the old tokens expire on
their own TTL). Review the audit log. Unkill only once the root cause is understood.
