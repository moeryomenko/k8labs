#!/usr/bin/env bash
#
# apply.sh — manage the long-lived linux-sched observer DaemonSet.
#
# Usage:
#   apply.sh apply    # validate, then apply namespace + observer DaemonSet
#   apply.sh delete   # delete them
#   apply.sh test     # offline manifest validation only
#
# The observer runs with capabilities (never the privileged flag) and only on
# nodes labelled linux-sched/research=observer. Validation is a gate, not
# advice: a manifest that quietly regains privilege never reaches the cluster.

set -Eeuo pipefail
shopt -s inherit_errexit
IFS=$'\n\t'

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
LINUX_SCHED_DIR="$(cd -- "${HERE}/.." && pwd -P)"
# shellcheck source=../experiments/lib.sh
source "${LINUX_SCHED_DIR}/experiments/lib.sh"

MANIFEST="${HERE}/observer-daemonset.yaml"
KUBECONFIG_PATH="$(workload_kubeconfig)"

run_offline_validation() {
    uv run python "${HERE}/validate.py"
}

case "${1:-}" in
    apply)
        require_cmd kubectl
        run_offline_validation
        [[ -f "$KUBECONFIG_PATH" ]] || die "workload kubeconfig missing: ${KUBECONFIG_PATH}"
        kubectl --kubeconfig "$KUBECONFIG_PATH" apply -f "$MANIFEST"
        ;;
    delete)
        require_cmd kubectl
        [[ -f "$KUBECONFIG_PATH" ]] || die "workload kubeconfig missing: ${KUBECONFIG_PATH}"
        kubectl --kubeconfig "$KUBECONFIG_PATH" delete -f "$MANIFEST" --ignore-not-found
        ;;
    test)
        run_offline_validation
        ;;
    *)
        sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
        exit "${1:+1}"
        ;;
esac
