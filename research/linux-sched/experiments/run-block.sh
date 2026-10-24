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
#   run-block.sh calibrate  --scenario NAME --configs-dir DIR --data-root DIR
#
# `plan` computes the full seeded paired-block schedule and prints it; it needs
# no cluster. `run` performs the protocol and fails loudly on any missing
# prerequisite rather than reporting a fake success.
#
# `calibrate` freezes the offered rate for a scenario whose load carries one,
# on the control arm, before any measured block runs.

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
#
# `smoke` is a pipeline check, not an experiment: one block to prove the whole
# path works, written to its own tree. Keeping it out of the pilot tree matters,
# because the pilot is what the confirmatory block count is derived from.
block_count() {
    case "$1" in
        smoke) echo "${LINUX_SCHED_SMOKE_BLOCKS:-1}" ;;
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
            "$b" "$seed" "$(balanced_block_order "$b" | tr '\n' ' ')"
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

# resolve_hosts — choose the measured target and the load generator.
#
# The target is the worker with the most free space, not simply the first by
# name: the 5 GiB filesystem is the binding constraint, and choosing a fuller
# node makes the storage gate fail for a reason that has nothing to do with the
# experiment. The remaining worker becomes the load generator. The choice is
# recorded because it must stay fixed across the study.
resolve_hosts() {
    local workers
    workers="$(ready_workers)"
    [[ -n "$workers" ]] || die "no Ready worker nodes found"

    # A pinned assignment wins: the study must measure one fixed target, and a
    # per-invocation re-ranking would silently move it between calibrations,
    # pilots and confirmatory blocks.
    if [[ -n "${LINUX_SCHED_TARGET_NODE:-}" ]]; then
        printf '%s\n' "$workers" | grep -qx "$LINUX_SCHED_TARGET_NODE" \
            || die "pinned target ${LINUX_SCHED_TARGET_NODE} is not a Ready observer worker"
        TARGET_NODE="$LINUX_SCHED_TARGET_NODE"
        if [[ -n "${LINUX_SCHED_LOAD_NODE:-}" ]]; then
            LOAD_NODE="$LINUX_SCHED_LOAD_NODE"
        else
            LOAD_NODE="$(printf '%s\n' "$workers" | grep -vx "$TARGET_NODE" | head -1)"
        fi
        log "target=${TARGET_NODE} load=${LOAD_NODE:-none} (pinned)"
        return 0
    fi

    local node free best_node="" best_free=-1 ordered=""
    local -a ranked=() ordered_nodes=()
    while IFS= read -r node; do
        [[ -n "$node" ]] || continue
        free="$(guest_free_bytes "$node")"
        [[ "$free" =~ ^[0-9]+$ ]] || free=0
        ranked+=("${free} ${node}")
    done < <(printf '%s\n' "$workers")

    # Sort by free space descending; ties fall back to the node name so the
    # result is deterministic.
    while IFS= read -r line; do
        ordered+="${line#* }"$'\n'
    done < <(printf '%s\n' "${ranked[@]}" | sort -k1,1rn -k2,2)

    mapfile -t ordered_nodes < <(printf '%s' "$ordered")
    best_node="${ordered_nodes[0]:-}"
    [[ -n "$best_node" ]] || die "no observer-labelled worker found; run 'task linux-sched-observers-apply'"

    TARGET_NODE="$best_node"
    LOAD_NODE="${ordered_nodes[1]:-}"
    log "target=${TARGET_NODE} load=${LOAD_NODE:-none}"
    local summary=""
    while IFS= read -r line; do
        summary+="${line#* }=$(printf '%s' "${line%% *}")B "
    done < <(printf '%s\n' "${ranked[@]}" | sort -k1,1rn -k2,2)
    log "  free space by worker: ${summary}"
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
        scx_bpfland|scx_mlfq)
            SCX_PID="$(scx_start "$TARGET_NODE" "$arm")"
            log "started ${arm} (pid ${SCX_PID}) on ${TARGET_NODE}"
            ;;
        scx_lavd|scx_mavd)
            # These two honour cpu.max only when asked, and only if the kernel
            # forwards ops.cgroup_set_bandwidth. Without the flag they ignore the
            # quota treatment entirely, which would make the limited policy a
            # no-op for the arm.
            local bw_args=()
            if kernel_cgroup_bw_supported "$TARGET_NODE"; then
                bw_args+=(--enable-cpu-bw)
                log "  ${arm}: cpu.max support present, enabling bandwidth"
            else
                log "  ${arm}: kernel has no ops.cgroup_set_bandwidth; cpu.max will be ignored"
            fi
            SCX_PID="$(scx_start "$TARGET_NODE" "$arm" "${bw_args[@]}")"
            log "started ${arm} (pid ${SCX_PID}) on ${TARGET_NODE}"
            ;;
        scx_nitosis)
            # Nitosis receives the Kubernetes workload cgroup parent so its
            # cells map onto pod scopes rather than the host root.
            NITOSIS_CGROUP_PARENT="${LINUX_SCHED_NITOSIS_CGROUP_PARENT:-/kubepods.slice}"
            SCX_PID="$(scx_start "$TARGET_NODE" "$arm" --cell-parent-cgroup "$NITOSIS_CGROUP_PARENT")"
            log "started ${arm} (pid ${SCX_PID}) on ${TARGET_NODE} (cell parent ${NITOSIS_CGROUP_PARENT})"
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
        --metric-command "$metric_command" --raw "$raw" --duration-s "$duration" \
        --warmup-s "${WARMUP_S:-0}" > "$out"
}

# collect_secondary SCENARIO ARM RAW_DIR — extract a scenario's secondary
# outcome, if it declares one, into a CSV the frozen analysis never reads.
#
# The mixed scenario is the only user: its batch throughput is reported beside
# the primary verdict, not as part of it, so it lives in its own file rather
# than in blocks.csv where the decision rule would pick it up as a contrast.
collect_secondary() {
    local scenario="$1" arm="$2" raw_dir="$3" block="$4" policy="${5:-}"
    [[ -n "${SECONDARY_METRIC_COMMAND:-}" ]] || return 0
    local suffix=""
    [[ -n "$policy" ]] && suffix="-${policy}"
    local source="${raw_dir}/${arm}${suffix}.log"
    [[ "${SECONDARY_SOURCE:-podlog}" == "load" ]] && source="${raw_dir}/${arm}${suffix}.txt"
    local csv="${DATA_ROOT}/secondary-${scenario}.csv"
    if [[ ! -s "$csv" ]]; then
        printf 'block_id,scheduler,workload,metric,value,direction,policy\n' > "$csv"
    fi
    local value
    value="$(python3 "${SCRIPT_DIR}/metrics.py" \
        --metric-command "$SECONDARY_METRIC_COMMAND" --raw "$source" \
        --duration-s "$DURATION_S" --warmup-s "${WARMUP_S:-0}")" \
        || die "could not extract secondary metric for block ${block} arm ${arm}"
    printf '%s,%s,%s,%s,%s,%s,%s\n' \
        "$block" "$arm" "$scenario" "$SECONDARY_METRIC_COMMAND" "$value" \
        "${SECONDARY_DIRECTION:-higher-is-better}" "$policy" >> "$csv"
}

# run_block SCENARIO ARM BLOCK DIR [POLICY] — one arm of one paired block.
#
# POLICY is empty for the scheduler-only series. For the quota campaign it is
# "unlimited" or "limited"; the limited run reads the frozen quota for the
# workload and fails closed when the manifest is missing.
run_block() {
    local scenario="$1" arm="$2" block="$3" raw_dir="$4" policy="${5:-}"
    SCX_PID=""
    local suffix=""
    [[ -n "$policy" ]] && suffix="-${policy}"

    export LINUX_SCHED_CPU_POLICY="$policy"
    case "$policy" in
        "")
            ;;
        unlimited)
            unset LINUX_SCHED_CPU_LIMIT || true
            log "  policy unlimited: no cpu.max"
            ;;
        limited)
            LINUX_SCHED_CPU_LIMIT="$(frozen_quota_for "$scenario")" \
                || die "no frozen quota for ${scenario}"
            export LINUX_SCHED_CPU_LIMIT
            log "  policy limited: cpu.max=${LINUX_SCHED_CPU_LIMIT}"
            ;;
        *)
            die "unknown policy: ${policy}"
            ;;
    esac

    log "block ${block} arm ${arm}: resetting ${TARGET_NODE}"
    # No reboot: SSH is unavailable and the lab has no other way to restart a
    # VM, so each arm starts from a killed-observer, quiescent guest instead.
    # Recorded as a protocol deviation in GATES.md.
    scx_reset "$TARGET_NODE"

    # Refuse to start a sample the worker cannot hold: the measured failure was a
    # workload Pod evicted for ephemeral-storage mid-load.
    local free_bytes
    free_bytes="$(require_guest_disk "$TARGET_NODE")"
    log "  ${TARGET_NODE}: ${free_bytes} B free (gate $(required_guest_free_bytes) B)"

    wait_quiescent "$TARGET_NODE" || die "guest ${TARGET_NODE} never became quiescent"

    activate_arm "$arm"

    # A scenario whose load carries a rate placeholder must run on the frozen
    # calibration value, never on a guess. The calibration is a property of the
    # scenario, not of a phase, so it is read from the raw data root while the
    # phase root holds this phase's results.
    local rate=""
    if load_needs_rate; then
        rate="$(frozen_offered_rate "$RAW_DATA_ROOT" "$SCENARIO_NAME")"
    fi
    local load_command warmup_args=()
    load_command="$(render_load_command "$rate" "$DURATION_S")"
    # The scenarios declare a warmup; without it the measured window carries the
    # cold start. The same load runs for WARMUP_S first and its result is thrown
    # away, so the metric describes a warmed system.
    if [[ "${WARMUP_S:-0}" -gt 0 ]]; then
        warmup_args=(--warmup-command "$(render_load_command "$rate" "$WARMUP_S")"
                     --warmup-seconds "$WARMUP_S")
    fi

    local await_completion=()
    if [[ "${PRIMARY_SOURCE:-load}" == "podlog" ]]; then
        # The metric is read from the workload's own log, so the workload has
        # to finish before the log is collected.
        await_completion=(--await-workload-completion)
    fi

    local load_raw="${raw_dir}/${arm}${suffix}.txt"
    local podlog_raw="${raw_dir}/${arm}${suffix}.log"
    "${SCRIPT_DIR}/../capture/capture-block.sh" \
        --target-node "$TARGET_NODE" --duration "$DURATION_S" --out-dir "$raw_dir" \
        --arm "$arm" --load-node "${LOAD_NODE:-}" \
        --manifest "${SCRIPT_DIR}/../${WORKLOAD_MANIFEST}" \
        --load-command "$load_command" \
        "${await_completion[@]}" \
        "${warmup_args[@]}" \
        --load-ready-probe "${LOAD_READY_PROBE:-}" \
        --raw "$load_raw" --podlog "$podlog_raw" \
        --cpu-stat-before "${raw_dir}/${arm}${suffix}.cpu-before.stat" \
        --cpu-stat-after "${raw_dir}/${arm}${suffix}.cpu-after.stat" \
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
        "${raw_dir}/${arm}${suffix}.metric" \
        || die "could not extract primary metric for block ${block} arm ${arm}"

    append_block_row "$BLOCKS_CSV" "$block" "$SCENARIO_NAME" "$PRIMARY_METRIC" \
        "$arm" "$(cat "${raw_dir}/${arm}${suffix}.metric")" "$PRIMARY_DIRECTION" "$policy"

    collect_secondary "$SCENARIO_NAME" "$arm" "$raw_dir" "$block" "$policy"
}

cmd_plan() {
    local scenario="$1" mode="$2" dir="$3"
    [[ -f "${dir}/${scenario}.conf" ]] || die "scenario not found: ${dir}/${scenario}.conf"
    print_plan "$scenario" "$mode"
}

# write_run_summary PHASE_ROOT MODE FIRST LAST COMPLETED CAP STARTED CAP_HIT SECONDS...
#
# Records the run's shape, above all whether the wall-clock cap cut it short:
# without it a truncated run and a complete one leave the same blocks.csv behind.
write_run_summary() {
    local phase_root="$1" mode="$2" first_block="$3" last_block="$4" \
        completed="$5" cap_s="$6" started_at="$7" cap_hit="$8"
    shift 8
    local out="${phase_root}/run-summary-${SCENARIO_NAME}.json"
    local seconds_csv="" seconds
    for seconds in "$@"; do
        seconds_csv+="${seconds},";
    done
    seconds_csv="${seconds_csv%,}"
    local cap_flag=()
    (( cap_hit )) && cap_flag=(--cap-reached)
    python3 "${SCRIPT_DIR}/../analysis/run_summary.py" \
        --out "$out" --protocol "$LINUX_SCHED_PROTOCOL_VERSION" --phase "$mode" \
        --scenario "$SCENARIO_NAME" --first "$first_block" --last "$last_block" \
        --completed "$completed" --cap-s "$cap_s" \
        --elapsed-s "$(( $(date -u +%s) - started_at ))" "${cap_flag[@]}" \
        --duration-s "$DURATION_S" --warmup-s "${WARMUP_S:-0}" \
        --block-seconds "$seconds_csv" || die "could not write the run summary"
    log "run summary: ${out}"
}

cmd_run() {
    local scenario="$1" mode="$2" dir="$3" data_root="$4"
    load_scenario "$dir" "$scenario"
    require_cmd kubectl python3
    resolve_hosts

    case "$data_root" in
        "$(project_root)"/*) die "data root must live outside the Git worktree: ${data_root}" ;;
    esac

    # Pilot and confirmatory data are separate trees and separate CSVs: pilot
    # blocks estimate variance and must never enter a final verdict, and their
    # block numbers would otherwise collide with (or overwrite) confirmatory
    # artifacts.
    local phase_root="${data_root}/${mode}"
    BLOCKS_CSV="${phase_root}/blocks.csv"
    # RAW_DATA_ROOT holds scenario-level calibration artifacts (the frozen
    # offered rate), which belong to no single phase; DATA_ROOT is this phase's
    # results tree.
    RAW_DATA_ROOT="$data_root"
    DATA_ROOT="$phase_root"
    mkdir -p "$phase_root"

    # New blocks are numbered after any that already exist, so a re-run after an
    # interrupted block or a gate refusal cannot duplicate a (block, scheduler)
    # pair. The numbering continues rather than restarting.
    local first_block
    first_block="$(next_block_id "$BLOCKS_CSV")"
    local blocks; blocks="$(block_count "$mode")"
    local last_block=$(( first_block + blocks - 1 ))
    if (( first_block > 1 )); then
        log "${SCENARIO_NAME}: blocks 1..$(( first_block - 1 )) already recorded; running ${first_block}..${last_block}"
    fi

    # Time is the stopping rule, not the data. Recorded with the run so a short
    # run is never mistaken for a complete one.
    local cap_s="$LINUX_SCHED_WALL_CLOCK_CAP_S"
    local started_at; started_at="$(date -u +%s)"
    local estimate_s="${LINUX_SCHED_BLOCK_ESTIMATE_S:-600}"
    local observed=()
    local completed=0 cap_hit=0 worst_s=0

    # The quota campaign crosses schedulers with two CPU policies per block.
    # The scheduler-only series has no policy dimension: an empty list keeps one
    # run per arm and writes an empty policy column.
    local -a policies=()
    mapfile -t policies < <(split_words "${LINUX_SCHED_POLICIES:-}")
    if (( ${#policies[@]} == 0 )) && [[ "$mode" == "confirmatory" ]] \
            && [[ "$LINUX_SCHED_PROTOCOL_VERSION" == "v3" ]]; then
        policies=(unlimited limited)
    fi
    if (( ${#policies[@]} == 0 )); then
        policies=("")
    fi
    log "${SCENARIO_NAME}: policies = ${policies[*]:-none}"

    local b arm seed policy
    for (( b = first_block; b <= last_block; b++ )); do
        local elapsed=$(( $(date -u +%s) - started_at ))
        if ! within_wall_clock_cap "$elapsed" "$estimate_s" "$cap_s"; then
            log "wall-clock cap: ${elapsed}s elapsed, ~${estimate_s}s per block, cap ${cap_s}s; not starting block ${b}"
            cap_hit=1
            break
        fi
        local block_started; block_started="$(date -u +%s)"
        seed=$(( LINUX_SCHED_PLAN_SEED + b ))
        local raw_dir="${phase_root}/${SCENARIO_NAME}/block-$(printf '%02d' "$b")"
        mkdir -p "$raw_dir"
        printf '{"scenario":"%s","block":%s,"phase":"%s","protocol":"%s","policies":"%s","duration_s":%s,"warmup_s":%s,"seed":%s,"order":"%s"}\n' \
            "$SCENARIO_NAME" "$b" "$mode" "$LINUX_SCHED_PROTOCOL_VERSION" "${policies[*]}" \
            "$DURATION_S" "${WARMUP_S:-0}" "$seed" \
            "$(balanced_block_order "$b" | tr '\n' ' ')" > "${raw_dir}/metadata.json"
        # IFS excludes the space, so the arm order must be split explicitly.
        mapfile -t arms < <(balanced_block_order "$b")
        # Arm outer, policy inner: the two quota treatments of one arm run
        # back-to-back so a slow drift on the worker cannot masquerade as an
        # interaction between scheduler and quota.
        for arm in "${arms[@]}"; do
            for policy in "${policies[@]}"; do
                run_block "$SCENARIO_NAME" "$arm" "$b" "$raw_dir" "$policy"
            done
        done
        completed=$(( completed + 1 ))
        local block_seconds=$(( $(date -u +%s) - block_started ))
        observed+=( "$block_seconds" )
        # Charge the next block the slowest block so far plus the cleanup
        # allowance, so admission errs towards not starting work that cannot
        # finish: a block cut off by the cap wastes its arms and cannot count.
        (( block_seconds > worst_s )) && worst_s="$block_seconds"
        estimate_s=$(( worst_s + LINUX_SCHED_CLEANUP_ALLOWANCE_S ))
        log "block ${b} complete in ${block_seconds}s; cooldown ${LINUX_SCHED_COOLDOWN_S}s"
        sleep "$LINUX_SCHED_COOLDOWN_S"
    done

    write_run_summary "$phase_root" "$mode" "$first_block" "$last_block" \
        "$completed" "$cap_s" "$started_at" "$cap_hit" "${observed[@]}"
    if (( cap_hit )); then
        log "${SCENARIO_NAME}: stopped at the ${cap_s}s wall-clock cap after ${completed} block(s); undecided contrasts are inconclusive"
    else
        log "all ${blocks} requested blocks complete for ${SCENARIO_NAME}; blocks.csv: ${BLOCKS_CSV}"
    fi
}

# cmd_quota_calibrate SCENARIO DIR DATA_ROOT - EEVDF limit sweep for one workload.
#
# Starts at LINUX_SCHED_QUOTA_START (default 1 CPU) and tightens by the frozen
# calibration step until the workload reaches the throttling target. Each step is
# a normal measured block on the control arm under the limited policy, so the
# ratio comes from the same cpu.stat counters the confirmatory analysis uses.
cmd_quota_calibrate() {
    local scenario="$1" dir="$2" data_root="$3"
    load_scenario "$dir" "$scenario"
    require_cmd kubectl python3
    resolve_hosts

    local phase_root="${data_root}/pilot"
    mkdir -p "$phase_root"
    RAW_DATA_ROOT="$data_root"
    DATA_ROOT="$phase_root"

    local steps_csv="${phase_root}/quota-sweep-${SCENARIO_NAME}.csv"
    printf 'step,cpu_limit,throttled_period_ratio,healthy\n' > "$steps_csv"
    local calibration="${phase_root}/calibration.csv"
    local quota_file="${phase_root}/.sweep-quota-${SCENARIO_NAME}.yaml"
    export LINUX_SCHED_FROZEN_QUOTA_FILE="$quota_file"

    local limit="${LINUX_SCHED_QUOTA_START:-1}"
    local max_steps="${LINUX_SCHED_QUOTA_STEPS:-6}"
    local target="${LINUX_SCHED_QUOTA_TARGET:-0.10}"
    local max_ratio="${LINUX_SCHED_QUOTA_MAX:-0.90}"
    local floor="${LINUX_SCHED_QUOTA_FLOOR:-500m}"
    # stress-ng is a saturation control: it is expected to be throttled hard.
    if [[ "$SCENARIO_NAME" == "stress-ng" ]]; then
        max_ratio=1.0
    fi

    scx_reset "$TARGET_NODE"
    wait_quiescent "$TARGET_NODE" || die "guest ${TARGET_NODE} never became quiescent"

    local step block=0
    for (( step = 1; step <= max_steps; step++ )); do
        block=$(( block + 1 ))
        printf 'schema_version: 1\nkind: frozen-cpu-quotas\nquotas:\n  - workload: %s\n    cpu_limit: "%s"\n' \
            "$SCENARIO_NAME" "$limit" > "$quota_file"

        local raw_dir="${phase_root}/${SCENARIO_NAME}/quota-sweep-$(printf '%02d' "$step")"
        mkdir -p "$raw_dir"
        BLOCKS_CSV="${raw_dir}/blocks.csv"

        log "quota sweep ${SCENARIO_NAME}: step ${step}, limit ${limit}"
        run_block "$SCENARIO_NAME" "$LINUX_SCHED_CONTROL" "$block" "$raw_dir" limited

        local before="${raw_dir}/${LINUX_SCHED_CONTROL}-limited.cpu-before.stat"
        local after="${raw_dir}/${LINUX_SCHED_CONTROL}-limited.cpu-after.stat"
        [[ -s "$before" && -s "$after" ]] || die "no cpu.stat snapshots for step ${step}"
        local measured ratio
        measured="$(python3 "${SCRIPT_DIR}/cgroup_stats.py" --before "$before" --after "$after" --json)" \
            || die "could not compute the throttled ratio for step ${step}"
        ratio="$(printf '%s' "$measured" | python3 -c 'import json,sys; print(json.load(sys.stdin)["throttled_period_ratio"])')"

        printf '%s,%s,%s,true\n' "$step" "$limit" "$ratio" >> "$steps_csv"
        log "  throttled_period_ratio=${ratio} at ${limit}"

        if awk -v r="$ratio" -v hi="$max_ratio" 'BEGIN { exit !(r > hi) }'; then
            die "${SCENARIO_NAME}: ${ratio} throttling at ${limit} exceeds the healthy maximum ${max_ratio}"
        fi
        if awk -v r="$ratio" -v t="$target" 'BEGIN { exit !(r >= t) }'; then
            python3 - "$calibration" "$SCENARIO_NAME" "$limit" "$ratio" <<'PYCAL'
import csv
import pathlib
import sys

path, workload, limit, ratio = sys.argv[1:5]
rows = []
if pathlib.Path(path).exists():
    with open(path, newline="", encoding="utf-8") as handle:
        rows = [r for r in csv.DictReader(handle) if r.get("workload") != workload]
with open(path, "w", newline="", encoding="utf-8") as handle:
    writer = csv.DictWriter(
        handle, fieldnames=["workload", "cpu_limit", "throttled_period_ratio", "healthy"]
    )
    writer.writeheader()
    writer.writerows(rows)
    writer.writerow(
        {"workload": workload, "cpu_limit": limit, "throttled_period_ratio": ratio, "healthy": "true"}
    )
PYCAL
            log "frozen quota ${SCENARIO_NAME}: ${limit} (${ratio} throttled)"
            return 0
        fi

        limit="$(python3 "${SCRIPT_DIR}/quota_calibration.py" \
            --current "$limit" --observed "$ratio" --target "$target" --floor "$floor")" \
            || die "${SCENARIO_NAME}: no quota reached ${target} throttling above the request floor ${floor}"
    done

    die "${SCENARIO_NAME}: no limit reached ${target} throttling within ${max_steps} steps"
}

cmd_overhead() {
    local scenario="$1" dir="$2" data_root="$3"
    load_scenario "$dir" "$scenario"
    require_cmd kubectl python3
    resolve_hosts
    RAW_DATA_ROOT="$data_root"
    DATA_ROOT="$data_root"
    export LINUX_SCHED_CPU_POLICY=unlimited
    unset LINUX_SCHED_CPU_LIMIT || true
    local out="${data_root}/${SCENARIO_NAME}/overhead"
    mkdir -p "$out"

    local rate=""
    if load_needs_rate; then
        rate="$(frozen_offered_rate "$DATA_ROOT" "$SCENARIO_NAME")"
    fi
    local load_command
    load_command="$(render_load_command "$rate" "$DURATION_S")"

    # The overhead pair must use the same workload *and* the same warmup as the
    # comparisons, or it measures a different condition.
    local warmup_args=()
    if [[ "${WARMUP_S:-0}" -gt 0 ]]; then
        warmup_args=(--warmup-command "$(render_load_command "$rate" "$WARMUP_S")"
                     --warmup-seconds "$WARMUP_S")
    fi

    local await_completion=()
    if [[ "${PRIMARY_SOURCE:-load}" == "podlog" ]]; then
        await_completion=(--await-workload-completion)
    fi

    require_guest_disk "$TARGET_NODE" >/dev/null

    # EEVDF is the baseline for this calibration, and it means "no sched_ext
    # scheduler attached". Without this the pair would be measured under whatever
    # arm the previous run happened to finish on — the overhead of the observer
    # under scx_bpfland, labelled as the control arm's overhead.
    scx_reset "$TARGET_NODE"

    log "observer-overhead calibration for ${SCENARIO_NAME} on ${LINUX_SCHED_CONTROL}"
    local variant
    # The capture matches `enabled`/`disabled` exactly; passing
    # `instruments-enabled` left BOTH variants uninstrumented, which made the
    # comparison meaningless.
    for variant in enabled disabled; do
        "${SCRIPT_DIR}/../capture/capture-block.sh" \
            --target-node "$TARGET_NODE" --duration "$DURATION_S" --out-dir "$out" \
            --arm "eevdf-instruments-${variant}" --load-node "${LOAD_NODE:-}" \
            --manifest "${SCRIPT_DIR}/../${WORKLOAD_MANIFEST}" \
            --load-command "$load_command" \
            "${await_completion[@]}" \
            "${warmup_args[@]}" \
            --load-ready-probe "${LOAD_READY_PROBE:-}" \
            --raw "${out}/instruments-${variant}.txt" \
            --podlog "${out}/instruments-${variant}.log" \
            --instrumentation "$variant" \
            || die "overhead ${variant} capture failed"
        local metric_source="${out}/instruments-${variant}.txt"
        if [[ "${PRIMARY_SOURCE:-load}" == "podlog" ]]; then
            metric_source="${out}/instruments-${variant}.log"
        fi
        collect_metric "$metric_source" "$METRIC_COMMAND" "$DURATION_S" \
            "${out}/instruments-${variant}.metric" \
            || die "overhead ${variant} metric extraction failed"
    done
    log "overhead calibration complete: ${out}"
}

# calibration_probe RATE OUT_DIR CSV — run one rate probe.
#
# The probe's stdout is redirected to stderr at the call site: the capture it
# runs writes `kubectl apply` output to stdout, so a probe that echoed its value
# on stdout would hand the caller a value with "pod/... created" in front of it —
# which the latency comparison then reads as a string and rejects. The measured
# p99 is read back from the metric file the probe wrote, not from stdout.
calibration_probe() {
    local rate="$1" out="$2" csv="$3"
    local probe_dir="${out}/rate-${rate}"
    mkdir -p "$probe_dir"
    local warm_args=()
    if [[ "${WARMUP_S:-0}" -gt 0 ]]; then
        warm_args=(--warmup-command "$(render_load_command "$rate" "$WARMUP_S")"
                   --warmup-seconds "$WARMUP_S")
    fi
    "${SCRIPT_DIR}/../capture/capture-block.sh" \
        --target-node "$TARGET_NODE" --duration "$LINUX_SCHED_RATE_PROBE_S" \
        --out-dir "$probe_dir" --arm "rate-${rate}" --load-node "${LOAD_NODE:-}" \
        --manifest "${SCRIPT_DIR}/../${WORKLOAD_MANIFEST}" \
        --load-command "$(render_load_command "$rate" "$LINUX_SCHED_RATE_PROBE_S")" \
        "${warm_args[@]}" \
        --load-ready-probe "${LOAD_READY_PROBE:-}" \
        --instrumentation disabled \
        --raw "${probe_dir}/load.txt" --podlog "${probe_dir}/pod.log" \
        || die "calibration probe at ${rate} rps failed"
    collect_metric "${probe_dir}/load.txt" "$METRIC_COMMAND" \
        "$LINUX_SCHED_RATE_PROBE_S" "${probe_dir}/p99.metric" \
        || die "could not read p99 at ${rate} rps"
    local p99
    p99="$(cat "${probe_dir}/p99.metric")"
    printf '%s,%s\n' "$rate" "$p99" >> "$csv"
    log "  ${rate} rps -> p99 ${p99} ms"
}

# calibration_passes P99 — true when a probe met the frozen latency target.
calibration_passes() {
    awk -v a="$1" -v b="$LINUX_SCHED_RATE_P99_TARGET_MS" 'BEGIN { exit !(a <= b) }'
}

# cmd_calibrate SCENARIO DIR DATA_ROOT — find one scenario's offered rate and
# freeze it.
#
# The rule is a latency knee on the control arm: find the bracket where the rate
# passes and the next step fails, then refine that bracket until it is within
# `LINUX_SCHED_RATE_REFINE_PCT` of the passing rate, so the frozen value is not
# simply the last coarse step that happened to fit. Freezing on the control arm
# is the point — the arms are then compared at equal offered load, not at equal
# achieved throughput, which would hide the scheduler behind the load
# generator's own back-pressure.
cmd_calibrate() {
    local scenario="$1" dir="$2" data_root="$3"
    load_scenario "$dir" "$scenario"
    load_needs_rate \
        || die "scenario ${SCENARIO_NAME} has no __RPS__ placeholder to calibrate"
    require_cmd kubectl python3
    resolve_hosts
    DATA_ROOT="$data_root"

    # The offered rate is a property of the workload, not of the quota: calibrate
    # it with no CPU limit, exactly as the confirmatory unlimited policy runs.
    export LINUX_SCHED_CPU_POLICY=unlimited
    unset LINUX_SCHED_CPU_LIMIT || true

    local out="${data_root}/${SCENARIO_NAME}/calibration"
    mkdir -p "$out"
    local csv="${out}/rate-sweep.csv"
    printf 'rate_rps,p99_ms\n' > "$csv"

    log "calibrating ${SCENARIO_NAME}: ${LINUX_SCHED_CONTROL} control arm, " \
        "target p99 <= ${LINUX_SCHED_RATE_P99_TARGET_MS} ms, " \
        "${LINUX_SCHED_RATE_PROBE_S}s probes, refine to ${LINUX_SCHED_RATE_REFINE_PCT}%"
    scx_reset "$TARGET_NODE"
    activate_arm "$LINUX_SCHED_CONTROL"

    local rate="$LINUX_SCHED_RATE_SWEEP_START"
    local p99="" best="" worst="" probes=0

    # 1. Geometric sweep upward until the target is crossed.
    while (( rate <= LINUX_SCHED_RATE_SWEEP_MAX )); do
        probes=$(( probes + 1 ))
        calibration_probe "$rate" "$out" "$csv" >&2
        p99="$(cat "${out}/rate-${rate}/p99.metric")"
        if calibration_passes "$p99"; then
            best="$rate"
            rate="$(awk -v r="$rate" -v f="$LINUX_SCHED_RATE_SWEEP_FACTOR" \
                'BEGIN { printf "%d", r * f }')"
        else
            worst="$rate"
            break
        fi
    done

    # 2. Refine the bracket until it is tight, so the frozen rate is close to
    #    the real knee rather than one coarse step below it.
    if [[ -n "$best" && -n "$worst" ]]; then
        while awk -v b="$best" -v w="$worst" -v pct="$LINUX_SCHED_RATE_REFINE_PCT" \
            'BEGIN { exit !((w - b) > (b * pct / 100)) }'; do
            local mid=$(( (best + worst) / 2 ))
            (( mid > best )) || break
            probes=$(( probes + 1 ))
            calibration_probe "$mid" "$out" "$csv" >&2
            p99="$(cat "${out}/rate-${mid}/p99.metric")"
            if calibration_passes "$p99"; then
                best="$mid"
            else
                worst="$mid"
            fi
        done
    fi

    scx_reset "$TARGET_NODE"

    [[ -n "$best" ]] || die "no rate held p99 at or under ${LINUX_SCHED_RATE_P99_TARGET_MS} ms; see ${csv}"
    printf '%s\n' "$best" > "$(offered_rate_file "$data_root" "$SCENARIO_NAME")"
    log "frozen offered rate ${best} rps for ${SCENARIO_NAME} after ${probes} probe(s): ${csv}"
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
            plan|run|overhead|calibrate|quota-calibrate) subcmd="$1"; shift ;;
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
        quota-calibrate)
            [[ -n "$scenario" ]] || die "--scenario is required"
            [[ -n "$data_root" ]] || die "--data-root is required"
            cmd_quota_calibrate "$scenario" "$dir" "$data_root"
            ;;
        overhead)
            [[ -n "$scenario" ]] || die "--scenario is required"
            [[ -n "$data_root" ]] || die "--data-root is required"
            cmd_overhead "$scenario" "$dir" "$data_root"
            ;;
        calibrate)
            [[ -n "$scenario" ]] || die "--scenario is required"
            [[ -n "$data_root" ]] || die "--data-root is required"
            cmd_calibrate "$scenario" "$dir" "$data_root"
            ;;
        *) die "no subcommand given; see --help" ;;
    esac
}

main "$@"
