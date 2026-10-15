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
