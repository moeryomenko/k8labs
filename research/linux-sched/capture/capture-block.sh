#!/usr/bin/env bash
#
# capture-block.sh — capture one measured sample on a linux-sched worker.
#
# Runs the required observer stack (BCC runqlat/runqlen, the sched_* bpftrace
# probe, and `perf sched record`) on the target worker while the workload and
# its load generator run, then copies the raw artifacts back.
#
# Load-generator output and the workload pod log are written to SEPARATE files
# so the analysis can parse only the artifact the primary metric is defined on.
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
REMOTE_DIR="/var/tmp/linux-sched-capture"

usage() {
    cat <<'EOF'
Usage: capture-block.sh --target-ip IP --duration N --out-dir DIR --arm ARM
                        [--load-ip IP] [--manifest FILE] [--load-command CMD]
                        [--raw FILE] [--podlog FILE]
                        [--instrumentation enabled|disabled]

Captures one measured sample on ARM. --raw receives the load-generator output;
--podlog receives the workload pod log. With --instrumentation disabled the
observer stack is not started (used only for the observer-overhead
calibration); the default is enabled.
EOF
    exit "${1:-0}"
}

require_remote_tool() {
    local tool="$1"
    ssh_node "$TARGET_IP" "command -v ${tool} >/dev/null 2>&1" \
        || die "required capture tool '${tool}' not present on ${TARGET_IP}"
}

main() {
    local target_ip="" duration="" out_dir="" arm="" load_ip=""
    local manifest="" load_command="" raw="" podlog="" instrumentation="enabled"

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --target-ip) target_ip="$2"; shift 2 ;;
            --duration) duration="$2"; shift 2 ;;
            --out-dir) out_dir="$2"; shift 2 ;;
            --arm) arm="$2"; shift 2 ;;
            --load-ip) load_ip="$2"; shift 2 ;;
            --manifest) manifest="$2"; shift 2 ;;
            --load-command) load_command="$2"; shift 2 ;;
            --raw) raw="$2"; shift 2 ;;
            --podlog) podlog="$2"; shift 2 ;;
            --instrumentation) instrumentation="$2"; shift 2 ;;
            -h|--help) usage 0 ;;
            *) die "unknown argument: $1" ;;
        esac
    done

    [[ -n "$target_ip" ]] || die "--target-ip is required"
    [[ -n "$duration" ]] || die "--duration is required"
    [[ -n "$out_dir" ]] || die "--out-dir is required"
    [[ -n "$arm" ]] || die "--arm is required"
    TARGET_IP="$target_ip"

    require_cmd ssh python3
    mkdir -p "$out_dir"
    local load_file="${raw:-${out_dir}/${arm}.txt}"
    local podlog_file="${podlog:-${out_dir}/${arm}.log}"

    local pids=()
    cleanup() {
        local pid
        for pid in "${pids[@]}"; do
            ssh_node "$TARGET_IP" "sudo kill ${pid} 2>/dev/null || true" 2>/dev/null || true
        done
    }
    trap cleanup EXIT INT TERM

    local runqlat_tool=""
    if [[ "$instrumentation" == "enabled" ]]; then
        require_remote_tool bpftrace
        require_remote_tool perf
        runqlat_tool="$(ssh_node "$TARGET_IP" \
            'command -v bpf-runqlat || command -v runqlat || command -v /usr/share/bcc/tools/runqlat || command -v runqlat-bpfcc' || true)"
        [[ -n "$runqlat_tool" ]] \
            || die "no runqlat tool present on ${TARGET_IP} (want libbpf-tools bpf-runqlat)"
        ssh_node "$TARGET_IP" "sudo install -d -m 0755 ${REMOTE_DIR}"

        log "staging observer probe on ${TARGET_IP}"
        scp -i "$(ssh_key)" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
            -o LogLevel=ERROR "$PROBE" "fedora@${TARGET_IP}:${REMOTE_DIR}/sched-trace.bt" \
            || die "failed to copy the bpftrace probe to ${TARGET_IP}"

        log "starting observers on ${TARGET_IP} (arm ${arm}, ${duration}s)"
        start_remote_with_log "${runqlat_tool} 1 ${duration}" "runqlat" "runqlat.txt"
        start_remote_with_log "bpftrace ${REMOTE_DIR}/sched-trace.bt" "bpftrace" "sched-trace.txt"
        start_remote_with_log "perf sched record -o ${REMOTE_DIR}/perf.data -- sleep ${duration}" "perf-sched" "perf.log"
    fi

    if [[ -n "$manifest" ]]; then
        log "applying workload manifest ${manifest}"
        local rendered="${out_dir}/workload-${arm}.yaml"
        sed -e "s|__NODE_NAME__|$(kubectl_wl get nodes --no-headers -l '!node-role.kubernetes.io/control-plane' -o custom-columns=:metadata.name | sed -n '1p')|" \
            -e "s|__ARM__|${arm}|g" \
            "$manifest" > "$rendered"
        kubectl_wl apply -f "$rendered" || die "failed to apply workload ${manifest}"
    fi

    if [[ -n "$load_command" && -n "$load_ip" ]]; then
        log "running load on ${load_ip}: ${load_command}"
        ssh_node "$load_ip" "$load_command" > "$load_file" \
            || die "load generator failed on ${load_ip}"
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
        log "fetching capture artifacts into ${out_dir}"
        scp -i "$(ssh_key)" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
            -o LogLevel=ERROR -r "fedora@${TARGET_IP}:${REMOTE_DIR}/." "$out_dir/" \
            || die "failed to fetch capture artifacts from ${TARGET_IP}"

        # A capture is complete only if the required observer outputs exist and
        # are non-empty AND no perf events were lost.
        local status="complete" artifact
        for artifact in runqlat.txt sched-trace.txt; do
            if [[ ! -s "${out_dir}/${artifact}" ]]; then
                log "ERROR: observer artifact ${artifact} is missing or empty for arm ${arm}"
                status="failed"
            fi
        done
        if grep -qiE 'lost [1-9]|dropped [1-9]' "${out_dir}/perf.log" 2>/dev/null; then
            log "WARN: perf reported lost events for arm ${arm}; trace metrics invalid"
            status="incomplete"
        fi
        printf '%s\n' "$status" > "${out_dir}/${arm}.capture-status"
        [[ "$status" != "failed" ]] \
            || die "capture failed for arm ${arm}: required observer output missing"
    fi

    log "capture complete for arm ${arm}"
}

# start_remote_with_log CMD LABEL LOGNAME — detached start with remote logging.
# Runs in the CURRENT shell (no pipeline, no subshell) so `pids` survives.
start_remote_with_log() {
    local cmd="$1" label="$2" logname="$3" pid
    pid="$(ssh_node "$TARGET_IP" \
        "sudo nohup ${cmd} </dev/null >${REMOTE_DIR}/${logname} 2>&1 & echo \$!")"
    [[ -n "$pid" ]] || die "could not start ${label} on ${TARGET_IP}"
    sleep 1
    ssh_node "$TARGET_IP" "sudo kill -0 ${pid} 2>/dev/null" \
        || die "${label} exited immediately on ${TARGET_IP}; see ${REMOTE_DIR}/${logname}"
    pids+=("$pid")
    log "  ${label}: pid ${pid}"
}

main "$@"
