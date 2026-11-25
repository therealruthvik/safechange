#!/usr/bin/env bash
# End-to-end safechange demo. BACKEND=kubectl (default, needs `make cluster`) or BACKEND=fake.
set -uo pipefail
cd "$(dirname "$0")/.."

BACKEND="${BACKEND:-kubectl}"
CTX="${CTX:-safechange}"
export FORCE_COLOR=1
SC_BIN=".venv/bin/safechange"

if [[ "$BACKEND" == fake ]]; then
  STATE=.demo-state
  rm -rf "$STATE"; mkdir -p "$STATE"
  cat > "$STATE/config.yaml" <<YAML
state_dir: $STATE
backend: fake
fake: {ready_after_polls: 3}
rollout: {timeout_seconds: 12, poll_seconds: 1}
YAML
  CONFIG="$STATE/config.yaml"
else
  CONFIG=safechange.yaml
  rm -rf .safechange
fi
SC="$SC_BIN --config $CONFIG"

B=$'\033[1m'; G=$'\033[32m'; R=$'\033[31m'; Y=$'\033[33m'; C=$'\033[36m'; N=$'\033[0m'
banner() { echo; echo "${B}${C}================================================================${N}"; echo "${B}${C}== $*${N}"; echo "${B}${C}================================================================${N}"; }
step()   { echo; echo "${B}\$ $*${N}"; }
run()    { step "safechange $*"; $SC "$@"; local rc=$?; echo "${Y}(exit $rc)${N}"; return $rc; }
ph()     { $SC show "$1" --json | python3 -c 'import sys,json;print(json.load(sys.stdin)["plan_hash"][:8])'; }
propose(){ step "safechange propose $*" >&2; local id; id=$($SC propose "$@" --id-only) || { echo "${R}propose failed${N}" >&2; exit 1; }; echo "  -> proposal $id" >&2; echo "$id"; }
expect() { local want=$1; shift; "$@"; local rc=$?
           if [[ $rc -ne $want ]]; then echo "${R}DEMO ASSERTION FAILED: expected exit $want, got $rc${N}"; exit 1; fi; }
expect_status() { local got; got=$($SC show "$1" --json | python3 -c 'import sys,json;print(json.load(sys.stdin)["status"])')
           if [[ "$got" != "$2" ]]; then echo "${R}DEMO ASSERTION FAILED: $1 is $got, expected $2${N}"; exit 1; fi
           echo "${G}✔ proposal $1 is $got${N}"; }
kube()   { if [[ "$BACKEND" == kubectl ]]; then step "kubectl $*"; kubectl --context "$CTX" "$@"; fi; }
showweb(){ kube -n demo get deploy web -o 'custom-columns=NAME:.metadata.name,REPLICAS:.spec.replicas,READY:.status.readyReplicas,IMAGE:.spec.template.spec.containers[0].image'; }

# --------------------------------------------------------------------------- reset
banner "0. Reset to a known baseline"
$SC unkill --as alice >/dev/null 2>&1 || true
if [[ "$BACKEND" == kubectl ]]; then
  kubectl --context "$CTX" apply -f deploy/web.yaml >/dev/null
  kubectl --context "$CTX" -n demo rollout status deploy/web --timeout=180s
fi
showweb
run status

# --------------------------------------------------------------------------- 1
banner "1. Happy path: scale web 2 -> 4, approve, apply, healthy -> applied"
ID=$(propose "scale web to 4")
run show "$ID"
HASH=$(ph "$ID")
expect 0 run approve "$ID" --plan-hash "$HASH" --as alice
expect 0 run apply "$ID" --as alice
expect_status "$ID" applied
showweb

# --------------------------------------------------------------------------- 2
banner "2. Bad image: readiness never passes -> automatic rollback"
ID=$(propose "set image web=docker.io/library/nginx:1.27-bad")
HASH=$(ph "$ID")
expect 0 run approve "$ID" --plan-hash "$HASH" --as bob
expect 0 run apply "$ID" --as bob
expect_status "$ID" rolled_back
showweb

# --------------------------------------------------------------------------- 3
banner "3. Policy denials: ':latest' image, privileged container, too-large scale"
step "safechange propose 'set image web=nginx:latest'"
expect 2 $SC propose "set image web=nginx:latest"
PRIV=$(mktemp)
echo '[{"op":"add","path":"/spec/template/spec/containers/0/securityContext","value":{"privileged":true}}]' > "$PRIV"
step "a compromised proposer emits a privileged patch: $(cat "$PRIV")"
expect 2 $SC propose --patch-file "$PRIV" --target deployment/web --rationale "totally routine"
step "safechange propose 'scale web to 10'   (4 -> 10 is +150%)"
$SC propose "scale web to 10" | grep -E "FAIL|status"
rm -f "$PRIV"

# --------------------------------------------------------------------------- 4
banner "4. Approval gate: self-approval and wrong plan hash are blocked"
ID=$(propose "scale web to 5")
HASH=$(ph "$ID")
expect 2 run approve "$ID" --plan-hash "$HASH" --as safechange-agent
expect 2 run approve "$ID" --plan-hash deadbeef --as alice
expect 2 run approve "$ID" --plan-hash "$HASH" --as mallory
expect 0 run approve "$ID" --plan-hash "$HASH" --as alice

# --------------------------------------------------------------------------- 5
banner "5. Drift: a human scales web after the proposal was approved -> apply aborts"
if [[ "$BACKEND" == kubectl ]]; then
  kube -n demo scale deploy/web --replicas=3
else
  step "(fake) out-of-band scale to 3"
  .venv/bin/python - "$CONFIG" <<'PY'
import sys
from safechange import config
from safechange.backends import build_backends
from safechange.models import Target
build_backends(config.load(sys.argv[1])).admin.scale(Target("Deployment", "demo", "web"), 3)
PY
fi
expect 2 run apply "$ID" --as alice
expect_status "$ID" aborted
showweb

# --------------------------------------------------------------------------- 6
banner "6. Kill switch"
ID=$(propose "scale web to 4")
HASH=$(ph "$ID")
expect 0 run approve "$ID" --plan-hash "$HASH" --as alice
expect 0 run kill --as alice
run status
step "propose while killed"
expect 2 $SC propose "scale web to 2"
expect 2 run apply "$ID" --as alice
expect_status "$ID" aborted
expect 0 run unkill --as alice

echo; echo "${B}6b. Engage the HARD kill switch mid-rollout (on_engage_during_apply=freeze)${N}"
ID=$(propose "set image web=docker.io/library/nginx:1.27-bad")
HASH=$(ph "$ID")
expect 0 run approve "$ID" --plan-hash "$HASH" --as bob
step "safechange apply $ID --as bob   (in background)"
$SC apply "$ID" --as bob & APPLY_PID=$!
sleep 8
expect 0 run kill --hard --as alice
wait $APPLY_PID; echo "${Y}(apply exit $?)${N}"
expect_status "$ID" frozen
run status
showweb
step "operator decides: restore access and roll back the frozen change"
expect 0 run unkill --as alice
expect 0 run rollback "$ID" --as alice
expect_status "$ID" rolled_back
showweb

# --------------------------------------------------------------------------- 7
banner "7. Audit trail"
run list
run audit list --limit 100
expect 0 run audit verify
echo; echo "${B}${G}Demo complete.${N}"
