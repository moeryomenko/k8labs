#!/usr/bin/env bash
#
# run-block.sh — linux-sched paired-block experiment driver.
#
# Usage:
#   run-block.sh --validate --configs-dir DIR
#   run-block.sh --list     --configs-dir DIR
#   run-block.sh plan       --scenario NAME --mode pilot|confirmatory \
#                           --configs-dir DIR --data-root DIR
#   run-block.sh run        --scenario NAME --mode pilot|confirmatory \
#                           --configs-dir DIR --data-root DIR
#   run-block.sh overhead   --scenario NAME --configs-dir DIR --data-root DIR
#
# `plan` computes the full seeded paired-block schedule and prints it; it needs
# no cluster. `run` performs the protocol and fails loudly on any missing
# prerequisite rather than reporting a fake success.

set -Eeuo pipefail
shopt -s inherit_errexit
IFS=$'\n\t'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=./lib.sh
source "${SCRIPT_DIR}/lib.sh"

EXPERIMENTS_DIR="${SCRIPT_DIR}"

usage() {
    sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

# ---- plan ------------------------------------------------------------------

# block_count MODE — number of paired blocks for the run mode.
block_count() {
    case "$1" in
        pilot) echo "${LINUX_SCHED_PILOT_BLOCKS}" ;;
        confirmatory) echo "${LINUX_SCHED_REQUIRED_BLOCKS:-${LINUX_SCHED_MIN_BLOCKS}}" ;;
        *) die "unknown mode: $1" ;;
    esac
}

# print_plan SCENARIO MODE — deterministic seeded schedule.
print_plan() {
    local scenario="$1" mode="$2"
    local blocks seed
    blocks="$(block_count "$mode")"
    printf 'scenario=%s mode=%s blocks=%s arms=%s\n' \
        "$scenario" "$mode" "$blocks" "$LINUX_SCHED_ARMS"
    local b
    for (( b = 1; b <= blocks; b++ )); do
        seed=$(( LINUX_SCHED_PLAN_SEED + b ))
        printf '  block=%s seed=%s order=%s\n' \
            "$b" "$seed" "$(seeded_permutation "$seed")"
    done
}

# ---- validation ------------------------------------------------------------

cmd_validate() {
    local dir="$1" name
    local count=0
    for name in $(scenario_names "$dir"); do
        (
            # shellcheck disable=SC1090
            source "${dir}/${name}.conf"
            : "${SCENARIO_NAME:?}"
            : "${WORKLOAD:?}"
            : "${PRIMARY_METRIC:?}"
            : "${PRIMARY_DIRECTION:?}"
            : "${DURATION_S:?}"
            case "${PRIMARY_DIRECTION}" in
                lower-is-better|higher-is-better) ;;
                *) echo "  [FAIL] ${name}: bad PRIMARY_DIRECTION=${PRIMARY_DIRECTION}" >&2; exit 1 ;;
            esac
            case "${PRIMARY_SOURCE:-load}" in
                load|podlog) ;;
                *) echo "  [FAIL] ${name}: bad PRIMARY_SOURCE=${PRIMARY_SOURCE}" >&2; exit 1 ;;
            esac
            [[ "${SCENARIO_NAME}" == "${name}" ]] \
                || { echo "  [FAIL] ${name}: SCENARIO_NAME=${SCENARIO_NAME} != ${name}" >&2; exit 1; }
            [[ "${WORKLOAD_MANIFEST:-}" == "" || -f "${SCRIPT_DIR}/../${WORKLOAD_MANIFEST}" ]] \
                || { echo "  [FAIL] ${name}: missing manifest ${WORKLOAD_MANIFEST}" >&2; exit 1; }
        ) || die "scenario ${name} failed validation"
        printf '  [OK] %s\n' "$name"
        count=$(( count + 1 ))
    done
    (( count > 0 )) || die "no scenario configs found in ${dir}"
}

cmd_list() {
    local dir="$1" name
    for name in $(scenario_names "$dir"); do
        (
            # shellcheck disable=SC1090
            source "${dir}/${name}.conf"
            printf '%-20s metric=%-24s direction=%-16s %s\n' \
                "$SCENARIO_NAME" "$PRIMARY_METRIC" "$PRIMARY_DIRECTION" "$WORKLOAD"
        )
    done
}

# ---- execution -------------------------------------------------------------

# resolve_hosts — map TARGET_HOST/LOAD_HOST to reachable node IPs.
resolve_hosts() {
    local workers
    workers="$(ready_workers)"
    [[ -n "$workers" ]] || die "no Ready worker nodes found"
    local target load
    target="$(printf '%s\n' "$workers" | sed -n '1p')"
    load="$(printf '%s\n' "$workers" | sed -n '2p')"
    TARGET_NODE="$target"
    LOAD_NODE="$load"
    [[ -n "$TARGET_NODE" ]] || die "no observer-labelled worker found; run 'make linux-sched-observers-apply'"
    log "target=${TARGET_NODE} load=${LOAD_NODE:-none}"
}

# activate_arm ARM — make ARM the active scheduler on the target worker.
#
# Runs through the observer Pod (hostPID, so it sees host processes). EEVDF
# means "no sched_ext scheduler loaded"; the scx arms start the staged binary
# and wait for it to attach before the sample begins.
activate_arm() {
    local arm="$1"
    case "$arm" in
        eevdf)
            scx_reset "$TARGET_NODE"
            ;;
        scx_bpfland|scx_lavd)
            SCX_PID="$(scx_start "$TARGET_NODE" "$arm")"
            log "started ${arm} (pid ${SCX_PID}) on ${TARGET_NODE}"
            ;;
        scx_mavd)
            die "scx_mavd is deferred: its object sets BPF_F_ARENA_SCALAR, which " \
                "the pinned kernel rejects (see GATES.md); set LINUX_SCHED_ARMS to " \
                "include it only on a kernel that has that flag"
            ;;
        *)
            die "unknown scheduler arm: ${arm}"
            ;;
    esac
}

# collect_metric SCENARIO_RAW METRIC_COMMAND DURATION_S OUT_FILE
collect_metric() {
    local raw="$1" metric_command="$2" duration="$3" out="$4"
    python3 "${SCRIPT_DIR}/metrics.py" \
        --metric-command "$metric_command" --raw "$raw" --duration-s "$duration" > "$out"
}

# run_block SCENARIO ARM BLOCK DIR — one arm of one paired block.
run_block() {
    local scenario="$1" arm="$2" block="$3" raw_dir="$4"
    SCX_PID=""

    log "block ${block} arm ${arm}: resetting ${TARGET_NODE}"
    # No reboot: SSH is unavailable and the lab has no other way to restart a
    # VM, so each arm starts from a killed-observer, quiescent guest instead.
    # Recorded as a protocol deviation in GATES.md.
    scx_reset "$TARGET_NODE"

    wait_quiescent "$TARGET_NODE" || die "guest ${TARGET_NODE} never became quiescent"

    activate_arm "$arm"

    local load_raw="${raw_dir}/${arm}.txt"
    local podlog_raw="${raw_dir}/${arm}.log"
    "${SCRIPT_DIR}/../capture/capture-block.sh" \
        --target-node "$TARGET_NODE" --duration "$DURATION_S" --out-dir "$raw_dir" \
        --arm "$arm" --load-node "${LOAD_NODE:-}" \
        --manifest "${SCRIPT_DIR}/../${WORKLOAD_MANIFEST}" \
        --load-command "${LOAD_COMMAND:-}${LOAD_ARGS:+ ${LOAD_ARGS}}" \
        --raw "$load_raw" --podlog "$podlog_raw" \
        || die "capture failed for block ${block} arm ${arm}"

    # Stop the scheduler so the next arm starts from a clean scheduler state.
    scx_reset "$TARGET_NODE"

    # Parse only the artifact the primary metric is defined on, so a
    # concatenated log can never be mined for a stale value.
    local metric_source
    case "${PRIMARY_SOURCE:-load}" in
        load) metric_source="$load_raw" ;;
        podlog) metric_source="$podlog_raw" ;;
        *) die "scenario ${SCENARIO_NAME}: bad PRIMARY_SOURCE=${PRIMARY_SOURCE}" ;;
    esac

    collect_metric "$metric_source" "$METRIC_COMMAND" "$DURATION_S" \
        "${raw_dir}/${arm}.metric" \
        || die "could not extract primary metric for block ${block} arm ${arm}"

    append_block_row "$BLOCKS_CSV" "$block" "$SCENARIO_NAME" "$PRIMARY_METRIC" \
        "$arm" "$(cat "${raw_dir}/${arm}.metric")" "$PRIMARY_DIRECTION"
}

cmd_plan() {
    local scenario="$1" mode="$2" dir="$3"
    [[ -f "${dir}/${scenario}.conf" ]] || die "scenario not found: ${dir}/${scenario}.conf"
    print_plan "$scenario" "$mode"
}

cmd_run() {
    local scenario="$1" mode="$2" dir="$3" data_root="$4"
    load_scenario "$dir" "$scenario"
    require_cmd kubectl python3 ssh
    resolve_hosts

    case "$data_root" in
        "$(project_root)"/*) die "data root must live outside the Git worktree: ${data_root}" ;;
    esac

    BLOCKS_CSV="${data_root}/blocks.csv"
    mkdir -p "$data_root"

    local blocks; blocks="$(block_count "$mode")"
    local b arm seed
    for (( b = 1; b <= blocks; b++ )); do
        seed=$(( LINUX_SCHED_PLAN_SEED + b ))
        local raw_dir="${data_root}/${SCENARIO_NAME}/block-$(printf '%02d' "$b")"
        mkdir -p "$raw_dir"
        printf '{"scenario":"%s","block":%s,"seed":%s,"mode":"%s"}\n' \
            "$SCENARIO_NAME" "$b" "$seed" "$mode" > "${raw_dir}/metadata.json"
        # IFS excludes the space, so the arm order must be split explicitly.
        mapfile -t arms < <(split_words "$(seeded_permutation "$seed")")
        for arm in "${arms[@]}"; do
            run_block "$SCENARIO_NAME" "$arm" "$b" "$raw_dir"
        done
        log "block ${b} complete; cooldown ${LINUX_SCHED_COOLDOWN_S}s"
        sleep "$LINUX_SCHED_COOLDOWN_S"
    done
    log "all ${blocks} blocks complete for ${SCENARIO_NAME}; blocks.csv: ${BLOCKS_CSV}"
}

cmd_overhead() {
    local scenario="$1" dir="$2" data_root="$3"
    load_scenario "$dir" "$scenario"
    require_cmd kubectl python3 ssh
    resolve_hosts
    local out="${data_root}/${SCENARIO_NAME}/overhead"
    mkdir -p "$out"
    log "observer-overhead calibration for ${SCENARIO_NAME} on EEVDF"
    local variant
    for variant in instruments-enabled instruments-disabled; do
        "${SCRIPT_DIR}/../capture/capture-block.sh" \
            --target-node "$TARGET_NODE" --duration "$DURATION_S" --out-dir "$out" \
            --arm "eevdf-${variant}" --load-node "${LOAD_NODE:-}" \
            --manifest "${SCRIPT_DIR}/../${WORKLOAD_MANIFEST}" \
            --load-command "${LOAD_COMMAND:-}${LOAD_ARGS:+ ${LOAD_ARGS}}" \
            --raw "${out}/${variant}.txt" --podlog "${out}/${variant}.log" \
            --instrumentation "$variant" \
            || die "overhead ${variant} capture failed"
        local metric_source="${out}/${variant}.txt"
        [[ "${PRIMARY_SOURCE:-load}" == "podlog" ]] && metric_source="${out}/${variant}.log"
        collect_metric "$metric_source" "$METRIC_COMMAND" "$DURATION_S" \
            "${out}/${variant}.metric" \
            || die "overhead ${variant} metric extraction failed"
    done
    log "overhead calibration complete: ${out}"
}

# ---- argument parsing ------------------------------------------------------

LINUX_SCHED_PLAN_SEED="${LINUX_SCHED_PLAN_SEED:-20261015}"

main() {
    local subcmd="" scenario="" dir="" data_root="" run_mode="pilot"
    if [[ $# -eq 0 ]]; then usage 1; fi

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --help|-h) usage 0 ;;
            --validate) subcmd="validate"; shift ;;
            --list) subcmd="list"; shift ;;
            plan|run|overhead) subcmd="$1"; shift ;;
            --scenario) scenario="$2"; shift 2 ;;
            --mode) run_mode="$2"; shift 2 ;;
            --configs-dir) dir="$2"; shift 2 ;;
            --data-root) data_root="$2"; shift 2 ;;
            *) die "unknown argument: $1" ;;
        esac
    done

    [[ -n "$dir" ]] || die "--configs-dir is required"
    case "$subcmd" in
        validate) cmd_validate "$dir" ;;
        list) cmd_list "$dir" ;;
        plan)
            [[ -n "$scenario" ]] || die "--scenario is required"
            cmd_plan "$scenario" "$run_mode" "$dir"
            ;;
        run)
            [[ -n "$scenario" ]] || die "--scenario is required"
            [[ -n "$data_root" ]] || die "--data-root is required"
            cmd_run "$scenario" "$run_mode" "$dir" "$data_root"
            ;;
        overhead)
            [[ -n "$scenario" ]] || die "--scenario is required"
            [[ -n "$data_root" ]] || die "--data-root is required"
            cmd_overhead "$scenario" "$dir" "$data_root"
            ;;
        *) die "no subcommand given; see --help" ;;
    esac
}

main "$@"
