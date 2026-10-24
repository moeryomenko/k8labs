#!/usr/bin/env bash
set -Eeuo pipefail
shopt -s inherit_errexit
IFS=$'\n\t'

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd -- "${script_dir}/../../.." && pwd -P)"
manifest="${script_dir}/collector-daemonset.yaml"
test_file="${repo_root}/research/cpu-limits/tests/test_observer_manifest.py"

usage() {
  printf 'Usage: %s {apply|delete|test}\n' "$(basename -- "$0")" >&2
}

if [[ "$#" -ne 1 ]]; then
  usage
  exit 64
fi

namespace="cpu-limits"
configmap="cpu-limits-collector"

case "$1" in
  apply)
    kubectl create namespace "${namespace}" --dry-run=client -o yaml \
      | kubectl apply -f -
    kubectl -n "${namespace}" create configmap "${configmap}" \
      --from-file=collector.py="${script_dir}/collector.py" \
      --from-file=cgroup_probe.py="${script_dir}/../experiments/cgroup_probe.py" \
      --from-file=runtime_gate.py="${script_dir}/../experiments/runtime_gate.py" \
      --dry-run=client -o yaml \
      | kubectl apply -f -
    exec kubectl apply -f "${manifest}"
    ;;
  delete)
    kubectl delete --ignore-not-found -f "${manifest}"
    kubectl -n "${namespace}" delete configmap "${configmap}" --ignore-not-found
    ;;
  test)
    bash -n "${BASH_SOURCE[0]}"
    cd -- "${repo_root}"
    exec uv run pytest -q "${test_file}"
    ;;
  *)
    usage
    exit 64
    ;;
esac
