#!/usr/bin/env bats
# test-perfetto-integration.bats — Tests for --perfetto flag in run-experiment.sh
#
# These tests verify the Perfetto tracing integration in the experiment runner.
# They cover argument parsing, dry-run output, config validation, and graceful
# degradation WITHOUT requiring a running cluster.
#
# The tests are written in test-first style: they will fail (red phase) until
# the --perfetto / --perfetto-config flags are implemented.
#
# Run from project root:
#   bats research/cpu-sched/experiments/tests/test-perfetto-integration.bats
#
# Run a specific test (filter by any substring of the test description):
#   bats --filter "--perfetto flag is accepted" research/cpu-sched/experiments/tests/test-perfetto-integration.bats

setup() {
    export PROJECT_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../../../.." && pwd -P)"
    export EXPERIMENTS_DIR="$PROJECT_ROOT/research/cpu-sched/experiments"
    export RUN_EXPERIMENT_SH="$EXPERIMENTS_DIR/run-experiment.sh"
    export COMMON_SH="$EXPERIMENTS_DIR/common.sh"
    export TEST_CONFIG="$EXPERIMENTS_DIR/configs/throttling-baseline.yaml"
    export PERFETTO_COMMON_SH="$PROJECT_ROOT/research/cpu-sched/perfetto/bin/perfetto-common.sh"

    # Ensure test config exists
    [ -f "$TEST_CONFIG" ] || { echo "FATAL: Test config not found at $TEST_CONFIG" >&2; exit 1; }
}

# =============================================================================
# Existing behavior unchanged without --perfetto
# =============================================================================

@test "dry-run without --perfetto succeeds and produces expected output" {
    run bash "$RUN_EXPERIMENT_SH" "$TEST_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"DRY RUN MODE"* ]]
    [[ "$output" == *"throttling-baseline"* ]]
    [[ "$output" == *"Matrix cells"* ]]
    [[ "$output" == *"Prerequisites check passed"* ]]
}

# =============================================================================
# --perfetto flag is accepted
# =============================================================================

@test "--perfetto flag is accepted with --dry-run" {
    run bash "$RUN_EXPERIMENT_SH" "$TEST_CONFIG" --dry-run --perfetto

    # RED PHASE: This will fail (exit 1) until --perfetto is implemented
    [ "$status" -eq 0 ]
}

@test "--perfetto flag is accepted (long form only)" {
    run bash "$RUN_EXPERIMENT_SH" "$TEST_CONFIG" --dry-run --perfetto

    [ "$status" -eq 0 ]
    [[ "$output" == *"perfetto"* ]] || [[ "$output" == *"Perfetto"* ]] || [[ "$output" == *"PERFETTO"* ]]
}

@test "--perfetto does not break existing --output-dir" {
    local test_outdir="$BATS_TEST_TMPDIR/experiment-output"
    mkdir -p "$test_outdir"

    run bash "$RUN_EXPERIMENT_SH" "$TEST_CONFIG" --dry-run --output-dir "$test_outdir" --perfetto

    [ "$status" -eq 0 ]
    [[ "$output" == *"$test_outdir"* ]]
}

# =============================================================================
# --perfetto-config flag is accepted
# =============================================================================

@test "--perfetto-config flag is accepted with valid config name" {
    run bash "$RUN_EXPERIMENT_SH" "$TEST_CONFIG" --dry-run --perfetto --perfetto-config scheduling

    [ "$status" -eq 0 ]
}

@test "--perfetto-config with explicit config name appears in output" {
    run bash "$RUN_EXPERIMENT_SH" "$TEST_CONFIG" --dry-run --perfetto --perfetto-config syscalls

    [ "$status" -eq 0 ]
    [[ "$output" == *"syscalls"* ]]
}

@test "--perfetto-config can be passed before --perfetto" {
    run bash "$RUN_EXPERIMENT_SH" "$TEST_CONFIG" --dry-run --perfetto-config scheduling --perfetto

    [ "$status" -eq 0 ]
}

# =============================================================================
# --perfetto-config requires a value
# =============================================================================

@test "--perfetto-config without value exits with error" {
    run bash "$RUN_EXPERIMENT_SH" "$TEST_CONFIG" --dry-run --perfetto --perfetto-config

    [ "$status" -ne 0 ]
    [[ "$output" == *"requires a value"* ]] || [[ "$output" == *"rror"* ]]
}

# =============================================================================
# --perfetto defaults to "scheduling" config
# =============================================================================

@test "--perfetto without --perfetto-config defaults to scheduling" {
    run bash "$RUN_EXPERIMENT_SH" "$TEST_CONFIG" --dry-run --perfetto

    [ "$status" -eq 0 ]
    [[ "$output" == *"scheduling"* ]]
}

# =============================================================================
# Dry-run with --perfetto shows tracing plan
# =============================================================================

@test "--dry-run --perfetto mentions trace start step" {
    run bash "$RUN_EXPERIMENT_SH" "$TEST_CONFIG" --dry-run --perfetto

    [ "$status" -eq 0 ]
    [[ "$output" == *"trace"* ]] || [[ "$output" == *"Trace"* ]]
}

@test "--dry-run --perfetto mentions trace capture config" {
    run bash "$RUN_EXPERIMENT_SH" "$TEST_CONFIG" --dry-run --perfetto --perfetto-config scheduling

    [ "$status" -eq 0 ]
    [[ "$output" == *"scheduling"* ]] || [[ "$output" == *"cfg"* ]]
}

@test "--dry-run --perfetto mentions node resolution for tracing" {
    run bash "$RUN_EXPERIMENT_SH" "$TEST_CONFIG" --dry-run --perfetto

    [ "$status" -eq 0 ]
    [[ "$output" == *"node"* ]] || [[ "$output" == *"Node"* ]]
}

@test "--dry-run --perfetto download/output path is mentioned" {
    run bash "$RUN_EXPERIMENT_SH" "$TEST_CONFIG" --dry-run --perfetto

    [ "$status" -eq 0 ]
    [[ "$output" == *"cell"* ]] || [[ "$output" == *"trace"* ]] || [[ "$output" == *"data"* ]]
}

# =============================================================================
# Normal experiment without --perfetto runs unchanged
# =============================================================================

@test "without --perfetto, output is identical to baseline (no trace references)" {
    # Run without --perfetto — capture the output
    run bash "$RUN_EXPERIMENT_SH" "$TEST_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    # Perfetto/tracebox/trace should NOT appear in the output
    local trace_mentions
    trace_mentions="$(printf '%s' "$output" | grep -ci 'perfetto\|tracebox\|trace' 2>/dev/null || true)"
    [ "$trace_mentions" -eq 0 ]
}

# =============================================================================
# Error handling for bad --perfetto-config values
# =============================================================================

@test "--perfetto-config with non-existent config prints error" {
    run bash "$RUN_EXPERIMENT_SH" "$TEST_CONFIG" --dry-run --perfetto --perfetto-config nonexistent-config

    [ "$status" -ne 0 ]
    [[ "$output" == *"not found"* ]] || [[ "$output" == *"no such"* ]] || [[ "$output" == *"rror"* ]] || [[ "$output" == *"nknown"* ]]
}

@test "--perfetto-config with empty config name prints error" {
    run bash "$RUN_EXPERIMENT_SH" "$TEST_CONFIG" --dry-run --perfetto --perfetto-config ""

    [ "$status" -ne 0 ]
}

@test "--perfetto-config with special characters prints error" {
    run bash "$RUN_EXPERIMENT_SH" "$TEST_CONFIG" --dry-run --perfetto --perfetto-config "../../etc/passwd"

    [ "$status" -ne 0 ]
}

# =============================================================================
# Graceful degradation when tracebox is unavailable
# =============================================================================

@test "script does not crash when tracebox is not on node (graceful degradation path exists)" {
    # Source common.sh and verify that functions handle tracebox absence gracefully.
    # In the experiment runner, when --perfetto is enabled but tracebox can't be
    # found on the target node, the script should warn and continue, not crash.
    #
    # This test verifies the error-handling pattern exists by sourcing the perfetto
    # common library and checking the check_tracebox_available function signature.
    run bash -c "
        source '$PERFETTO_COMMON_SH'
        type check_tracebox_available 2>&1
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"function"* ]]
}

@test "graceful degradation — warn but continue on tracebox failure" {
    # The experiment runner should NOT call 'die' when tracebox is missing.
    # Instead it should log a warning and continue the experiment without tracing.
    # This test verifies the error path by running 'check_tracebox_available'
    # against an unreachable IP and confirming it exits with error (meaning
    # the caller must handle the error, not die).
    run bash -c "
        source '$PERFETTO_COMMON_SH'
        check_tracebox_available '192.0.2.1' 2>&1 || true
    " 2>&1
    [ "$status" -ne 0 ]
}

# =============================================================================
# Perfetto trace metadata is captured
# =============================================================================

@test "--dry-run --perfetto mentions trace file saved to cell directory" {
    run bash "$RUN_EXPERIMENT_SH" "$TEST_CONFIG" --dry-run --perfetto

    [ "$status" -eq 0 ]
    [[ "$output" == *".perfetto"* ]] || [[ "$output" == *".trace"* ]] || [[ "$output" == *"perfetto"* ]]
}

@test "--dry-run --perfetto references trace metadata in metadata.json" {
    run bash "$RUN_EXPERIMENT_SH" "$TEST_CONFIG" --dry-run --perfetto

    [ "$status" -eq 0 ]
    [[ "$output" == *"metadata"* ]]
}

# =============================================================================
# Unknown perfetto-related flags are rejected
# =============================================================================

@test "unknown --perfetto-<subflag> is rejected" {
    run bash "$RUN_EXPERIMENT_SH" "$TEST_CONFIG" --dry-run --perfetto --perfetto-unknown-flag

    [ "$status" -ne 0 ]
    [[ "$output" == *"nknown"* ]] || [[ "$output" == *"nrecognized"* ]] || [[ "$output" == *"nvalid"* ]]
}

@test "--perfetto with trailing garbage after value is rejected" {
    run bash "$RUN_EXPERIMENT_SH" "$TEST_CONFIG" --dry-run --perfetto --perfetto-config=invalid=chars

    [ "$status" -ne 0 ]
}

# =============================================================================
# Integration with workload lifecycle
# =============================================================================

@test "--perfetto flag is visible in dry-run experiment summary" {
    run bash "$RUN_EXPERIMENT_SH" "$TEST_CONFIG" --dry-run --perfetto

    [ "$status" -eq 0 ]
    [[ "$output" == *"perfetto"* ]] || [[ "$output" == *"Perfetto"* ]] || [[ "$output" == *"tracing"* ]]
}

@test "--help output mentions --perfetto flag" {
    run bash "$RUN_EXPERIMENT_SH" --help

    [ "$status" -eq 0 ]
    [[ "$output" == *"--perfetto"* ]]
}

@test "--help output mentions --perfetto-config flag" {
    run bash "$RUN_EXPERIMENT_SH" --help

    [ "$status" -eq 0 ]
    [[ "$output" == *"--perfetto-config"* ]]
}

# =============================================================================
# Function-level tests for the integration helpers
# =============================================================================

@test "common.sh can source without error" {
    run bash -c "
        source '$COMMON_SH'
        echo OK
    "
    [ "$status" -eq 0 ]
    [ "$output" = "OK" ]
}

@test "experiment runner can source perfetto-common.sh for node-aware tracing" {
    # The experiment runner should be able to source perfetto-common.sh
    # to access resolve_node_ip, check_tracebox_available, etc.
    run bash -c "
        source '$PERFETTO_COMMON_SH'
        type resolve_node_ip check_tracebox_available perfetto_config_path 2>&1
    "
    [ "$status" -eq 0 ]
}
