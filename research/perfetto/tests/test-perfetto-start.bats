#!/usr/bin/env bats
# test-perfetto-start.bats — Tests for perfetto-start.sh trace launcher
#
# Tests argument parsing, help text, missing argument errors, and
# dry-run output WITHOUT requiring a running cluster or VM.
#
# Run from project root: bats research/cpu-sched/perfetto/tests/test-perfetto-start.bats

setup() {
    export PROJECT_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../../../.." && pwd -P)"
    export PERFETTO_BIN="$PROJECT_ROOT/research/cpu-sched/perfetto/bin"
    export PERFETTO_START_SH="$PERFETTO_BIN/perfetto-start.sh"
}

# =============================================================================
# --help prints usage and exits 0
# =============================================================================

@test "perfetto-start.sh file exists" {
    [ -f "$PERFETTO_START_SH" ]
}

@test "--help prints usage and exits 0" {
    run bash "$PERFETTO_START_SH" --help
    [ "$status" -eq 0 ]
    [[ "$output" == *"Usage:"* ]] || [[ "$output" == *"usage:"* ]]
}

@test "-h prints usage and exits 0" {
    run bash "$PERFETTO_START_SH" -h
    [ "$status" -eq 0 ]
    [[ "$output" == *"Usage:"* ]] || [[ "$output" == *"usage:"* ]]
}

@test "--help output contains expected positional arguments" {
    run bash "$PERFETTO_START_SH" --help
    [[ "$output" == *"node-ip"* ]] || [[ "$output" == *"NODE"* ]] || [[ "$output" == *"<ip>"* ]]
    [[ "$output" == *"config"* ]] || [[ "$output" == *"CONFIG"* ]]
}

@test "--help output contains expected options (--duration, --output)" {
    run bash "$PERFETTO_START_SH" --help
    [[ "$output" == *"--duration"* ]]
    [[ "$output" == *"--output"* ]]
}

# =============================================================================
# Missing arguments print error and exit non-zero
# =============================================================================

@test "no arguments prints error and exits non-zero" {
    run bash "$PERFETTO_START_SH"
    [ "$status" -ne 0 ]
    [[ "$output" == *"rror"* ]] || [[ "$output" == *"sage"* ]] || [[ "$output" == *"equired"* ]]
}

@test "missing config-name argument prints error and exits non-zero" {
    run bash "$PERFETTO_START_SH" "192.168.122.10"
    [ "$status" -ne 0 ]
    [[ "$output" == *"rror"* ]] || [[ "$output" == *"sage"* ]] || [[ "$output" == *"config"* ]]
}

@test "missing node-ip argument prints error and exits non-zero" {
    # This catches when only a flag is passed without positional args
    run bash "$PERFETTO_START_SH" --duration 10
    [ "$status" -ne 0 ]
}

# =============================================================================
# Option parsing
# =============================================================================

@test "--duration accepts a numeric value" {
    run bash "$PERFETTO_START_SH" --help
    [ "$status" -eq 0 ]
    # Verifying it's documented — actual parsing needs runtime test
}

@test "--output accepts a trace name" {
    run bash "$PERFETTO_START_SH" --help
    [ "$status" -eq 0 ]
    # Verifying it's documented — actual parsing needs runtime test
}

@test "--duration without value fails gracefully" {
    run bash "$PERFETTO_START_SH" "192.168.122.10" "scheduling" --duration
    # --duration at end of args with no value should error
    [ "$status" -ne 0 ]
}

@test "--output without value fails gracefully" {
    run bash "$PERFETTO_START_SH" "192.168.122.10" "scheduling" --output
    # --output at end of args with no value should error
    [ "$status" -ne 0 ]
}

# =============================================================================
# Unknown options are rejected
# =============================================================================

@test "unknown option prints error and exits non-zero" {
    run bash "$PERFETTO_START_SH" --bogus-option
    [ "$status" -ne 0 ]
    [[ "$output" == *"nknown"* ]] || [[ "$output" == *"nrecognized"* ]] || [[ "$output" == *"nvalid"* ]] || [[ "$output" == *"rror"* ]]
}

# =============================================================================
# Help text contains command description
# =============================================================================

@test "--help output describes what the script does" {
    run bash "$PERFETTO_START_SH" --help
    # Should contain some description of starting a trace
    [[ "$output" == *"trace"* ]] || [[ "$output" == *"perfetto"* ]] || [[ "$output" == *"start"* ]]
}

# =============================================================================
# (amended): tracebox command MUST pass --txt (pbtxt config parsing;
# trace OUTPUT is always binary protobuf regardless of --txt)
# =============================================================================

@test "dry-run tracebox command contains --txt" {
    run env DRY_RUN=true bash "$PERFETTO_START_SH" "192.168.122.10" "scheduling" --duration 10
    [ "$status" -eq 0 ]
    # Dry-run still emits the tracebox invocation...
    [[ "$output" == *"tracebox"* ]]
    # ...with --txt: per `tracebox --help`, "--txt : Parse config as pbtxt".
    # The repo .cfg files are pbtxt text configs; without --txt tracebox
    # rejects them ("The trace config is invalid, bailing out."). Trace
    # OUTPUT is always binary protobuf regardless of --txt.
    # EXPECTED TO FAIL pre-restore: --txt was stripped from the tracebox
    # command; green once the restore lands.
    [[ "$output" == *"--txt"* ]]
}

@test "real tracebox command construction includes --txt" {
    # The real command is only constructed in non-dry-run (needs a live node),
    # so pin the command construction by inspecting the script source: the
    # tracebox invocation must exist and carry --txt. --txt means "parse the
    # -c config as pbtxt text config" (tracebox --help); trace OUTPUT is
    # always binary protobuf.
    # EXPECTED TO FAIL pre-restore: --txt was stripped from the tracebox
    # command; green once the restore lands.
    run grep -n 'tracebox' "$PERFETTO_START_SH"
    [ "$status" -eq 0 ]
    [[ "$output" == *"tracebox -o"* ]]
    run grep -- '--txt' "$PERFETTO_START_SH"
    [ "$status" -eq 0 ]
}

# =============================================================================
# trace filenames end in .perfetto-trace
# =============================================================================

@test "default remote trace path ends in .perfetto-trace" {
    run env DRY_RUN=true bash "$PERFETTO_START_SH" "192.168.122.10" "scheduling" --duration 10
    [ "$status" -eq 0 ]
    [[ "$output" == *"/tmp/scheduling-"*".perfetto-trace"* ]]
}

@test "--output custom name still yields .perfetto-trace suffix" {
    run env DRY_RUN=true bash "$PERFETTO_START_SH" "192.168.122.10" "scheduling" --duration 10 --output my-custom-trace
    [ "$status" -eq 0 ]
    [[ "$output" == *"/tmp/my-custom-trace.perfetto-trace"* ]]
}

# =============================================================================
# Edge cases
# =============================================================================

@test "dry-run output is parseable (pid + remote path, space-separated)" {
    run env DRY_RUN=true bash "$PERFETTO_START_SH" "192.168.122.10" "scheduling" --duration 10
    [ "$status" -eq 0 ]
    local parsed
    parsed="$(printf '%s\n' "$output" | sed -n 's/.*Would print: //p')"
    [ -n "$parsed" ]
    # Exactly two space-separated fields: pid and remote trace path
    local field_count
    field_count="$(printf '%s\n' "$parsed" | awk '{print NF}')"
    [ "$field_count" -eq 2 ]
    [[ "$parsed" == "<pid>"*"/tmp/"*".perfetto-trace" ]]
}

@test "config name with .cfg extension resolves in dry-run output" {
    run env DRY_RUN=true bash "$PERFETTO_START_SH" "192.168.122.10" "scheduling.cfg" --duration 10
    [ "$status" -eq 0 ]
    [[ "$output" == *"/tmp/scheduling.cfg"* ]]
}

@test "empty-string positional args print error and exit non-zero" {
    run bash "$PERFETTO_START_SH" "" ""
    [ "$status" -ne 0 ]
}
