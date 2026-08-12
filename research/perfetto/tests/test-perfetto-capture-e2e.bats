#!/usr/bin/env bats
# test-perfetto-capture-e2e.bats — End-to-end smoke test for perfetto-capture.sh
#
# Proves the capture lifecycle against a LIVE node: upload config, start
# tracebox, wait, stop, download. Then validates the downloaded artifact is a
# genuine binary Perfetto trace — NOT the shell/python wrapper script, which is
# the regression this suite guards against.
#
# =============================================================================
# How this suite runs (after tracebox has been deployed to the node):
#
#   NODE_IP=<node-ip> bats research/cpu-sched/perfetto/tests/test-perfetto-capture-e2e.bats
#
# Example:
#   NODE_IP=192.168.124.11 bats research/cpu-sched/perfetto/tests/test-perfetto-capture-e2e.bats
#
# Without NODE_IP the live capture tests skip cleanly. The fixture-based
# assertion tests and the skip-path tests run without a node, so the
# regression detection logic is always exercised.
#
# The smoke experiment config used by the runner is
# research/cpu-sched/experiments/configs/perfetto-smoke.yaml; validate it with:
#   research/cpu-sched/experiments/run-experiment.sh \
#     research/cpu-sched/experiments/configs/perfetto-smoke.yaml --dry-run \
#     --perfetto --perfetto-config eevdf-deep
# (the --perfetto flag is what makes the runner print the Perfetto plan block).
# =============================================================================
#
# Coverage:
#   live capture against a node, 30s, eevdf-deep config
#   trace exists, non-empty, file(1) reports binary, not ASCII text / Python
#   script (fixture-based assertion tests cover the rejection paths)
#   filename ends in .perfetto-trace (fixture test)
#   skip (not fail) on unset NODE_IP / unreachable node, time-bounded via
#   timeout(1) so it cannot hang

setup() {
    export PROJECT_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../../../.." && pwd -P)"
    export PERFETTO_BIN="$PROJECT_ROOT/research/cpu-sched/perfetto/bin"
    export PERFETTO_CAPTURE_SH="$PERFETTO_BIN/perfetto-capture.sh"
    export E2E_CONFIG_NAME="eevdf-deep"
    export E2E_DURATION=30
    # Time-bounded SSH options: ConnectTimeout keeps the unreachable-node probe
    # fast; BatchMode prevents password prompts from hanging the test.
    SSH_OPTS=(-o ConnectTimeout=2 -o BatchMode=yes \
        -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null)
    CAPTURE_TIMEOUT=120
}

# ---------------------------------------------------------------------------
# _live_node_or_skip — Skip the current test unless a node is available
#
# Skips when NODE_IP is unset/empty, or when the node cannot be reached over
# SSH within a bounded timeout (skip, never fail, never hang).
# NODE_IP_OVERRIDE is for the deterministic skip-path tests.
# ---------------------------------------------------------------------------
_live_node_or_skip() {
    local node_ip
    if [[ -n "${NODE_IP_OVERRIDE+x}" ]]; then
        node_ip="$NODE_IP_OVERRIDE"
    else
        node_ip="${NODE_IP:-}"
    fi

    if [[ -z "$node_ip" ]]; then
        skip "NODE_IP unset — live node required for E2E capture test"
    fi

    if ! timeout 8 ssh "${SSH_OPTS[@]}" "root@${node_ip}" true >/dev/null 2>&1; then
        skip "node ${node_ip} unreachable over SSH — E2E capture test skipped"
    fi
}

# ---------------------------------------------------------------------------
# _run_capture — Run the full capture lifecycle and print the saved trace path
#
# Runs perfetto-capture.sh with a bounded timeout. On success prints only the
# downloaded trace path on stdout (so callers can `run _run_capture` and read
# the path from $output); on failure returns 1 with the capture output on
# stderr. Uses bats' `run` internally to keep the capture from tripping
# errexit while still surfacing its exit status.
# ---------------------------------------------------------------------------
_run_capture() {
    local out_dir trace_path
    out_dir="$(mktemp -d "${BATS_TEST_TMPDIR:-/tmp}/perfetto-e2e.XXXXXX")"

    run timeout "$CAPTURE_TIMEOUT" bash "$PERFETTO_CAPTURE_SH" \
        "$NODE_IP" "$E2E_CONFIG_NAME" \
        --duration "$E2E_DURATION" --output-dir "$out_dir"
    if [[ "$status" -ne 0 ]]; then
        printf 'perfetto-capture.sh exited %s. Output:\n%s\n' "$status" "$output" >&2
        return 1
    fi

    trace_path="$(printf '%s\n' "$output" | sed -n 's/^Trace saved to: //p' | head -1)"
    if [[ -z "$trace_path" ]]; then
        printf 'capture did not print a "Trace saved to:" path. Output:\n%s\n' "$output" >&2
        return 1
    fi
    printf '%s\n' "$trace_path"
}

# ---------------------------------------------------------------------------
# _assert_binary_trace — Assert a downloaded trace looks like a real trace
#
# Checks, in order: file exists, non-empty, filename ends in .perfetto-trace,
# and file(1) reports binary data — NOT "ASCII text" / "Python script" (that
# would mean the wrapper script was downloaded instead of the trace binary).
# Returns non-zero (with a reason on stderr) on the first failing check.
# ---------------------------------------------------------------------------
_assert_binary_trace() {
    local trace_file="$1"

    [[ -f "$trace_file" ]] || { echo "trace file missing: ${trace_file}" >&2; return 1; }
    [[ -s "$trace_file" ]] || { echo "trace file empty: ${trace_file}" >&2; return 1; }
    [[ "$trace_file" == *.perfetto-trace ]] || {
        echo "trace filename lacks .perfetto-trace suffix: ${trace_file}" >&2
        return 1
    }

    local ftype
    ftype="$(file -b "$trace_file")"
    case "$ftype" in
        *"ASCII text"*|*"Python script"*)
            echo "trace file is text, not binary Perfetto data (wrapper regression?): ${ftype}" >&2
            return 1
            ;;
    esac
    if [[ "$ftype" == *"text"* ]]; then
        echo "trace file reported as text by file(1): ${ftype}" >&2
        return 1
    fi
    return 0
}

# =============================================================================
# Invoke perfetto-capture.sh against a LIVE node
# =============================================================================

@test "capture runs end-to-end against live node" {
    _live_node_or_skip
    run _run_capture
    [ "$status" -eq 0 ]
    [ -n "$output" ]
}

# =============================================================================
# Trace exists, is non-empty, and file(1) reports binary data
# =============================================================================

@test "trace file exists and is non-empty" {
    _live_node_or_skip
    run _run_capture
    [ "$status" -eq 0 ]
    [ -f "$output" ]
    [ -s "$output" ]
}

@test "file(1) reports binary data, not ASCII text / Python script" {
    _live_node_or_skip
    run _run_capture
    [ "$status" -eq 0 ]
    run _assert_binary_trace "$output"
    [ "$status" -eq 0 ]
}

@test "binary assertion rejects a Python-script trace (wrapper regression)" {
    local fake="${BATS_TEST_TMPDIR:-/tmp}/fake-trace.perfetto-trace"
    printf '#!/usr/bin/env python3\n# fake perfetto trace wrapper\n' > "$fake"
    run _assert_binary_trace "$fake"
    [ "$status" -ne 0 ]
}

@test "binary assertion rejects a plain ASCII text trace" {
    local fake="${BATS_TEST_TMPDIR:-/tmp}/fake-trace.perfetto-trace"
    printf 'this is not a perfetto trace, just plain text\n' > "$fake"
    run _assert_binary_trace "$fake"
    [ "$status" -ne 0 ]
}

@test "binary assertion rejects an empty trace file" {
    local fake="${BATS_TEST_TMPDIR:-/tmp}/fake-trace.perfetto-trace"
    : > "$fake"
    run _assert_binary_trace "$fake"
    [ "$status" -ne 0 ]
}

@test "binary assertion rejects a missing trace file" {
    run _assert_binary_trace "${BATS_TEST_TMPDIR:-/tmp}/does-not-exist.perfetto-trace"
    [ "$status" -ne 0 ]
}

@test "binary assertion accepts genuine binary data (positive control)" {
    local real="${BATS_TEST_TMPDIR:-/tmp}/genuine.perfetto-trace"
    printf '\x0a\x00\x00\x00\x08\x00\x12\x03bin' > "$real"
    run _assert_binary_trace "$real"
    [ "$status" -eq 0 ]
}

# =============================================================================
# Trace filename ends in .perfetto-trace
# =============================================================================

@test "trace filename ends in .perfetto-trace" {
    _live_node_or_skip
    run _run_capture
    [ "$status" -eq 0 ]
    [[ "$output" == *.perfetto-trace ]]
}

@test "binary assertion rejects a non .perfetto-trace filename" {
    local wrong="${BATS_TEST_TMPDIR:-/tmp}/trace.bin"
    printf '\x0a\x00\x00\x00\x08\x00' > "$wrong"
    run _assert_binary_trace "$wrong"
    [ "$status" -ne 0 ]
}

# =============================================================================
# Skip (not fail) when NODE_IP unset or node unreachable; no hang
# =============================================================================

@test "unreachable node skips instead of failing" {
    # 192.0.2.1 is TEST-NET-1 (RFC 5737): no route exists, so the SSH probe
    # fails within ConnectTimeout=2 and the test must skip, not fail.
    NODE_IP_OVERRIDE="192.0.2.1" _live_node_or_skip
    false  # reached only if the probe unexpectedly succeeded — must have skipped
}

@test "unset/empty NODE_IP skips instead of failing" {
    NODE_IP_OVERRIDE="" _live_node_or_skip
    false  # reached only if the probe unexpectedly succeeded — must have skipped
}
