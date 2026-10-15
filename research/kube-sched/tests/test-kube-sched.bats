#!/usr/bin/env bats
#
# Shell contracts for the kube-sched placement runner (offline paths only).

setup() {
    SERIES_DIR="$(cd "${BATS_TEST_DIRNAME}/.." && pwd)"
    RUNNER="${SERIES_DIR}/experiments/run-placement.sh"
    PROFILES="${SERIES_DIR}/profiles"
}

@test "validate accepts every scheduler profile" {
    run "${RUNNER}" --validate --profiles-dir "${PROFILES}"
    [ "$status" -eq 0 ]
    [[ "$output" == *"[OK] least-allocated"* ]]
    [[ "$output" == *"[OK] most-allocated"* ]]
    [[ "$output" == *"[OK] topology-spread"* ]]
}

@test "plan keeps the control profile first and lists treatments once" {
    run "${RUNNER}" plan --profiles-dir "${PROFILES}"
    [ "$status" -eq 0 ]
    [[ "$output" == *"control=least-allocated"* ]]

    local block_lines
    block_lines="$(printf '%s\n' "$output" | grep '^  block=')"
    local total
    total="$(printf '%s\n' "$block_lines" | grep -c '^  block=')"
    [ "$total" -eq 5 ]

    local line occurrences order
    while IFS= read -r line; do
        occurrences="$(printf '%s' "$line" | grep -o 'least-allocated' | wc -l)"
        [ "$occurrences" -eq 1 ]
        order="${line##*order=}"
        [[ "$order" == least-allocated\ * ]]
    done <<< "$block_lines"
}

@test "plan is deterministic across runs" {
    run "${RUNNER}" plan --profiles-dir "${PROFILES}"
    [ "$status" -eq 0 ]
    first="$output"
    run "${RUNNER}" plan --profiles-dir "${PROFILES}"
    [ "$status" -eq 0 ]
    [ "$output" = "$first" ]
}

@test "run path splits the seeded order explicitly" {
    # Regression: the same IFS/space-split hazard as linux-sched. The run path
    # must split the order with read -a rather than a bare command substitution.
    run grep -F 'read -r -a order <<< "$(seeded_order' \
        "${SERIES_DIR}/experiments/run-placement.sh"
    [ "$status" -eq 0 ]
}

@test "unknown profile directory is rejected" {
    run "${RUNNER}" plan --profiles-dir "${BATS_TEST_TMPDIR}/nope"
    [ "$status" -ne 0 ]
}
