#!/usr/bin/env bats
# test-node-pinning.bats — Tests for config-driven node pinning
#
# These tests encode the target behavior of parameterizing node
# pinning so experiments can target a second worker w2: a top-level `node:`
# key in the experiment config makes the runner inject `nodeName: <node>` into
# each pod manifest, defaulting to `w1` when the key is absent (backward
# compat — the workload templates currently hardcode nodeName: w1 and must
# stop doing so). They are written test-first: the node-injection tests FAIL
# (red phase) against the current runner, which neither reads `node:` nor
# prints any node marker in --dry-run; the regression guards assert the
# existing dry-run shape stays intact.
#
# No running cluster is required — every assertion targets --dry-run
# stdout/stderr, exit codes, or the raw workload templates.
#
# Covered behaviors:
#   multi-pod config with node: w2 dry-runs with per-pod markers
#   backward compat: existing weight-share.yaml defaults to w1
#   single-pod configs: node: w2 vs w1 default
#   rendered manifests for a node: w2 config carry nodeName: w2
#   templates no longer hardcode a literal nodeName; composes with --eevdf
#
# Run from project root:
#   bats research/experiments/tests/test-node-pinning.bats
#
# Run a specific test (filter by any substring of the test description):
#   bats --filter "nodeName: w1" research/experiments/tests/test-node-pinning.bats

setup() {
    export PROJECT_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../../.." && pwd -P)"
    export EXPERIMENTS_DIR="$PROJECT_ROOT/research/experiments"
    export RUN_EXPERIMENT_SH="$EXPERIMENTS_DIR/run-experiment.sh"
    export COMMON_SH="$EXPERIMENTS_DIR/common.sh"
    export WEIGHT_SHARE_CONFIG="$EXPERIMENTS_DIR/configs/weight-share.yaml"
    export BASELINE_CONFIG="$EXPERIMENTS_DIR/configs/throttling-baseline.yaml"
    export REQUEST_LIMIT_MATRIX_CONFIG="$EXPERIMENTS_DIR/configs/request-limit-matrix.yaml"

    # Temp configs are written per-test into $BATS_TEST_TMPDIR so the suite
    # never needs a live cluster or extra fixture files.
    export MULTIPOD_W2_CONFIG="$BATS_TEST_TMPDIR/np-multipod-w2.yaml"
    export BASELINE_W2_CONFIG="$BATS_TEST_TMPDIR/np-baseline-w2.yaml"
    export REQUEST_LIMIT_MATRIX_W2_CONFIG="$BATS_TEST_TMPDIR/np-request-limit-matrix-w2.yaml"

    # Fixture: three stress-ng pods pinned to w2 via a top-level node:
    # key (weight-share shape, single cell — node injection is per pod, not
    # per cell, so one cell is enough for the dry-run contract).
    cat > "$MULTIPOD_W2_CONFIG" <<'EOF'
experiment:
  name: np-multipod-w2
  description: "Three stress-ng pods pinned to w2 via the top-level node: key"
node: w2
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

    # Single-pod fixtures: the real single-pod configs plus one top-level node: key
    # (sed-inserted at the top; the flat-YAML parser reads top-level keys in
    # any order).
    sed '1i node: w2' "$BASELINE_CONFIG" > "$BASELINE_W2_CONFIG"
    sed '1i node: w2' "$REQUEST_LIMIT_MATRIX_CONFIG" > "$REQUEST_LIMIT_MATRIX_W2_CONFIG"

    # Sanity checks on runner and pre-existing configs
    [ -f "$RUN_EXPERIMENT_SH" ] || { echo "FATAL: runner not found at $RUN_EXPERIMENT_SH" >&2; exit 1; }
    [ -f "$WEIGHT_SHARE_CONFIG" ] || { echo "FATAL: weight-share.yaml not found" >&2; exit 1; }
    [ -f "$BASELINE_CONFIG" ] || { echo "FATAL: throttling-baseline.yaml not found" >&2; exit 1; }
    [ -f "$REQUEST_LIMIT_MATRIX_CONFIG" ] || { echo "FATAL: request-limit-matrix.yaml not found" >&2; exit 1; }
}

# ---------------------------------------------------------------------------
# count_node_markers — number of node markers for a node name in $output
#
# Accepts the canonical k8s field (`nodeName: <node>`) and the requirement's
# shorthand (`node: <node>`). Both dry-run rendering styles are contract.
# ---------------------------------------------------------------------------
count_node_markers() {
    local node="$1"
    printf '%s\n' "$output" | grep -oE "nodeName:[[:space:]]*${node}|node:[[:space:]]*${node}" | wc -l
}

# =============================================================================
# A config with node: w2 dry-runs exit 0 and the per-pod
# deployment output shows nodeName: w2 (or "node: w2") for every pod.
# =============================================================================

@test "multi-pod config with node: w2 dry-runs successfully" {
    run bash "$RUN_EXPERIMENT_SH" "$MULTIPOD_W2_CONFIG" --dry-run

    # RED PHASE: the config is accepted today (unknown top-level keys are
    # tolerated), but the node markers are absent — the marker assertion is the
    # failing half.
    [ "$status" -eq 0 ]
    [[ "$output" == *"DRY RUN MODE"* ]]
}

@test "multi-pod node: w2 dry-run shows nodeName: w2 for every pod" {
    run bash "$RUN_EXPERIMENT_SH" "$MULTIPOD_W2_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    local markers stale
    markers="$(count_node_markers w2)"
    # One node marker per pod (3 pods) — the target node must be visible
    [ "$markers" -ge 3 ]
    # Guard: no stale w1 marker leaks into a w2 config (mixed injection breaks
    # the manifest with duplicate spec keys)
    stale="$(count_node_markers w1)"
    [ "$stale" -eq 0 ]
}

# =============================================================================
# Backward compat — existing weight-share.yaml (no node:
# key) dry-runs exit 0 and shows nodeName: w1 for every pod (default preserved).
# =============================================================================

@test "existing weight-share.yaml (no node: key) dry-runs with unchanged shape" {
    run bash "$RUN_EXPERIMENT_SH" "$WEIGHT_SHARE_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Detected co-located experiment configuration"* ]]
    [[ "$output" == *"DRY RUN MODE"* ]]
    [[ "$output" == *"Matrix cells:"* ]]
    [[ "$output" == *"Prerequisites check passed"* ]]
}

@test "weight-share default dry-run shows nodeName: w1 for every pod" {
    run bash "$RUN_EXPERIMENT_SH" "$WEIGHT_SHARE_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    local markers stale
    markers="$(count_node_markers w1)"
    # Absent node: key keeps the historical w1 pinning — one marker per pod
    [ "$markers" -ge 3 ]
    stale="$(count_node_markers w2)"
    [ "$stale" -eq 0 ]
}

# =============================================================================
# Single-pod configs — node: w2 shows nodeName: w2;
# without node: shows the w1 default.
# =============================================================================

@test "single-pod config with node: w2 shows nodeName: w2" {
    run bash "$RUN_EXPERIMENT_SH" "$BASELINE_W2_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"throttling-baseline"* ]]
    local markers stale
    markers="$(count_node_markers w2)"
    [ "$markers" -ge 1 ]
    stale="$(count_node_markers w1)"
    [ "$stale" -eq 0 ]
}

@test "single-pod config without node: shows the w1 default" {
    run bash "$RUN_EXPERIMENT_SH" "$BASELINE_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"DRY RUN MODE"* ]]
    [[ "$output" == *"Matrix cells:"* ]]
    local markers stale
    markers="$(count_node_markers w1)"
    [ "$markers" -ge 1 ]
    stale="$(count_node_markers w2)"
    [ "$stale" -eq 0 ]
}

@test "request-limit-matrix with node: w2 shows nodeName: w2" {
    run bash "$RUN_EXPERIMENT_SH" "$REQUEST_LIMIT_MATRIX_W2_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"request-limit-matrix"* ]]
    local markers stale
    markers="$(count_node_markers w2)"
    [ "$markers" -ge 1 ]
    stale="$(count_node_markers w1)"
    [ "$stale" -eq 0 ]
}

# =============================================================================
# The manifest files generated for a node: w2 config
# contain nodeName: w2. Strongest cluster-free assertion: if the runner writes
# rendered manifests during --dry-run, assert file contents; otherwise assert
# the dry-run output's per-pod node markers (mirrors the multi-pod test's
# branch pattern).
# =============================================================================

@test "manifests rendered for a node: w2 config contain nodeName: w2" {
    local out_dir="$BATS_TEST_TMPDIR/np-out"
    run bash "$RUN_EXPERIMENT_SH" "$MULTIPOD_W2_CONFIG" --dry-run --output-dir "$out_dir"

    [ "$status" -eq 0 ]

    # Branch A — the runner writes rendered manifests during dry-run: every
    # written manifest must carry nodeName: w2 (and none the stale w1).
    local -a manifests=()
    while IFS= read -r -d '' m; do
        manifests+=("$m")
    done < <(find "$out_dir" -type f -name '*.yaml' -print0 2>/dev/null)

    if [[ ${#manifests[@]} -gt 0 ]]; then
        # One manifest per pod in the config
        [ "${#manifests[@]}" -ge 3 ]
        local m
        for m in "${manifests[@]}"; do
            grep -qE 'nodeName:[[:space:]]*w2' "$m"
            ! grep -qE 'nodeName:[[:space:]]*w1' "$m"
        done
    else
        # Branch B — dry-run only prints the plan: per-pod node markers must
        # be visible in the output instead (fallback)
        local markers
        markers="$(count_node_markers w2)"
        [ "$markers" -ge 3 ]
    fi
}

# =============================================================================
# Regression — templates no longer hardcode w1 (default
# injection gives w1) and the new feature composes with existing paths.
# =============================================================================

@test "workload templates no longer hardcode a literal nodeName value" {
    # The runner supplies the node now. A template that still pins a
    # literal node (e.g. nodeName: w1) would conflict with config-driven
    # injection (duplicate spec-level key in the rendered manifest) — the 5
    # workload templates must drop it. A {{NODE_NAME}}-style marker is allowed
    # because it carries no literal node name.
    local t
    for t in \
        "$PROJECT_ROOT/research/workloads/api-server/deploy.yaml" \
        "$PROJECT_ROOT/research/workloads/cpu-burner/deploy.yaml" \
        "$PROJECT_ROOT/research/workloads/db-simulator/deploy.yaml" \
        "$PROJECT_ROOT/research/workloads/stress-ng/deploy.yaml" \
        "$PROJECT_ROOT/research/workloads/stress-ng/deploy-guaranteed.yaml"; do
        [ -f "$t" ] || { echo "FATAL: template not found: $t" >&2; return 1; }
        ! grep -qE 'nodeName:[[:space:]]*[A-Za-z0-9._-]' "$t"
    done
}

@test "node: w2 composes with --eevdf in dry-run" {
    run bash "$RUN_EXPERIMENT_SH" "$MULTIPOD_W2_CONFIG" --dry-run --eevdf

    [ "$status" -eq 0 ]
    # Regression: the EEVDF collection plan is still advertised alongside
    # node injection (the eevdf-wiring assertions must hold)
    printf '%s\n' "$output" | grep -qi 'eevdf'
    local markers
    markers="$(count_node_markers w2)"
    [ "$markers" -ge 3 ]
}
