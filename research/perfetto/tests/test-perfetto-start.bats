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

# =============================================================================
# REQ-001 (amended): tracebox command MUST pass --txt (pbtxt config parsing;
# trace OUTPUT is always binary protobuf regardless of --txt)
# =============================================================================

@test "S15: REQ-001 dry-run tracebox command contains --txt" {
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

@test "S16: REQ-001 real tracebox command construction includes --txt" {
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
# REQ-002: trace filenames end in .perfetto-trace
# =============================================================================

@test "S17: REQ-002 default remote trace path ends in .perfetto-trace" {
    run env DRY_RUN=true bash "$PERFETTO_START_SH" "192.168.122.10" "scheduling" --duration 10
    [ "$status" -eq 0 ]
    [[ "$output" == *"/tmp/scheduling-"*".perfetto-trace"* ]]
}

@test "S18: REQ-002 --output custom name still yields .perfetto-trace suffix" {
    run env DRY_RUN=true bash "$PERFETTO_START_SH" "192.168.122.10" "scheduling" --duration 10 --output my-custom-trace
    [ "$status" -eq 0 ]
    [[ "$output" == *"/tmp/my-custom-trace.perfetto-trace"* ]]
}

# =============================================================================
# Edge cases
# =============================================================================

@test "S19: dry-run output is parseable (pid + remote path, space-separated)" {
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

@test "S20: config name with .cfg extension resolves in dry-run output" {
    run env DRY_RUN=true bash "$PERFETTO_START_SH" "192.168.122.10" "scheduling.cfg" --duration 10
    [ "$status" -eq 0 ]
    [[ "$output" == *"/tmp/scheduling.cfg"* ]]
}

@test "S21: empty-string positional args print error and exit non-zero" {
    run bash "$PERFETTO_START_SH" "" ""
    [ "$status" -ne 0 ]
}
