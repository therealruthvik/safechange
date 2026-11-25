#!/usr/bin/env bash
# Generate one kubeconfig per safechange ServiceAccount from short-lived tokens.
# Each file contains exactly one user/context and no admin credentials, so a process
# handed reader.kubeconfig physically cannot write to the cluster.
set -euo pipefail

ADMIN_CTX="${ADMIN_CTX:-safechange}"
OUT="${OUT:-.kube-safechange}"
DURATION="${DURATION:-8h}"
mkdir -p "$OUT"
chmod 700 "$OUT"

SERVER=$(kubectl config view --raw --minify --flatten --context "$ADMIN_CTX" -o jsonpath='{.clusters[0].cluster.server}')
CA=$(kubectl config view --raw --minify --flatten --context "$ADMIN_CTX" -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')

for role in reader applier; do
  sa="safechange-$role"
  file="$OUT/$role.kubeconfig"
  rm -f "$file"
  token=$(kubectl --context "$ADMIN_CTX" -n safechange-system create token "$sa" --duration="$DURATION")
  kubectl --kubeconfig "$file" config set-cluster safechange --server="$SERVER" >/dev/null
  kubectl --kubeconfig "$file" config set clusters.safechange.certificate-authority-data "$CA" >/dev/null
  kubectl --kubeconfig "$file" config set-credentials "$sa" --token="$token" >/dev/null
  kubectl --kubeconfig "$file" config set-context "$sa" --cluster=safechange --user="$sa" --namespace=demo >/dev/null
  kubectl --kubeconfig "$file" config use-context "$sa" >/dev/null
  chmod 600 "$file"
  echo "wrote $file (context $sa, token valid $DURATION)"
done

echo "--- permission probe ---"
probe() { printf '%-9s %-40s %s\n' "$1" "$2" "$(kubectl --kubeconfig "$OUT/$1.kubeconfig" auth can-i $3 2>/dev/null || true)"; }
probe reader  "get deployments -n demo"                 "get deployments -n demo"
probe reader  "patch deployments -n demo"               "patch deployments -n demo"
probe applier "patch deployments -n demo"               "patch deployments -n demo"
probe applier "delete deployments -n demo"              "delete deployments -n demo"
probe applier "get secrets -n demo"                     "get secrets -n demo"
probe applier "patch deployments -n kube-system"        "patch deployments -n kube-system"
probe applier "get cm/safechange-control"               "get configmap/safechange-control -n safechange-system"
probe applier "patch cm/safechange-control"             "patch configmap/safechange-control -n safechange-system"
