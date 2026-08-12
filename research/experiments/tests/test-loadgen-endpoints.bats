#!/usr/bin/env bats
# test-loadgen-endpoints.bats — Tests for endpoint mappings in
# load-generator.sh (cpu-burner + db-simulator endpoints, api-server regression)
#
# These tests encode the target behavior of endpoint mappings:
# load-generator.sh gains
# endpoint mappings for cpu-burner (burn/fib/pi) and db-simulator
# (select/insert/update/checkpoint) while the existing api-server mappings
# (users/orders/search/reports) stay unchanged and unknown names keep erroring.
#
# Red/reg split against the CURRENT tree:
#   burn/fib/pi + select/insert/update/checkpoint  FAIL (red) — new endpoint
#                               names are unknown to the case block today; the
#                               generator exits 1 during endpoint parsing, so
#                               no request reaches the recorder and the
#                               path/method assertions fail.
#   unknown-name + api-server  PASS (reg)  — unknown-name error + api-server
#                               mappings are existing behavior and must
#                               survive the change.
#   one-shot burn              FAIL (red)  — the rate loop fires one /load
#                               call per request (burn:100 at rate 50 over 2s
#                               stacks ~100 burners), but the pinned behavior
#                               is a SINGLE /load call at start, one CSV row,
#                               summary total requests 1.
#   api-server/db mixes        PASS (reg)  — api-server and db-simulator mixes
#                               keep looping at the configured rate; unknown
#                               names keep erroring via the unknown-name test.
#
# No running cluster is required: each mapping test runs load-generator.sh
# against a tiny local Python HTTP server that RECORDS "METHOD PATH" (and
# BODY= for POSTs) into a per-test log; assertions grep the log. This follows
# the existing local-server pattern from test-latency-loadgen.bats, extended
# with request recording so path/method/body can be verified.
#
# Run from project root:
#   bats research/cpu-sched/experiments/tests/test-loadgen-endpoints.bats

setup() {
    export PROJECT_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../../../.." && pwd -P)"
    export EXPERIMENTS_DIR="$PROJECT_ROOT/research/cpu-sched/experiments"
    export LOAD_GENERATOR_SH="$EXPERIMENTS_DIR/load-generator.sh"
    export RECORDER_PID=""

    # Sanity checks on the scripts under test
    [ -f "$LOAD_GENERATOR_SH" ] || { echo "FATAL: load-generator.sh not found at $LOAD_GENERATOR_SH" >&2; exit 1; }
}

teardown() {
    if [[ -n "$RECORDER_PID" ]]; then
        kill "$RECORDER_PID" 2>/dev/null || true
        wait "$RECORDER_PID" 2>/dev/null || true
    fi
}

# ---------------------------------------------------------------------------
# Helpers (cluster-free)
# ---------------------------------------------------------------------------

# pick_free_port — Ask the kernel for a currently-free TCP port on loopback.
pick_free_port() {
    python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'
}

# start_recorder — Background a Python HTTP server that answers 200 and
# appends "METHOD PATH" (or "METHOD PATH BODY=<body>" for POSTs) to a log
# file. Prints the PID.
start_recorder() {
    local log="$1"
    local port="$2"
    python3 -c '
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

log_path, port = sys.argv[1], int(sys.argv[2])

class Recorder(BaseHTTPRequestHandler):
    def _record(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        with open(log_path, "a") as f:
            if body:
                f.write("%s %s BODY=%s\n" % (self.command, self.path, body))
            else:
                f.write("%s %s\n" % (self.command, self.path))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    do_GET = _record
    do_POST = _record

    def log_message(self, *args):
        pass

HTTPServer(("127.0.0.1", port), Recorder).serve_forever()
' "$log" "$port" >/dev/null 2>&1 &
    echo $!
}

# run_generator_endpoint — Run load-generator.sh against the local recorder
# with a SINGLE-endpoint mix (so every request exercises that mapping) and a
# short duration. Cleans the server up even if the generator fails.
run_generator_endpoint() {
    local endpoints="$1"
    local log="$2"
    local port
    port="$(pick_free_port)"
    RECORDER_PID="$(start_recorder "$log" "$port")"
    sleep 0.5
    run bash "$LOAD_GENERATOR_SH" "http://127.0.0.1:${port}" \
        --rate 5 --duration 1 --workers 1 --endpoints "$endpoints"
}

# run_generator_mix — Run load-generator.sh against the local recorder with
# full control over rate/duration/workers/endpoints and an optional CSV
# output path. The recorder log receives one "METHOD PATH" line per HTTP
# request, so line counts pin request counts. Cleans the server up even if
# the generator fails.
run_generator_mix() {
    local endpoints="$1" log="$2" rate="$3" duration="$4" workers="$5" out="${6:-}"
    local port
    port="$(pick_free_port)"
    RECORDER_PID="$(start_recorder "$log" "$port")"
    sleep 0.5
    local args=(bash "$LOAD_GENERATOR_SH" "http://127.0.0.1:${port}" \
        --rate "$rate" --duration "$duration" --workers "$workers" --endpoints "$endpoints")
    if [[ -n "$out" ]]; then
        args+=(--output "$out")
    fi
    run "${args[@]}"
}

# count_records — count recorder log lines matching an ERE pattern. Prints 0
# when nothing matches (grep exits 1 on no match, which is swallowed).
count_records() {
    local log="$1" pattern="$2"
    grep -cE "$pattern" "$log" || true
}

# =============================================================================
# cpu-burner mappings (RED today — names are unknown to the case block)
# =============================================================================

@test "burn maps to GET /load with percent and duration=<experiment duration>" {
    run_generator_endpoint "burn:100" "$BATS_TEST_TMPDIR/lg01.log"

    [ "$status" -eq 0 ]
    # --duration 1 passed above; the mapping must embed that exact value
    # (duration override equal to the experiment duration), not the default 60.
    grep -qE '^GET /load\?percent=[0-9]+&duration=1$' "$BATS_TEST_TMPDIR/lg01.log"
}

@test "burn duration override equals experiment duration, not the default 60" {
    # Run with a non-default duration to prove the override is wired to
    # --duration rather than hardcoded to the 60s default.
    local port log
    port="$(pick_free_port)"
    log="$BATS_TEST_TMPDIR/lg02.log"
    RECORDER_PID="$(start_recorder "$log" "$port")"
    sleep 0.5
    run bash "$LOAD_GENERATOR_SH" "http://127.0.0.1:${port}" \
        --rate 5 --duration 3 --workers 1 --endpoints "burn:100"

    [ "$status" -eq 0 ]
    grep -qE '^GET /load\?percent=[0-9]+&duration=3$' "$log"
    if grep -q 'duration=60' "$log"; then
        echo "burn path must use the passed --duration (3), not the default 60" >&2
        return 1
    fi
}

@test "fib maps to GET /fibonacci?n=38" {
    run_generator_endpoint "fib:100" "$BATS_TEST_TMPDIR/lg03.log"

    [ "$status" -eq 0 ]
    grep -qx 'GET /fibonacci?n=38' "$BATS_TEST_TMPDIR/lg03.log"
}

@test "pi maps to GET /pi?digits=2000" {
    run_generator_endpoint "pi:100" "$BATS_TEST_TMPDIR/lg04.log"

    [ "$status" -eq 0 ]
    grep -qx 'GET /pi?digits=2000' "$BATS_TEST_TMPDIR/lg04.log"
}

# =============================================================================
# db-simulator mappings (RED today — names are unknown to the case
# block)
# =============================================================================

@test "select maps to GET /query/select?rows=1000&complexity=medium" {
    run_generator_endpoint "select:100" "$BATS_TEST_TMPDIR/lg05.log"

    [ "$status" -eq 0 ]
    grep -qx 'GET /query/select?rows=1000&complexity=medium' "$BATS_TEST_TMPDIR/lg05.log"
}

@test "insert maps to POST /query/insert?rows=100 (method POST)" {
    run_generator_endpoint "insert:100" "$BATS_TEST_TMPDIR/lg06.log"

    [ "$status" -eq 0 ]
    # Method matters: insert is a write endpoint and must be POST, not GET.
    grep -qx 'POST /query/insert?rows=100' "$BATS_TEST_TMPDIR/lg06.log"
}

@test "update maps to POST /query/update?rows=100&cols=1 (method POST)" {
    run_generator_endpoint "update:100" "$BATS_TEST_TMPDIR/lg07.log"

    [ "$status" -eq 0 ]
    grep -qx 'POST /query/update?rows=100&cols=1' "$BATS_TEST_TMPDIR/lg07.log"
}

@test "checkpoint maps to GET /query/checkpoint" {
    run_generator_endpoint "checkpoint:100" "$BATS_TEST_TMPDIR/lg08.log"

    [ "$status" -eq 0 ]
    grep -qx 'GET /query/checkpoint' "$BATS_TEST_TMPDIR/lg08.log"
}

# =============================================================================
# unknown-name error preserved (REG today — must stay green)
# =============================================================================

@test "unknown endpoint name still errors with a non-zero exit" {
    run bash "$LOAD_GENERATOR_SH" "http://127.0.0.1:1" --endpoints "bogus:100"

    [ "$status" -ne 0 ]
    [[ "$output" == *"unknown endpoint"* ]]
    [[ "$output" == *"bogus"* ]]
}

# =============================================================================
# api-server mappings unchanged (REG today — regression guards)
# =============================================================================

@test "users still maps to GET /api/v1/users (regression)" {
    run_generator_endpoint "users:100" "$BATS_TEST_TMPDIR/lg10.log"

    [ "$status" -eq 0 ]
    grep -qx 'GET /api/v1/users' "$BATS_TEST_TMPDIR/lg10.log"
}

@test "orders still maps to GET /api/v1/orders (regression)" {
    run_generator_endpoint "orders:100" "$BATS_TEST_TMPDIR/lg11.log"

    [ "$status" -eq 0 ]
    grep -qx 'GET /api/v1/orders' "$BATS_TEST_TMPDIR/lg11.log"
}

@test "search still maps to GET /api/v1/search?q=kubernetes (regression)" {
    run_generator_endpoint "search:100" "$BATS_TEST_TMPDIR/lg12.log"

    [ "$status" -eq 0 ]
    grep -qx 'GET /api/v1/search?q=kubernetes' "$BATS_TEST_TMPDIR/lg12.log"
}

@test "reports still maps to POST /api/v1/reports with the pinned JSON body (regression)" {
    run_generator_endpoint "reports:100" "$BATS_TEST_TMPDIR/lg13.log"

    [ "$status" -eq 0 ]
    grep -qF 'POST /api/v1/reports BODY={"period":"daily","dimension":"revenue"}' "$BATS_TEST_TMPDIR/lg13.log"
}

# =============================================================================
# One-shot burn (RED today — the rate loop stacks independent /load
# burners: with burn:100 at rate N over duration D the loop fires ~N*D /load
# calls, saturating ~1 CPU instead of a steady ~30% light burn)
# =============================================================================
#
# Pinned behavior: when the endpoint mix contains the cpu-burner `burn`
# entry, the generator issues a SINGLE /load?percent=30&duration=<--duration>
# request at the start (one-shot, outside the rate loop), then exits cleanly
# with the summary. The rate loop keeps driving the NON-burn entries.

@test "burn-only mix performs exactly ONE /load request at rate 1" {
    local log="$BATS_TEST_TMPDIR/lg20.log"
    run_generator_mix "burn:100" "$log" 1 2 1

    [ "$status" -eq 0 ]
    # Exactly one HTTP request total, and it is the one-shot /load call with
    # percent=30 and duration equal to --duration (2).
    [ "$(wc -l < "$log")" -eq 1 ]
    grep -Fqx 'GET /load?percent=30&duration=2' "$log"
}

@test "burn-only mix performs exactly ONE /load request at rate 50" {
    local log="$BATS_TEST_TMPDIR/lg21.log"
    run_generator_mix "burn:100" "$log" 50 2 1

    [ "$status" -eq 0 ]
    # The headline regression: a high --rate must NOT stack independent
    # burners. Today the loop fires ~100 /load calls in 2s; after the
    # one-shot change exactly one /load call reaches the server regardless
    # of --rate.
    [ "$(wc -l < "$log")" -eq 1 ]
    grep -Fqx 'GET /load?percent=30&duration=2' "$log"
}

@test "burn-only one-shot writes a latency CSV with exactly one data row" {
    local log="$BATS_TEST_TMPDIR/lg22.log"
    local csv="$BATS_TEST_TMPDIR/lg22.csv"
    run_generator_mix "burn:100" "$log" 50 2 1 "$csv"

    [ "$status" -eq 0 ]
    # Header + exactly one data row for the one-shot burn (today: ~100 rows).
    [ "$(wc -l < "$csv")" -eq 2 ]
    head -1 "$csv" | grep -Fqx 'timestamp,endpoint,latency_ms,status'
    local row ts ep lat st
    row="$(tail -n +2 "$csv" | head -1)"
    IFS=',' read -r ts ep lat st <<< "$row"
    [ "$ep" = "burn" ]
    [[ "$lat" =~ ^[0-9]+$ ]]
    [ "$st" = "200" ]
}

@test "burn-only one-shot prints the summary with total requests 1 and exits 0" {
    local log="$BATS_TEST_TMPDIR/lg23.log"
    run_generator_mix "burn:100" "$log" 50 2 1

    [ "$status" -eq 0 ]
    [[ "$output" == *"=== Load Test Summary ==="* ]]
    [[ "$output" == *"p50:"* ]]
    [[ "$output" == *"p95:"* ]]
    [[ "$output" == *"p99:"* ]]
    # Whole-line match is deliberate: "total requests: 100" (today's stacking)
    # must NOT satisfy this assertion.
    printf '%s\n' "$output" | grep -Fqx '  total requests: 1'
}

@test "burn mixed with fib — exactly one /load call, fib rate loop unchanged" {
    local log="$BATS_TEST_TMPDIR/lg24.log"
    run_generator_mix "burn:50,fib:50" "$log" 10 2 1

    [ "$status" -eq 0 ]
    local load_count fib_count
    load_count="$(count_records "$log" '^GET /load')"
    fib_count="$(count_records "$log" '^GET /fibonacci')"
    echo "load calls: $load_count, fib calls: $fib_count" >&2
    # The burn entry is one-shot: exactly one /load call total (today the
    # loop fires ~10 of them). 0 would mean the burn was dropped — also wrong.
    [ "$load_count" -eq 1 ]
    # The non-burn entry keeps its rate loop (>= 1 request in the window).
    [ "$fib_count" -ge 1 ]
}

@test "burn mixed with a db endpoint — exactly one /load call, select loop unchanged" {
    local log="$BATS_TEST_TMPDIR/lg25.log"
    run_generator_mix "burn:50,select:50" "$log" 10 2 1

    [ "$status" -eq 0 ]
    local load_count select_count
    load_count="$(count_records "$log" '^GET /load')"
    select_count="$(count_records "$log" '^GET /query/select')"
    echo "load calls: $load_count, select calls: $select_count" >&2
    [ "$load_count" -eq 1 ]
    [ "$select_count" -ge 1 ]
}

# =============================================================================
# Rate-loop regression guards (GREEN today — the one-shot change must
# not leak into mixes without burn: api-server and db-simulator keep looping
# at the configured rate; unknown names keep erroring)
# =============================================================================

@test "api-server mix keeps looping at the configured rate (regression)" {
    local log="$BATS_TEST_TMPDIR/lg26.log"
    run_generator_mix "users:30,orders:30,search:20,reports:20" "$log" 10 2 1

    [ "$status" -eq 0 ]
    # The default api-server mix must NOT be routed through the one-shot path:
    # at rate 10 over 2s the loop fires ~20 requests. A count clearly above
    # the one-shot baseline (>= 5) proves the loop is intact.
    local total
    total="$(wc -l < "$log")"
    echo "api-server mix fired $total requests" >&2
    [ "$total" -ge 5 ]
}

@test "db-simulator mix keeps looping at the configured rate (regression)" {
    local log="$BATS_TEST_TMPDIR/lg27.log"
    run_generator_mix "select:40,insert:25,update:25,checkpoint:10" "$log" 10 2 1

    [ "$status" -eq 0 ]
    local total
    total="$(wc -l < "$log")"
    echo "db-simulator mix fired $total requests" >&2
    [ "$total" -ge 5 ]
}
