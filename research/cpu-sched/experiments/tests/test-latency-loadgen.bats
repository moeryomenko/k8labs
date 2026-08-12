#!/usr/bin/env bats
# test-latency-loadgen.bats — Tests for latency-recording load generation wiring
#
# These tests encode the target behavior of wiring the latency load generator
# into run-experiment.sh when the config declares
# workload.params.latency_load. They are written test-first: the wiring tests
# FAIL (red phase) against the current runner, while the CSV-contract tests and
# backward-compat tests are regression guards that already pass and must stay
# green after the wiring lands.
#
# No running cluster is required. The host has no route to the pod CIDR, so
# real generation runs on the pod's node via SSH (existing pattern); these
# tests instead exercise the two cluster-free contracts:
#   1. the load-generator.sh CSV contract (against a local 200 HTTP server)
#   2. the runner's --dry-run wiring contract (config -> dry-run output)
#
# Covered behaviors:
#   CSV-contract: generator writes timestamp,endpoint,latency_ms,status rows
#   wiring: a config declaring workload.params.latency_load is planned in
#   backward compat: endpoint-based and plain configs unchanged
#   graceful degradation: unreachable target fails the generator, not the cell
#
# Run from project root:
#   bats research/cpu-sched/experiments/tests/test-latency-loadgen.bats
#
# Run a specific test (filter by any substring of the test description):
#   bats --filter "latency_load config dry-run" research/cpu-sched/experiments/tests/test-latency-loadgen.bats

setup() {
    export PROJECT_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../../../.." && pwd -P)"
    export EXPERIMENTS_DIR="$PROJECT_ROOT/research/cpu-sched/experiments"
    export RUN_EXPERIMENT_SH="$EXPERIMENTS_DIR/run-experiment.sh"
    export COMMON_SH="$EXPERIMENTS_DIR/common.sh"
    export LOAD_GENERATOR_SH="$EXPERIMENTS_DIR/load-generator.sh"
    export BASELINE_CONFIG="$EXPERIMENTS_DIR/configs/throttling-baseline.yaml"

    # Fixture configs are written per-test into $BATS_TEST_TMPDIR so the suite
    # never needs a live cluster or extra fixture files.
    export LATENCY_CONFIG="$BATS_TEST_TMPDIR/ll-latency-load.yaml"
    export ENDPOINT_CONFIG="$BATS_TEST_TMPDIR/ll-endpoint.yaml"

    cat > "$LATENCY_CONFIG" <<'EOF'
experiment:
  name: ll-loadgen
  description: "Load generation fixture (latency_load wiring)"
replicates: 1
pre_warm: 1
duration: 3
cooldown: 1
workload:
  type: api-server
  params:
    latency_load:
      rate: 5
      duration: 3
      endpoints: "users:50,search:50"
measurement:
  cgroup_interval: 5
matrix:
  - request: ""
    limit: ""
EOF

    cat > "$ENDPOINT_CONFIG" <<'EOF'
experiment:
  name: ll-endpoint
  description: "Backward-compat endpoint-based load fixture"
replicates: 1
pre_warm: 1
duration: 3
cooldown: 1
workload:
  type: cpu-burner
  params:
    endpoint: /fibonacci?n=38
measurement:
  cgroup_interval: 5
matrix:
  - request: ""
    limit: ""
EOF

    # Local HTTP server state for the CSV-contract tests.
    export LL_SERVER_PID=""
    export LL_SERVER_PORT=""

    # Sanity checks on runner and pre-existing scripts/configs
    [ -f "$RUN_EXPERIMENT_SH" ] || { echo "FATAL: runner not found at $RUN_EXPERIMENT_SH" >&2; exit 1; }
    [ -f "$LOAD_GENERATOR_SH" ] || { echo "FATAL: load-generator.sh not found at $LOAD_GENERATOR_SH" >&2; exit 1; }
    [ -f "$BASELINE_CONFIG" ] || { echo "FATAL: throttling-baseline.yaml not found" >&2; exit 1; }
}

teardown() {
    if [[ -n "$LL_SERVER_PID" ]]; then
        kill "$LL_SERVER_PID" 2>/dev/null || true
        wait "$LL_SERVER_PID" 2>/dev/null || true
    fi
}

# ---------------------------------------------------------------------------
# Helpers (cluster-free)
# ---------------------------------------------------------------------------

# pick_free_port — Ask the kernel for a currently-free TCP port on loopback.
pick_free_port() {
    python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'
}

# start_ok_server — Background a tiny HTTP server that answers 200 to any
# request (quiet logging). Prints the PID.
start_ok_server() {
    local port="$1"
    python3 -c '
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

class OkHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    do_POST = do_GET

    def log_message(self, *args):
        pass

HTTPServer(("127.0.0.1", int(sys.argv[1])), OkHandler).serve_forever()
' "$port" >/dev/null 2>&1 &
    echo $!
}

# run_generator_local — Run load-generator.sh against the local ok server and
# clean the server up even if the generator fails.
run_generator_local() {
    local out="$1"
    LL_SERVER_PORT="$(pick_free_port)"
    LL_SERVER_PID="$(start_ok_server "$LL_SERVER_PORT")"
    sleep 0.5
    run bash "$LOAD_GENERATOR_SH" "http://127.0.0.1:${LL_SERVER_PORT}" \
        --rate 10 --duration 2 --workers 1 --endpoints "users:50,search:50" --output "$out"
}

# =============================================================================
# A latency-recording load loop writes per-request CSV
# (timestamp,endpoint,latency_ms,status) with usable rows.
#
# These are GREEN regression guards today: load-generator.sh already implements
# the CSV contract. The wiring task may extend it or add a helper; either way
# the CSV contract must survive.
# =============================================================================

@test "generator writes CSV header timestamp,endpoint,latency_ms,status" {
    run_generator_local "$BATS_TEST_TMPDIR/ll-01.csv"

    [ "$status" -eq 0 ]
    head -1 "$BATS_TEST_TMPDIR/ll-01.csv" | grep -q '^timestamp,endpoint,latency_ms,status$'
}

@test "generator writes usable data rows (non-empty, 4 fields each)" {
    run_generator_local "$BATS_TEST_TMPDIR/ll-02.csv"

    [ "$status" -eq 0 ]
    local rows=0
    local row
    while IFS= read -r row; do
        [[ "$row" == timestamp* ]] && continue
        [[ -z "$row" ]] && continue
        local nf
        nf="$(printf '%s' "$row" | awk -F',' '{print NF}')"
        if ! [ "$nf" -eq 4 ]; then
            echo "malformed row: $row" >&2
            return 1
        fi
        rows=$((rows + 1))
    done < "$BATS_TEST_TMPDIR/ll-02.csv"
    [ "$rows" -gt 0 ]
}

@test "data rows are usable — known endpoint, integer latency, numeric status" {
    run_generator_local "$BATS_TEST_TMPDIR/ll-03.csv"

    [ "$status" -eq 0 ]
    local data_rows=0
    local row
    while IFS= read -r row; do
        [[ "$row" == timestamp* ]] && continue
        [[ -z "$row" ]] && continue
        local ts ep lat st
        IFS=',' read -r ts ep lat st <<< "$row"
        if [[ -z "$ts" || -z "$ep" || -z "$lat" || -z "$st" ]]; then
            echo "row with empty field: $row" >&2
            return 1
        fi
        if [[ "$ep" != "users" && "$ep" != "search" ]]; then
            echo "unexpected endpoint '$ep' in row: $row" >&2
            return 1
        fi
        if ! [[ "$lat" =~ ^[0-9]+$ ]]; then
            echo "latency not an integer in row: $row" >&2
            return 1
        fi
        if ! [[ "$st" =~ ^[0-9]{3}$ ]]; then
            echo "status not 3-digit numeric in row: $row" >&2
            return 1
        fi
        data_rows=$((data_rows + 1))
    done < "$BATS_TEST_TMPDIR/ll-03.csv"
    [ "$data_rows" -gt 0 ]
}

@test "generator exits 0 and prints p50/p95/p99 summary on low-error run" {
    run_generator_local "$BATS_TEST_TMPDIR/ll-04.csv"

    [ "$status" -eq 0 ]
    [[ "$output" == *"p50:"* ]]
    [[ "$output" == *"p95:"* ]]
    [[ "$output" == *"p99:"* ]]
}

# =============================================================================
# Runner wiring — a config declaring workload.params.
# latency_load must be recognized and planned in --dry-run: latency load
# generation step, latency.csv into the cell dir, configured params echoed.
# RED PHASE: none of this exists in the runner today.
# =============================================================================

@test "latency_load config dry-run exits 0" {
    run bash "$RUN_EXPERIMENT_SH" "$LATENCY_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"DRY RUN MODE"* ]]
}

@test "latency_load config dry-run mentions latency load generation" {
    run bash "$RUN_EXPERIMENT_SH" "$LATENCY_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    printf '%s\n' "$output" | grep -qi 'latency'
}

@test "latency_load config dry-run mentions latency.csv saved to the cell dir" {
    run bash "$RUN_EXPERIMENT_SH" "$LATENCY_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    printf '%s\n' "$output" | grep -Eqi 'latency.*csv|csv.*latency'
}

@test "latency_load config dry-run reflects configured rate and endpoint mix" {
    run bash "$RUN_EXPERIMENT_SH" "$LATENCY_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    # Endpoint mix from the config fixture (users:50,search:50)
    printf '%s\n' "$output" | grep -q 'users:50\|search:50'
    # Rate from the config fixture (5 req/s)
    printf '%s\n' "$output" | grep -qi 'rate'
}

# =============================================================================
# Backward compatibility — existing workload.params.endpoint
# behavior (start_load_generation) unchanged; configs without latency_load do
# not mention latency generation in dry-run. These are regression guards that
# already pass and must stay green after the wiring lands.
# =============================================================================

@test "endpoint-based config dry-runs with unchanged shape, no latency mentions" {
    run bash "$RUN_EXPERIMENT_SH" "$ENDPOINT_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"DRY RUN MODE"* ]]
    ! printf '%s\n' "$output" | grep -qi 'latency'
}

@test "baseline config (no latency params) dry-runs with unchanged shape, no latency mentions" {
    run bash "$RUN_EXPERIMENT_SH" "$BASELINE_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"throttling-baseline"* ]]
    [[ "$output" == *"Matrix cells:"* ]]
    [[ "$output" == *"Prerequisites check passed"* ]]
    ! printf '%s\n' "$output" | grep -qi 'latency'
}

@test "start_load_generation still exists in common.sh" {
    run bash -c "
        source '$COMMON_SH'
        type start_load_generation 2>&1
    "

    [ "$status" -eq 0 ]
    [[ "$output" == *"function"* ]]
}

# =============================================================================
# Graceful degradation — if the latency generator fails
# (target unreachable), the cell continues, a warning is logged, latency.csv
# may be missing, but the run does not hard-fail.
# =============================================================================

@test "generator exits non-zero when target is unreachable" {
    # Pick a free port with nothing listening — curl fails instantly with
    # connection refused, so every request is an error (>50% error rate).
    LL_SERVER_PORT="$(pick_free_port)"

    run bash "$LOAD_GENERATOR_SH" "http://127.0.0.1:${LL_SERVER_PORT}" \
        --rate 5 --duration 1 --workers 1

    [ "$status" -ne 0 ]
}

@test "latency_load dry-run marks generation as non-fatal/degradable" {
    run bash "$RUN_EXPERIMENT_SH" "$LATENCY_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    printf '%s\n' "$output" | grep -qi 'latency'
    # A failing generator must not abort the cell; the dry-run plan
    # must signal degradation. Any of several wordings is accepted (this is
    # the most wording-sensitive assertion).
    printf '%s\n' "$output" | grep -Eqi 'warn|continu|non-fatal|degrad|skip|tolerat'
}

@test "common.sh sources cleanly (regression guard)" {
    run bash -c "
        source '$COMMON_SH'
        echo OK
    "

    [ "$status" -eq 0 ]
    [ "$output" = "OK" ]
}
