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
                        [--await-workload-completion]
                        [--warmup-command CMD] [--warmup-seconds N]
                        [--load-ready-probe CMD]

Captures one measured sample on ARM. --raw receives the load-generator output;
--podlog receives the workload pod log. Observers run inside the observer
DaemonSet Pod on --target-node; the load generator runs on --load-node. With
--instrumentation disabled the observer stack is not started (observer-overhead
calibration only). --await-workload-completion waits for the workload Pods to
stop, which a podlog-sourced metric needs and a load-sourced one does not.
--warmup-command runs once, for --warmup-seconds, before the measured load and
its result is discarded; the observers still cover it. --load-ready-probe runs on
the load node until it succeeds, so the load never starts against an endpoint
that exists but is not yet serving.
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
    local await_completion=0 warmup_command="" warmup_seconds=0 load_ready_probe=""

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
            --await-workload-completion) await_completion=1; shift ;;
            --warmup-command) warmup_command="$2"; shift 2 ;;
            --warmup-seconds) warmup_seconds="$2"; shift 2 ;;
            --load-ready-probe) load_ready_probe="$2"; shift 2 ;;
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
    # The EXIT trap needs these after main returns, when its locals are gone.
    TARGET_POD="$target_pod"
    REMOTE_WORK="${REMOTE_DIR}/${arm}"
    log "target observer: ${target_pod} (${target_node})"

    # The load node's observer is resolved once: the readiness probe and the load
    # itself both run there.
    local load_pod=""
    if [[ -n "$load_node" ]]; then
        load_pod="$(observer_pod "$load_node")"
        [[ -n "$load_pod" ]] || die "no observer pod on load node ${load_node}"
    fi

    # Observers are supervised by long-lived local `kubectl exec` calls.
    #
    # A process started by `kubectl exec` dies when that exec ends, so the
    # earlier form — start the tool inside the Pod and return immediately —
    # left every observer dead a second later. The capture then failed its own
    # liveness check, or collected empty artifacts. The local exec has to stay
    # open for as long as the observer should run.
    local obs_pids=()
    cleanup() {
        local pid name remote_pid tool
        for pid in "${obs_pids[@]}"; do
            kill -TERM "$pid" 2>/dev/null || true
        done
        sleep 1
        for pid in "${obs_pids[@]}"; do
            kill -KILL "$pid" 2>/dev/null || true
        done
        # Stop the tools by name as well. Ending the supervising exec does not
        # reliably take a pipeline with it: a leftover `bpftrace | gzip` keeps
        # tracing, keeps its output growing — into a deleted file once the spool
        # is wiped — and burns enough CPU to block the next arm's quiescence
        # gate. Observed, not theoretical: it cost one arm's quiesce and left a
        # truncated trace behind.
        if [[ -n "${TARGET_POD:-}" ]]; then
            # Producers first. The trace observer is `bpftrace | gzip`, and
            # killing the compressor leaves an incomplete gzip stream: the
            # footer is written when it reaches end of input. Stop the producers,
            # give the compressor a moment to finish, and only then kill it.
            kexec "$TARGET_POD" sh -c '
                for tool in bpftrace perf bpf-runqlat bpf-runqlen; do
                    pkill -x "$tool" 2>/dev/null || true
                done
                sleep 3
                pkill -x gzip 2>/dev/null || true
            ' >/dev/null 2>&1 || true
        fi
        # The recorded remote pids are a final sweep for anything that escaped
        # the name match (the tools run under fixed names, so this is belt and
        # braces rather than the primary kill).
        for name in runqlat runqlen sched-trace perf-sched; do
            [[ -n "${TARGET_POD:-}" && -n "${REMOTE_WORK:-}" ]] || continue
            remote_pid="$(kexec "$TARGET_POD" sh -c \
                "cat ${REMOTE_WORK}/${name}.pid 2>/dev/null" 2>/dev/null || true)"
            [[ -n "$remote_pid" ]] || continue
            kexec "$TARGET_POD" sh -c "kill ${remote_pid} 2>/dev/null || true" \
                >/dev/null 2>&1 || true
        done
    }
    trap cleanup EXIT INT TERM

    start_observer() {
        # Runs in the CURRENT shell (no pipeline, no subshell) so obs_pids
        # survives for the trap that has to kill the supervisors.
        local name="$1" cmd="$2" pid
        kubectl --kubeconfig "$(workload_kubeconfig)" -n "$OBSERVER_NAMESPACE" \
            exec "$target_pod" -- sh -c \
            "mkdir -p ${work}; echo \$\$ > ${work}/${name}.pid; exec ${cmd} </dev/null >${work}/${name}.txt 2>&1" \
            >/dev/null 2>&1 &
        pid=$!
        sleep 2
        if ! kill -0 "$pid" 2>/dev/null; then
            kexec "$target_pod" sh -c "tail -5 ${work}/${name}.txt" >&2 2>&1 || true
            die "${name} exited immediately; see ${work}/${name}.txt"
        fi
        obs_pids+=("$pid")
        log "  ${name}: supervisor pid ${pid}"
    }

    # wait_workload_ready — block until the arm's workload Pod reports Ready.
    #
    # The load used to start against a Pod that was still initialising: nginx
    # not yet accepting connections, PostgreSQL still restoring its snapshot.
    # The sample then measured startup rather than the workload.
    wait_workload_ready() {
        local deadline=$(( SECONDS + LINUX_SCHED_WORKLOAD_READY_TIMEOUT_S )) ready
        while (( SECONDS < deadline )); do
            ready="$(kubectl_wl get pods -l "linux-sched/run=${arm}" \
                -o jsonpath='{range .items[*]}{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' \
                2>/dev/null || true)"
            if [[ "$ready" == *True* ]]; then
                log "workload for arm ${arm} is Ready"
                return 0
            fi
            sleep 2
        done
        kubectl_wl describe pods -l "linux-sched/run=${arm}" >&2 2>/dev/null || true
        die "workload for arm ${arm} never became Ready within ${LINUX_SCHED_WORKLOAD_READY_TIMEOUT_S}s"
    }

    # wait_workload_finished — block until the arm's workload Pods stop running.
    #
    # A podlog-sourced metric is read from the workload's own log, so collecting
    # that log while the job still runs records a partial run.
    wait_workload_finished() {
        local deadline=$(( SECONDS + duration + 60 )) phase
        while (( SECONDS < deadline )); do
            phase="$(kubectl_wl get pods -l "linux-sched/run=${arm}" \
                -o jsonpath='{range .items[*]}{.status.phase}{"\n"}{end}' \
                2>/dev/null || true)"
            if [[ -z "$phase" ]] \
                || [[ "$phase" != *Running* && "$phase" != *Pending* ]]; then
                log "workload for arm ${arm} finished"
                return 0
            fi
            sleep 2
        done
        die "workload for arm ${arm} still running after ${duration}s + 60s"
    }

    # wait_workload_probe — block until the load node can actually reach the
    # workload.
    #
    # A ready Pod and a programmed Service endpoint were not enough: a real warmup
    # then failed with `Connection refused`, because endpoint presence says the
    # routing entry exists, not that the server behind it accepts connections.
    wait_workload_probe() {
        local deadline=$(( SECONDS + LINUX_SCHED_WORKLOAD_READY_TIMEOUT_S ))
        while (( SECONDS < deadline )); do
            if kexec "$load_pod" sh -c "$load_ready_probe" >/dev/null 2>&1; then
                log "load node can reach the workload for arm ${arm}"
                return 0
            fi
            sleep 2
        done
        die "load node cannot reach the workload for arm ${arm} (${load_ready_probe})"
    }

    # wait_workload_endpoints — block until the arm's Service has a ready
    # backend, when the workload exposes one.
    #
    # The load runs on another node and connects through the Service, so Pod
    # readiness is not the precondition that matters: a Ready Pod whose
    # EndpointSlice has not been programmed still refuses connections, which is
    # how the first postgres sample failed.
    wait_workload_endpoints() {
        local deadline=$(( SECONDS + LINUX_SCHED_WORKLOAD_READY_TIMEOUT_S )) ips
        while (( SECONDS < deadline )); do
            ips="$(kubectl_wl get endpoints -l "linux-sched/run=${arm}" \
                -o jsonpath='{range .items[*]}{.subsets[*].addresses[*].ip}{"\n"}{end}' \
                2>/dev/null || true)"
            if [[ -n "$ips" ]]; then
                log "workload Service for arm ${arm} has a ready endpoint"
                return 0
            fi
            sleep 2
        done
        return 1
    }

    # Observers cover the warmup as well as the measured window. If they only
    # covered the measurement, adding a warmup would silently truncate the
    # collection by its length.
    local observer_window=$(( duration + warmup_seconds ))

    if [[ "$instrumentation" == "enabled" ]]; then
        for tool in bpftrace perf bpf-runqlat bpf-runqlen; do
            kexec "$target_pod" sh -c "command -v ${tool} >/dev/null 2>&1" \
                || die "required observer tool '${tool}' missing in ${target_pod}"
        done
        # Stage the scheduler-trace probe and run the file. The previous inline
        # form had an empty body, so it emitted nothing: that artifact was a
        # banner, and the wakeup-to-run and migration data never existed.
        # The spool directory is created by the observer start, so make it here.
        # Wipe it first: it is reused across blocks, so an earlier sample's
        # files would otherwise stay and be copied along with this one.
        kexec "$target_pod" sh -c "rm -rf ${work}; mkdir -p ${work}" >/dev/null \
            || die "could not prepare ${work} in ${target_pod}"
        kubectl_wl cp "${PROBE}" \
            "${OBSERVER_NAMESPACE}/${target_pod}:${work}/sched-trace.bt" >/dev/null \
            || die "could not stage the sched-trace probe into ${target_pod}"
        # The run-queue histograms cover the whole window and cost kilobytes.
        # `stdbuf -oL`: both tools buffer their output when stdout is a file,
        # and the observer is killed rather than allowed to exit, so an
        # unbuffered line mode is what keeps the histogram from being lost.
        start_observer runqlat "stdbuf -oL bpf-runqlat 1 ${observer_window}"
        # The protocol requires run-queue length as well as latency; only
        # runqlat was ever collected.
        start_observer runqlen "stdbuf -oL bpf-runqlen 1 ${observer_window}"
        # The trace and `perf sched` are bounded and start inside the measured
        # period, below: both are large, and starting them here would spend
        # their budget before the workload was even running.
    fi

    if [[ -n "$manifest" ]]; then
        log "applying workload manifest ${manifest}"
        local rendered="${out_dir}/workload-${arm}.yaml"
        sed -e "s|__NODE_NAME__|${target_node}|g" -e "s|__ARM__|${arm}|g" \
            "$manifest" > "$rendered"
        kubectl_wl apply -f "$rendered" || die "failed to apply workload ${manifest}"
        wait_workload_ready
        # Only workloads that expose a Service are reached over the network,
        # and for those the Service endpoint is the real precondition.
        if kubectl_wl get svc -l "linux-sched/run=${arm}" -o name 2>/dev/null \
            | grep -q .; then
            wait_workload_endpoints \
                || die "workload Service for arm ${arm} never got a ready endpoint"
        fi
        if [[ -n "$load_ready_probe" && -n "$load_pod" ]]; then
            wait_workload_probe
        fi
    fi

    # The declared warmup: run the same load for its length and discard the
    # result, so the measured window does not carry the cold start (JIT and
    # connection setup, page-cache warmup) that the scenarios declare a warmup
    # to remove.
    if [[ -n "$warmup_command" && -n "$load_node" ]]; then
        local warm_pod
        warm_pod="$(observer_pod "$load_node")"
        log "warming up on ${warm_pod} for ${warmup_seconds}s (result discarded)"
        LINUX_SCHED_EXEC_TIMEOUT_S="$(( warmup_seconds + 120 ))" \
            kexec "$warm_pod" sh -c "$warmup_command" \
            > "${out_dir}/${arm}.warmup.txt" \
            || die "warmup load failed on ${load_node}"
    fi

    # The trace and `perf sched` run inside the measured period and for bounded
    # windows. Both are the largest artifacts by far and the node's filesystem is
    # small, so a full-window recording does not fit: a workload sharing the node
    # was evicted for ephemeral storage mid-load. `timeout` ends the producer so
    # the compressor behind it finishes its stream cleanly; the raw perf stream is
    # summarised and then removed, leaving its latency report.
    if [[ "$instrumentation" == "enabled" ]]; then
        local perf_window="$LINUX_SCHED_PERF_WINDOW_S"
        local trace_window="$LINUX_SCHED_TRACE_WINDOW_S"
        if (( perf_window > duration )); then perf_window="$duration"; fi
        if (( trace_window > duration )); then trace_window="$duration"; fi
        start_observer sched-trace \
            "sh -c 'timeout ${trace_window} bpftrace ${work}/sched-trace.bt 2>/dev/null | gzip > ${work}/sched-trace.csv.gz'"
        start_observer perf-sched \
            "sh -c 'perf sched record -o ${work}/perf.data -- sleep ${perf_window} 2>&1; perf sched latency -i ${work}/perf.data > ${work}/perf-latency.txt 2>&1; rm -f ${work}/perf.data'"
    fi

    if [[ -n "$load_command" && -n "$load_node" ]]; then
        log "running load on ${load_pod} (${load_node})"
        # The load runs for the whole sample window, so the helper's default
        # exec bound — there to catch a hung control call — must not cut it
        # short. Raise it for this call only.
        LINUX_SCHED_EXEC_TIMEOUT_S="$(( duration + 120 ))" \
            kexec "$load_pod" sh -c "$load_command" > "$load_file" \
            || die "load generator failed on ${load_node}"
    else
        : > "$load_file"
    fi

    # A podlog-sourced metric is read from the workload's own log, so the
    # workload has to have stopped before that log is taken.
    if (( await_completion )); then
        wait_workload_finished
    fi

    if [[ -n "$manifest" ]]; then
        log "collecting workload log for ${arm}"
        kubectl_wl logs -l "linux-sched/run=${arm}" --all-containers=true --tail=-1 > "$podlog_file" \
            || die "failed to collect workload log for arm ${arm}"
        kubectl_wl delete -f "${out_dir}/workload-${arm}.yaml" \
            --ignore-not-found --wait=true --timeout=120s >/dev/null \
            || die "failed to delete workload for arm ${arm}; refusing to leave it running"
    else
        : > "$podlog_file"
    fi

    if [[ "$instrumentation" == "enabled" ]]; then
        # The perf chain post-processes after its recording window ends: it runs
        # `perf sched latency` over the trace and compresses the raw stream. Give
        # the last supervisor time to finish rather than cutting it off, which
        # would lose the summary and leave the raw file uncompressed.
        if [[ "${#obs_pids[@]}" -gt 0 ]]; then
            local last_pid="${obs_pids[${#obs_pids[@]} - 1]}"
            local wait_deadline=$(( SECONDS + 120 ))
            while kill -0 "$last_pid" 2>/dev/null \
                && (( SECONDS < wait_deadline )); do
                sleep 2
            done
        fi
        sleep 2
        cleanup
        obs_pids=()
        # Observer artifacts go under an arm-scoped directory. They used to be
        # copied into the block directory itself, so each arm overwrote the
        # previous arm's evidence and a block kept only the last arm's.
        local obs_dir="${out_dir}/observer-${arm}"
        mkdir -p "$obs_dir"

        # Enforce the spool cap before copying. The node's filesystem is small
        # enough that an unexpectedly large sample must fail loudly rather than
        # fill it and get the observer evicted.
        local spool_mib
        spool_mib="$(kexec "$target_pod" sh -c "du -sm ${work} 2>/dev/null | cut -f1" 2>/dev/null || echo 0)"
        log "  spool ${spool_mib} MiB (cap ${LINUX_SCHED_SPOOL_CAP_MIB} MiB)"
        if [[ "${spool_mib:-0}" =~ ^[0-9]+$ ]] \
            && (( spool_mib > LINUX_SCHED_SPOOL_CAP_MIB )); then
            kexec "$target_pod" sh -c "rm -rf ${work}" >/dev/null 2>&1 || true
            die "observer spool for arm ${arm} is ${spool_mib} MiB, over the ${LINUX_SCHED_SPOOL_CAP_MIB} MiB cap"
        fi

        log "copying capture artifacts into ${obs_dir}"
        kubectl_wl cp "${OBSERVER_NAMESPACE}/${target_pod}:${work}/." "$obs_dir/" \
            || die "failed to copy capture artifacts from ${target_pod}"
        # Free the node's disk: this filesystem is small and a sample is
        # hundreds of megabytes, so holding it after the copy is what filled
        # the node and got the observer evicted.
        kexec "$target_pod" sh -c "rm -rf ${work}" >/dev/null 2>&1 || true
        log "  ${target_node}: $(kexec "$target_pod" sh -c \
            'df -B1 --output=avail /var/tmp | tail -1' 2>/dev/null | tr -d '[:space:]' || echo '?') B free after the sample"

        # A capture is complete only if the required observer outputs exist and
        # are non-empty AND no perf events were lost.
        local status="complete" artifact
        for artifact in runqlat.txt runqlen.txt sched-trace.csv.gz perf-latency.txt; do
            if [[ ! -s "${obs_dir}/${artifact}" ]]; then
                log "ERROR: observer artifact ${artifact} is missing or empty for arm ${arm}"
                status="failed"
            fi
        done
        if grep -qiE 'lost [1-9]|dropped [1-9]' "${obs_dir}/perf-sched.txt" 2>/dev/null; then
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
