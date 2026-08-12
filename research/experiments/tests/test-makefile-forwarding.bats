#!/usr/bin/env bats
# test-makefile-forwarding.bats — Tests for the research make-target contract
# after the series restructure: the repo-root experiment/interaction
# delegation targets are removed, the research/Makefile targets are
# namespaced cpu-sched-*, and the research root becomes a pure aggregator
# that includes series makefiles.
#
# Sections:
#   forwarding target names     — repo-root removal + research-level rename
#   interaction-report pipeline — analyzer listing, hermetic, idempotent
#   dist target flags           — renamed dist experiment/analysis/aggregation
#   aggregator help listing     — namespaced help contract + old-name failures
#
# RED NOW (pre-move): the suite is written for its post-move location
# (research/cpu-sched/experiments/tests/), so at the current tree location the
# four-level PROJECT_ROOT resolves one level ABOVE the repo root. Until the
# tree moves and the rename lands (repo-root targets removed, research targets
# renamed cpu-sched-*, research/Makefile converted to an aggregator), the
# suite is red through a path/target mix: the renamed-target and aggregator
# tests fail on missing paths/targets (e.g. "make: *** research: No such file
# or directory"), while the failure-assertion tests pass vacuously because
# make runs in a Makefile-less directory. The meaningful contract-inversion
# failures — renamed targets resolving to their recipes, old names failing
# with `No rule to make target` — appear only after the tree moves into the
# cpu-sched series directory and the rename lands. These tests pin the
# post-restructure contract and move with the tree.
#
# No cluster required. Every recipe assertion uses `make -n` (dry-run), which
# prints the recipes that would run WITHOUT executing them. GNU make recurses
# through `$(MAKE)` invocations and prerequisite lists even in dry-run mode, so
# a single invocation exercises the whole dependency chain.
# `make -C research help` is a read-only grep pipeline over the Makefile text.
#
# Covered behaviors:
#   former root delegations fail at the repo root
#   renamed experiment targets reach the runner and their family config
#   old research target names fail after the rename
#   interaction-report pipeline lists analyzers + report generator
#   interaction-report reproducible/idempotent by construction
#   renamed dist experiment targets pin configs and the three capture flags
#   renamed dist analysis targets invoke their scripts via python3
#   cpu-sched-dist-all reaches every family config in order
#   aggregator help lists namespaced targets and no bare target lines
#   old target names and removed root delegations fail via make -n only
#
# Run from repo root:
#   bats research/cpu-sched/experiments/tests/test-makefile-forwarding.bats

setup() {
    export PROJECT_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../../../.." && pwd -P)"
    cd "$PROJECT_ROOT"
    export RESEARCH_DIR="$PROJECT_ROOT/research"

    # The seven experiment/interaction targets that used to be delegated from
    # the repo-root Makefile. Post-restructure they are removed at the root
    # and renamed cpu-sched-* inside research/.
    ROOT_REMOVED_TARGETS=(
        experiment-weight-share
        experiment-request-limit-matrix
        experiment-qos-hierarchy
        experiment-latency-interference
        experiment-cpu-burst
        experiment-tunables-contention
        interaction-report
    )

    # The six renamed experiment targets. Order must match
    # RENAMED_EXPERIMENT_CONFIGS below.
    RENAMED_EXPERIMENT_TARGETS=(
        cpu-sched-experiment-weight-share
        cpu-sched-experiment-request-limit-matrix
        cpu-sched-experiment-qos-hierarchy
        cpu-sched-experiment-latency-interference
        cpu-sched-experiment-cpu-burst
        cpu-sched-experiment-tunables-contention
    )

    # Config each renamed target must reach (proves the delegation is real,
    # not a stub). Basenames are unique across the configs dir.
    RENAMED_EXPERIMENT_CONFIGS=(
        weight-share.yaml
        request-limit-matrix.yaml
        qos-hierarchy.yaml
        latency-interference.yaml
        cpu-burst.yaml
        tunables-contention.yaml
    )

    # The old (pre-rename) names of the six experiment targets.
    OLD_EXPERIMENT_TARGETS=(
        experiment-weight-share
        experiment-request-limit-matrix
        experiment-qos-hierarchy
        experiment-latency-interference
        experiment-cpu-burst
        experiment-tunables-contention
    )

    # The five analyzers + report generator the interaction-report target
    # must invoke (the interaction-report rerun pipeline), by basename.
    REPORT_SCRIPTS=(
        weight-share-analyze.py
        interaction-heatmap.py
        qos-analyze.py
        latency-analyze.py
        tunables-analyze.py
        generate-report.py
    )

    # The six renamed dist experiment families.
    # Order must match RENAMED_DIST_EXPERIMENT_CONFIGS and is the family
    # order (Family A api/db/burner/stress, then B weight, then C qos).
    RENAMED_DIST_EXPERIMENT_TARGETS=(
        cpu-sched-experiment-dist-api
        cpu-sched-experiment-dist-db
        cpu-sched-experiment-dist-burner
        cpu-sched-experiment-dist-stress
        cpu-sched-experiment-dist-weight
        cpu-sched-experiment-dist-qos
    )

    # Config each renamed dist target must reach (file names; the
    # Makefile forwarding is what this section pins, not the config files
    # themselves, which are separate deliverables).
    RENAMED_DIST_EXPERIMENT_CONFIGS=(
        dist-api-server.yaml
        dist-db-simulator.yaml
        dist-cpu-burner.yaml
        dist-stress-ng.yaml
        dist-weight-share.yaml
        dist-qos-hierarchy.yaml
    )

    # Every dist experiment target must pass ALL THREE flags (each
    # passing `--eevdf --perfetto --perfetto-config eevdf-deep`).
    DIST_PERFETTO_CONFIG_VALUE="eevdf-deep"

    # The five renamed analysis targets and the scripts they must invoke, by
    # basename. NOTE: cpu-sched-dist-plots must invoke dist-plot.py
    # (singular), not dist-plots.py.
    RENAMED_DIST_ANALYSIS_TARGETS=(
        cpu-sched-dist-analyze
        cpu-sched-dist-plots
        cpu-sched-dist-gif
        cpu-sched-dist-steps
        cpu-sched-dist-report
    )

    RENAMED_DIST_ANALYSIS_SCRIPTS=(
        dist-analyze.py
        dist-plot.py
        dist-gif.py
        dist-steps.py
        dist-report.py
    )

    # The namespaced targets the aggregator help must list.
    AGGREGATOR_HELP_TARGETS=(
        cpu-sched-experiment-baseline
        cpu-sched-dist-analyze
        cpu-sched-eevdf-analyze
        cpu-sched-perfetto-view
        cpu-sched-clean
        cpu-sched-setup
        cpu-sched-test
    )

    # Bare (unprefixed) target names that must NOT appear as help lines once
    # every target carries the cpu-sched- namespace.
    BARE_TARGET_NAMES=(
        experiment-baseline
        dist-analyze
        perfetto-view
        clean
    )

    # Old research target names that must fail after the rename.
    OLD_RESEARCH_TARGETS=(
        experiment-baseline
        dist-analyze
        interaction-report
        validate-configs
    )
}

assert_output_contains() {
    local needle="$1"
    if [[ "$output" == *"$needle"* ]]; then
        return 0
    fi
    echo "expected dry-run output to contain: $needle" >&2
    echo "--- actual output ---" >&2
    echo "$output" >&2
    return 1
}

assert_output_not_word() {
    local word="$1"
    if echo "$output" | grep -qw "$word"; then
        echo "unexpected word '$word' in interaction-report dry-run output" >&2
        echo "--- actual output ---" >&2
        echo "$output" >&2
        return 1
    fi
    return 0
}

# Assert $output contains the standalone `--perfetto` flag. A naive substring
# check is not enough: `--perfetto` is a prefix of `--perfetto-config`, so
# `*"$output"*--perfetto*` would pass even if only --perfetto-config appears.
# This regex requires the char after --perfetto to be a non-dash (or EOL),
# i.e. exactly the standalone flag.
assert_output_has_standalone_perfetto() {
    if ! echo "$output" | grep -E -- '--perfetto([^-]|$)' >/dev/null; then
        echo "expected standalone --perfetto flag in dry-run output" >&2
        echo "--- actual output ---" >&2
        echo "$output" >&2
        return 1
    fi
    return 0
}

# Assert each needle appears in $output and their FIRST occurrences are in
# the given order (strictly increasing positions). Used to pin that dist-all
# runs the families sequentially, regardless of whether the recipe uses
# `$(MAKE)` recursion, direct runner invocations, or prerequisite lists —
# all three render the recipe lines in execution order under `make -n`.
assert_first_occurrences_in_order() {
    local prev=-1
    local needle prefix pos
    for needle in "$@"; do
        prefix="${output%%"$needle"*}"
        if [ "$prefix" = "$output" ]; then
            echo "expected output to contain: $needle" >&2
            echo "--- actual output ---" >&2
            echo "$output" >&2
            return 1
        fi
        pos="${#prefix}"
        if [ "$pos" -le "$prev" ]; then
            echo "out-of-order first occurrence of: $needle (pos $pos, after $prev)" >&2
            echo "--- actual output ---" >&2
            echo "$output" >&2
            return 1
        fi
        prev="$pos"
    done
    return 0
}

# Assert $output contains NO help line whose target name is exactly $name.
# A namespaced line (cpu-sched-<name>) must NOT satisfy this check — only a
# line that STARTS with the bare name (followed by whitespace/colon/dot/EOL)
# counts as a bare target line.
assert_no_bare_target_line() {
    local name="$1"
    if echo "$output" | grep -E "^[[:space:]]*${name}([[:space:]:.]|\$)" >/dev/null; then
        echo "help output still lists a bare target line: $name" >&2
        echo "--- actual output ---" >&2
        echo "$output" >&2
        return 1
    fi
    return 0
}

# --- Forwarding target names ---
#
# Post-restructure contract: the seven experiment/interaction targets are
# removed from the repo-root Makefile and renamed cpu-sched-* in research/.
# These assertions invert the pre-restructure suite (which pinned the root
# delegations) and are RED until the restructure lands.

@test "repo-root make -n experiment-weight-share fails (target removed)" {
    run make -n experiment-weight-share
    [ "$status" -ne 0 ]
}

@test "repo-root make -n interaction-report fails (target removed)" {
    run make -n interaction-report
    [ "$status" -ne 0 ]
}

@test "all seven former root delegations fail at the repo root" {
    for target in "${ROOT_REMOVED_TARGETS[@]}"; do
        run make -n "$target"
        [ "$status" -ne 0 ] || {
            echo "make -n $target still resolves at the repo root (status $status)" >&2
            echo "$output" >&2
            return 1
        }
    done
}

@test "make -n -C research cpu-sched-experiment-weight-share reaches the runner and family config" {
    run make -n -C research cpu-sched-experiment-weight-share
    [ "$status" -eq 0 ]
    assert_output_contains "run-experiment.sh"
    assert_output_contains "weight-share.yaml"
}

@test "each renamed experiment target reaches its family config" {
    for i in "${!RENAMED_EXPERIMENT_TARGETS[@]}"; do
        local target="${RENAMED_EXPERIMENT_TARGETS[$i]}"
        local config="${RENAMED_EXPERIMENT_CONFIGS[$i]}"
        run make -n -C research "$target"
        [ "$status" -eq 0 ] || {
            echo "make -n -C research $target failed (status $status)" >&2
            echo "$output" >&2
            return 1
        }
        assert_output_contains "$config"
    done
}

@test "make -n -C research experiment-weight-share fails (renamed away)" {
    run make -n -C research experiment-weight-share
    [ "$status" -ne 0 ]
}

@test "all six old research experiment names fail after the rename" {
    for target in "${OLD_EXPERIMENT_TARGETS[@]}"; do
        run make -n -C research "$target"
        [ "$status" -ne 0 ] || {
            echo "make -n -C research $target still resolves (status $status)" >&2
            echo "$output" >&2
            return 1
        }
    done
}

# --- Interaction-report pipeline ---
#
# The renamed cpu-sched-interaction-report target must invoke the five
# analyzers plus the report generator, from staged data only (no cluster, no
# network, no timestamps: rerunning with the same data is byte-identical).

@test "make -n -C research cpu-sched-interaction-report lists analyzers + report generator" {
    run make -n -C research cpu-sched-interaction-report
    [ "$status" -eq 0 ]
    for script in "${REPORT_SCRIPTS[@]}"; do
        assert_output_contains "$script"
    done
}

@test "interaction-report recipe has no network or timestamp commands" {
    run make -n -C research cpu-sched-interaction-report
    [ "$status" -eq 0 ]
    assert_output_not_word "curl"
    assert_output_not_word "wget"
    assert_output_not_word "date"
}

@test "interaction-report dry-run is byte-identical across two runs" {
    run make -n -C research cpu-sched-interaction-report
    [ "$status" -eq 0 ]
    local first_output="$output"
    run make -n -C research cpu-sched-interaction-report
    [ "$status" -eq 0 ]
    [ "$first_output" = "$output" ]
}

# --- Dist target flags ---
#
# The renamed dist experiment targets (cpu-sched-experiment-dist-*) must
# forward their family config to the shared runner with full capture
# (--eevdf plus standalone --perfetto plus --perfetto-config eevdf-deep).
# The renamed analysis targets (cpu-sched-dist-*) invoke their scripts via
# python3; cpu-sched-dist-plots invokes dist-plot.py (singular).

@test "all six cpu-sched-experiment-dist-* targets exist at research level" {
    for target in "${RENAMED_DIST_EXPERIMENT_TARGETS[@]}"; do
        run make -n -C research "$target"
        [ "$status" -eq 0 ] || {
            echo "make -n -C research $target failed (status $status)" >&2
            echo "$output" >&2
            return 1
        }
    done
}

@test "each cpu-sched-experiment-dist-* target reaches its family config" {
    for i in "${!RENAMED_DIST_EXPERIMENT_TARGETS[@]}"; do
        local target="${RENAMED_DIST_EXPERIMENT_TARGETS[$i]}"
        local config="${RENAMED_DIST_EXPERIMENT_CONFIGS[$i]}"
        run make -n -C research "$target"
        [ "$status" -eq 0 ] || {
            echo "make -n -C research $target failed (status $status)" >&2
            echo "$output" >&2
            return 1
        }
        assert_output_contains "$config"
    done
}

@test "each cpu-sched-experiment-dist-* target passes --eevdf" {
    for target in "${RENAMED_DIST_EXPERIMENT_TARGETS[@]}"; do
        run make -n -C research "$target"
        [ "$status" -eq 0 ] || {
            echo "make -n -C research $target failed (status $status)" >&2
            echo "$output" >&2
            return 1
        }
        assert_output_contains "--eevdf"
    done
}

@test "each cpu-sched-experiment-dist-* target passes standalone --perfetto" {
    # Uses the regex helper: `--perfetto` must appear NOT followed by `-`,
    # otherwise a recipe that only has --perfetto-config would falsely pass.
    for target in "${RENAMED_DIST_EXPERIMENT_TARGETS[@]}"; do
        run make -n -C research "$target"
        [ "$status" -eq 0 ] || {
            echo "make -n -C research $target failed (status $status)" >&2
            echo "$output" >&2
            return 1
        }
        assert_output_has_standalone_perfetto
    done
}

@test "each cpu-sched-experiment-dist-* target passes --perfetto-config eevdf-deep" {
    for target in "${RENAMED_DIST_EXPERIMENT_TARGETS[@]}"; do
        run make -n -C research "$target"
        [ "$status" -eq 0 ] || {
            echo "make -n -C research $target failed (status $status)" >&2
            echo "$output" >&2
            return 1
        }
        assert_output_contains "--perfetto-config $DIST_PERFETTO_CONFIG_VALUE"
    done
}

@test "each cpu-sched-experiment-dist-* target invokes run-experiment.sh" {
    # Proves the forwarding is real (reaches the runner), not a stub echo.
    for target in "${RENAMED_DIST_EXPERIMENT_TARGETS[@]}"; do
        run make -n -C research "$target"
        [ "$status" -eq 0 ] || {
            echo "make -n -C research $target failed (status $status)" >&2
            echo "$output" >&2
            return 1
        }
        assert_output_contains "run-experiment.sh"
    done
}

@test "all five cpu-sched-dist analysis targets exist at research level" {
    for target in "${RENAMED_DIST_ANALYSIS_TARGETS[@]}"; do
        run make -n -C research "$target"
        [ "$status" -eq 0 ] || {
            echo "make -n -C research $target failed (status $status)" >&2
            echo "$output" >&2
            return 1
        }
    done
}

@test "each cpu-sched-dist analysis target invokes its script via python3" {
    # Script names are pinned per analysis target. NOTE: cpu-sched-dist-plots
    # must invoke dist-plot.py (singular) — a dist-plots.py would fail here.
    for i in "${!RENAMED_DIST_ANALYSIS_TARGETS[@]}"; do
        local target="${RENAMED_DIST_ANALYSIS_TARGETS[$i]}"
        local script="${RENAMED_DIST_ANALYSIS_SCRIPTS[$i]}"
        run make -n -C research "$target"
        [ "$status" -eq 0 ] || {
            echo "make -n -C research $target failed (status $status)" >&2
            echo "$output" >&2
            return 1
        }
        assert_output_contains "python3"
        assert_output_contains "$script"
    done
}

@test "cpu-sched-dist-all dry-run reaches every family config in sequence" {
    run make -n -C research cpu-sched-dist-all
    [ "$status" -eq 0 ] || {
        echo "make -n -C research cpu-sched-dist-all failed (status $status)" >&2
        echo "$output" >&2
        return 1
    }
    # All six family configs must appear, in the family order
    # (Family A api/db/burner/stress, then B weight, then C qos).
    assert_first_occurrences_in_order "${RENAMED_DIST_EXPERIMENT_CONFIGS[@]}"
}

@test "cpu-sched-dist-all passes the three required flags for every family" {
    run make -n -C research cpu-sched-dist-all
    [ "$status" -eq 0 ] || {
        echo "make -n -C research cpu-sched-dist-all failed (status $status)" >&2
        echo "$output" >&2
        return 1
    }
    # The dry-run renders all six family recipes, each with the three flags;
    # assert the flags appear at least once each (completeness per family is
    # pinned per-target above; here we prove dist-all inherits them).
    assert_output_contains "--eevdf"
    assert_output_has_standalone_perfetto
    assert_output_contains "--perfetto-config $DIST_PERFETTO_CONFIG_VALUE"
    assert_output_contains "run-experiment.sh"
}

@test "cpu-sched-validate-configs is wired to run-experiment.sh --dry-run" {
    # The dry-run validation gate must survive the rename. Dry-run only — the
    # dist configs are separate deliverables and their presence is pinned
    # elsewhere.
    run make -n -C research cpu-sched-validate-configs
    [ "$status" -eq 0 ]
    assert_output_contains "run-experiment.sh"
    assert_output_contains "--dry-run"
}

# --- Aggregator help listing ---
#
# The research/Makefile becomes a pure aggregator: every target lives in a
# series makefile with a cpu-sched- prefix, and `make -C research help`
# (a read-only grep pipeline over the Makefile text) must list the namespaced
# targets and NOT the bare (unprefixed) ones. Old names fail with
# `No rule to make target`, and the removed repo-root delegations fail via
# `make -n` from the repo root.

@test "research help lists the namespaced targets" {
    run make -C research help
    [ "$status" -eq 0 ]
    for target in "${AGGREGATOR_HELP_TARGETS[@]}"; do
        assert_output_contains "$target"
    done
}

@test "research help lists the renamed dist targets" {
    run make -C research help
    [ "$status" -eq 0 ]
    for target in "${RENAMED_DIST_EXPERIMENT_TARGETS[@]}" "${RENAMED_DIST_ANALYSIS_TARGETS[@]}"; do
        assert_output_contains "$target"
    done
}

@test "research help contains no bare target lines" {
    run make -C research help
    [ "$status" -eq 0 ]
    for name in "${BARE_TARGET_NAMES[@]}"; do
        assert_no_bare_target_line "$name"
    done
}

@test "old research target names fail at the research level" {
    # make -n only: a bare `make -C research experiment-baseline` would execute
    # the live runner recipe in the pre-restructure tree. Dry-run keeps every
    # target-failure assertion free of real recipe execution.
    for target in "${OLD_RESEARCH_TARGETS[@]}"; do
        run make -n -C research "$target"
        [ "$status" -ne 0 ] || {
            echo "make -n -C research $target still resolves (status $status)" >&2
            echo "$output" >&2
            return 1
        }
    done
}

@test "repo-root make -n -C . experiment-weight-share fails" {
    run make -n -C . experiment-weight-share
    [ "$status" -ne 0 ]
}

@test "repo-root make -n -C . interaction-report fails" {
    run make -n -C . interaction-report
    [ "$status" -ne 0 ]
}
