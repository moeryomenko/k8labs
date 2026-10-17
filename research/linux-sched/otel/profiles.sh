#!/usr/bin/env bash
#
# profiles.sh — the OTel profiling campaign: fifteen labelled sessions, one at a
# time, never beside a comparison block.
#
# Each session:
#   1. refuses to start if a comparison block is running (the plan forbids
#      overlap, and the two would contend for the same worker),
#   2. renders the session's labels into Alloy's configuration and rolls the
#      profiling DaemonSet, so the labels Pyroscope stores are the labels
#      sessions.json records,
#   3. records the UTC start, runs the scenario's workload for SESSION_SECONDS on
#      the session's arm, and records the UTC end,
#   4. requires Alloy to be healthy, its profile counter to have advanced and its
#      failing-session counter to be unchanged,
#   5. appends the session to sessions.json, and stops ingest at 9 GiB.
#
# The workload phase reuses the comparison capture with instrumentation disabled:
# the session needs the same workload, the same arm activation and the same
# readiness handling the measured blocks use, and reusing it keeps the two from
# drifting apart.

set -Eeuo pipefail
shopt -s inherit_errexit
IFS=$'\n\t'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
LINUX_SCHED_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
CONFIGS_DIR="${LINUX_SCHED_DIR}/experiments/scenarios"
CAPTURE="${LINUX_SCHED_DIR}/capture/capture-block.sh"
source "${LINUX_SCHED_DIR}/experiments/lib.sh"

SESSION_SECONDS="${OTEL_SESSION_SECONDS:-60}"
PROFILE_VOLUME="${PROFILE_VOLUME:?set PROFILE_VOLUME to the host-owned profile directory}"
PYROSCOPE_URL="${PYROSCOPE_URL:-http://127.0.0.1:4040}"
STOP_BYTES="${OTEL_STOP_BYTES:-9663676416}"   # 9 GiB
ALLOY_TIMEOUT_S="${OTEL_ALLOY_TIMEOUT_S:-300}"
SCENARIOS="${OTEL_SCENARIOS:-http postgres ffmpeg stress-ng mixed-http-ffmpeg}"

DATA_ROOT="${K8LABS_RESEARCH_DATA_ROOT:+${K8LABS_RESEARCH_DATA_ROOT}/linux-sched}"
DATA_ROOT="${DATA_ROOT:-$(project_root)/build/research/linux-sched}"
RECORD="${DATA_ROOT}/sessions.json"
PROFILE_TMP="$(mktemp -d)"
trap 'rm -rf "${PROFILE_TMP}"' EXIT

# resolve_targets — pick the measured target and the load generator the same way
# a comparison block does: the roomiest worker is the target.
resolve_targets() {
    local workers ranked node free
    workers="$(ready_workers)"
    [[ -n "$workers" ]] || die "no Ready worker nodes found"
    ranked=""
    while IFS= read -r node; do
        [[ -n "$node" ]] || continue
        free="$(guest_free_bytes "$node")"
        [[ "$free" =~ ^[0-9]+$ ]] || free=0
        ranked+="${free} ${node}"$'\n'
    done < <(printf '%s\n' "$workers")
    local -a ordered=()
    while IFS= read -r line; do
        [[ -n "$line" ]] || continue
        ordered+=( "${line#* }" )
    done < <(printf '%s' "$ranked" | sort -k1,1rn -k2,2)
    TARGET_NODE="${ordered[0]:-}"
    LOAD_NODE="${ordered[1]:-}"
    [[ -n "$TARGET_NODE" ]] || die "no observer-labelled worker found"
}

# activate_arm ARM — make ARM the active scheduler on the target worker. Mirrors
# the measured runner: EEVDF means no sched_ext scheduler attached.
activate_arm() {
    local arm="$1"
    case "$arm" in
        eevdf)
            scx_reset "$TARGET_NODE"
            ;;
        scx_bpfland|scx_lavd)
            scx_start "$TARGET_NODE" "$arm" >/dev/null
            ;;
        scx_mavd)
            die "scx_mavd is deferred (see GATES.md)"
            ;;
        *)
            die "unknown scheduler arm: ${arm}"
            ;;
    esac
}

worker_kernel() {
    kubectl get node "$1" -o jsonpath='{.status.nodeInfo.kernelVersion}'
}

# workload_image SCENARIO — the first image in the scenario's workload manifest
# that is not the load generator's, recorded as the session's `image` label.
workload_image() {
    local scenario="$1" manifest
    manifest="${LINUX_SCHED_DIR}/$(grep -h '^WORKLOAD_MANIFEST=' "${CONFIGS_DIR}/${scenario}.conf" | cut -d= -f2- | tr -d '"')"
    [[ -f "$manifest" ]] || { printf 'unknown'; return 0; }
    awk '/^ *image: /{print $2; exit}' "$manifest"
}

refuse_if_comparing() {
    if pgrep -f 'run-block\.sh run' >/dev/null 2>&1 \
        || pgrep -f 'capture-block\.sh' >/dev/null 2>&1; then
        die "a comparison block is running; the profiling campaign must not overlap it"
    fi
}

profile_bytes() { du -sb "$PROFILE_VOLUME" 2>/dev/null | awk '{print $1}'; }

start_forward() {
    local pod port
    pod="$(kubectl -n "$OBSERVER_NAMESPACE" get pods \
        -l app.kubernetes.io/name=linux-sched-otel-alloy \
        -o jsonpath='{.items[0].metadata.name}')"
    [[ -n "$pod" ]] || die "no Alloy pod; run 'make linux-sched-otel-apply' first"
    port="$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
    kubectl -n "$OBSERVER_NAMESPACE" port-forward "pod/${pod}" "${port}:12345" \
        >"${PROFILE_TMP}/forward.log" 2>&1 &
    FORWARD_PID=$!
    local deadline=$(( SECONDS + 90 ))
    while (( SECONDS < deadline )); do
        if curl -sS -m 3 "http://127.0.0.1:${port}/-/healthy" >/dev/null 2>&1; then
            printf '%s' "$port"
            return 0
        fi
        sleep 1
    done
    die "Alloy port-forward never became reachable"
}

stop_forward() {
    [[ -n "${FORWARD_PID:-}" ]] || return 0
    kill "$FORWARD_PID" 2>/dev/null || true
    wait "$FORWARD_PID" 2>/dev/null || true
    FORWARD_PID=""
}

alloy_metric() {
    curl -sS -m 5 "http://127.0.0.1:$1/metrics" 2>/dev/null \
        | awk -v m="$2" '$1 ~ "^"m { print $2; exit }'
}

session_health() {
    local body
    body="$(curl -sS -m 5 "http://127.0.0.1:$1/-/healthy" 2>/dev/null || true)"
    [[ "$body" == *"All Alloy components are healthy."* ]] \
        || die "Alloy is not healthy: ${body:-<no response>}"
}

render_alloy() {
    local workload="$1" arm="$2" node="$3" kernel="$4" image="$5" run_id="$6"
    local rendered="${PROFILE_TMP}/config.alloy"
    sed -e "s|__WORKLOAD__|${workload}|g" -e "s|__SCHEDULER__|${arm}|g" \
        -e "s|__NODE__|${node}|g" -e "s|__KERNEL__|${kernel}|g" \
        -e "s|__IMAGE__|${image}|g" -e "s|__RUN_ID__|${run_id}|g" \
        -e "s|__PYROSCOPE_URL__|${PYROSCOPE_URL}|g" \
        "${SCRIPT_DIR}/k8s/alloy-config.alloy.tmpl" > "$rendered"
    kubectl -n "$OBSERVER_NAMESPACE" create configmap linux-sched-otel-alloy \
        --from-file=config.alloy="$rendered" --dry-run=client -o yaml \
        | kubectl -n "$OBSERVER_NAMESPACE" apply -f - >/dev/null
    kubectl -n "$OBSERVER_NAMESPACE" rollout restart daemonset/linux-sched-otel-alloy >/dev/null
    kubectl -n "$OBSERVER_NAMESPACE" rollout status daemonset/linux-sched-otel-alloy \
        --timeout="${ALLOY_TIMEOUT_S}s" >/dev/null \
        || die "Alloy did not roll out with the session's labels"
}

record_session() {
    python3 - "$RECORD" <<PY
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
sessions = json.loads(path.read_text()) if path.exists() else []
sessions.append({
    "service_name": "linux-sched-profile",
    "workload": "$1",
    "scheduler": "$2",
    "node": "$3",
    "kernel": "$4",
    "image": "$5",
    "run_id": "$6",
    "start_utc": "$7",
    "end_utc": "$8",
    "seconds": $SESSION_SECONDS,
    "target_node": "$TARGET_NODE",
    "profile_bytes_before": int("$9" or 0),
    "profile_bytes_after": int("${10}" or 0),
})
path.write_text(json.dumps(sessions, indent=2) + "\n")
PY
}

main() {
    require_cmd kubectl curl jq python3 du awk sed
    mkdir -p "$DATA_ROOT"

    local scenario arm
    for scenario in $(split_words "$SCENARIOS"); do
        for arm in $(split_words "$LINUX_SCHED_ARMS"); do
            refuse_if_comparing
            load_scenario "$CONFIGS_DIR" "$scenario"
            resolve_targets

            local run_id kernel image
            run_id="$(cat /proc/sys/kernel/random/uuid)"
            kernel="$(worker_kernel "$TARGET_NODE")"
            image="$(workload_image "$scenario")"

            local rate=""
            if load_needs_rate; then
                rate="$(frozen_offered_rate "$DATA_ROOT" "$SCENARIO_NAME")"
            fi

            log "session ${scenario}/${arm}: preparing Alloy labels (node ${TARGET_NODE}, run_id ${run_id})"
            render_alloy "$scenario" "$arm" "$TARGET_NODE" "$kernel" "$image" "$run_id"
            local port sent_before failed_before bytes_before
            port="$(start_forward)"
            session_health "$port"
            sent_before="$(alloy_metric "$port" 'pyroscope_ebpf_pprofs_total' || echo 0)"
            failed_before="$(alloy_metric "$port" 'pyroscope_ebpf_profiling_sessions_failing_total' || echo 0)"
            bytes_before="$(profile_bytes)"

            activate_arm "$arm"
            local start end
            start="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
            "${CAPTURE}" \
                --target-node "$TARGET_NODE" --duration "$SESSION_SECONDS" \
                --out-dir "${PROFILE_TMP}/sessions/${scenario}-${arm}" \
                --arm "otel-${arm}" --load-node "${LOAD_NODE:-}" \
                --manifest "${LINUX_SCHED_DIR}/${WORKLOAD_MANIFEST}" \
                --load-command "$(render_load_command "$rate" "$SESSION_SECONDS")" \
                --load-ready-probe "${LOAD_READY_PROBE:-}" \
                --instrumentation disabled \
                --raw "${PROFILE_TMP}/${scenario}-${arm}.txt" \
                --podlog "${PROFILE_TMP}/${scenario}-${arm}.log" \
                || die "session workload failed for ${scenario}/${arm}"
            end="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
            scx_reset "$TARGET_NODE"
            stop_forward
            wait_quiescent "$TARGET_NODE" || true

            local sent_after failed_after bytes_after
            port="$(start_forward)"
            sent_after="$(alloy_metric "$port" 'pyroscope_ebpf_pprofs_total' || echo 0)"
            failed_after="$(alloy_metric "$port" 'pyroscope_ebpf_profiling_sessions_failing_total' || echo 0)"
            stop_forward
            bytes_after="$(profile_bytes)"

            (( sent_after > sent_before )) \
                || die "session ${scenario}/${arm} collected no profiles (pprofs ${sent_before} -> ${sent_after})"
            (( failed_after == failed_before )) \
                || die "session ${scenario}/${arm} had a failing profiling session (${failed_before} -> ${failed_after})"

            record_session "$scenario" "$arm" "$TARGET_NODE" "$kernel" "$image" \
                "$run_id" "$start" "$end" "$bytes_before" "$bytes_after"
            log "session ${scenario}/${arm} recorded (pprofs ${sent_before} -> ${sent_after}, ${bytes_after} B stored)"

            if (( bytes_after >= STOP_BYTES )); then
                log "profile volume at ${bytes_after} B, at or above the 9 GiB stop threshold; stopping ingest"
                exit 0
            fi
        done
    done
    log "profiling campaign complete: $(jq 'length' "$RECORD") session(s) in ${RECORD}"
}

main "$@"
