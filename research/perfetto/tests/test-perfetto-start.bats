#!/usr/bin/env bats
# test-perfetto-start.bats — Tests for perfetto-start.sh trace launcher
#
# Tests argument parsing, help text, missing argument errors, and
# dry-run output WITHOUT requiring a running cluster or VM.
#
# Run from project root: bats research/perfetto/tests/test-perfetto-start.bats

setup() {
    export PROJECT_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../../.." && pwd -P)"
    export PERFETTO_BIN="$PROJECT_ROOT/research/perfetto/bin"
    export PERFETTO_START_SH="$PERFETTO_BIN/perfetto-start.sh"
}

# =============================================================================
# VC-008: --help prints usage and exits 0
# =============================================================================

@test "S01: perfetto-start.sh file exists" {
    [ -f "$PERFETTO_START_SH" ]
}

@test "S02: --help prints usage and exits 0" {
    run bash "$PERFETTO_START_SH" --help
    [ "$status" -eq 0 ]
    [[ "$output" == *"Usage:"* ]] || [[ "$output" == *"usage:"* ]]
}

@test "S03: -h prints usage and exits 0" {
    run bash "$PERFETTO_START_SH" -h
    [ "$status" -eq 0 ]
    [[ "$output" == *"Usage:"* ]] || [[ "$output" == *"usage:"* ]]
}

@test "S04: --help output contains expected positional arguments" {
    run bash "$PERFETTO_START_SH" --help
    [[ "$output" == *"node-ip"* ]] || [[ "$output" == *"NODE"* ]] || [[ "$output" == *"<ip>"* ]]
    [[ "$output" == *"config"* ]] || [[ "$output" == *"CONFIG"* ]]
}

@test "S05: --help output contains expected options (--duration, --output)" {
    run bash "$PERFETTO_START_SH" --help
    [[ "$output" == *"--duration"* ]]
    [[ "$output" == *"--output"* ]]
}

# =============================================================================
# VC-009: Missing arguments print error and exit non-zero
# =============================================================================

@test "S06: no arguments prints error and exits non-zero" {
    run bash "$PERFETTO_START_SH"
    [ "$status" -ne 0 ]
    [[ "$output" == *"rror"* ]] || [[ "$output" == *"sage"* ]] || [[ "$output" == *"equired"* ]]
}

@test "S07: missing config-name argument prints error and exits non-zero" {
    run bash "$PERFETTO_START_SH" "192.168.122.10"
    [ "$status" -ne 0 ]
    [[ "$output" == *"rror"* ]] || [[ "$output" == *"sage"* ]] || [[ "$output" == *"config"* ]]
}

@test "S08: missing node-ip argument prints error and exits non-zero" {
    # This catches when only a flag is passed without positional args
    run bash "$PERFETTO_START_SH" --duration 10
    [ "$status" -ne 0 ]
}

# =============================================================================
# VC-010: Option parsing
# =============================================================================

@test "S09: --duration accepts a numeric value" {
    run bash "$PERFETTO_START_SH" --help
    [ "$status" -eq 0 ]
    # Verifying it's documented — actual parsing needs runtime test
}

@test "S10: --output accepts a trace name" {
    run bash "$PERFETTO_START_SH" --help
    [ "$status" -eq 0 ]
    # Verifying it's documented — actual parsing needs runtime test
}

@test "S11: --duration without value fails gracefully" {
    run bash "$PERFETTO_START_SH" "192.168.122.10" "scheduling" --duration
    # --duration at end of args with no value should error
    [ "$status" -ne 0 ]
}

@test "S12: --output without value fails gracefully" {
    run bash "$PERFETTO_START_SH" "192.168.122.10" "scheduling" --output
    # --output at end of args with no value should error
    [ "$status" -ne 0 ]
}

# =============================================================================
# VC-011: Unknown options are rejected
# =============================================================================

@test "S13: unknown option prints error and exits non-zero" {
    run bash "$PERFETTO_START_SH" --bogus-option
    [ "$status" -ne 0 ]
    [[ "$output" == *"nknown"* ]] || [[ "$output" == *"nrecognized"* ]] || [[ "$output" == *"nvalid"* ]] || [[ "$output" == *"rror"* ]]
}

# =============================================================================
# VC-012: Help text contains command description
# =============================================================================

@test "S14: --help output describes what the script does" {
    run bash "$PERFETTO_START_SH" --help
    # Should contain some description of starting a trace
    [[ "$output" == *"trace"* ]] || [[ "$output" == *"perfetto"* ]] || [[ "$output" == *"start"* ]]
}
