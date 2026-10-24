#!/usr/bin/env bash
#
# stage-scx.sh — copy the built sched_ext schedulers into the observer Pod on
# every research worker.
#
# The binaries must already be built against the *running* kernel's BTF (see
# GATES.md); this only distributes them. Staging is separate from running so a
# failed build is caught before an experiment starts.

set -Eeuo pipefail
shopt -s inherit_errexit
IFS=$'\n\t'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=./lib.sh
source "${SCRIPT_DIR}/lib.sh"

main() {
    require_cmd kubectl
    local node count=0
    for node in $(ready_workers); do
        scx_stage "$node"
        log "staged schedulers on ${node}"
        count=$(( count + 1 ))
    done
    (( count > 0 )) || die "no observer-labelled workers; run 'task linux-sched-observers-apply' first"
    log "staged schedulers on ${count} worker(s)"
}

main "$@"
