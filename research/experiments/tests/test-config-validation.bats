#!/usr/bin/env bats
# test-config-validation.bats — Tests for the six new experiment configs
#
# These tests encode the target behavior of TASK-011/012/013 (creating six
# experiment configs for the requests-vs-limits scheduler-interaction study).
# They are written test-first: the config files DO NOT exist yet, so every
# test that asserts their presence or dry-run behavior FAILS (red phase)
# against the current tree. The only tests expected to pass today are the
# REQ-6 current-behavior guard (CV-06b) and the REQ-7 backward-compatibility
# regression guards (CV-07a, CV-07b).
#
# No running cluster is required — every assertion targets --dry-run
# stdout/stderr and exit codes.
#
# The six configs (created by future tasks — these tests never create them):
#   weight-share.yaml        Family A: N-pod weight sharing (2-3 stress-ng pods)
#   request-limit-matrix.yaml Family B: single-pod request x limit matrix
#   qos-hierarchy.yaml       Family C: guaranteed/burstable/besteffort pods
#   latency-interference.yaml Family D: api-server LS + stress-ng batch + latency_load
#   cpu-burst.yaml           Family E: db-simulator low-limit + cpu.burst cells
#   tunables-contention.yaml Family F: api-server + stress-ng tunable sweep
#
# Requirements covered (full mapping in TEST-DESIGN.md):
#   REQ-1 -> VC-CV-01 (CV-01a..CV-01f)
#   REQ-2 -> VC-CV-02 (CV-02a..CV-02f)
#   REQ-3 -> VC-CV-03 (CV-03a, CV-03b, plus plan-derived counts CV-03c..e)
#   REQ-4 -> VC-CV-04 (CV-04a..CV-04d)
#   REQ-5 -> VC-CV-05 (CV-05)
#   REQ-6 -> VC-CV-06 (CV-06a target, CV-06b current-behavior guard)
#   REQ-7 -> VC-CV-07 (CV-07a, CV-07b)
#
# Run from project root:
#   bats research/experiments/tests/test-config-validation.bats
#
# Run a specific test:
#   bats --filter "CV-03a" research/experiments/tests/test-config-validation.bats

setup() {
    export PROJECT_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../../.." && pwd -P)"
    export EXPERIMENTS_DIR="$PROJECT_ROOT/research/experiments"
    export RUN_EXPERIMENT_SH="$EXPERIMENTS_DIR/run-experiment.sh"
    export CONFIGS_DIR="$EXPERIMENTS_DIR/configs"

    # The six new configs — created by TASK-011/012/013. Presence is what the
    # REQ-1 tests assert; they are RED until those tasks land.
    export WEIGHT_SHARE_CONFIG="$CONFIGS_DIR/weight-share.yaml"
    export REQUEST_LIMIT_MATRIX_CONFIG="$CONFIGS_DIR/request-limit-matrix.yaml"
    export QOS_HIERARCHY_CONFIG="$CONFIGS_DIR/qos-hierarchy.yaml"
    export LATENCY_INTERFERENCE_CONFIG="$CONFIGS_DIR/latency-interference.yaml"
    export CPU_BURST_CONFIG="$CONFIGS_DIR/cpu-burst.yaml"
    export TUNABLES_CONTENTION_CONFIG="$CONFIGS_DIR/tunables-contention.yaml"

    # Existing configs used by the REQ-7 backward-compatibility regression guards
    export BASELINE_CONFIG="$CONFIGS_DIR/throttling-baseline.yaml"
    export CO_LOCATED_CONFIG="$CONFIGS_DIR/co-located.yaml"

    # REQ-6 fixture: a temp config whose request exceeds its limit. Written by
    # the test (never committed); the runner must reject it once validation
    # exists. Fixtures live in $BATS_TEST_TMPDIR so the suite stays cluster-free.
    export REQ6_INVALID_CONFIG="$BATS_TEST_TMPDIR/cv-req6-invalid.yaml"
    cat > "$REQ6_INVALID_CONFIG" <<'EOF'
experiment:
  name: cv-req6-invalid
  description: "request exceeds limit — invalid CPU combo"
replicates: 1
pre_warm: 5
duration: 30
cooldown: 5
workload:
  type: stress-ng
  params:
    cores: 2
    load: 100
measurement:
  cgroup_interval: 5
matrix:
  - request: "1000m"; limit: "500m"
EOF

    # Sanity checks on runner and pre-existing configs
    [ -f "$RUN_EXPERIMENT_SH" ] || { echo "FATAL: runner not found at $RUN_EXPERIMENT_SH" >&2; exit 1; }
    [ -f "$BASELINE_CONFIG" ] || { echo "FATAL: throttling-baseline.yaml not found" >&2; exit 1; }
    [ -f "$CO_LOCATED_CONFIG" ] || { echo "FATAL: co-located.yaml not found" >&2; exit 1; }
}

# =============================================================================
# VC-CV-01 (REQ-1): All six config files exist.
# RED until TASK-011/012/013 create them.
# =============================================================================

@test "CV-01a: config weight-share.yaml exists (REQ-1)" {
    [ -f "$WEIGHT_SHARE_CONFIG" ]
}

@test "CV-01b: config request-limit-matrix.yaml exists (REQ-1)" {
    [ -f "$REQUEST_LIMIT_MATRIX_CONFIG" ]
}

@test "CV-01c: config qos-hierarchy.yaml exists (REQ-1)" {
    [ -f "$QOS_HIERARCHY_CONFIG" ]
}

@test "CV-01d: config latency-interference.yaml exists (REQ-1)" {
    [ -f "$LATENCY_INTERFERENCE_CONFIG" ]
}

@test "CV-01e: config cpu-burst.yaml exists (REQ-1)" {
    [ -f "$CPU_BURST_CONFIG" ]
}

@test "CV-01f: config tunables-contention.yaml exists (REQ-1)" {
    [ -f "$TUNABLES_CONTENTION_CONFIG" ]
}

# =============================================================================
# VC-CV-02 (REQ-2): Each config dry-runs successfully (exit 0) and prints the
# experiment name and the matrix cell count.
# RED until the configs exist (runner dies with "Config file not found").
# =============================================================================

@test "CV-02a: weight-share.yaml dry-runs, prints name and cell count (REQ-2)" {
    run bash "$RUN_EXPERIMENT_SH" "$WEIGHT_SHARE_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Experiment: weight-share"* ]]
    [[ "$output" == *"Matrix cells:"* ]]
    [[ "$output" == *"DRY RUN MODE"* ]]
}

@test "CV-02b: request-limit-matrix.yaml dry-runs, prints name and cell count (REQ-2)" {
    run bash "$RUN_EXPERIMENT_SH" "$REQUEST_LIMIT_MATRIX_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Experiment: request-limit-matrix"* ]]
    [[ "$output" == *"Matrix cells:"* ]]
    [[ "$output" == *"DRY RUN MODE"* ]]
}

@test "CV-02c: qos-hierarchy.yaml dry-runs, prints name and cell count (REQ-2)" {
    run bash "$RUN_EXPERIMENT_SH" "$QOS_HIERARCHY_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Experiment: qos-hierarchy"* ]]
    [[ "$output" == *"Matrix cells:"* ]]
    [[ "$output" == *"DRY RUN MODE"* ]]
}

@test "CV-02d: latency-interference.yaml dry-runs, prints name and cell count (REQ-2)" {
    run bash "$RUN_EXPERIMENT_SH" "$LATENCY_INTERFERENCE_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Experiment: latency-interference"* ]]
    [[ "$output" == *"Matrix cells:"* ]]
    [[ "$output" == *"DRY RUN MODE"* ]]
}

@test "CV-02e: cpu-burst.yaml dry-runs, prints name and cell count (REQ-2)" {
    run bash "$RUN_EXPERIMENT_SH" "$CPU_BURST_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Experiment: cpu-burst"* ]]
    [[ "$output" == *"Matrix cells:"* ]]
    [[ "$output" == *"DRY RUN MODE"* ]]
}

@test "CV-02f: tunables-contention.yaml dry-runs, prints name and cell count (REQ-2)" {
    run bash "$RUN_EXPERIMENT_SH" "$TUNABLES_CONTENTION_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Experiment: tunables-contention"* ]]
    [[ "$output" == *"Matrix cells:"* ]]
    [[ "$output" == *"DRY RUN MODE"* ]]
}

# =============================================================================
# VC-CV-03 (REQ-3): Exact matrix cell counts. REQ-3 mandates weight-share (6)
# and request-limit-matrix (15); the other counts are plan-derived and pinned
# here so TASK-011/012/013 cannot drift from the plan.
# =============================================================================

@test "CV-03a: weight-share.yaml dry-run prints 6 matrix cells (REQ-3)" {
    run bash "$RUN_EXPERIMENT_SH" "$WEIGHT_SHARE_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Matrix cells: 6"* ]]
}

@test "CV-03b: request-limit-matrix.yaml dry-run prints 15 matrix cells (REQ-3)" {
    run bash "$RUN_EXPERIMENT_SH" "$REQUEST_LIMIT_MATRIX_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Matrix cells: 15"* ]]
}

@test "CV-03c: qos-hierarchy.yaml dry-run prints 2 matrix cells (plan-derived)" {
    run bash "$RUN_EXPERIMENT_SH" "$QOS_HIERARCHY_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Matrix cells: 2"* ]]
}

@test "CV-03d: latency-interference.yaml dry-run prints 4 matrix cells (plan-derived)" {
    run bash "$RUN_EXPERIMENT_SH" "$LATENCY_INTERFERENCE_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Matrix cells: 4"* ]]
}

@test "CV-03e: tunables-contention.yaml dry-run prints 3 matrix cells (plan-derived)" {
    run bash "$RUN_EXPERIMENT_SH" "$TUNABLES_CONTENTION_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Matrix cells: 3"* ]]
}

# =============================================================================
# VC-CV-04 (REQ-4): Multi-pod configs print per-pod deployment lines in
# --dry-run. The runner shows the deployments of the FIRST matrix cell; the
# denominator N comes from the size of the workloads: mapping, so a config
# declaring 3 pods prints "Deployment i/3" lines for every cell.
# =============================================================================

@test "CV-04a: weight-share.yaml dry-run shows 3 pod deployments, pod-a/b/c named (REQ-4)" {
    run bash "$RUN_EXPERIMENT_SH" "$WEIGHT_SHARE_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    # All three deployment slots must appear (workloads: mapping = pod-a/pod-b/pod-c)
    printf '%s\n' "$output" | grep -qE 'Deployment 1/3:'
    printf '%s\n' "$output" | grep -qE 'Deployment 2/3:'
    printf '%s\n' "$output" | grep -qE 'Deployment 3/3:'
    # Each pod is named in its own deployment line
    printf '%s\n' "$output" | grep -E 'Deployment [0-9]+/3:' | grep -q 'pod-a'
    printf '%s\n' "$output" | grep -E 'Deployment [0-9]+/3:' | grep -q 'pod-b'
    printf '%s\n' "$output" | grep -E 'Deployment [0-9]+/3:' | grep -q 'pod-c'
}

@test "CV-04b: qos-hierarchy.yaml dry-run shows 3 pod deployments (REQ-4)" {
    run bash "$RUN_EXPERIMENT_SH" "$QOS_HIERARCHY_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    local deploys
    deploys="$(printf '%s\n' "$output" | grep -cE 'Deployment [0-9]+/3:' 2>/dev/null || true)"
    [ "$deploys" -eq 3 ]
}

@test "CV-04c: latency-interference.yaml dry-run shows 2 pod deployments (REQ-4)" {
    run bash "$RUN_EXPERIMENT_SH" "$LATENCY_INTERFERENCE_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    local deploys
    deploys="$(printf '%s\n' "$output" | grep -cE 'Deployment [0-9]+/2:' 2>/dev/null || true)"
    [ "$deploys" -eq 2 ]
}

@test "CV-04d: tunables-contention.yaml dry-run shows 2 pod deployments (REQ-4)" {
    run bash "$RUN_EXPERIMENT_SH" "$TUNABLES_CONTENTION_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    local deploys
    deploys="$(printf '%s\n' "$output" | grep -cE 'Deployment [0-9]+/2:' 2>/dev/null || true)"
    [ "$deploys" -eq 2 ]
}

# =============================================================================
# VC-CV-05 (REQ-5): The latency-interference config declares latency_load, so
# the dry-run plan must mention the latency generation step. The runner prints
# "Latency load generation (top-level latency_load):" only when the config has
# a top-level latency_load block (see TEST-DESIGN.md contract T-LATENCY).
# =============================================================================

@test "CV-05: latency-interference.yaml dry-run mentions latency load generation (REQ-5)" {
    run bash "$RUN_EXPERIMENT_SH" "$LATENCY_INTERFERENCE_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    printf '%s\n' "$output" | grep -qi 'Latency load generation'
    printf '%s\n' "$output" | grep -q 'latency.csv'
}

# =============================================================================
# VC-CV-06 (REQ-6): A config with request > limit must be rejected with a clear
# error. The runner today has NO request<=limit validation (verified: the
# fixture dry-runs with exit 0), so CV-06a is the RED acceptance test for the
# target behavior and CV-06b pins the CURRENT behavior to keep the suite
# honest. The gap is documented in TEST-DESIGN.md.
# =============================================================================

@test "CV-06a: config with request > limit fails dry-run with a clear error (REQ-6 target)" {
    run bash "$RUN_EXPERIMENT_SH" "$REQ6_INVALID_CONFIG" --dry-run

    # RED PHASE: the runner currently accepts request=1000m;limit=500m (exit 0).
    # REQ-6 is unmet until run-experiment.sh/common.sh validate request <= limit.
    [ "$status" -ne 0 ]
    # The clear error must name the offending combo (request and limit keys)
    [[ "$output" == *"request"* ]]
    [[ "$output" == *"limit"* ]]
    [[ "$output" == *"xceed"* || "$output" == *"nvalid"* || "$output" == *"rror"* ]]
}

@test "CV-06b: config with request > limit is rejected (REQ-6 validation landed)" {
    # Companion to CV-06a. Once request<=limit validation landed (TASK-011),
    # the same fixture is rejected by both tests. This assertion was FLIPPED
    # from exit-0 to exit-non-zero per the TEST-DESIGN.md instruction: "Flip
    # this assertion (expect non-zero) when the validation lands; CV-06a then
    # goes green."
    run bash "$RUN_EXPERIMENT_SH" "$REQ6_INVALID_CONFIG" --dry-run

    [ "$status" -ne 0 ]
}

# =============================================================================
# VC-CV-07 (REQ-7): Backward compatibility — the existing configs still
# validate. These are regression guards: they pass today and must stay green
# after TASK-011/012/013 (the new configs must not break the runner).
# =============================================================================

@test "CV-07a: existing throttling-baseline.yaml dry-runs successfully (REQ-7)" {
    run bash "$RUN_EXPERIMENT_SH" "$BASELINE_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Experiment: throttling-baseline"* ]]
    [[ "$output" == *"Matrix cells:"* ]]
    [[ "$output" == *"DRY RUN MODE"* ]]
}

@test "CV-07b: existing co-located.yaml dry-runs successfully (REQ-7)" {
    run bash "$RUN_EXPERIMENT_SH" "$CO_LOCATED_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Experiment: co-located"* ]]
    [[ "$output" == *"Matrix cells:"* ]]
    [[ "$output" == *"DRY RUN MODE"* ]]
}
