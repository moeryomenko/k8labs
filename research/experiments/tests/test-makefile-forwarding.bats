#!/usr/bin/env bats
# test-makefile-forwarding.bats — Tests for TASK-D09 root Makefile forwarding
# targets and the interaction-report pipeline target.
#
# Test-first (red phase): the root Makefile has NO research experiment targets
# today, so every REQ-1/REQ-2/REQ-3-listing test FAILS until the engineer adds
# the forwarding targets; the REQ-4 backward-compat tests already pass and are
# regression guards that must stay green after TASK-D09 lands.
#
# No cluster required. Every assertion uses `make -n` (dry-run), which prints
# the recipes that would run WITHOUT executing them. GNU make recurses through
# `$(MAKE)` invocations even in dry-run mode (verified: `make -n` on a
# forwarding target prints the delegation line AND the delegated sub-make's
# recipe lines), so a single root invocation exercises both Makefiles.
#
# Requirements covered (full mapping in TEST-DESIGN.md):
#   REQ-1 -> VC-MF-D09-01 (MF-01, MF-02, MF-03, MF-04)
#   REQ-2 -> VC-MF-D09-02 (MF-05, MF-06)
#   REQ-3 -> VC-MF-D09-03 (MF-07, MF-08)
#   REQ-4 -> VC-MF-D09-04 (MF-09, MF-10)
#   REQ-5 -> VC-MF-D09-05 (MF-11, MF-12)
#
# Run from repo root:
#   bats research/experiments/tests/test-makefile-forwarding.bats
#   bats --filter "MF-" research/experiments/tests/test-makefile-forwarding.bats

setup() {
    export PROJECT_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../../.." && pwd -P)"
    cd "$PROJECT_ROOT"
    export RESEARCH_DIR="$PROJECT_ROOT/research"

    # The six experiment families this task forwards from the root Makefile.
    # Order must match EXPERIMENT_CONFIGS below.
    EXPERIMENT_TARGETS=(
        experiment-weight-share
        experiment-request-limit-matrix
        experiment-qos-hierarchy
        experiment-latency-interference
        experiment-cpu-burst
        experiment-tunables-contention
    )

    # Config each target must reach (proves the delegation is real, not a
    # stub). Basenames are unique across the configs dir.
    EXPERIMENT_CONFIGS=(
        weight-share.yaml
        request-limit-matrix.yaml
        qos-hierarchy.yaml
        latency-interference.yaml
        cpu-burst.yaml
        tunables-contention.yaml
    )

    # The five analyzers + report generator the interaction-report target
    # must invoke (TASK-022 rerun pipeline), by basename.
    REPORT_SCRIPTS=(
        weight-share-analyze.py
        interaction-heatmap.py
        qos-analyze.py
        latency-analyze.py
        tunables-analyze.py
        generate-report.py
    )
}

assert_output_contains() {
    local needle="$1"
    if [[ "$output" == *"$needle"* ]]; then
        return 0
    fi
    echo "expected dry-run output to contain: $needle" >&2
    echo "--- actual output ---" >&2
    echo "$output" >&2
    return 1
}

assert_output_not_word() {
    local word="$1"
    if echo "$output" | grep -qw "$word"; then
        echo "unexpected word '$word' in interaction-report dry-run output" >&2
        echo "--- actual output ---" >&2
        echo "$output" >&2
        return 1
    fi
    return 0
}

# --- REQ-1: root forwarding targets (VC-MF-D09-01) ---

@test "MF-01: root make -n experiment-weight-share delegates to research (VC-MF-D09-01)" {
    run make -n experiment-weight-share
    [ "$status" -eq 0 ]
    assert_output_contains "make -C research experiment-weight-share"
    assert_output_contains "run-experiment.sh"
    assert_output_contains "weight-share.yaml"
}

@test "MF-02: root make -n experiment-request-limit-matrix delegates to research (VC-MF-D09-01)" {
    run make -n experiment-request-limit-matrix
    [ "$status" -eq 0 ]
    assert_output_contains "make -C research experiment-request-limit-matrix"
    assert_output_contains "run-experiment.sh"
    assert_output_contains "request-limit-matrix.yaml"
}

@test "MF-03: all six experiment targets exist at root and delegate (VC-MF-D09-01)" {
    for i in "${!EXPERIMENT_TARGETS[@]}"; do
        local target="${EXPERIMENT_TARGETS[$i]}"
        run make -n "$target"
        [ "$status" -eq 0 ] || {
            echo "make -n $target failed (status $status)" >&2
            echo "$output" >&2
            return 1
        }
        assert_output_contains "make -C research $target"
    done
}

@test "MF-04: each forwarded target reaches its family config (VC-MF-D09-01)" {
    for i in "${!EXPERIMENT_TARGETS[@]}"; do
        local target="${EXPERIMENT_TARGETS[$i]}"
        local config="${EXPERIMENT_CONFIGS[$i]}"
        run make -n "$target"
        [ "$status" -eq 0 ] || {
            echo "make -n $target failed (status $status)" >&2
            echo "$output" >&2
            return 1
        }
        assert_output_contains "$config"
    done
}

# --- REQ-2: interaction-report pipeline listing (VC-MF-D09-02) ---

@test "MF-05: root make -n interaction-report lists analyzers + report generator (VC-MF-D09-02)" {
    run make -n interaction-report
    [ "$status" -eq 0 ]
    assert_output_contains "make -C research interaction-report"
    for script in "${REPORT_SCRIPTS[@]}"; do
        assert_output_contains "$script"
    done
}

@test "MF-06: research make -n interaction-report lists analyzers + report generator (VC-MF-D09-02)" {
    run make -n -C research interaction-report
    [ "$status" -eq 0 ]
    for script in "${REPORT_SCRIPTS[@]}"; do
        assert_output_contains "$script"
    done
}

# --- REQ-3: help target unchanged + lists new targets (VC-MF-D09-03) ---

@test "MF-07: root make -n help still exits 0 (VC-MF-D09-03)" {
    run make -n help
    [ "$status" -eq 0 ]
}

@test "MF-08: root make help lists experiment-weight-share and interaction-report (VC-MF-D09-03)" {
    # `make help` runs a read-only grep/sort/awk pipeline over the Makefile
    # text — safe, no cluster, no network. (`make -n help` only prints the
    # recipe lines, so the listing assertion must run the real help output.)
    run make help
    [ "$status" -eq 0 ]
    assert_output_contains "experiment-weight-share"
    assert_output_contains "interaction-report"
}

# --- REQ-4: research-Makefile-only invocation backward compat (VC-MF-D09-04) ---

@test "MF-09: make -n -C research experiment-weight-share still works (VC-MF-D09-04)" {
    run make -n -C research experiment-weight-share
    [ "$status" -eq 0 ]
    assert_output_contains "run-experiment.sh"
    assert_output_contains "weight-share.yaml"
}

@test "MF-10: all six research-Makefile-only invocations still work (VC-MF-D09-04)" {
    for i in "${!EXPERIMENT_TARGETS[@]}"; do
        local target="${EXPERIMENT_TARGETS[$i]}"
        local config="${EXPERIMENT_CONFIGS[$i]}"
        run make -n -C research "$target"
        [ "$status" -eq 0 ] || {
            echo "make -n -C research $target failed (status $status)" >&2
            echo "$output" >&2
            return 1
        }
        assert_output_contains "$config"
    done
}

# --- REQ-5: interaction-report reproducible/idempotent by construction (VC-MF-D09-05) ---

@test "MF-11: interaction-report recipe has no network or timestamp commands (VC-MF-D09-05)" {
    run make -n -C research interaction-report
    [ "$status" -eq 0 ]
    assert_output_not_word "curl"
    assert_output_not_word "wget"
    assert_output_not_word "date"
}

@test "MF-12: interaction-report dry-run is byte-identical across two runs (VC-MF-D09-05)" {
    run make -n -C research interaction-report
    [ "$status" -eq 0 ]
    local first_output="$output"
    run make -n -C research interaction-report
    [ "$status" -eq 0 ]
    [ "$first_output" = "$output" ]
}
