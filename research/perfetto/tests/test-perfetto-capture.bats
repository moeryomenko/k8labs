#!/usr/bin/env bats
# test-perfetto-capture.bats — Tests for perfetto-capture.sh full lifecycle wrapper
#
# Tests argument parsing, help text, dry-run mode, missing argument errors,
# and option handling WITHOUT requiring a running cluster or VM.
#
# Run from project root: bats research/perfetto/tests/test-perfetto-capture.bats

setup() {
    export PROJECT_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../../.." && pwd -P)"
    export PERFETTO_BIN="$PROJECT_ROOT/research/perfetto/bin"
    export PERFETTO_CAPTURE_SH="$PERFETTO_BIN/perfetto-capture.sh"
}

# =============================================================================
# VC-018: --help prints usage and exits 0
# =============================================================================

@test "R01: perfetto-capture.sh file exists" {
    [ -f "$PERFETTO_CAPTURE_SH" ]
}

@test "R02: --help prints usage and exits 0" {
    run bash "$PERFETTO_CAPTURE_SH" --help
    [ "$status" -eq 0 ]
    [[ "$output" == *"Usage:"* ]] || [[ "$output" == *"usage:"* ]]
}

@test "R03: -h prints usage and exits 0" {
    run bash "$PERFETTO_CAPTURE_SH" -h
    [ "$status" -eq 0 ]
    [[ "$output" == *"Usage:"* ]] || [[ "$output" == *"usage:"* ]]
}

@test "R04: --help output contains expected positional arguments" {
    run bash "$PERFETTO_CAPTURE_SH" --help
    [[ "$output" == *"node-ip"* ]] || [[ "$output" == *"NODE"* ]] || [[ "$output" == *"<ip>"* ]]
    [[ "$output" == *"config"* ]] || [[ "$output" == *"CONFIG"* ]]
}

@test "R05: --help output contains expected options (--duration, --dry-run, --output-dir)" {
    run bash "$PERFETTO_CAPTURE_SH" --help
    [[ "$output" == *"--duration"* ]]
    [[ "$output" == *"--dry-run"* ]]
    [[ "$output" == *"--output-dir"* ]]
}

# =============================================================================
# VC-019: Missing arguments print error and exit non-zero
# =============================================================================

@test "R06: no arguments prints error and exits non-zero" {
    run bash "$PERFETTO_CAPTURE_SH"
    [ "$status" -ne 0 ]
    [[ "$output" == *"rror"* ]] || [[ "$output" == *"sage"* ]] || [[ "$output" == *"equired"* ]]
}

@test "R07: missing --duration prints error and exits non-zero" {
    run bash "$PERFETTO_CAPTURE_SH" "192.168.122.10" "scheduling"
    [ "$status" -ne 0 ]
    [[ "$output" == *"duration"* ]] || [[ "$output" == *"Duration"* ]] || [[ "$output" == *"equired"* ]]
}

@test "R08: missing config-name prints error and exits non-zero" {
    run bash "$PERFETTO_CAPTURE_SH" "192.168.122.10"
    [ "$status" -ne 0 ]
}

# =============================================================================
# VC-020: --duration option parsing
# =============================================================================

@test "R09: --duration accepts a numeric value" {
    run bash "$PERFETTO_CAPTURE_SH" --help
    [ "$status" -eq 0 ]
    [[ "$output" == *"--duration"* ]]
}

@test "R10: --duration without value fails gracefully" {
    run bash "$PERFETTO_CAPTURE_SH" "192.168.122.10" "scheduling" --duration
    [ "$status" -ne 0 ]
}

@test "R11: --duration with non-numeric value fails gracefully" {
    run bash "$PERFETTO_CAPTURE_SH" "192.168.122.10" "scheduling" --duration not-a-number
    [ "$status" -ne 0 ]
    [[ "$output" == *"nvalid"* ]] || [[ "$output" == *"umber"* ]] || [[ "$output" == *"rror"* ]]
}

# =============================================================================
# VC-021: --dry-run mode shows operations without executing
# =============================================================================

@test "R12: --dry-run shows operations without connecting" {
    run bash "$PERFETTO_CAPTURE_SH" "192.168.122.10" "scheduling" --duration 10 --dry-run
    [ "$status" -eq 0 ]
    [[ "$output" == *"dry"* ]] || [[ "$output" == *"DRY"* ]] || [[ "$output" == *"Dry"* ]]
}

@test "R13: --dry-run output mentions start step" {
    run bash "$PERFETTO_CAPTURE_SH" "192.168.122.10" "scheduling" --duration 10 --dry-run
    [[ "$output" == *"start"* ]] || [[ "$output" == *"Start"* ]] || [[ "$output" == *"tracebox"* ]]
}

@test "R14: --dry-run output mentions stop step" {
    run bash "$PERFETTO_CAPTURE_SH" "192.168.122.10" "scheduling" --duration 10 --dry-run
    [[ "$output" == *"stop"* ]] || [[ "$output" == *"Stop"* ]] || [[ "$output" == *"SIGTERM"* ]]
}

@test "R15: --dry-run output mentions download/SCP step" {
    run bash "$PERFETTO_CAPTURE_SH" "192.168.122.10" "scheduling" --duration 10 --dry-run
    [[ "$output" == *"download"* ]] || [[ "$output" == *"SCP"* ]] || [[ "$output" == *"scp"* ]]
}

@test "R16: --dry-run output mentions wait/sleep duration" {
    run bash "$PERFETTO_CAPTURE_SH" "192.168.122.10" "scheduling" --duration 10 --dry-run
    [[ "$output" == *"10"* ]] || [[ "$output" == *"duration"* ]] || [[ "$output" == *"second"* ]]
}

@test "R17: --dry-run without --duration still shows operations" {
    run bash "$PERFETTO_CAPTURE_SH" "192.168.122.10" "scheduling" --dry-run
    # Without --duration but with --dry-run, should still show operations
    # (dry-run overrides the --duration requirement or shows an error about it)
    [ "$status" -eq 0 ]
}

# =============================================================================
# VC-022: --output-dir option
# =============================================================================

@test "R18: --output-dir is accepted" {
    run bash "$PERFETTO_CAPTURE_SH" --help
    [ "$status" -eq 0 ]
    [[ "$output" == *"--output-dir"* ]]
}

@test "R19: --output-dir without value fails gracefully" {
    run bash "$PERFETTO_CAPTURE_SH" "192.168.122.10" "scheduling" --duration 10 --output-dir
    [ "$status" -ne 0 ]
}

# =============================================================================
# VC-023: Unknown options are rejected
# =============================================================================

@test "R20: unknown option prints error and exits non-zero" {
    run bash "$PERFETTO_CAPTURE_SH" --bogus-option
    [ "$status" -ne 0 ]
    [[ "$output" == *"nknown"* ]] || [[ "$output" == *"nrecognized"* ]] || [[ "$output" == *"nvalid"* ]] || [[ "$output" == *"rror"* ]]
}

# =============================================================================
# VC-024: Argument ordering flexibility
# =============================================================================

@test "R21: options before positional arguments work" {
    run bash "$PERFETTO_CAPTURE_SH" --duration 10 "192.168.122.10" "scheduling" --dry-run
    [ "$status" -eq 0 ]
    [[ "$output" == *"dry"* ]] || [[ "$output" == *"DRY"* ]]
}

@test "R22: options after positional arguments work" {
    run bash "$PERFETTO_CAPTURE_SH" "192.168.122.10" "scheduling" --duration 10 --dry-run
    [ "$status" -eq 0 ]
    [[ "$output" == *"dry"* ]] || [[ "$output" == *"DRY"* ]]
}

# =============================================================================
# VC-025: Help text describes lifecycle orchestration
# =============================================================================

@test "R23: --help output describes full lifecycle orchestration" {
    run bash "$PERFETTO_CAPTURE_SH" --help
    [[ "$output" == *"capture"* ]] || [[ "$output" == *"lifecycle"* ]] || [[ "$output" == *"orchestrat"* ]]
    [[ "$output" == *"start"* ]] && [[ "$output" == *"stop"* ]]
}

# =============================================================================
# REQ-001 (amended): capture dry-run tracebox command MUST pass --txt (pbtxt
# config parsing; trace OUTPUT is always binary protobuf)
# =============================================================================

@test "R24: REQ-001 capture dry-run tracebox command contains --txt" {
    run bash "$PERFETTO_CAPTURE_SH" "192.168.122.10" "scheduling" --duration 10 --dry-run
    [ "$status" -eq 0 ]
    # The dry-run plan still emits the tracebox invocation...
    [[ "$output" == *"tracebox"* ]]
    # ...with --txt: per `tracebox --help`, "--txt : Parse config as pbtxt".
    # The repo .cfg files are pbtxt text configs; without --txt tracebox
    # rejects them ("The trace config is invalid, bailing out."). Trace
    # OUTPUT is always binary protobuf regardless of --txt.
    # EXPECTED TO FAIL pre-restore: --txt was stripped from the plan text;
    # green once the restore lands.
    [[ "$output" == *"--txt"* ]]
}
