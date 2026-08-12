#!/usr/bin/env bats
# test-config-validation.bats — Tests for the six new experiment configs
#
# These tests encode the target behavior of creating six experiment configs
# for the requests-vs-limits scheduler-interaction study. They are written
# test-first: the config files DO NOT exist yet, so every test that asserts
# their presence or dry-run behavior FAILS (red phase) against the current
# tree. The only tests expected to pass today are the request-exceeds-limit
# current-behavior guard and the backward-compatibility regression guards.
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
# Covered behaviors: config existence (six files), dry-run success with name
# and cell count, exact matrix cell counts (weight-share 6, request-limit-
# matrix 15, plus plan-derived counts for qos-hierarchy 2, latency-interference
# 4, tunables-contention 3), multi-pod deployment lines, latency_load
# generation mention, request-exceeds-limit rejection (target + current-behavior
# guard), and backward compatibility of the pre-existing configs.
#
# FIX-3 additions (TRUE Guaranteed pod support):
#   guaranteed stress-ng template exists and hardcodes memory request==limit
#   128Mi while keeping CPU request/limit matrix markers; get_workload_template
#   resolves type stress-ng-guaranteed (via --dry-run of a temp config; unknown
#   types die at validation); qos-hierarchy.yaml's guaranteed pod uses the
#   guaranteed type; counts stay 2 cells / 3 deployments and dry-run stays
#   exit 0.
#
# Run from project root:
#   bats research/cpu-sched/experiments/tests/test-config-validation.bats
#
# Run a specific test (filter by any substring of the test description):
#   bats --filter "matrix cells" research/cpu-sched/experiments/tests/test-config-validation.bats
#
# Distribution-config additions: the six NEW distribution configs are pinned —
# dist-api-server, dist-db-simulator, dist-cpu-burner, dist-stress-ng (Family A,
# shared 6-cell matrix), dist-weight-share (Family B, 6 cells),
# dist-qos-hierarchy (Family C, 3 cells). They are RED until the configs are
# created. The pre-existing config tests are regression guards and must stay
# green.

setup() {
    export PROJECT_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../../../.." && pwd -P)"
    export EXPERIMENTS_DIR="$PROJECT_ROOT/research/cpu-sched/experiments"
    export RUN_EXPERIMENT_SH="$EXPERIMENTS_DIR/run-experiment.sh"
    export CONFIGS_DIR="$EXPERIMENTS_DIR/configs"

    # The six new configs. Presence is what the config-existence tests assert;
    # they are RED until the configs land.
    export WEIGHT_SHARE_CONFIG="$CONFIGS_DIR/weight-share.yaml"
    export REQUEST_LIMIT_MATRIX_CONFIG="$CONFIGS_DIR/request-limit-matrix.yaml"
    export QOS_HIERARCHY_CONFIG="$CONFIGS_DIR/qos-hierarchy.yaml"
    export LATENCY_INTERFERENCE_CONFIG="$CONFIGS_DIR/latency-interference.yaml"
    export CPU_BURST_CONFIG="$CONFIGS_DIR/cpu-burst.yaml"
    export TUNABLES_CONTENTION_CONFIG="$CONFIGS_DIR/tunables-contention.yaml"

    # Existing configs used by the backward-compatibility regression guards
    export BASELINE_CONFIG="$CONFIGS_DIR/throttling-baseline.yaml"
    export CO_LOCATED_CONFIG="$CONFIGS_DIR/co-located.yaml"

    # The six distribution configs. Presence is asserted by the existence tests
    # — RED until the configs are created.
    export DIST_API_SERVER_CONFIG="$CONFIGS_DIR/dist-api-server.yaml"
    export DIST_DB_SIMULATOR_CONFIG="$CONFIGS_DIR/dist-db-simulator.yaml"
    export DIST_CPU_BURNER_CONFIG="$CONFIGS_DIR/dist-cpu-burner.yaml"
    export DIST_STRESS_NG_CONFIG="$CONFIGS_DIR/dist-stress-ng.yaml"
    export DIST_WEIGHT_SHARE_CONFIG="$CONFIGS_DIR/dist-weight-share.yaml"
    export DIST_QOS_HIERARCHY_CONFIG="$CONFIGS_DIR/dist-qos-hierarchy.yaml"

    # FIX-3: TRUE Guaranteed stress-ng template (memory request==limit
    # hardcoded) + workload type stress-ng-guaranteed. The template is created
    # by FIX-3 (RED until it lands); the temp config proves the type mapping
    # via --dry-run without touching the real config.
    export GUARANTEED_TEMPLATE="$PROJECT_ROOT/research/cpu-sched/workloads/stress-ng/deploy-guaranteed.yaml"
    export GUARANTEED_TYPE_CONFIG="$BATS_TEST_TMPDIR/cv-guaranteed-type.yaml"
    cat > "$GUARANTEED_TYPE_CONFIG" <<'EOF'
experiment:
  name: cv-guaranteed-type
  description: "stress-ng-guaranteed type mapping — memory hardcoded 128Mi/128Mi"
replicates: 1
pre_warm: 5
duration: 30
cooldown: 5
workloads:
  gpod:
    type: stress-ng-guaranteed
    params:
      cores: 2
      load: 100
measurement:
  cgroup_interval: 5
matrix:
  - gpod_request: "500m"; gpod_limit: "500m"
EOF

    # Fixture: a temp config whose request exceeds its limit. Written by
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
# All six config files exist. RED until the configs are created.
# =============================================================================

@test "config weight-share.yaml exists" {
    [ -f "$WEIGHT_SHARE_CONFIG" ]
}

@test "config request-limit-matrix.yaml exists" {
    [ -f "$REQUEST_LIMIT_MATRIX_CONFIG" ]
}

@test "config qos-hierarchy.yaml exists" {
    [ -f "$QOS_HIERARCHY_CONFIG" ]
}

@test "config latency-interference.yaml exists" {
    [ -f "$LATENCY_INTERFERENCE_CONFIG" ]
}

@test "config cpu-burst.yaml exists" {
    [ -f "$CPU_BURST_CONFIG" ]
}

@test "config tunables-contention.yaml exists" {
    [ -f "$TUNABLES_CONTENTION_CONFIG" ]
}

# =============================================================================
# Each config dry-runs successfully (exit 0) and prints the experiment name
# and the matrix cell count. RED until the configs exist (runner dies with
# "Config file not found").
# =============================================================================

@test "weight-share.yaml dry-runs, prints name and cell count" {
    run bash "$RUN_EXPERIMENT_SH" "$WEIGHT_SHARE_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Experiment: weight-share"* ]]
    [[ "$output" == *"Matrix cells:"* ]]
    [[ "$output" == *"DRY RUN MODE"* ]]
}

@test "request-limit-matrix.yaml dry-runs, prints name and cell count" {
    run bash "$RUN_EXPERIMENT_SH" "$REQUEST_LIMIT_MATRIX_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Experiment: request-limit-matrix"* ]]
    [[ "$output" == *"Matrix cells:"* ]]
    [[ "$output" == *"DRY RUN MODE"* ]]
}

@test "qos-hierarchy.yaml dry-runs, prints name and cell count" {
    run bash "$RUN_EXPERIMENT_SH" "$QOS_HIERARCHY_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Experiment: qos-hierarchy"* ]]
    [[ "$output" == *"Matrix cells:"* ]]
    [[ "$output" == *"DRY RUN MODE"* ]]
}

@test "latency-interference.yaml dry-runs, prints name and cell count" {
    run bash "$RUN_EXPERIMENT_SH" "$LATENCY_INTERFERENCE_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Experiment: latency-interference"* ]]
    [[ "$output" == *"Matrix cells:"* ]]
    [[ "$output" == *"DRY RUN MODE"* ]]
}

@test "cpu-burst.yaml dry-runs, prints name and cell count" {
    run bash "$RUN_EXPERIMENT_SH" "$CPU_BURST_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Experiment: cpu-burst"* ]]
    [[ "$output" == *"Matrix cells:"* ]]
    [[ "$output" == *"DRY RUN MODE"* ]]
}

@test "tunables-contention.yaml dry-runs, prints name and cell count" {
    run bash "$RUN_EXPERIMENT_SH" "$TUNABLES_CONTENTION_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Experiment: tunables-contention"* ]]
    [[ "$output" == *"Matrix cells:"* ]]
    [[ "$output" == *"DRY RUN MODE"* ]]
}

# =============================================================================
# Exact matrix cell counts. weight-share mandates 6 cells and
# request-limit-matrix 15; the other counts are plan-derived and pinned here
# so the configs cannot drift from the plan.
# =============================================================================

@test "weight-share.yaml dry-run prints 6 matrix cells" {
    run bash "$RUN_EXPERIMENT_SH" "$WEIGHT_SHARE_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Matrix cells: 6"* ]]
}

@test "request-limit-matrix.yaml dry-run prints 15 matrix cells" {
    run bash "$RUN_EXPERIMENT_SH" "$REQUEST_LIMIT_MATRIX_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Matrix cells: 15"* ]]
}

@test "qos-hierarchy.yaml dry-run prints 2 matrix cells (plan-derived)" {
    run bash "$RUN_EXPERIMENT_SH" "$QOS_HIERARCHY_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Matrix cells: 2"* ]]
}

@test "latency-interference.yaml dry-run prints 4 matrix cells (plan-derived)" {
    run bash "$RUN_EXPERIMENT_SH" "$LATENCY_INTERFERENCE_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Matrix cells: 4"* ]]
}

@test "tunables-contention.yaml dry-run prints 3 matrix cells (plan-derived)" {
    run bash "$RUN_EXPERIMENT_SH" "$TUNABLES_CONTENTION_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Matrix cells: 3"* ]]
}

# =============================================================================
# Multi-pod configs print per-pod deployment lines in --dry-run. The runner
# shows the deployments of the FIRST matrix cell; the denominator N comes from
# the size of the workloads: mapping, so a config declaring 3 pods prints
# "Deployment i/3" lines for every cell.
# =============================================================================

@test "weight-share.yaml dry-run shows 3 pod deployments, pod-a/b/c named" {
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

@test "qos-hierarchy.yaml dry-run shows 3 pod deployments" {
    run bash "$RUN_EXPERIMENT_SH" "$QOS_HIERARCHY_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    local deploys
    deploys="$(printf '%s\n' "$output" | grep -cE 'Deployment [0-9]+/3:' 2>/dev/null || true)"
    [ "$deploys" -eq 3 ]
}

@test "latency-interference.yaml dry-run shows 2 pod deployments" {
    run bash "$RUN_EXPERIMENT_SH" "$LATENCY_INTERFERENCE_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    local deploys
    deploys="$(printf '%s\n' "$output" | grep -cE 'Deployment [0-9]+/2:' 2>/dev/null || true)"
    [ "$deploys" -eq 2 ]
}

@test "tunables-contention.yaml dry-run shows 2 pod deployments" {
    run bash "$RUN_EXPERIMENT_SH" "$TUNABLES_CONTENTION_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    local deploys
    deploys="$(printf '%s\n' "$output" | grep -cE 'Deployment [0-9]+/2:' 2>/dev/null || true)"
    [ "$deploys" -eq 2 ]
}

# =============================================================================
# The latency-interference config declares latency_load, so the dry-run plan
# must mention the latency generation step. The runner prints "Latency load
# generation (top-level latency_load):" only when the config has a top-level
# latency_load block.
# =============================================================================

@test "latency-interference.yaml dry-run mentions latency load generation" {
    run bash "$RUN_EXPERIMENT_SH" "$LATENCY_INTERFERENCE_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    printf '%s\n' "$output" | grep -qi 'Latency load generation'
    printf '%s\n' "$output" | grep -q 'latency.csv'
}

# =============================================================================
# A config with request > limit must be rejected with a clear error. The
# runner today has NO request<=limit validation (verified: the fixture dry-runs
# with exit 0), so one test is the RED acceptance test for the target behavior
# and the other pins the CURRENT behavior to keep the suite honest.
# =============================================================================

@test "config with request > limit fails dry-run with a clear error" {
    run bash "$RUN_EXPERIMENT_SH" "$REQ6_INVALID_CONFIG" --dry-run

    # RED PHASE: the runner currently accepts request=1000m;limit=500m (exit 0).
    # Request<=limit validation is unmet until run-experiment.sh/common.sh
    # validate it.
    [ "$status" -ne 0 ]
    # The clear error must name the offending combo (request and limit keys)
    [[ "$output" == *"request"* ]]
    [[ "$output" == *"limit"* ]]
    [[ "$output" == *"xceed"* || "$output" == *"nvalid"* || "$output" == *"rror"* ]]
}

@test "config with request > limit is rejected" {
    # Companion to the failing-acceptance test. Once request<=limit validation
    # landed, the same fixture is rejected by both tests. This assertion was
    # FLIPPED from exit-0 to exit-non-zero when the validation landed.
    run bash "$RUN_EXPERIMENT_SH" "$REQ6_INVALID_CONFIG" --dry-run

    [ "$status" -ne 0 ]
}

# =============================================================================
# Backward compatibility — the existing configs still validate. These are
# regression guards: they pass today and must stay green after the new configs
# land (the new configs must not break the runner).
# =============================================================================

@test "existing throttling-baseline.yaml dry-runs successfully" {
    run bash "$RUN_EXPERIMENT_SH" "$BASELINE_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Experiment: throttling-baseline"* ]]
    [[ "$output" == *"Matrix cells:"* ]]
    [[ "$output" == *"DRY RUN MODE"* ]]
}

@test "existing co-located.yaml dry-runs successfully" {
    run bash "$RUN_EXPERIMENT_SH" "$CO_LOCATED_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Experiment: co-located"* ]]
    [[ "$output" == *"Matrix cells:"* ]]
    [[ "$output" == *"DRY RUN MODE"* ]]
}

# =============================================================================
# FIX-3: TRUE Guaranteed pod support
#
# Kubernetes classifies a pod Guaranteed only when EVERY resource has
# request==limit. The stress-ng template sets CPU only, so request==limit CPU
# pods are Burstable. FIX-3 adds a guaranteed template whose MEMORY
# request==limit (128Mi/128Mi) is hardcoded while CPU request/limit stay
# matrix markers, and a workload type stress-ng-guaranteed mapping to it.
# =============================================================================

# ---------------------------------------------------------------------------
# The guaranteed template exists and hardcodes memory requests==limits
# (128Mi/128Mi) while keeping the CPU request/limit markers.
# ---------------------------------------------------------------------------

@test "guaranteed workload template exists at the pinned path" {
    [ -f "$GUARANTEED_TEMPLATE" ]
}

@test "guaranteed template hardcodes memory request==limit 128Mi and keeps CPU markers" {
    [ -f "$GUARANTEED_TEMPLATE" ]

    # Exactly two memory keys (one in requests, one in limits) and exactly two
    # 128Mi values — memory is hardcoded, not matrix-driven.
    [ "$(grep -cE '^[[:space:]]*memory:' "$GUARANTEED_TEMPLATE")" -eq 2 ]
    [ "$(grep -c '128Mi' "$GUARANTEED_TEMPLATE")" -eq 2 ]

    # CPU request/limit must stay matrix-driven so the matrix can supply equal
    # values (Guaranteed requires ALL resources request==limit).
    grep -q '{{CPU_REQUEST}}' "$GUARANTEED_TEMPLATE"
    grep -q '{{CPU_LIMIT}}' "$GUARANTEED_TEMPLATE"
}

# ---------------------------------------------------------------------------
# get_workload_template resolves the type stress-ng-guaranteed. Asserted via
# --dry-run of a temp config: the runner resolves every workload type at
# validation time, so an unknown type dies with "Unknown workload type" before
# any plan output (RED today).
# ---------------------------------------------------------------------------

@test "temp config with type stress-ng-guaranteed dry-runs and deploys the pod" {
    run bash "$RUN_EXPERIMENT_SH" "$GUARANTEED_TYPE_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    printf '%s\n' "$output" | grep -q 'Deployment 1/1: gpod (type: stress-ng-guaranteed'
}

@test "stress-ng-guaranteed type does not die on unknown type" {
    run bash "$RUN_EXPERIMENT_SH" "$GUARANTEED_TYPE_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    ! printf '%s\n' "$output" | grep -q 'Unknown workload type'
}

# ---------------------------------------------------------------------------
# qos-hierarchy.yaml's guaranteed pod switches to the guaranteed type; counts
# stay 2 cells / 3 deployments; dry-run stays exit 0.
# ---------------------------------------------------------------------------

@test "qos-hierarchy.yaml guaranteed pod uses type stress-ng-guaranteed, others stay stress-ng" {
    run bash "$RUN_EXPERIMENT_SH" "$QOS_HIERARCHY_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    printf '%s\n' "$output" | grep -q 'Deployment .*: guaranteed (type: stress-ng-guaranteed'
    printf '%s\n' "$output" | grep -q 'Deployment .*: burstable (type: stress-ng,'
    printf '%s\n' "$output" | grep -q 'Deployment .*: besteffort (type: stress-ng,'
}

@test "qos-hierarchy.yaml dry-run still reports 2 cells and 3 deployments" {
    run bash "$RUN_EXPERIMENT_SH" "$QOS_HIERARCHY_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Matrix cells: 2"* ]]
    local deploys
    deploys="$(printf '%s\n' "$output" | grep -cE 'Deployment [0-9]+/3:' 2>/dev/null || true)"
    [ "$deploys" -eq 3 ]
}

# =============================================================================
# The six NEW distribution configs.
#
# Family A: dist-api-server.yaml, dist-db-simulator.yaml, dist-cpu-burner.yaml,
#   dist-stress-ng.yaml — single-pod, each with the SAME 6-cell request/limit
#   matrix and the common measurement values (replicates 3, pre_warm 10,
#   duration 90, cooldown 10, cgroup_interval 5), node w1, plus the pinned
#   latency_load blocks.
# Family B: dist-weight-share.yaml — 3 stress-ng pods, 6 cells identical to the
#   existing weight-share.yaml.
# Family C: dist-qos-hierarchy.yaml — 3 QoS pods, 3 cells, hyphen-free pod
#   names, guaranteed pod uses type stress-ng-guaranteed.
# Each config dry-runs and prints exactly its pinned cells.
#
# Dry-run assertions are tolerant of endpoint-mapping/target-resolution
# ordering: they assert exit 0, the experiment name, the pinned matrix cells,
# and the config-declared latency_load block — never load-plan text owned by
# the load-plan tasks (endpoint URL mapping / latency target resolution). The
# single-pod latency_load block is asserted via the runner's own "Latency load
# generation (workload.params.latency_load)" dry-run section, which is
# config-driven and independent of those tasks.
# =============================================================================

# ---------------------------------------------------------------------------
# Family A: presence (RED until the configs exist)
# ---------------------------------------------------------------------------

@test "dist-api-server.yaml exists" {
    [ -f "$DIST_API_SERVER_CONFIG" ]
}

@test "dist-db-simulator.yaml exists" {
    [ -f "$DIST_DB_SIMULATOR_CONFIG" ]
}

@test "dist-cpu-burner.yaml exists" {
    [ -f "$DIST_CPU_BURNER_CONFIG" ]
}

@test "dist-stress-ng.yaml exists" {
    [ -f "$DIST_STRESS_NG_CONFIG" ]
}

# ---------------------------------------------------------------------------
# Family A: dry-run clean + common measurement values (replicates 3,
# pre_warm 10, duration 90, cooldown 10, cgroup_interval 5) + node w1.
# ---------------------------------------------------------------------------

@test "dist-api-server.yaml dry-runs with name and common measurement values" {
    run bash "$RUN_EXPERIMENT_SH" "$DIST_API_SERVER_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Experiment: dist-api-server"* ]]
    [[ "$output" == *"DRY RUN MODE"* ]]
    [[ "$output" == *"Replicates: 3"* ]]
    [[ "$output" == *"Pre-warm: 10s"* ]]
    [[ "$output" == *"Duration: 90s"* ]]
    [[ "$output" == *"Cooldown: 10s"* ]]
    [[ "$output" == *"Cgroup interval: 5s"* ]]
    printf '%s\n' "$output" | grep -q 'nodeName: w1'
}

@test "dist-db-simulator.yaml dry-runs with name and common measurement values" {
    run bash "$RUN_EXPERIMENT_SH" "$DIST_DB_SIMULATOR_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Experiment: dist-db-simulator"* ]]
    [[ "$output" == *"DRY RUN MODE"* ]]
    [[ "$output" == *"Replicates: 3"* ]]
    [[ "$output" == *"Pre-warm: 10s"* ]]
    [[ "$output" == *"Duration: 90s"* ]]
    [[ "$output" == *"Cooldown: 10s"* ]]
    [[ "$output" == *"Cgroup interval: 5s"* ]]
    printf '%s\n' "$output" | grep -q 'nodeName: w1'
}

@test "dist-cpu-burner.yaml dry-runs with name and common measurement values" {
    run bash "$RUN_EXPERIMENT_SH" "$DIST_CPU_BURNER_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Experiment: dist-cpu-burner"* ]]
    [[ "$output" == *"DRY RUN MODE"* ]]
    [[ "$output" == *"Replicates: 3"* ]]
    [[ "$output" == *"Pre-warm: 10s"* ]]
    [[ "$output" == *"Duration: 90s"* ]]
    [[ "$output" == *"Cooldown: 10s"* ]]
    [[ "$output" == *"Cgroup interval: 5s"* ]]
    printf '%s\n' "$output" | grep -q 'nodeName: w1'
}

@test "dist-stress-ng.yaml dry-runs with name and common measurement values" {
    run bash "$RUN_EXPERIMENT_SH" "$DIST_STRESS_NG_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Experiment: dist-stress-ng"* ]]
    [[ "$output" == *"DRY RUN MODE"* ]]
    [[ "$output" == *"Replicates: 3"* ]]
    [[ "$output" == *"Pre-warm: 10s"* ]]
    [[ "$output" == *"Duration: 90s"* ]]
    [[ "$output" == *"Cooldown: 10s"* ]]
    [[ "$output" == *"Cgroup interval: 5s"* ]]
    printf '%s\n' "$output" | grep -q 'nodeName: w1'
}

# ---------------------------------------------------------------------------
# Family A: exact matrix size (6 cells) — "prints exactly its pinned cells".
# ---------------------------------------------------------------------------

@test "dist-api-server.yaml dry-run prints 6 matrix cells" {
    run bash "$RUN_EXPERIMENT_SH" "$DIST_API_SERVER_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Matrix cells: 6"* ]]
}

@test "dist-db-simulator.yaml dry-run prints 6 matrix cells" {
    run bash "$RUN_EXPERIMENT_SH" "$DIST_DB_SIMULATOR_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Matrix cells: 6"* ]]
}

@test "dist-cpu-burner.yaml dry-run prints 6 matrix cells" {
    run bash "$RUN_EXPERIMENT_SH" "$DIST_CPU_BURNER_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Matrix cells: 6"* ]]
}

@test "dist-stress-ng.yaml dry-run prints 6 matrix cells" {
    run bash "$RUN_EXPERIMENT_SH" "$DIST_STRESS_NG_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Matrix cells: 6"* ]]
}

# ---------------------------------------------------------------------------
# Family A: exact pinned cell values — the shared 6-cell matrix table
# (none/none, 100m/100m, 100m/1000m, 500m/500m, 500m/2000m, 1000m/2000m),
# request-first key order (runner convention).
# ---------------------------------------------------------------------------

@test "dist-api-server.yaml prints the 6 pinned cells" {
    run bash "$RUN_EXPERIMENT_SH" "$DIST_API_SERVER_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    printf '%s\n' "$output" | grep -qF 'request=;limit='
    printf '%s\n' "$output" | grep -qF 'request=100m;limit=100m'
    printf '%s\n' "$output" | grep -qF 'request=100m;limit=1000m'
    printf '%s\n' "$output" | grep -qF 'request=500m;limit=500m'
    printf '%s\n' "$output" | grep -qF 'request=500m;limit=2000m'
    printf '%s\n' "$output" | grep -qF 'request=1000m;limit=2000m'
}

@test "dist-db-simulator.yaml prints the 6 pinned cells" {
    run bash "$RUN_EXPERIMENT_SH" "$DIST_DB_SIMULATOR_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    printf '%s\n' "$output" | grep -qF 'request=;limit='
    printf '%s\n' "$output" | grep -qF 'request=100m;limit=100m'
    printf '%s\n' "$output" | grep -qF 'request=100m;limit=1000m'
    printf '%s\n' "$output" | grep -qF 'request=500m;limit=500m'
    printf '%s\n' "$output" | grep -qF 'request=500m;limit=2000m'
    printf '%s\n' "$output" | grep -qF 'request=1000m;limit=2000m'
}

@test "dist-cpu-burner.yaml prints the 6 pinned cells" {
    run bash "$RUN_EXPERIMENT_SH" "$DIST_CPU_BURNER_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    printf '%s\n' "$output" | grep -qF 'request=;limit='
    printf '%s\n' "$output" | grep -qF 'request=100m;limit=100m'
    printf '%s\n' "$output" | grep -qF 'request=100m;limit=1000m'
    printf '%s\n' "$output" | grep -qF 'request=500m;limit=500m'
    printf '%s\n' "$output" | grep -qF 'request=500m;limit=2000m'
    printf '%s\n' "$output" | grep -qF 'request=1000m;limit=2000m'
}

@test "dist-stress-ng.yaml prints the 6 pinned cells" {
    run bash "$RUN_EXPERIMENT_SH" "$DIST_STRESS_NG_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    printf '%s\n' "$output" | grep -qF 'request=;limit='
    printf '%s\n' "$output" | grep -qF 'request=100m;limit=100m'
    printf '%s\n' "$output" | grep -qF 'request=100m;limit=1000m'
    printf '%s\n' "$output" | grep -qF 'request=500m;limit=500m'
    printf '%s\n' "$output" | grep -qF 'request=500m;limit=2000m'
    printf '%s\n' "$output" | grep -qF 'request=1000m;limit=2000m'
}

# ---------------------------------------------------------------------------
# Family A: latency_load blocks. Pinned via the runner's own dry-run latency
# section (config-driven). The rate is pinned ONLY for api-server (50 req/s
# there); db-simulator and cpu-burner pin their endpoint mixes.
# ---------------------------------------------------------------------------

@test "dist-api-server.yaml latency_load rate 50 with pinned endpoint mix" {
    run bash "$RUN_EXPERIMENT_SH" "$DIST_API_SERVER_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    printf '%s\n' "$output" | grep -q 'Pod: api-server (type: api-server)'
    printf '%s\n' "$output" | grep -q 'Latency load generation (workload.params.latency_load):'
    printf '%s\n' "$output" | grep -qF 'Rate: 50 req/s'
    printf '%s\n' "$output" | grep -qF 'Endpoints: users:30,orders:30,search:20,reports:20'
}

@test "dist-db-simulator.yaml latency_load with pinned db endpoint mix" {
    run bash "$RUN_EXPERIMENT_SH" "$DIST_DB_SIMULATOR_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    printf '%s\n' "$output" | grep -q 'Pod: db-simulator (type: db-simulator)'
    printf '%s\n' "$output" | grep -q 'Latency load generation (workload.params.latency_load):'
    printf '%s\n' "$output" | grep -qF 'Endpoints: select:40,insert:25,update:25,checkpoint:10'
}

@test "dist-cpu-burner.yaml latency_load with pinned burn endpoint mix" {
    run bash "$RUN_EXPERIMENT_SH" "$DIST_CPU_BURNER_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    printf '%s\n' "$output" | grep -q 'Pod: cpu-burner (type: cpu-burner)'
    printf '%s\n' "$output" | grep -q 'Latency load generation (workload.params.latency_load):'
    printf '%s\n' "$output" | grep -qF 'Endpoints: burn:100'
}

@test "dist-stress-ng.yaml is a saturating stress-ng workload, cores 2 load 100" {
    [ -f "$DIST_STRESS_NG_CONFIG" ]

    # params are not printed by --dry-run, so the saturating profile is pinned
    # from the config file itself.
    grep -qE '^\s+type: stress-ng' "$DIST_STRESS_NG_CONFIG"
    grep -qE '^\s+cores: 2' "$DIST_STRESS_NG_CONFIG"
    grep -qE '^\s+load: 100' "$DIST_STRESS_NG_CONFIG"

    run bash "$RUN_EXPERIMENT_SH" "$DIST_STRESS_NG_CONFIG" --dry-run
    [ "$status" -eq 0 ]
    printf '%s\n' "$output" | grep -q 'Pod: stress-ng (type: stress-ng)'
}

# ---------------------------------------------------------------------------
# Family B: dist-weight-share.yaml — 3 stress-ng pods, 6 cells identical to
# the existing weight-share.yaml.
# ---------------------------------------------------------------------------

@test "dist-weight-share.yaml exists" {
    [ -f "$DIST_WEIGHT_SHARE_CONFIG" ]
}

@test "dist-weight-share.yaml dry-runs with name, common values, node w1" {
    run bash "$RUN_EXPERIMENT_SH" "$DIST_WEIGHT_SHARE_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Experiment: dist-weight-share"* ]]
    [[ "$output" == *"DRY RUN MODE"* ]]
    [[ "$output" == *"Replicates: 3"* ]]
    [[ "$output" == *"Pre-warm: 10s"* ]]
    [[ "$output" == *"Duration: 90s"* ]]
    [[ "$output" == *"Cooldown: 10s"* ]]
    [[ "$output" == *"Cgroup interval: 5s"* ]]
    printf '%s\n' "$output" | grep -q 'nodeName: w1'
}

@test "dist-weight-share.yaml prints the 6 cells identical to weight-share.yaml" {
    run bash "$RUN_EXPERIMENT_SH" "$DIST_WEIGHT_SHARE_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Matrix cells: 6"* ]]
    printf '%s\n' "$output" | grep -qF 'a_request=500m;a_limit=;b_request=500m;b_limit=;c_request=;c_limit='
    printf '%s\n' "$output" | grep -qF 'a_request=250m;a_limit=;b_request=1000m;b_limit=;c_request=;c_limit='
    printf '%s\n' "$output" | grep -qF 'a_request=100m;a_limit=;b_request=500m;b_limit=;c_request=;c_limit='
    printf '%s\n' "$output" | grep -qF 'a_request=100m;a_limit=;b_request=1000m;b_limit=;c_request=;c_limit='
    printf '%s\n' "$output" | grep -qF 'a_request=800m;a_limit=;b_request=800m;b_limit=;c_request=;c_limit='
    printf '%s\n' "$output" | grep -qF 'a_request=200m;a_limit=;b_request=500m;b_limit=;c_request=;c_limit='
}

@test "dist-weight-share.yaml deploys 3 stress-ng pods pod-a/b/c" {
    run bash "$RUN_EXPERIMENT_SH" "$DIST_WEIGHT_SHARE_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [ "$(printf '%s\n' "$output" | grep -cE 'Deployment [0-9]+/3:' 2>/dev/null || true)" -eq 3 ]
    printf '%s\n' "$output" | grep -E 'Deployment [0-9]+/3:' | grep -q 'pod-a'
    printf '%s\n' "$output" | grep -E 'Deployment [0-9]+/3:' | grep -q 'pod-b'
    printf '%s\n' "$output" | grep -E 'Deployment [0-9]+/3:' | grep -q 'pod-c'
    printf '%s\n' "$output" | grep -E 'Deployment [0-9]+/3:' | grep -qE '\(type: stress-ng,'
}

# ---------------------------------------------------------------------------
# Family C: dist-qos-hierarchy.yaml — 3 QoS pods, 3 cells, hyphen-free pod
# names, guaranteed pod uses type stress-ng-guaranteed.
# ---------------------------------------------------------------------------

@test "dist-qos-hierarchy.yaml exists" {
    [ -f "$DIST_QOS_HIERARCHY_CONFIG" ]
}

@test "dist-qos-hierarchy.yaml dry-runs with name, common values, node w1" {
    run bash "$RUN_EXPERIMENT_SH" "$DIST_QOS_HIERARCHY_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Experiment: dist-qos-hierarchy"* ]]
    [[ "$output" == *"DRY RUN MODE"* ]]
    [[ "$output" == *"Replicates: 3"* ]]
    [[ "$output" == *"Pre-warm: 10s"* ]]
    [[ "$output" == *"Duration: 90s"* ]]
    [[ "$output" == *"Cooldown: 10s"* ]]
    [[ "$output" == *"Cgroup interval: 5s"* ]]
    printf '%s\n' "$output" | grep -q 'nodeName: w1'
}

@test "dist-qos-hierarchy.yaml prints the 3 pinned cells" {
    run bash "$RUN_EXPERIMENT_SH" "$DIST_QOS_HIERARCHY_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Matrix cells: 3"* ]]
    printf '%s\n' "$output" | grep -qF 'guaranteed_request=500m;guaranteed_limit=500m;burstable_request=500m;burstable_limit=2000m;besteffort_request=;besteffort_limit='
    printf '%s\n' "$output" | grep -qF 'guaranteed_request=1000m;guaranteed_limit=1000m;burstable_request=250m;burstable_limit=1000m;besteffort_request=;besteffort_limit='
    printf '%s\n' "$output" | grep -qF 'guaranteed_request=250m;guaranteed_limit=250m;burstable_request=250m;burstable_limit=1000m;besteffort_request=;besteffort_limit='
}

@test "dist-qos-hierarchy.yaml deploys 3 QoS pods, guaranteed uses stress-ng-guaranteed" {
    run bash "$RUN_EXPERIMENT_SH" "$DIST_QOS_HIERARCHY_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    printf '%s\n' "$output" | grep -q 'Deployment .*: guaranteed (type: stress-ng-guaranteed'
    printf '%s\n' "$output" | grep -q 'Deployment .*: burstable (type: stress-ng,'
    printf '%s\n' "$output" | grep -q 'Deployment .*: besteffort (type: stress-ng,'
    [ "$(printf '%s\n' "$output" | grep -cE 'Deployment [0-9]+/3:' 2>/dev/null || true)" -eq 3 ]
}
