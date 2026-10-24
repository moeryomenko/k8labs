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

case "$1" in
  apply)
    exec kubectl apply -f "${manifest}"
    ;;
  delete)
    exec kubectl delete --ignore-not-found -f "${manifest}"
    ;;
  test)
    cd -- "${repo_root}"
    exec uv run pytest -q "${test_file}"
    ;;
  *)
    usage
    exit 64
    ;;
esac
