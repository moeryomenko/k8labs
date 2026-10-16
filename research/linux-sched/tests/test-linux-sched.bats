#!/usr/bin/env bats
#
# Shell contracts for the linux-sched paired-block runner. These exercise the
# offline paths only (validate/plan); execution requires the Phase 2 gates.

setup() {
    SERIES_DIR="$(cd "${BATS_TEST_DIRNAME}/.." && pwd)"
    RUNNER="${SERIES_DIR}/experiments/run-block.sh"
    SCENARIOS="${SERIES_DIR}/experiments/scenarios"
}

@test "validate accepts every scenario config" {
    run "${RUNNER}" --validate --configs-dir "${SCENARIOS}"
    [ "$status" -eq 0 ]
    [[ "$output" == *"[OK] http"* ]]
    [[ "$output" == *"[OK] postgres"* ]]
    [[ "$output" == *"[OK] ffmpeg"* ]]
    [[ "$output" == *"[OK] stress-ng"* ]]
}

@test "list reports each scenario with its metric and direction" {
    run "${RUNNER}" --list --configs-dir "${SCENARIOS}"
    [ "$status" -eq 0 ]
    [[ "$output" == *"latency_p99_ms"* ]]
    [[ "$output" == *"transactions_per_sec"* ]]
}

@test "plan is deterministic and covers every scheduler arm" {
    run "${RUNNER}" plan --scenario http --mode pilot --configs-dir "${SCENARIOS}"
    [ "$status" -eq 0 ]
    first="$output"

    run "${RUNNER}" plan --scenario http --mode pilot --configs-dir "${SCENARIOS}"
    [ "$status" -eq 0 ]
    [ "$output" = "$first" ]

    [[ "$first" == *"arms=eevdf scx_bpfland scx_lavd"* ]]
    [[ "$first" == *"block=1"* ]]
    [[ "$first" == *"block=3"* ]]
}

@test "plan with confirmatory mode honours the frozen minimum block count" {
    LINUX_SCHED_MIN_BLOCKS=5 run "${RUNNER}" plan --scenario http --mode confirmatory \
        --configs-dir "${SCENARIOS}"
    [ "$status" -eq 0 ]
    [[ "$output" == *"blocks=5"* ]]
}

@test "plan requires a data root only for run, not for plan" {
    run "${RUNNER}" plan --scenario http --configs-dir "${SCENARIOS}"
    [ "$status" -eq 0 ]
}

@test "seeded_permutation output splits into one arm per line" {
    # Regression: IFS excludes the space, so a space-joined command
    # substitution must be split explicitly or the loop runs once with all
    # arms concatenated.
    run bash -c "source '${SERIES_DIR}/experiments/lib.sh'; split_words \"\$(seeded_permutation 20261016)\""
    [ "$status" -eq 0 ]
    expected="$(bash -c "source '${SERIES_DIR}/experiments/lib.sh'; split_words \"\${LINUX_SCHED_ARMS}\" | wc -l | tr -d ' '")"
    [ "${#lines[@]}" -ge 3 ]
    [ "${#lines[@]}" -eq "${expected}" ]
    for line in "${lines[@]}"; do
        [[ "$line" != *" "* ]]
    done
}

@test "unknown scenario is rejected" {
    run "${RUNNER}" plan --scenario does-not-exist --configs-dir "${SCENARIOS}"
    [ "$status" -ne 0 ]
}

@test "node-native observer tooling gate passes" {
    run python3 "${SERIES_DIR}/experiments/observer_check.py"
    [ "$status" -eq 0 ]
    [[ "$output" == *"node-native observer tooling OK"* ]]
}

@test "render_load_command fills the rate and duration placeholders" {
    run bash -c "source '${SERIES_DIR}/experiments/lib.sh'; LOAD_COMMAND=wrk2; LOAD_ARGS='-d __DURATION__s -R __RPS__ --latency http://x/'; render_load_command 1500 30"
    [ "$status" -eq 0 ]
    [ "$output" = "wrk2 -d 30s -R 1500 --latency http://x/" ]
}

@test "load_needs_rate detects only the rate placeholder" {
    run bash -c "source '${SERIES_DIR}/experiments/lib.sh'; LOAD_ARGS='-R __RPS__'; load_needs_rate && echo yes || echo no"
    [ "$output" = "yes" ]
    run bash -c "source '${SERIES_DIR}/experiments/lib.sh'; LOAD_ARGS='-R 1500'; load_needs_rate && echo yes || echo no"
    [ "$output" = "no" ]
}

@test "a missing frozen rate fails loudly instead of guessing" {
    local tmp
    tmp="$(mktemp -d)"
    run bash -c "source '${SERIES_DIR}/experiments/lib.sh'; frozen_offered_rate '${tmp}' http"
    [ "$status" -ne 0 ]
    [[ "$output" == *"no calibrated offered rate"* ]]
    rm -rf "$tmp"
}

@test "a frozen rate is read back verbatim" {
    local tmp
    tmp="$(mktemp -d)"
    mkdir -p "${tmp}/http"
    printf '2000\n' > "${tmp}/http/offered-rate-rps"
    run bash -c "source '${SERIES_DIR}/experiments/lib.sh'; frozen_offered_rate '${tmp}' http"
    [ "$status" -eq 0 ]
    [ "$output" = "2000" ]
    rm -rf "$tmp"
}

@test "help documents the calibrate subcommand" {
    run "${RUNNER}" --help
    [ "$status" -eq 0 ]
    [[ "$output" == *"calibrate"* ]]
}

@test "rate scenarios carry a duration placeholder for calibration probes" {
    run grep -h "LOAD_ARGS" "${SCENARIOS}/http.conf" "${SCENARIOS}/mixed-http-ffmpeg.conf"
    [ "$status" -eq 0 ]
    [[ "$output" == *"__RPS__"* ]]
    [[ "$output" == *"__DURATION__s"* ]]
}

@test "balanced_block_order places each arm in each position once per cycle" {
    run bash -c "source '${SERIES_DIR}/experiments/lib.sh'
        for b in 1 2 3; do balanced_block_order \"\$b\" | tr '\n' ' '; echo; done"
    [ "$status" -eq 0 ]
    [ "${#lines[@]}" -eq 3 ]
    # Distinct arms in the first, second and third position across the cycle.
    for position in 1 2 3; do
        distinct="$(printf '%s\n' "${lines[@]}" | awk -v p="$position" '{print $p}' | sort -u | wc -l)"
        [ "$distinct" -eq 3 ]
    done
}

@test "balanced_block_order is deterministic and keeps every arm" {
    first="$(bash -c "source '${SERIES_DIR}/experiments/lib.sh'; balanced_block_order 2")"
    second="$(bash -c "source '${SERIES_DIR}/experiments/lib.sh'; balanced_block_order 2")"
    [ "$first" = "$second" ]
    for arm in eevdf scx_bpfland scx_lavd; do
        [[ "$first" == *"$arm"* ]]
    done
}

@test "the storage gate is the documented sum of threshold, spool, workload and reserve" {
    run bash -c "source '${SERIES_DIR}/experiments/lib.sh'; required_guest_free_bytes"
    [ "$status" -eq 0 ]
    # 789102828 + (192 + 64 + 128) MiB: the kubelet eviction threshold plus the
    # spool cap, the workload's ephemeral storage and a reserve.
    [ "$output" = "1191756012" ]
}

@test "rate scenarios declare a readiness probe so the load never starts blind" {
    run grep -h "LOAD_READY_PROBE" \
        "${SCENARIOS}/http.conf" "${SCENARIOS}/postgres.conf" \
        "${SCENARIOS}/mixed-http-ffmpeg.conf"
    [ "$status" -eq 0 ]
    [[ "$output" == *"curl -fsS"* ]]
    [[ "$output" == *"pg_isready"* ]]
}

@test "the mixed scenario loads its own Service, not the http scenario's" {
    run grep -h "mixed-nginx" "${SCENARIOS}/mixed-http-ffmpeg.conf"
    [ "$status" -eq 0 ]
    run grep -h "LOAD_ARGS" "${SCENARIOS}/mixed-http-ffmpeg.conf"
    [[ "$output" == *"mixed-nginx"* ]]
    [[ "$output" != *"//nginx."* ]]
}

@test "next_block_id continues after the highest recorded block" {
    tmp="$(mktemp -d)"
    printf 'block_id,scheduler,workload,metric,value,direction\n' > "${tmp}/blocks.csv"
    run bash -c "source '${SERIES_DIR}/experiments/lib.sh'; next_block_id '${tmp}/blocks.csv'"
    [ "$output" = "1" ]
    printf '1,eevdf,http,m,1,higher-is-better\n2,eevdf,http,m,1,higher-is-better\n' >> "${tmp}/blocks.csv"
    run bash -c "source '${SERIES_DIR}/experiments/lib.sh'; next_block_id '${tmp}/blocks.csv'"
    [ "$output" = "3" ]
    rm -rf "$tmp"
}

@test "next_block_id returns 1 for a results file that does not exist yet" {
    run bash -c "source '${SERIES_DIR}/experiments/lib.sh'; next_block_id '/nonexistent/blocks.csv'"
    [ "$status" -eq 0 ]
    [ "$output" = "1" ]
}

@test "smoke mode plans exactly one block, kept out of the pilot tree" {
    run "${SERIES_DIR}/experiments/run-block.sh" plan --scenario postgres --mode smoke \
        --configs-dir "${SCENARIOS}"
    [ "$status" -eq 0 ]
    [[ "$output" == *"mode=smoke blocks=1"* ]]
}

@test "the frozen rate is read from the raw root, not the phase root" {
    # Regression: splitting pilot and confirmatory into phase trees left the
    # writer putting the calibrated rate under the raw root while the reader
    # looked under the phase root, so a rate-driven scenario could never find
    # the rate it had just frozen.
    run grep -n 'frozen_offered_rate "\$RAW_DATA_ROOT" "\$SCENARIO_NAME"' \
        "${SERIES_DIR}/experiments/run-block.sh"
    [ "$status" -eq 0 ]
    run grep -n 'RAW_DATA_ROOT="\$data_root"' "${SERIES_DIR}/experiments/run-block.sh"
    [ "$status" -eq 0 ]
}

@test "the observer wrapper puts wrk2's Lua module on the module path" {
    # Regression: wrk2's Lua module is corrupted at bake time, so a bare symlink
    # to the binary panics on every run. The observer must supply wrk.lua.
    run grep -n 'LUA_PATH=' "${SERIES_DIR}/observers/observer-daemonset.yaml"
    [ "$status" -eq 0 ]
    [[ "$output" == *"/opt/linux-sched/bin/?.lua"* ]]
    # wrk2 must not be symlinked directly any more.
    run grep -c 'case "$(basename "$t")" in wrk2) continue' "${SERIES_DIR}/observers/observer-daemonset.yaml"
    [ "$status" -eq 0 ]
}

@test "the calibration probe never hands its caller a log as a value" {
    # Regression: the capture writes `kubectl apply` output on stdout. Capturing
    # the probe's stdout mixed "pod/... created" into the measured p99, which the
    # latency comparison then read as a string and rejected, so a rate that met
    # the target was reported as never having met it.
    run grep -n 'calibration_probe "\$rate" "\$out" "\$csv" >&2' "${SERIES_DIR}/experiments/run-block.sh"
    [ "$status" -eq 0 ]
    run grep -c 'p99="$(cat "${out}/rate-${rate}/p99.metric")"' "${SERIES_DIR}/experiments/run-block.sh"
    [ "$status" -eq 0 ]
}
