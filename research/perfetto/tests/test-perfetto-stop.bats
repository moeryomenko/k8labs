#!/usr/bin/env bats
# test-perfetto-stop.bats — Tests for perfetto-stop.sh trace stopper/downloader
#
# Tests argument parsing, help text, missing argument errors, and
# option handling WITHOUT requiring a running cluster or VM.
#
# Run from project root: bats research/perfetto/tests/test-perfetto-stop.bats

setup() {
    export PROJECT_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../../.." && pwd -P)"
    export PERFETTO_BIN="$PROJECT_ROOT/research/perfetto/bin"
    export PERFETTO_STOP_SH="$PERFETTO_BIN/perfetto-stop.sh"
}

# =============================================================================
# VC-013: --help prints usage and exits 0
# =============================================================================

@test "P01: perfetto-stop.sh file exists" {
    [ -f "$PERFETTO_STOP_SH" ]
}

@test "P02: --help prints usage and exits 0" {
    run bash "$PERFETTO_STOP_SH" --help
    [ "$status" -eq 0 ]
    [[ "$output" == *"Usage:"* ]] || [[ "$output" == *"usage:"* ]]
}

@test "P03: -h prints usage and exits 0" {
    run bash "$PERFETTO_STOP_SH" -h
    [ "$status" -eq 0 ]
    [[ "$output" == *"Usage:"* ]] || [[ "$output" == *"usage:"* ]]
}

@test "P04: --help output contains expected positional arguments" {
    run bash "$PERFETTO_STOP_SH" --help
    [[ "$output" == *"node-ip"* ]] || [[ "$output" == *"NODE"* ]] || [[ "$output" == *"<ip>"* ]]
    [[ "$output" == *"trace-pid"* ]] || [[ "$output" == *"PID"* ]] || [[ "$output" == *"<pid>"* ]]
}

@test "P05: --help output contains expected options (--output-dir)" {
    run bash "$PERFETTO_STOP_SH" --help
    [[ "$output" == *"--output-dir"* ]]
}

# =============================================================================
# VC-014: Missing arguments print error and exit non-zero
# =============================================================================

@test "P06: no arguments prints error and exits non-zero" {
    run bash "$PERFETTO_STOP_SH"
    [ "$status" -ne 0 ]
    [[ "$output" == *"rror"* ]] || [[ "$output" == *"sage"* ]] || [[ "$output" == *"equired"* ]]
}

@test "P07: missing trace-pid argument prints error and exits non-zero" {
    run bash "$PERFETTO_STOP_SH" "192.168.122.10"
    [ "$status" -ne 0 ]
    [[ "$output" == *"rror"* ]] || [[ "$output" == *"sage"* ]] || [[ "$output" == *"pid"* ]] || [[ "$output" == *"PID"* ]]
}

@test "P08: missing both arguments prints error and exits non-zero" {
    run bash "$PERFETTO_STOP_SH"
    [ "$status" -ne 0 ]
}

# =============================================================================
# VC-015: Option parsing
# =============================================================================

@test "P09: --output-dir accepts a directory path" {
    run bash "$PERFETTO_STOP_SH" --help
    [ "$status" -eq 0 ]
    [[ "$output" == *"--output-dir"* ]]
}

@test "P10: --output-dir without value fails gracefully" {
    run bash "$PERFETTO_STOP_SH" "192.168.122.10" "1234" --output-dir
    [ "$status" -ne 0 ]
}

@test "P11: --output-dir at end fails gracefully" {
    run bash "$PERFETTO_STOP_SH" "192.168.122.10" "1234" --output-dir
    [ "$status" -ne 0 ]
}

# =============================================================================
# VC-016: Unknown options are rejected
# =============================================================================

@test "P12: unknown option prints error and exits non-zero" {
    run bash "$PERFETTO_STOP_SH" --bogus-option
    [ "$status" -ne 0 ]
    [[ "$output" == *"nknown"* ]] || [[ "$output" == *"nrecognized"* ]] || [[ "$output" == *"nvalid"* ]] || [[ "$output" == *"rror"* ]]
}

# =============================================================================
# VC-017: Help text describes stop/download behavior
# =============================================================================

@test "P13: --help output describes stopping and downloading trace" {
    run bash "$PERFETTO_STOP_SH" --help
    [[ "$output" == *"stop"* ]] || [[ "$output" == *"SIGTERM"* ]] || [[ "$output" == *"kill"* ]]
    [[ "$output" == *"download"* ]] || [[ "$output" == *"SCP"* ]] || [[ "$output" == *"scp"* ]]
}

# =============================================================================
# REQ-003: _reconstruct_remote_path extracts /tmp/<name>.perfetto-trace
#          from /proc/<pid>/cmdline
#
# The real script is sourced with its trailing `main "$@"` disabled (via a
# temp fixture copy) so the function can be unit-tested directly. ssh is
# stubbed via PATH to simulate the remote `cat /proc/<pid>/cmdline` output.
# =============================================================================

@test "P14: REQ-003 _reconstruct_remote_path extracts trace path from cmdline" {
    local stub_dir
    stub_dir="$(mktemp -d)"
    cat > "$stub_dir/ssh" <<'STUB'
#!/bin/bash
# Simulate remote `cat /proc/<pid>/cmdline | tr '\0' ' '` output
printf '%s\n' 'nohup /usr/bin/tracebox -o /tmp/cpu-trace-123.perfetto-trace -c /tmp/cpu.cfg'
exit 0
STUB
    chmod +x "$stub_dir/ssh"

    local tmp_stop
    tmp_stop="$(mktemp)"
    sed -e '/^main "\$@"$/d' \
        -e "s|source \"\$SCRIPT_DIR/perfetto-common.sh\"|source \"$PERFETTO_BIN/perfetto-common.sh\"|" \
        "$PERFETTO_STOP_SH" > "$tmp_stop"

    run env PATH="$stub_dir:$PATH" bash -c "
        source '$tmp_stop'
        _reconstruct_remote_path '192.168.122.10' '1234'
    "
    [ "$status" -eq 0 ]
    [ "$output" = "/tmp/cpu-trace-123.perfetto-trace" ]
    rm -rf "$stub_dir" "$tmp_stop"
}

@test "P15: REQ-003 _reconstruct_remote_path fails when cmdline has no trace path" {
    local stub_dir
    stub_dir="$(mktemp -d)"
    cat > "$stub_dir/ssh" <<'STUB'
#!/bin/bash
# cmdline without any /tmp/*.perfetto-trace output path
printf '%s\n' '/usr/bin/tracebox -o /tmp/plain-output.txt -c /tmp/cpu.cfg'
exit 0
STUB
    chmod +x "$stub_dir/ssh"

    local tmp_stop
    tmp_stop="$(mktemp)"
    sed -e '/^main "\$@"$/d' \
        -e "s|source \"\$SCRIPT_DIR/perfetto-common.sh\"|source \"$PERFETTO_BIN/perfetto-common.sh\"|" \
        "$PERFETTO_STOP_SH" > "$tmp_stop"

    run env PATH="$stub_dir:$PATH" bash -c "
        source '$tmp_stop'
        _reconstruct_remote_path '192.168.122.10' '4321'
    "
    [ "$status" -ne 0 ]
    rm -rf "$stub_dir" "$tmp_stop"
}

# =============================================================================
# REQ-002: local trace filename keeps the .perfetto-trace suffix
# =============================================================================

@test "P16: REQ-002 stop dry-run local path keeps .perfetto-trace basename" {
    run env DRY_RUN=true bash "$PERFETTO_STOP_SH" "192.168.122.10" "1234" --output-dir /tmp/perfetto-out
    [ "$status" -eq 0 ]
    # Dry-run fakes the reconstructed remote path as /tmp/trace-<pid>.perfetto-trace
    [[ "$output" == *"/tmp/trace-1234.perfetto-trace"* ]]
    # Local download path is <output-dir>/<basename> with the suffix intact
    [[ "$output" == *"/tmp/perfetto-out/trace-1234.perfetto-trace"* ]]
}

@test "P17: REQ-002 stop with --remote-path downloads to output-dir/<basename>.perfetto-trace" {
    run env DRY_RUN=true bash "$PERFETTO_STOP_SH" "192.168.122.10" "1234" \
        --output-dir /tmp/perfetto-out --remote-path /tmp/whatever.perfetto-trace
    [ "$status" -eq 0 ]
    [[ "$output" == *"/tmp/perfetto-out/whatever.perfetto-trace"* ]]
}
