#!/usr/bin/env bats
# test-target-resolution.bats — Tests for resolve_latency_load_target
# (db-simulator treated as HTTP-capable, priority order preserved)
#
# The runner's
# resolve_latency_load_target must treat db-simulator as an HTTP-capable target
# so Family A db cells get load generation, with the existing priority order
# api-server -> cpu-burner -> latency-sensitive preserved and db-simulator as
# the final (fallback) HTTP-capable tier.
#
# IMPORTANT — status against the CURRENT tree: these tests are REGRESSION
# GUARDS, not red-phase. Empirically verified (2026-08-05): common.sh already
# resolves db-simulator via `_is_http_capable_type` (lists db-simulator) in the
# HTTP-capable fallback loop (commit bab8e134, "six-family experiment suite").
# The dry-run integration fixture prints "Target pod: dbsim". The task brief
# expected db-simulator resolution to be RED; the code is ahead of that
# assumption. These tests pin the resolution contract so it cannot regress.
#
# Function under test lives in common.sh (sourced by run-experiment.sh);
# following the existing pattern from test-latency-loadgen.bats,
# tests source common.sh directly and call the function with
# "<pod-name>:<type>" pairs in workloads: mapping order.
#
# Run from project root:
#   bats research/experiments/tests/test-target-resolution.bats

setup() {
    export PROJECT_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../../.." && pwd -P)"
    export EXPERIMENTS_DIR="$PROJECT_ROOT/research/experiments"
    export COMMON_SH="$EXPERIMENTS_DIR/common.sh"
    export RUN_EXPERIMENT_SH="$EXPERIMENTS_DIR/run-experiment.sh"

    # Sanity checks on the scripts under test
    [ -f "$COMMON_SH" ] || { echo "FATAL: common.sh not found at $COMMON_SH" >&2; exit 1; }
    [ -f "$RUN_EXPERIMENT_SH" ] || { echo "FATAL: run-experiment.sh not found at $RUN_EXPERIMENT_SH" >&2; exit 1; }
}

# resolve_target — Source common.sh and call resolve_latency_load_target with
# the given pod:type pairs. Fails loudly if common.sh cannot source.
# Arguments are quoted individually so multi-arg calls survive the bash -c
# boundary (a naive '$@' inside the single-quoted string would expand empty).
resolve_target() {
    local script="source '$COMMON_SH'; resolve_latency_load_target"
    local arg
    for arg in "$@"; do
        script+=" '$arg'"
    done
    run bash -c "$script"
}

# =============================================================================
# db-simulator is an HTTP-capable latency target (REG today)
# =============================================================================

@test "db-simulator-only mapping resolves to the db pod" {
    resolve_target "db:db-simulator"

    [ "$status" -eq 0 ]
    [ "$output" = "db" ]
}

@test "api-server wins over db-simulator regardless of mapping order" {
    # db-simulator listed FIRST in the mapping; api-server must still win.
    resolve_target "db:db-simulator" "api:api-server"

    [ "$status" -eq 0 ]
    [ "$output" = "api" ]
}

@test "cpu-burner wins over db-simulator (priority preserved)" {
    resolve_target "db:db-simulator" "burn:cpu-burner"

    [ "$status" -eq 0 ]
    [ "$output" = "burn" ]
}

@test "latency-sensitive wins over db-simulator (priority preserved)" {
    resolve_target "db:db-simulator" "ls:latency-sensitive"

    [ "$status" -eq 0 ]
    [ "$output" = "ls" ]
}

@test "db-simulator wins over non-HTTP workloads (stress-ng)" {
    resolve_target "stress:stress-ng" "db:db-simulator"

    [ "$status" -eq 0 ]
    [ "$output" = "db" ]
}

@test "no HTTP-capable pod resolves with a non-zero exit" {
    resolve_target "stress:stress-ng" "batch:batch"

    [ "$status" -ne 0 ]
}

@test "api-server precedence is order-independent across the full set" {
    # Mixed co-located set with every HTTP-capable type; api-server must win.
    resolve_target "db:db-simulator" "ls:latency-sensitive" "api:api-server" "burn:cpu-burner"

    [ "$status" -eq 0 ]
    [ "$output" = "api" ]
}

# =============================================================================
# Integration: a co-located config with a db-simulator pod and a
# top-level latency_load block must dry-run with the db pod as the target
# (REG today)
# =============================================================================

@test "db-simulator co-located config dry-run targets the db pod" {
    local fixture="$BATS_TEST_TMPDIR/tr-db-colocated.yaml"
    cat > "$fixture" <<'EOF'
experiment:
  name: tr-db-colocated
  description: "fixture: db-simulator pod + top-level latency_load"
replicates: 1
pre_warm: 1
duration: 3
cooldown: 1
workloads:
  dbsim:
    type: db-simulator
  aux:
    type: stress-ng
    params:
      cores: 1
      load: 100
measurement:
  cgroup_interval: 5
latency_load:
  rate: 5
  duration: 3
  endpoints: "select:40,insert:25,update:25,checkpoint:10"
matrix:
  - dbsim_request: "500m"; dbsim_limit: "2000m"
EOF

    run bash "$RUN_EXPERIMENT_SH" "$fixture" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Target pod: dbsim"* ]]
    [[ "$output" == *"Latency load generation"* ]]
}
