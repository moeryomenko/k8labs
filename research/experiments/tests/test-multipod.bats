#!/usr/bin/env bats
# multipod.bats — Tests for N-pod (multi-pod) co-located runner generalization
#
# These tests encode the target behavior of generalizing the
# co-located experiment path in research/cpu-sched/experiments/run-experiment.sh from
# exactly 2 pods to N pods. They are written test-first: the N-pod tests
# FAIL (red phase) against the current runner, while the
# backward-compatibility tests are regression guards that already pass
# and must stay green after the generalization lands.
#
# No running cluster is required — every assertion targets --dry-run
# stdout/stderr and exit codes.
#
# Covered behaviors:
#   N-pod config recognized; all N pod names printed
#   per-pod data-collection streams
#   per-pod manifest/substitution values with no unresolved markers
#   unknown workload type errors clearly
#   backward compat: 2-pod and single-pod configs unchanged
#
# Run from project root:
#   bats research/cpu-sched/experiments/tests/test-multipod.bats
#
# Run a specific test (filter by any substring of the test description):
#   bats --filter "3-pod config" research/cpu-sched/experiments/tests/test-multipod.bats

setup() {
    export PROJECT_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../../../.." && pwd -P)"
    export EXPERIMENTS_DIR="$PROJECT_ROOT/research/cpu-sched/experiments"
    export RUN_EXPERIMENT_SH="$EXPERIMENTS_DIR/run-experiment.sh"
    export COMMON_SH="$EXPERIMENTS_DIR/common.sh"
    export CO_LOCATED_CONFIG="$EXPERIMENTS_DIR/configs/co-located.yaml"
    export BASELINE_CONFIG="$EXPERIMENTS_DIR/configs/throttling-baseline.yaml"

    # Fixture configs are written per-test into $BATS_TEST_TMPDIR so the suite
    # never needs a live cluster or extra fixture files.
    export THREE_PODS_CONFIG="$BATS_TEST_TMPDIR/mp-three-pods.yaml"
    export UNKNOWN_TYPE_CONFIG="$BATS_TEST_TMPDIR/mp-unknown-type.yaml"
    export MISSING_TYPE_CONFIG="$BATS_TEST_TMPDIR/mp-missing-type.yaml"

    cat > "$THREE_PODS_CONFIG" <<'EOF'
experiment:
  name: mp-three-pods
  description: "Three stress-ng pods co-located"
replicates: 1
pre_warm: 5
duration: 30
cooldown: 5
workloads:
  pod-a:
    type: stress-ng
    params:
      cores: 2
      load: 100
  pod-b:
    type: stress-ng
    params:
      cores: 2
      load: 100
  pod-c:
    type: stress-ng
    params:
      cores: 2
      load: 100
measurement:
  cgroup_interval: 5
matrix:
  - a_request: "500m"; a_limit: ""; b_request: "1000m"; b_limit: ""; c_request: ""; c_limit: ""
EOF

    cat > "$UNKNOWN_TYPE_CONFIG" <<'EOF'
experiment:
  name: mp-unknown-type
  description: "Unknown workload type under workloads:"
replicates: 1
pre_warm: 5
duration: 30
cooldown: 5
workloads:
  pod-a:
    type: futuristic-workload
    params:
      cores: 2
      load: 100
measurement:
  cgroup_interval: 5
matrix:
  - a_request: "500m"; a_limit: ""
EOF

    cat > "$MISSING_TYPE_CONFIG" <<'EOF'
experiment:
  name: mp-missing-type
  description: "Pod entry under workloads: without a type key"
replicates: 1
pre_warm: 5
duration: 30
cooldown: 5
workloads:
  pod-a:
    params:
      cores: 2
measurement:
  cgroup_interval: 5
matrix:
  - a_request: "500m"; a_limit: ""
EOF

    # Sanity checks on runner and pre-existing configs
    [ -f "$RUN_EXPERIMENT_SH" ] || { echo "FATAL: runner not found at $RUN_EXPERIMENT_SH" >&2; exit 1; }
    [ -f "$CO_LOCATED_CONFIG" ] || { echo "FATAL: co-located.yaml not found" >&2; exit 1; }
    [ -f "$BASELINE_CONFIG" ] || { echo "FATAL: throttling-baseline.yaml not found" >&2; exit 1; }
}

# =============================================================================
# A config with workloads: mapping N pods is recognized and
# the runner prints all N pod deployments in --dry-run output.
# =============================================================================

@test "3-pod config dry-run succeeds and lists all three pod names" {
    run bash "$RUN_EXPERIMENT_SH" "$THREE_PODS_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    # Every pod from the workloads: mapping must appear in dry-run output
    # (currently absent — red phase)
    [[ "$output" == *"pod-a"* ]]
    [[ "$output" == *"pod-b"* ]]
    [[ "$output" == *"pod-c"* ]]
}

@test "dry-run prints exactly N (=3) distinct pod names for an N-pod config" {
    run bash "$RUN_EXPERIMENT_SH" "$THREE_PODS_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    local distinct
    distinct="$(printf '%s\n' "$output" | grep -oE 'pod-[abc]' | sort -u | sed '/^$/d' | wc -l)"
    # Generality: all N pods are printed, not a fixed subset
    [ "$distinct" -eq 3 ]
}

# =============================================================================
# For a 3-pod config, --dry-run output shows 3 pod
# deployments and 3 data-collection streams (cgroup-watch per pod).
# =============================================================================

@test "3-pod config shows 3 data-collection streams, one per pod" {
    run bash "$RUN_EXPERIMENT_SH" "$THREE_PODS_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    local streams=0
    for pod in pod-a pod-b pod-c; do
        if printf '%s\n' "$output" | grep "$pod" | grep -qE 'cgroup-watch|collect_cgroup_data'; then
            streams=$((streams + 1))
        fi
    done
    # One cgroup data-collection stream per pod
    [ "$streams" -eq 3 ]
}

# =============================================================================
# Pod manifest generation substitutes per-pod request/limit
# template markers correctly (dry-run prints the substitution OR the manifest
# filenames; per-pod markers resolved).
# =============================================================================

@test "per-pod manifests or per-pod substitution values are printed, with no unresolved markers" {
    run bash "$RUN_EXPERIMENT_SH" "$THREE_PODS_CONFIG" --dry-run

    [ "$status" -eq 0 ]

    if printf '%s\n' "$output" | grep -q '\.yaml'; then
        # Branch A — "manifest filenames": one manifest per pod
        local yamls
        yamls="$(printf '%s\n' "$output" | grep -oE '[A-Za-z0-9_.-]+\.ya?ml' | sed '/^$/d' | wc -l)"
        [ "$yamls" -ge 3 ]
        for pod in pod-a pod-b pod-c; do
            printf '%s\n' "$output" | grep -qE "${pod}[A-Za-z0-9_.-]*\.ya?ml"
        done
    else
        # Branch B — "substitution": per-pod request values resolved next
        # to their pod (matrix line alone is not enough)
        printf '%s\n' "$output" | grep -qE 'pod-a.*500m|500m.*pod-a'
        printf '%s\n' "$output" | grep -qE 'pod-b.*1000m|1000m.*pod-b'
    fi

    # Guard: no unresolved CPU template markers remain anywhere in dry-run output
    ! printf '%s\n' "$output" | grep -qE '\{\{[A-Za-z_]*CPU_'
}

# =============================================================================
# The runner errors clearly on a config with an unknown
# workload type under workloads: (validation fails, message names the type).
# =============================================================================

@test "config with unknown workload type under workloads: fails" {
    run bash "$RUN_EXPERIMENT_SH" "$UNKNOWN_TYPE_CONFIG" --dry-run

    [ "$status" -ne 0 ]
}

@test "unknown workload type error names the offending type" {
    run bash "$RUN_EXPERIMENT_SH" "$UNKNOWN_TYPE_CONFIG" --dry-run

    [ "$status" -ne 0 ]
    # The message must identify the unknown type
    [[ "$output" == *"futuristic-workload"* ]]
    [[ "$output" == *"nknown"* ]]
}

@test "pod entry missing type under workloads: fails validation" {
    # Edge case: a pod without a type cannot be templated, so the
    # runner must reject it rather than silently continuing.
    run bash "$RUN_EXPERIMENT_SH" "$MISSING_TYPE_CONFIG" --dry-run

    [ "$status" -ne 0 ]
}

# =============================================================================
# Backward compatibility — existing co-located.yaml (2 pods)
# and single-pod configs still dry-run successfully with unchanged output
# shape. These are regression guards: they pass today and must stay green after
# the generalization lands.
# =============================================================================

@test "existing co-located.yaml (2 pods) dry-runs with unchanged output shape" {
    run bash "$RUN_EXPERIMENT_SH" "$CO_LOCATED_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Detected co-located experiment configuration"* ]]
    [[ "$output" == *"DRY RUN MODE"* ]]
    [[ "$output" == *"Matrix cells:"* ]]
    # The co-located matrix entry keeps its existing ls_/batch_ keys
    [[ "$output" == *"ls_request"* ]]
    [[ "$output" == *"batch_request"* ]]
    [[ "$output" == *"Prerequisites check passed"* ]]
}

@test "existing single-pod throttling-baseline.yaml dry-runs with unchanged output shape" {
    run bash "$RUN_EXPERIMENT_SH" "$BASELINE_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"DRY RUN MODE"* ]]
    [[ "$output" == *"throttling-baseline"* ]]
    [[ "$output" == *"Matrix cells:"* ]]
    [[ "$output" == *"Prerequisites check passed"* ]]
}
