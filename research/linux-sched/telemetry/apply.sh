#!/usr/bin/env bash
#
# apply.sh — manage the linux-sched telemetry manifests.
#
# Usage:
#   apply.sh apply    # apply the namespace + observer DaemonSets
#   apply.sh delete   # delete them (leaves backend storage untouched)
#   apply.sh test     # offline manifest validation + optional server dry-run
#
# The privileged observers run only on nodes labelled
# linux-sched/research=observer; see README.md for the labelling step.

set -Eeuo pipefail
shopt -s inherit_errexit
IFS=$'\n\t'

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
LINUX_SCHED_DIR="$(cd -- "${HERE}/.." && pwd -P)"
source "${LINUX_SCHED_DIR}/experiments/lib.sh"

KUBECONFIG_PATH="$(workload_kubeconfig)"

run_offline_validation() {
    uv run python "${HERE}/validate.py"
}

case "${1:-}" in
    apply)
        require_cmd kubectl
        # The offline security validation is a gate, not advice: a modified
        # privileged DaemonSet must not reach the cluster unvalidated.
        run_offline_validation
        [[ -f "$KUBECONFIG_PATH" ]] || die "workload kubeconfig missing: ${KUBECONFIG_PATH}"
        kubectl --kubeconfig "$KUBECONFIG_PATH" apply -f "${HERE}/namespace.yaml"
        kubectl --kubeconfig "$KUBECONFIG_PATH" apply -f "${HERE}/trace-observer-daemonset.yaml"
        kubectl --kubeconfig "$KUBECONFIG_PATH" apply -f "${HERE}/otel-profiler-daemonset.yaml"
        ;;
    delete)
        require_cmd kubectl
        [[ -f "$KUBECONFIG_PATH" ]] || die "workload kubeconfig missing: ${KUBECONFIG_PATH}"
        kubectl --kubeconfig "$KUBECONFIG_PATH" delete -f "${HERE}/otel-profiler-daemonset.yaml" --ignore-not-found
        kubectl --kubeconfig "$KUBECONFIG_PATH" delete -f "${HERE}/trace-observer-daemonset.yaml" --ignore-not-found
        kubectl --kubeconfig "$KUBECONFIG_PATH" delete -f "${HERE}/namespace.yaml" --ignore-not-found
        ;;
    test)
        run_offline_validation
        if [[ -f "$KUBECONFIG_PATH" ]] && kubectl --kubeconfig "$KUBECONFIG_PATH" cluster-info >/dev/null 2>&1; then
            kubectl --kubeconfig "$KUBECONFIG_PATH" apply --dry-run=server \
                -f "${HERE}/namespace.yaml" \
                -f "${HERE}/trace-observer-daemonset.yaml" \
                -f "${HERE}/otel-profiler-daemonset.yaml" >/dev/null \
                && echo "server dry-run OK"
        else
            echo "cluster not reachable; offline validation only"
        fi
        ;;
    *)
        sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
        exit "${1:+1}"
        ;;
esac
