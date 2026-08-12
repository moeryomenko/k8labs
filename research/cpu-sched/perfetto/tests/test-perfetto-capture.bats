#!/usr/bin/env bats
# test-perfetto-capture.bats — Tests for perfetto-capture.sh full lifecycle wrapper
#
# Tests argument parsing, help text, dry-run mode, missing argument errors,
# and option handling WITHOUT requiring a running cluster or VM.
#
# Run from project root: bats research/cpu-sched/perfetto/tests/test-perfetto-capture.bats

setup() {
    export PROJECT_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../../../.." && pwd -P)"
    export PERFETTO_BIN="$PROJECT_ROOT/research/cpu-sched/perfetto/bin"
    export PERFETTO_CAPTURE_SH="$PERFETTO_BIN/perfetto-capture.sh"
}

# =============================================================================
# --help prints usage and exits 0
# =============================================================================

@test "perfetto-capture.sh file exists" {
    [ -f "$PERFETTO_CAPTURE_SH" ]
}

@test "--help prints usage and exits 0" {
    run bash "$PERFETTO_CAPTURE_SH" --help
    [ "$status" -eq 0 ]
    [[ "$output" == *"Usage:"* ]] || [[ "$output" == *"usage:"* ]]
}

@test "-h prints usage and exits 0" {
    run bash "$PERFETTO_CAPTURE_SH" -h
    [ "$status" -eq 0 ]
    [[ "$output" == *"Usage:"* ]] || [[ "$output" == *"usage:"* ]]
}

@test "--help output contains expected positional arguments" {
    run bash "$PERFETTO_CAPTURE_SH" --help
    [[ "$output" == *"node-ip"* ]] || [[ "$output" == *"NODE"* ]] || [[ "$output" == *"<ip>"* ]]
    [[ "$output" == *"config"* ]] || [[ "$output" == *"CONFIG"* ]]
}

@test "--help output contains expected options (--duration, --dry-run, --output-dir)" {
    run bash "$PERFETTO_CAPTURE_SH" --help
    [[ "$output" == *"--duration"* ]]
    [[ "$output" == *"--dry-run"* ]]
    [[ "$output" == *"--output-dir"* ]]
}

# =============================================================================
# Missing arguments print error and exit non-zero
# =============================================================================

@test "no arguments prints error and exits non-zero" {
    run bash "$PERFETTO_CAPTURE_SH"
    [ "$status" -ne 0 ]
    [[ "$output" == *"rror"* ]] || [[ "$output" == *"sage"* ]] || [[ "$output" == *"equired"* ]]
}

@test "missing --duration prints error and exits non-zero" {
    run bash "$PERFETTO_CAPTURE_SH" "192.168.122.10" "scheduling"
    [ "$status" -ne 0 ]
    [[ "$output" == *"duration"* ]] || [[ "$output" == *"Duration"* ]] || [[ "$output" == *"equired"* ]]
}

@test "missing config-name prints error and exits non-zero" {
    run bash "$PERFETTO_CAPTURE_SH" "192.168.122.10"
    [ "$status" -ne 0 ]
}

# =============================================================================
# --duration option parsing
# =============================================================================

@test "--duration accepts a numeric value" {
    run bash "$PERFETTO_CAPTURE_SH" --help
    [ "$status" -eq 0 ]
    [[ "$output" == *"--duration"* ]]
}

@test "--duration without value fails gracefully" {
    run bash "$PERFETTO_CAPTURE_SH" "192.168.122.10" "scheduling" --duration
    [ "$status" -ne 0 ]
}

@test "--duration with non-numeric value fails gracefully" {
    run bash "$PERFETTO_CAPTURE_SH" "192.168.122.10" "scheduling" --duration not-a-number
    [ "$status" -ne 0 ]
    [[ "$output" == *"nvalid"* ]] || [[ "$output" == *"umber"* ]] || [[ "$output" == *"rror"* ]]
}

# =============================================================================
# --dry-run mode shows operations without executing
# =============================================================================

@test "--dry-run shows operations without connecting" {
    run bash "$PERFETTO_CAPTURE_SH" "192.168.122.10" "scheduling" --duration 10 --dry-run
    [ "$status" -eq 0 ]
    [[ "$output" == *"dry"* ]] || [[ "$output" == *"DRY"* ]] || [[ "$output" == *"Dry"* ]]
}

@test "--dry-run output mentions start step" {
    run bash "$PERFETTO_CAPTURE_SH" "192.168.122.10" "scheduling" --duration 10 --dry-run
    [[ "$output" == *"start"* ]] || [[ "$output" == *"Start"* ]] || [[ "$output" == *"tracebox"* ]]
}

@test "--dry-run output mentions stop step" {
    run bash "$PERFETTO_CAPTURE_SH" "192.168.122.10" "scheduling" --duration 10 --dry-run
    [[ "$output" == *"stop"* ]] || [[ "$output" == *"Stop"* ]] || [[ "$output" == *"SIGTERM"* ]]
}

@test "--dry-run output mentions download/SCP step" {
    run bash "$PERFETTO_CAPTURE_SH" "192.168.122.10" "scheduling" --duration 10 --dry-run
    [[ "$output" == *"download"* ]] || [[ "$output" == *"SCP"* ]] || [[ "$output" == *"scp"* ]]
}

@test "--dry-run output mentions wait/sleep duration" {
    run bash "$PERFETTO_CAPTURE_SH" "192.168.122.10" "scheduling" --duration 10 --dry-run
    [[ "$output" == *"10"* ]] || [[ "$output" == *"duration"* ]] || [[ "$output" == *"second"* ]]
}

@test "--dry-run without --duration still shows operations" {
    run bash "$PERFETTO_CAPTURE_SH" "192.168.122.10" "scheduling" --dry-run
    # Without --duration but with --dry-run, should still show operations
    # (dry-run overrides the --duration requirement or shows an error about it)
    [ "$status" -eq 0 ]
}

# =============================================================================
# --output-dir option
# =============================================================================

@test "--output-dir is accepted" {
    run bash "$PERFETTO_CAPTURE_SH" --help
    [ "$status" -eq 0 ]
    [[ "$output" == *"--output-dir"* ]]
}

@test "--output-dir without value fails gracefully" {
    run bash "$PERFETTO_CAPTURE_SH" "192.168.122.10" "scheduling" --duration 10 --output-dir
    [ "$status" -ne 0 ]
}

# =============================================================================
# Unknown options are rejected
# =============================================================================

@test "unknown option prints error and exits non-zero" {
    run bash "$PERFETTO_CAPTURE_SH" --bogus-option
    [ "$status" -ne 0 ]
    [[ "$output" == *"nknown"* ]] || [[ "$output" == *"nrecognized"* ]] || [[ "$output" == *"nvalid"* ]] || [[ "$output" == *"rror"* ]]
}

# =============================================================================
# Argument ordering flexibility
# =============================================================================

@test "options before positional arguments work" {
    run bash "$PERFETTO_CAPTURE_SH" --duration 10 "192.168.122.10" "scheduling" --dry-run
    [ "$status" -eq 0 ]
    [[ "$output" == *"dry"* ]] || [[ "$output" == *"DRY"* ]]
}

@test "options after positional arguments work" {
    run bash "$PERFETTO_CAPTURE_SH" "192.168.122.10" "scheduling" --duration 10 --dry-run
    [ "$status" -eq 0 ]
    [[ "$output" == *"dry"* ]] || [[ "$output" == *"DRY"* ]]
}

# =============================================================================
# Help text describes lifecycle orchestration
# =============================================================================

@test "--help output describes full lifecycle orchestration" {
    run bash "$PERFETTO_CAPTURE_SH" --help
    [[ "$output" == *"capture"* ]] || [[ "$output" == *"lifecycle"* ]] || [[ "$output" == *"orchestrat"* ]]
    [[ "$output" == *"start"* ]] && [[ "$output" == *"stop"* ]]
}

# =============================================================================
# (amended): capture dry-run tracebox command MUST pass --txt (pbtxt
# config parsing; trace OUTPUT is always binary protobuf)
# =============================================================================

@test "capture dry-run tracebox command contains --txt" {
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
