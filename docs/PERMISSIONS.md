# Permissions

safechange uses three identities. The two agent identities are Kubernetes
ServiceAccounts in `safechange-system` (see `deploy/rbac.yaml`). The third is a human
operator's own credentials.

`scripts/make-kubeconfigs.sh` mints short-lived tokens (`kubectl create token`, 8h by
default) and writes one kubeconfig per ServiceAccount
(`.kube-safechange/{reader,applier}.kubeconfig`). Each file contains exactly one user and
one context and **no admin credentials**. Every kubectl call passes `--kubeconfig` and
`--context` explicitly. Nothing relies on the user's current-context.

## Identity → verbs → resources → why

| Identity | Verbs | Resources | Scope | Why |
|---|---|---|---|---|
| `safechange-reader` (SA) | get, list, watch | `deployments`, `replicasets` (apps) | ns `demo` | Proposer reads the live object; preflight reads for drift; verify reads rollout status |
| `safechange-reader` (SA) | get, list, watch | `pods` | ns `demo` | Rollout/readiness diagnosis |
| `safechange-reader` (SA) | get | `configmaps` **resourceNames: `safechange-control`** | ns `safechange-system` | Read the soft kill switch |
| `safechange-applier` (SA) | get, patch | `deployments` (apps) | ns `demo` | Apply the approved JSON patch and the rollback patch; server-side dry-run |
| `safechange-applier` (SA) | get | `configmaps` **resourceNames: `safechange-control`** | ns `safechange-system` | Read the soft kill switch |
| human admin (e.g. `minikube` context) | apply, delete | ConfigMap `safechange-control`, RoleBinding `demo/safechange-applier` | — | `kill` / `unkill` only. Never used by propose/apply. |

`automountServiceAccountToken: false` is set on both ServiceAccounts. No pod runs as
them; the tokens exist only in the generated kubeconfigs.

Verified by `make kubeconfigs` (`kubectl auth can-i` probe):

```
reader    get deployments -n demo                  yes
reader    patch deployments -n demo                no
applier   patch deployments -n demo                yes
applier   delete deployments -n demo               no
applier   get secrets -n demo                      no
applier   patch deployments -n kube-system         no
applier   get cm/safechange-control                yes
applier   patch cm/safechange-control              no
```

## Which code holds which credential

| Stage | Identity |
|---|---|
| propose (rules / llm / manual) | reader only. `Proposer.propose(task, namespace, reader)` gets no other handle, and the LLM never sees a credential |
| guardrails `server_dry_run` | applier (`kubectl apply --dry-run=server`). Runs only after every static check passes, and never persists anything |
| preflight | reader (drift, kill switch) + applier (`auth can-i`, dry-run re-check) |
| apply / rollback | applier |
| verify | reader |
| kill / unkill / status | admin (human) |

## What the agent explicitly cannot do

- Create, delete, update (PUT) or scale-subresource any Deployment. It can only `patch`.
- Touch any other kind: Pods, Services, Ingresses, StatefulSets, DaemonSets, Jobs.
- Read or write Secrets, or ConfigMaps other than `safechange-control`.
- `exec`, `attach`, `port-forward` or read logs.
- Act in any namespace other than `demo`, or on any cluster-scoped resource (Nodes,
  Namespaces, ClusterRoles, CRDs, PVs).
- Change RBAC, including its own bindings. It cannot re-grant itself access after a hard
  kill.
- Modify the kill switch. It can `get` the control ConfigMap but has no
  update/patch/delete verb on it.
- Approve its own proposals. This is enforced in code: `agent_identity` is rejected as an
  approver and cannot even be configured into `approvers`.

Within `patch deployments`, `policy.py` narrows things further. Only these paths may be
touched: replicas, container image, resources, env, labels. Privileged settings,
host namespaces, `:latest` tags and non-allowlisted registries are all rejected. Moving
those rules into an admission controller (Kyverno/Gatekeeper, scoped to the applier SA)
is the recommended next layer, because it is enforced by the API server rather than by
the agent's own code.

## Blast radius if the agent is compromised

**Scenario A: the LLM is prompt-injected or hallucinates.** It holds no credentials, so
the most it can do is *propose*. A malicious patch, such as privilege escalation, a
foreign registry, a removed limit or a selector change, fails the guardrails and becomes
`policy_failed`, which can never be approved. A plausible-looking malicious patch, such as
an allowlisted-registry image with a backdoor, still has to get past a human who types
the plan hash after reading the diff.

**Scenario B: the safechange process or host is compromised**, so the attacker holds both
agent tokens and can skip every check in code. With `get/patch deployments` in `demo`, the
attacker could:

- change any field of any Deployment in `demo`: image (run arbitrary code *as the
  workload's own pod identity*), replicas (including 0, which is a DoS of `demo`), env,
  and also securityContext/volumes, because the path allowlist is enforced in code, not by
  RBAC.

The attacker could **not**:

- reach other namespaces, Secrets, RBAC, nodes or the control plane;
- delete Deployments or create new workload kinds;
- disable the soft kill switch, or survive a hard kill. Deleting the applier
  RoleBinding revokes write access immediately, at the API server.

Mitigations that shrink Scenario B further: admission policies scoped to the applier SA
(enforce the path allowlist and pod-security rules server-side); Pod Security Admission
`restricted` on `demo`; short token TTLs; running the applier in a separate process or
host from the proposer; audit-log shipping off-host.
