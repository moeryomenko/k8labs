#!/usr/bin/env bash
#
# capture-block.sh — capture one measured sample on a linux-sched worker.
#
# Drives the long-lived observer DaemonSet over the Kubernetes API: it starts
# the observer stack inside the target node's observer Pod, runs the workload
# and its load generator, then copies the raw artifacts back. SSH is not used —
# SSH to the lab VMs never completes.
#
# The observer tooling is node-native (see GATES.md). Run-queue metrics use the
# CO-RE bpf-runqlat because BCC 0.35 cannot compile against the 7.2 headers.
#
# Every required tool and every observer process is verified: a missing probe or
# a dead observer aborts the sample rather than reporting a partial success.

set -Eeuo pipefail
shopt -s inherit_errexit
IFS=$'\n\t'

CAPTURE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
LINUX_SCHED_DIR="$(cd -- "${CAPTURE_DIR}/.." && pwd -P)"
# shellcheck source=../experiments/lib.sh
source "${LINUX_SCHED_DIR}/experiments/lib.sh"

PROBE="${CAPTURE_DIR}/probes/sched-trace.bt"
REMOTE_DIR="/var/tmp/linux-sched"

usage() {
    cat <<'EOF'
Usage: capture-block.sh --target-node NODE --duration N --out-dir DIR --arm ARM
                        [--load-node NODE] [--manifest FILE] [--load-command CMD]
                        [--raw FILE] [--podlog FILE]
                        [--instrumentation enabled|disabled]

Captures one measured sample on ARM. --raw receives the load-generator output;
--podlog receives the workload pod log. Observers run inside the observer
DaemonSet Pod on --target-node; the load generator runs on --load-node. With
--instrumentation disabled the observer stack is not started (observer-overhead
calibration only).
EOF
    exit "${1:-0}"
}

# observer_pod NODE — the observer pod on that node, or a clear failure.
observer_pod() {
    local node="$1" pod
    pod="$(observer_pod_for_node "$node")"
    [[ -n "$pod" ]] || die "no observer pod on ${node}; run 'make linux-sched-observers-apply'"
    printf '%s' "$pod"
}

main() {
    local target_node="" duration="" out_dir="" arm="" load_node=""
    local manifest="" load_command="" raw="" podlog="" instrumentation="enabled"

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --target-node) target_node="$2"; shift 2 ;;
            --duration) duration="$2"; shift 2 ;;
            --out-dir) out_dir="$2"; shift 2 ;;
            --arm) arm="$2"; shift 2 ;;
            --load-node) load_node="$2"; shift 2 ;;
            --manifest) manifest="$2"; shift 2 ;;
            --load-command) load_command="$2"; shift 2 ;;
            --raw) raw="$2"; shift 2 ;;
            --podlog) podlog="$2"; shift 2 ;;
            --instrumentation) instrumentation="$2"; shift 2 ;;
            -h|--help) usage 0 ;;
            *) die "unknown argument: $1" ;;
        esac
    done

    [[ -n "$target_node" ]] || die "--target-node is required"
    [[ -n "$duration" ]] || die "--duration is required"
    [[ -n "$out_dir" ]] || die "--out-dir is required"
    [[ -n "$arm" ]] || die "--arm is required"

    require_cmd kubectl
    mkdir -p "$out_dir"
    local load_file="${raw:-${out_dir}/${arm}.txt}"
    local podlog_file="${podlog:-${out_dir}/${arm}.log}"
    local work="${REMOTE_DIR}/${arm}"

    local target_pod
    target_pod="$(observer_pod "$target_node")"
    log "target observer: ${target_pod} (${target_node})"

    local pids=()
    cleanup() {
        local pid
        for pid in "${pids[@]}"; do
            kexec "$target_pod" sh -c "kill ${pid} 2>/dev/null || true" >/dev/null 2>&1 || true
        done
    }
    trap cleanup EXIT INT TERM

    start_observer() {
        # Runs in the CURRENT shell (no pipeline, no subshell) so `pids`
        # survives for the trap that has to kill these processes.
        local name="$1" cmd="$2" pid
        pid="$(kexec "$target_pod" sh -c \
            "mkdir -p ${work} && ${cmd} </dev/null >${work}/${name}.txt 2>&1 & echo \$!")"
        [[ -n "$pid" ]] || die "could not start ${name} in ${target_pod}"
        sleep 1
        kexec "$target_pod" sh -c "kill -0 ${pid} 2>/dev/null" \
            || die "${name} exited immediately; see ${work}/${name}.txt"
        pids+=("$pid")
        log "  ${name}: pid ${pid}"
    }

    if [[ "$instrumentation" == "enabled" ]]; then
        for tool in bpftrace perf bpf-runqlat; do
            kexec "$target_pod" sh -c "command -v ${tool} >/dev/null 2>&1" \
                || die "required observer tool '${tool}' missing in ${target_pod}"
        done
        # The CO-RE probe reads BTF, so it needs no headers; the BCC tool does.
        start_observer runqlat "bpf-runqlat 1 ${duration}"
        start_observer sched-trace "bpftrace -e 'tracepoint:sched:sched_switch { }'"
        start_observer perf-sched "perf sched record -o ${work}/perf.data -- sleep ${duration}"
    fi

    if [[ -n "$manifest" ]]; then
        log "applying workload manifest ${manifest}"
        local rendered="${out_dir}/workload-${arm}.yaml"
        sed -e "s|__NODE_NAME__|${target_node}|g" -e "s|__ARM__|${arm}|g" \
            "$manifest" > "$rendered"
        kubectl_wl apply -f "$rendered" || die "failed to apply workload ${manifest}"
    fi

    if [[ -n "$load_command" && -n "$load_node" ]]; then
        local load_pod
        load_pod="$(observer_pod "$load_node")"
        log "running load on ${load_pod} (${load_node})"
        kexec "$load_pod" sh -c "$load_command" > "$load_file" \
            || die "load generator failed on ${load_node}"
    else
        : > "$load_file"
    fi

    if [[ -n "$manifest" ]]; then
        log "collecting workload log for ${arm}"
        kubectl_wl logs -l "linux-sched/run=${arm}" --tail=-1 > "$podlog_file" \
            || die "failed to collect workload log for arm ${arm}"
        kubectl_wl delete -f "${out_dir}/workload-${arm}.yaml" \
            --ignore-not-found --wait=true --timeout=120s >/dev/null \
            || die "failed to delete workload for arm ${arm}; refusing to leave it running"
    else
        : > "$podlog_file"
    fi

    if [[ "$instrumentation" == "enabled" ]]; then
        sleep 2
        cleanup
        pids=()
        log "copying capture artifacts into ${out_dir}"
        kubectl_wl cp "${OBSERVER_NAMESPACE}/${target_pod}:${work}/." "$out_dir/" \
            || die "failed to copy capture artifacts from ${target_pod}"

        # A capture is complete only if the required observer outputs exist and
        # are non-empty AND no perf events were lost.
        local status="complete" artifact
        for artifact in runqlat.txt sched-trace.txt; do
            if [[ ! -s "${out_dir}/${artifact}" ]]; then
                log "ERROR: observer artifact ${artifact} is missing or empty for arm ${arm}"
                status="failed"
            fi
        done
        if grep -qiE 'lost [1-9]|dropped [1-9]' "${out_dir}/perf-sched.txt" 2>/dev/null; then
            log "WARN: perf reported lost events for arm ${arm}; trace metrics invalid"
            status="incomplete"
        fi
        printf '%s\n' "$status" > "${out_dir}/${arm}.capture-status"
        [[ "$status" != "failed" ]] \
            || die "capture failed for arm ${arm}: required observer output missing"
    fi

    log "capture complete for arm ${arm}"
}

main "$@"
