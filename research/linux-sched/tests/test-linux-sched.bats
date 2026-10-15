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

    [[ "$first" == *"arms=eevdf scx_bpfland scx_lavd scx_mavd"* ]]
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
    [ "${#lines[@]}" -eq 4 ]
    for i in 0 1 2 3; do
        [[ "${lines[$i]}" != *" "* ]]
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
