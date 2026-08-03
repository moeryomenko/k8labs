#!/usr/bin/env bats
# test-weight-share-4v.bats — Tests for the 4-vCPU weight-share config + w2 variants (TASK-V05)
#
# These tests encode the target behavior of TASK-V05 (adding a 4-vCPU
# weight-share config and w2-pinned variants to the requests-vs-limits
# scheduler-interaction study). They are written test-first: the two new
# config files DO NOT exist yet, so every test that asserts their presence or
# dry-run behavior FAILS (red phase) against the current tree. The regression
# guards (W4-07..W4-09) assert the existing w1 behavior and the node: w2
# mechanism (TASK-V03) and PASS today.
#
# No running cluster is required — every assertion targets --dry-run
# stdout/stderr and exit codes.
#
# The configs (created by the engineer after this design lands — these tests
# never create them):
#   weight-share-4v.yaml   Family A scaled for 4-vCPU w2: 3 stress-ng pods
#                          (pod-a/pod-b/pod-c), 6 cells, node: w2, per-cell
#                          request sum <= 3600m (4 cores minus system headroom)
#   weight-share-w2.yaml   Family A rerun pinned to w2: 6 cells, node: w2;
#                          weight-share.yaml stays the w1 default (REQ-5)
#
# Requirements covered (full mapping in TEST-DESIGN.md):
#   REQ-1 -> VC-W4-V05-01 (W4-01, W4-02)
#   REQ-2 -> VC-W4-V05-02 (W4-03)
#   REQ-3 -> VC-W4-V05-03 (W4-04)
#   REQ-4 -> VC-W4-V05-04 (W4-05, W4-06)
#   REQ-5 -> VC-W4-V05-05 (W4-07)
#   REQ-6 -> VC-W4-V05-06 (W4-08, W4-09)
#
# Run from project root:
#   bats research/experiments/tests/test-weight-share-4v.bats
#
# Run a specific test:
#   bats --filter "W4-04" research/experiments/tests/test-weight-share-4v.bats

setup() {
    export PROJECT_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../../.." && pwd -P)"
    export EXPERIMENTS_DIR="$PROJECT_ROOT/research/experiments"
    export RUN_EXPERIMENT_SH="$EXPERIMENTS_DIR/run-experiment.sh"
    export CONFIGS_DIR="$EXPERIMENTS_DIR/configs"

    # TASK-V05 targets — created by the engineer later. Presence is what the
    # REQ-1/REQ-4 tests assert; they are RED until those files land.
    export WEIGHT_SHARE_4V_CONFIG="$CONFIGS_DIR/weight-share-4v.yaml"
    export WEIGHT_SHARE_W2_CONFIG="$CONFIGS_DIR/weight-share-w2.yaml"

    # Existing configs used by the REQ-5 backward-compat guard and the REQ-6
    # node: w2 composition checks (temp sed variants).
    export WEIGHT_SHARE_CONFIG="$CONFIGS_DIR/weight-share.yaml"
    export QOS_HIERARCHY_CONFIG="$CONFIGS_DIR/qos-hierarchy.yaml"
    export LATENCY_INTERFERENCE_CONFIG="$CONFIGS_DIR/latency-interference.yaml"

    # REQ-6 fixtures: sed-copied real configs with one top-level node: w2 key
    # (same technique as test-node-pinning.bats). The flat-YAML parser reads
    # top-level keys in any order, so inserting the key at line 1 is faithful.
    export QOS_W2_CONFIG="$BATS_TEST_TMPDIR/w4-qos-w2.yaml"
    export LATENCY_W2_CONFIG="$BATS_TEST_TMPDIR/w4-latency-w2.yaml"
    sed '1i node: w2' "$QOS_HIERARCHY_CONFIG" > "$QOS_W2_CONFIG"
    sed '1i node: w2' "$LATENCY_INTERFERENCE_CONFIG" > "$LATENCY_W2_CONFIG"

    # Sanity checks on runner and pre-existing configs
    [ -f "$RUN_EXPERIMENT_SH" ] || { echo "FATAL: runner not found at $RUN_EXPERIMENT_SH" >&2; exit 1; }
    [ -f "$WEIGHT_SHARE_CONFIG" ] || { echo "FATAL: weight-share.yaml not found" >&2; exit 1; }
    [ -f "$QOS_HIERARCHY_CONFIG" ] || { echo "FATAL: qos-hierarchy.yaml not found" >&2; exit 1; }
    [ -f "$LATENCY_INTERFERENCE_CONFIG" ] || { echo "FATAL: latency-interference.yaml not found" >&2; exit 1; }
}

# ---------------------------------------------------------------------------
# count_node_markers — number of node markers for a node name in $output
#
# Same contract as test-node-pinning.bats: accepts the canonical k8s field
# (`nodeName: <node>`) and the requirement's shorthand (`node: <node>`). Both
# dry-run rendering styles are contract.
# ---------------------------------------------------------------------------
count_node_markers() {
    local node="$1"
    printf '%s\n' "$output" | grep -oE "nodeName:[[:space:]]*${node}|node:[[:space:]]*${node}" | wc -l
}

# ---------------------------------------------------------------------------
# cell_request_mc — parse a key's CPU request from a dry-run matrix cell line
# as integer millicores.
#
# The dry-run prints normalized "key=value" pairs (parse_matrix_entries strips
# quotes), e.g. "a_request=1500m;a_limit=;b_request=1500m;...". Values use the
# repo's millicore convention ("500m"); empty values (e.g. "c_request=" for a
# BestEffort pod-c) parse to 0. Any other value shape fails the caller's
# assertions rather than silently passing.
#
# Arguments:
#   $1 — matrix cell line from dry-run output
#   $2 — key (a_request | b_request | c_request)
# Returns: integer millicores on stdout
# ---------------------------------------------------------------------------
cell_request_mc() {
    local line="$1"
    local key="$2"
    local val
    val="$(printf '%s' "$line" | sed -n "s/.*${key}=\([0-9][0-9]*\)\(m\)\?.*/\1/p")"
    if [[ -n "$val" ]]; then
        printf '%s\n' "$((10#$val))"
    else
        printf '0\n'
    fi
}

# =============================================================================
# VC-W4-V05-01 (REQ-1): weight-share-4v.yaml exists; dry-run exit 0 and prints
# 6 matrix cells. RED until the engineer creates the config.
# =============================================================================

@test "W4-01: config weight-share-4v.yaml exists (REQ-1)" {
    [ -f "$WEIGHT_SHARE_4V_CONFIG" ]
}

@test "W4-02: weight-share-4v.yaml dry-runs exit 0 with 6 matrix cells (REQ-1)" {
    run bash "$RUN_EXPERIMENT_SH" "$WEIGHT_SHARE_4V_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Matrix cells: 6"* ]]
    [[ "$output" == *"DRY RUN MODE"* ]]

    # Six cell lines must be printed under "Matrix cells:" (one per entry).
    # Deployment lines print "request=" without the a_/b_ prefix, so the
    # 'a_request=' pattern uniquely matches the matrix cell lines.
    local cells
    cells="$(printf '%s\n' "$output" | grep -cE 'a_request=[0-9]+m' || true)"
    [ "$cells" -eq 6 ]
}

# =============================================================================
# VC-W4-V05-02 (REQ-2): weight-share-4v.yaml pins every pod to w2 — the dry-run
# shows nodeName: w2 for all 3 pods and no stale w1 marker.
# =============================================================================

@test "W4-03: weight-share-4v.yaml dry-run shows nodeName: w2 for all 3 pods (REQ-2)" {
    run bash "$RUN_EXPERIMENT_SH" "$WEIGHT_SHARE_4V_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    # All three deployment slots must appear (workloads: mapping = 3 pods)
    printf '%s\n' "$output" | grep -qE 'Deployment 1/3:'
    printf '%s\n' "$output" | grep -qE 'Deployment 2/3:'
    printf '%s\n' "$output" | grep -qE 'Deployment 3/3:'

    # One w2 marker per pod; zero w1 markers (mixed injection would produce a
    # duplicate spec-level nodeName key in the rendered manifest)
    local markers stale
    markers="$(count_node_markers w2)"
    [ "$markers" -ge 3 ]
    stale="$(count_node_markers w1)"
    [ "$stale" -eq 0 ]
}

# =============================================================================
# VC-W4-V05-03 (REQ-3): every weight-share-4v cell fits the 4-vCPU request
# budget — a_request + b_request (+ c_request when set) <= 3600m per cell, with
# non-empty a/b requests. The suggested ratios are a starting point; the bound
# (not the exact values) is the pinned contract (see TEST-DESIGN.md §7).
# =============================================================================

@test "W4-04: every weight-share-4v cell sums to <= 3600m with non-empty a/b (REQ-3)" {
    run bash "$RUN_EXPERIMENT_SH" "$WEIGHT_SHARE_4V_CONFIG" --dry-run

    [ "$status" -eq 0 ]

    local cells
    cells="$(printf '%s\n' "$output" | grep -E 'a_request=[0-9]+m' || true)"
    [ "$(printf '%s\n' "$cells" | grep -cE 'a_request=')" -eq 6 ]

    local line a b c total
    while IFS= read -r line; do
        a="$(cell_request_mc "$line" a_request)"
        b="$(cell_request_mc "$line" b_request)"
        c="$(cell_request_mc "$line" c_request)"
        total=$((a + b + c))
        # a/b requests must be present (non-empty) — pod-c may be BestEffort
        [ "$a" -ge 1 ]
        [ "$b" -ge 1 ]
        # whole-cell request sum must stay inside the 4-vCPU budget
        [ "$total" -le 3600 ]
    done <<< "$cells"
}

# =============================================================================
# VC-W4-V05-04 (REQ-4): the w2 rerun variant of weight-share — a NEW file
# weight-share-w2.yaml (design decision, TEST-DESIGN.md §5) — exists and
# dry-runs exit 0 with 6 cells and nodeName: w2.
# =============================================================================

@test "W4-05: config weight-share-w2.yaml exists (REQ-4)" {
    [ -f "$WEIGHT_SHARE_W2_CONFIG" ]
}

@test "W4-06: weight-share-w2.yaml dry-runs exit 0 with 6 cells and nodeName: w2 (REQ-4)" {
    run bash "$RUN_EXPERIMENT_SH" "$WEIGHT_SHARE_W2_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Matrix cells: 6"* ]]
    [[ "$output" == *"DRY RUN MODE"* ]]

    local markers stale
    markers="$(count_node_markers w2)"
    [ "$markers" -ge 3 ]
    stale="$(count_node_markers w1)"
    [ "$stale" -eq 0 ]
}

# =============================================================================
# VC-W4-V05-05 (REQ-5): backward compat — weight-share.yaml (no node: key,
# w1 default) still dry-runs exit 0 with 6 cells and nodeName: w1. This
# mirrors NP-04 and keeps CV-03a green: the w2 rerun lives in a SEPARATE file
# so the 2-vCPU w1 config is untouched.
# =============================================================================

@test "W4-07: weight-share.yaml (w1 default) still dry-runs with nodeName: w1 (REQ-5)" {
    run bash "$RUN_EXPERIMENT_SH" "$WEIGHT_SHARE_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Matrix cells: 6"* ]]
    [[ "$output" == *"DRY RUN MODE"* ]]

    local markers stale
    markers="$(count_node_markers w1)"
    [ "$markers" -ge 3 ]
    stale="$(count_node_markers w2)"
    [ "$stale" -eq 0 ]
}

# =============================================================================
# VC-W4-V05-06 (REQ-6): the other multi-pod families compose with node: w2 via
# the same top-level-key mechanism (TASK-V03). Light regression check: temp
# sed variants of the real configs must show one w2 marker per pod and no w1.
# =============================================================================

@test "W4-08: qos-hierarchy.yaml composes with node: w2 (3 pods pinned, REQ-6)" {
    run bash "$RUN_EXPERIMENT_SH" "$QOS_W2_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    local markers stale
    markers="$(count_node_markers w2)"
    [ "$markers" -ge 3 ]
    stale="$(count_node_markers w1)"
    [ "$stale" -eq 0 ]
}

@test "W4-09: latency-interference.yaml composes with node: w2 (2 pods pinned, REQ-6)" {
    run bash "$RUN_EXPERIMENT_SH" "$LATENCY_W2_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    local markers stale
    markers="$(count_node_markers w2)"
    [ "$markers" -ge 2 ]
    stale="$(count_node_markers w1)"
    [ "$stale" -eq 0 ]
}
