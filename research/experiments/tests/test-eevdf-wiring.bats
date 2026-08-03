#!/usr/bin/env bats
# test-eevdf-wiring.bats — Tests for --eevdf flag in run-experiment.sh
#
# These tests encode the target behavior of TASK-005 (wiring EEVDF scheduler
# metric collection — eevdf-observe.sh JSON snapshots and cgroup-pid-watch.sh
# per-task time series — into the experiment runner behind a new --eevdf flag).
# They are written test-first: the REQ-1/REQ-3/REQ-4 tests FAIL (red phase)
# against the current runner (which rejects --eevdf as an unknown option),
# while the REQ-2/REQ-5-tool-contract/REQ-6 tests are regression guards that
# already pass and must stay green after TASK-005 lands.
#
# No running cluster is required — every assertion targets --dry-run
# stdout/stderr, exit codes, or the tooling's catchable-failure contract.
#
# Pod names pinned by the fixture manifests:
#   single-pod  throttling-baseline.yaml -> stress-ng
#   multi-pod   co-located.yaml          -> latency-sensitive, batch-burner
#
# Requirements covered (full mapping in TEST-DESIGN.md):
#   REQ-1 -> VC-EEVDF-01 (EEVDF-01, EEVDF-02, EEVDF-03)
#   REQ-2 -> VC-EEVDF-02 (EEVDF-04, EEVDF-05)
#   REQ-3 -> VC-EEVDF-03 (EEVDF-06, EEVDF-07, EEVDF-08)
#   REQ-4 -> VC-EEVDF-04 (EEVDF-09, EEVDF-10)
#   REQ-5 -> VC-EEVDF-05 (EEVDF-11, EEVDF-12)
#   REQ-6 -> VC-EEVDF-06 (EEVDF-13, EEVDF-14)
#
# Run from project root:
#   bats research/experiments/tests/test-eevdf-wiring.bats
#
# Run a specific test:
#   bats --filter "EEVDF-06" research/experiments/tests/test-eevdf-wiring.bats

setup() {
    export PROJECT_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../../.." && pwd -P)"
    export EXPERIMENTS_DIR="$PROJECT_ROOT/research/experiments"
    export RUN_EXPERIMENT_SH="$EXPERIMENTS_DIR/run-experiment.sh"
    export COMMON_SH="$EXPERIMENTS_DIR/common.sh"
    export BASELINE_CONFIG="$EXPERIMENTS_DIR/configs/throttling-baseline.yaml"
    export CO_LOCATED_CONFIG="$EXPERIMENTS_DIR/configs/co-located.yaml"
    export EEVDF_BIN_DIR="$PROJECT_ROOT/research/bin"
    export EEVDF_OBSERVE_SH="$EEVDF_BIN_DIR/eevdf-observe.sh"

    # Sanity checks on runner and pre-existing configs
    [ -f "$RUN_EXPERIMENT_SH" ] || { echo "FATAL: runner not found at $RUN_EXPERIMENT_SH" >&2; exit 1; }
    [ -f "$BASELINE_CONFIG" ] || { echo "FATAL: throttling-baseline.yaml not found" >&2; exit 1; }
    [ -f "$CO_LOCATED_CONFIG" ] || { echo "FATAL: co-located.yaml not found" >&2; exit 1; }
}

# =============================================================================
# VC-EEVDF-01 (REQ-1): --eevdf flag parses; --dry-run --eevdf succeeds and the
# output mentions EEVDF collection steps (per pod).
# =============================================================================

@test "EEVDF-01: --eevdf flag is accepted with --dry-run" {
    run bash "$RUN_EXPERIMENT_SH" "$BASELINE_CONFIG" --dry-run --eevdf

    # RED PHASE: this exits 1 (Unknown option: --eevdf) until TASK-005 lands
    [ "$status" -eq 0 ]
}

@test "EEVDF-02: --dry-run --eevdf output mentions EEVDF collection steps" {
    run bash "$RUN_EXPERIMENT_SH" "$BASELINE_CONFIG" --dry-run --eevdf

    [ "$status" -eq 0 ]
    # REQ-1: the plan must advertise the EEVDF collection step
    # (e.g. "eevdf-observe" or "EEVDF")
    printf '%s\n' "$output" | grep -qiE 'eevdf(-observe)?'
}

@test "EEVDF-03: --help output mentions --eevdf flag" {
    run bash "$RUN_EXPERIMENT_SH" --help

    [ "$status" -eq 0 ]
    [[ "$output" == *"--eevdf"* ]]
}

# =============================================================================
# VC-EEVDF-02 (REQ-2): Backward compatibility — without --eevdf the dry-run
# output does NOT claim EEVDF collection. Regression guards: they pass today
# and must stay green after TASK-005.
# =============================================================================

@test "EEVDF-04: single-pod dry-run without --eevdf succeeds and never mentions EEVDF" {
    run bash "$RUN_EXPERIMENT_SH" "$BASELINE_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"DRY RUN MODE"* ]]
    [[ "$output" == *"Matrix cells:"* ]]
    [[ "$output" == *"Prerequisites check passed"* ]]
    # REQ-2: zero EEVDF claims in the default path
    local mentions
    mentions="$(printf '%s' "$output" | grep -ci 'eevdf' 2>/dev/null || true)"
    [ "$mentions" -eq 0 ]
}

@test "EEVDF-05: multi-pod dry-run without --eevdf succeeds and never mentions EEVDF" {
    run bash "$RUN_EXPERIMENT_SH" "$CO_LOCATED_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Detected co-located experiment configuration"* ]]
    [[ "$output" == *"DRY RUN MODE"* ]]
    [[ "$output" == *"Matrix cells:"* ]]
    [[ "$output" == *"Prerequisites check passed"* ]]
    local mentions
    mentions="$(printf '%s' "$output" | grep -ci 'eevdf' 2>/dev/null || true)"
    [ "$mentions" -eq 0 ]
}

# =============================================================================
# VC-EEVDF-03 (REQ-3): Per-cell EEVDF artifacts are named in the dry-run plan —
# eevdf-<pod>.json snapshots and/or eevdf-<pod>-pids.csv time series in the
# cell dir. Per-pod naming is the invariant; which artifact types TASK-005
# prints is an either/or per the requirement's "e.g." wording.
# =============================================================================

@test "EEVDF-06: single-pod dry-run names the per-cell EEVDF artifact for stress-ng" {
    run bash "$RUN_EXPERIMENT_SH" "$BASELINE_CONFIG" --dry-run --eevdf

    [ "$status" -eq 0 ]
    # REQ-3: snapshot eevdf-stress-ng.json and/or time series
    # eevdf-stress-ng-pids.csv must be advertised for the cell
    printf '%s\n' "$output" | grep -qE 'eevdf-stress-ng(-pids)?\.(json|csv)'
}

@test "EEVDF-07: multi-pod dry-run names a per-cell EEVDF artifact for each pod" {
    run bash "$RUN_EXPERIMENT_SH" "$CO_LOCATED_CONFIG" --dry-run --eevdf

    [ "$status" -eq 0 ]
    # REQ-3: both co-located pods must get their own artifact name
    printf '%s\n' "$output" | grep -qE 'eevdf-latency-sensitive(-pids)?\.(json|csv)'
    printf '%s\n' "$output" | grep -qE 'eevdf-batch-burner(-pids)?\.(json|csv)'
}

@test "EEVDF-08: dry-run with --eevdf references EEVDF artifacts in cell metadata" {
    run bash "$RUN_EXPERIMENT_SH" "$BASELINE_CONFIG" --dry-run --eevdf

    [ "$status" -eq 0 ]
    # REQ-3: the plan records where the artifacts land (cell metadata.json)
    printf '%s\n' "$output" | grep -qi 'metadata'
    printf '%s\n' "$output" | grep -qi 'eevdf'
}

# =============================================================================
# VC-EEVDF-04 (REQ-4): --eevdf works for a single-pod config AND a multi-pod
# config, with per-pod EEVDF naming in both cases.
# =============================================================================

@test "EEVDF-09: --eevdf per-pod collection is advertised for the single pod (stress-ng)" {
    run bash "$RUN_EXPERIMENT_SH" "$BASELINE_CONFIG" --dry-run --eevdf

    [ "$status" -eq 0 ]
    # REQ-4: the single pod is the subject of an EEVDF collection step
    printf '%s\n' "$output" | grep -qi 'eevdf'
    printf '%s\n' "$output" | grep -q 'stress-ng'
}

@test "EEVDF-10: --eevdf per-pod collection is advertised for BOTH co-located pods" {
    run bash "$RUN_EXPERIMENT_SH" "$CO_LOCATED_CONFIG" --dry-run --eevdf

    [ "$status" -eq 0 ]
    # REQ-4: one EEVDF stream per pod, all pods covered
    local streams=0
    for pod in latency-sensitive batch-burner; do
        if printf '%s\n' "$output" | grep "$pod" | grep -qiE 'eevdf'; then
            streams=$((streams + 1))
        fi
    done
    [ "$streams" -eq 2 ]
}

# =============================================================================
# VC-EEVDF-05 (REQ-5): Graceful degradation — EEVDF collection failures are
# non-fatal (cell continues, warning logged).
#
# Without a live cluster the runner-side warn-and-continue branch cannot be
# driven to failure, so this VC pins the two strongest achievable contracts:
#   EEVDF-11 (green): the tool's failure mode is a catchable return code, not
#                     a hang or a hard process abort — the runner can guard it
#                     with `|| log WARNING`.
#   EEVDF-12 (red):   a guarded code path exists — common.sh (which the runner
#                     sources) exposes at least one eevdf availability/guard
#                     function that TASK-005 adds. Location pinned to common.sh
#                     by this contract (see TEST-DESIGN.md decision 4).
# =============================================================================

@test "EEVDF-11: eevdf-observe.sh fails non-fatally (catchable exit code, no hang) without a cluster" {
    # Force an unreachable cluster so the tool's failure path is deterministic
    # regardless of the host environment (mise may set KUBECONFIG).
    run timeout 30 env KUBECONFIG=/nonexistent-kubeconfig \
        bash "$EEVDF_OBSERVE_SH" nonexistent-pod

    # Failure must be a catchable non-zero exit (124 = timeout/hang -> fail)
    [ "$status" -ne 0 ]
    [ "$status" -ne 124 ]
    # The tool reports the failure instead of dying silently
    [[ "$output" == *"rror"* || "$output" == *"Missing"* || "$output" == *"annot reach"* ]]
}

@test "EEVDF-12: common.sh exposes an EEVDF guard function (guarded code path exists)" {
    # RED PHASE: common.sh currently defines no eevdf-related function. TASK-005
    # must add an availability guard (mirroring check_tracebox_available in
    # perfetto-common.sh / check_sched_debug_available in eevdf-common.sh).
    run bash -c "
        source '$COMMON_SH'
        declare -F | grep -ci eevdf || true
    "
    [ "$status" -eq 0 ]
    [ "$output" -ge 1 ]
}

# =============================================================================
# VC-EEVDF-06 (REQ-6): Unknown option handling — --eevdf=foo and misspelled
# flags still error. Regression guards: they pass today (verified) and must
# stay green after TASK-005 (the --eevdf case must stay exact/boolean).
# =============================================================================

@test "EEVDF-13: --eevdf=foo is rejected" {
    run bash "$RUN_EXPERIMENT_SH" "$BASELINE_CONFIG" --dry-run --eevdf=foo

    [ "$status" -ne 0 ]
    [[ "$output" == *"--eevdf"* ]]
    [[ "$output" == *"Unknown"* || "$output" == *"nvalid"* || "$output" == *"rror"* ]]
}

@test "EEVDF-14: misspelled --eevdfd is rejected" {
    run bash "$RUN_EXPERIMENT_SH" "$BASELINE_CONFIG" --dry-run --eevdfd

    [ "$status" -ne 0 ]
    [[ "$output" == *"--eevdfd"* ]]
    [[ "$output" == *"Unknown"* || "$output" == *"nvalid"* || "$output" == *"rror"* ]]
}
