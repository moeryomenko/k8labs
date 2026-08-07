#!/usr/bin/env bats
# test-makefile-forwarding.bats — Tests for root Makefile forwarding
# targets, the interaction-report pipeline target, and research/Makefile
# dist targets (EEVDF execution-distribution families).
#
# Sections:
#   MF-*  — root forwarding + interaction-report
#   DST-* — dist targets in research/Makefile. RED now: the research/Makefile
#           has no experiment-dist-*, dist-*, or dist-all targets today. The
#           dist tests PASS once the engineer adds them; the forwarding tests
#           are regression guards that must stay green.
#
# No cluster required. Every assertion uses `make -n` (dry-run), which prints
# the recipes that would run WITHOUT executing them. GNU make recurses through
# `$(MAKE)` invocations even in dry-run mode (verified: `make -n` on a
# forwarding target prints the delegation line AND the delegated sub-make's
# recipe lines), so a single root invocation exercises both Makefiles.
# `make -C research help` is a read-only grep pipeline over the Makefile text.
#
# Covered behaviors:
#   root forwarding targets delegate to research
#   interaction-report pipeline lists analyzers + report generator
#   help target unchanged + lists new targets
#   research-Makefile-only invocation backward compat
#   interaction-report reproducible/idempotent by construction
#   dist experiment and analysis targets in research/Makefile
#
# Run from repo root:
#   bats research/experiments/tests/test-makefile-forwarding.bats

setup() {
    export PROJECT_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../../.." && pwd -P)"
    cd "$PROJECT_ROOT"
    export RESEARCH_DIR="$PROJECT_ROOT/research"

    # The six experiment families this task forwards from the root Makefile.
    # Order must match EXPERIMENT_CONFIGS below.
    EXPERIMENT_TARGETS=(
        experiment-weight-share
        experiment-request-limit-matrix
        experiment-qos-hierarchy
        experiment-latency-interference
        experiment-cpu-burst
        experiment-tunables-contention
    )

    # Config each target must reach (proves the delegation is real, not a
    # stub). Basenames are unique across the configs dir.
    EXPERIMENT_CONFIGS=(
        weight-share.yaml
        request-limit-matrix.yaml
        qos-hierarchy.yaml
        latency-interference.yaml
        cpu-burst.yaml
        tunables-contention.yaml
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

    # The six dist experiment families in research/Makefile.
    # Order must match DIST_EXPERIMENT_CONFIGS and is the spec order
    # (Family A api/db/burner/stress, then B weight, then C qos).
    DIST_EXPERIMENT_TARGETS=(
        experiment-dist-api
        experiment-dist-db
        experiment-dist-burner
        experiment-dist-stress
        experiment-dist-weight
        experiment-dist-qos
    )

    # Config each dist target must reach (file names; the
    # Makefile forwarding is what this section pins, not the config files
    # themselves, which are separate deliverables).
    DIST_EXPERIMENT_CONFIGS=(
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

    # The five analysis targets in research/Makefile and
    # the scripts they must invoke, by basename. NOTE: the dist-plots target
    # must invoke dist-plot.py (singular), not dist-plots.py.
    DIST_ANALYSIS_TARGETS=(
        dist-analyze
        dist-plots
        dist-gif
        dist-steps
        dist-report
    )

    DIST_ANALYSIS_SCRIPTS=(
        dist-analyze.py
        dist-plot.py
        dist-gif.py
        dist-steps.py
        dist-report.py
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

# --- Root forwarding targets ---

@test "root make -n experiment-weight-share delegates to research" {
    run make -n experiment-weight-share
    [ "$status" -eq 0 ]
    assert_output_contains "make -C research experiment-weight-share"
    assert_output_contains "run-experiment.sh"
    assert_output_contains "weight-share.yaml"
}

@test "root make -n experiment-request-limit-matrix delegates to research" {
    run make -n experiment-request-limit-matrix
    [ "$status" -eq 0 ]
    assert_output_contains "make -C research experiment-request-limit-matrix"
    assert_output_contains "run-experiment.sh"
    assert_output_contains "request-limit-matrix.yaml"
}

@test "all six experiment targets exist at root and delegate" {
    for i in "${!EXPERIMENT_TARGETS[@]}"; do
        local target="${EXPERIMENT_TARGETS[$i]}"
        run make -n "$target"
        [ "$status" -eq 0 ] || {
            echo "make -n $target failed (status $status)" >&2
            echo "$output" >&2
            return 1
        }
        assert_output_contains "make -C research $target"
    done
}

@test "each forwarded target reaches its family config" {
    for i in "${!EXPERIMENT_TARGETS[@]}"; do
        local target="${EXPERIMENT_TARGETS[$i]}"
        local config="${EXPERIMENT_CONFIGS[$i]}"
        run make -n "$target"
        [ "$status" -eq 0 ] || {
            echo "make -n $target failed (status $status)" >&2
            echo "$output" >&2
            return 1
        }
        assert_output_contains "$config"
    done
}

# --- Interaction-report pipeline listing ---

@test "root make -n interaction-report lists analyzers + report generator" {
    run make -n interaction-report
    [ "$status" -eq 0 ]
    assert_output_contains "make -C research interaction-report"
    for script in "${REPORT_SCRIPTS[@]}"; do
        assert_output_contains "$script"
    done
}

@test "research make -n interaction-report lists analyzers + report generator" {
    run make -n -C research interaction-report
    [ "$status" -eq 0 ]
    for script in "${REPORT_SCRIPTS[@]}"; do
        assert_output_contains "$script"
    done
}

# --- Help target unchanged + lists new targets ---

@test "root make -n help still exits 0" {
    run make -n help
    [ "$status" -eq 0 ]
}

@test "root make help lists experiment-weight-share and interaction-report" {
    # `make help` runs a read-only grep/sort/awk pipeline over the Makefile
    # text — safe, no cluster, no network. (`make -n help` only prints the
    # recipe lines, so the listing assertion must run the real help output.)
    run make help
    [ "$status" -eq 0 ]
    assert_output_contains "experiment-weight-share"
    assert_output_contains "interaction-report"
}

# --- Research-Makefile-only invocation backward compat ---

@test "make -n -C research experiment-weight-share still works" {
    run make -n -C research experiment-weight-share
    [ "$status" -eq 0 ]
    assert_output_contains "run-experiment.sh"
    assert_output_contains "weight-share.yaml"
}

@test "all six research-Makefile-only invocations still work" {
    for i in "${!EXPERIMENT_TARGETS[@]}"; do
        local target="${EXPERIMENT_TARGETS[$i]}"
        local config="${EXPERIMENT_CONFIGS[$i]}"
        run make -n -C research "$target"
        [ "$status" -eq 0 ] || {
            echo "make -n -C research $target failed (status $status)" >&2
            echo "$output" >&2
            return 1
        }
        assert_output_contains "$config"
    done
}

# --- Interaction-report reproducible/idempotent by construction ---

@test "interaction-report recipe has no network or timestamp commands" {
    run make -n -C research interaction-report
    [ "$status" -eq 0 ]
    assert_output_not_word "curl"
    assert_output_not_word "wget"
    assert_output_not_word "date"
}

@test "interaction-report dry-run is byte-identical across two runs" {
    run make -n -C research interaction-report
    [ "$status" -eq 0 ]
    local first_output="$output"
    run make -n -C research interaction-report
    [ "$status" -eq 0 ]
    [ "$first_output" = "$output" ]
}

# --- Dist experiment targets ---
#
# RED phase: research/Makefile has no experiment-dist-* targets today, so
# the dist tests FAIL until the engineer adds them. The validate-configs test
# is a regression guard that must stay green.

@test "all six experiment-dist-* targets exist at research level" {
    for target in "${DIST_EXPERIMENT_TARGETS[@]}"; do
        run make -n -C research "$target"
        [ "$status" -eq 0 ] || {
            echo "make -n -C research $target failed (status $status)" >&2
            echo "$output" >&2
            return 1
        }
    done
}

@test "each experiment-dist-* target reaches its family config" {
    for i in "${!DIST_EXPERIMENT_TARGETS[@]}"; do
        local target="${DIST_EXPERIMENT_TARGETS[$i]}"
        local config="${DIST_EXPERIMENT_CONFIGS[$i]}"
        run make -n -C research "$target"
        [ "$status" -eq 0 ] || {
            echo "make -n -C research $target failed (status $status)" >&2
            echo "$output" >&2
            return 1
        }
        assert_output_contains "$config"
    done
}

@test "each experiment-dist-* target passes --eevdf" {
    for target in "${DIST_EXPERIMENT_TARGETS[@]}"; do
        run make -n -C research "$target"
        [ "$status" -eq 0 ] || {
            echo "make -n -C research $target failed (status $status)" >&2
            echo "$output" >&2
            return 1
        }
        assert_output_contains "--eevdf"
    done
}

@test "each experiment-dist-* target passes standalone --perfetto" {
    # Uses the regex helper: `--perfetto` must appear NOT followed by `-`,
    # otherwise a recipe that only has --perfetto-config would falsely pass.
    for target in "${DIST_EXPERIMENT_TARGETS[@]}"; do
        run make -n -C research "$target"
        [ "$status" -eq 0 ] || {
            echo "make -n -C research $target failed (status $status)" >&2
            echo "$output" >&2
            return 1
        }
        assert_output_has_standalone_perfetto
    done
}

@test "each experiment-dist-* target passes --perfetto-config eevdf-deep" {
    for target in "${DIST_EXPERIMENT_TARGETS[@]}"; do
        run make -n -C research "$target"
        [ "$status" -eq 0 ] || {
            echo "make -n -C research $target failed (status $status)" >&2
            echo "$output" >&2
            return 1
        }
        assert_output_contains "--perfetto-config $DIST_PERFETTO_CONFIG_VALUE"
    done
}

@test "each experiment-dist-* target invokes run-experiment.sh" {
    # Proves the forwarding is real (reaches the runner), not a stub echo.
    for target in "${DIST_EXPERIMENT_TARGETS[@]}"; do
        run make -n -C research "$target"
        [ "$status" -eq 0 ] || {
            echo "make -n -C research $target failed (status $status)" >&2
            echo "$output" >&2
            return 1
        }
        assert_output_contains "run-experiment.sh"
    done
}

@test "all five dist analysis targets exist at research level" {
    for target in "${DIST_ANALYSIS_TARGETS[@]}"; do
        run make -n -C research "$target"
        [ "$status" -eq 0 ] || {
            echo "make -n -C research $target failed (status $status)" >&2
            echo "$output" >&2
            return 1
        }
    done
}

@test "each dist analysis target invokes its script via python3" {
    # Script names are pinned per analysis script. NOTE: dist-plots must invoke
    # dist-plot.py (singular) — a dist-plots.py would fail here.
    for i in "${!DIST_ANALYSIS_TARGETS[@]}"; do
        local target="${DIST_ANALYSIS_TARGETS[$i]}"
        local script="${DIST_ANALYSIS_SCRIPTS[$i]}"
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

@test "research make help lists all six experiment-dist-* targets" {
    # `make -C research help` is a read-only grep/sort/awk pipeline over the
    # Makefile text — safe, no cluster, no network.
    run make -C research help
    [ "$status" -eq 0 ]
    for target in "${DIST_EXPERIMENT_TARGETS[@]}"; do
        assert_output_contains "$target"
    done
}

@test "research make help lists the five dist analysis targets" {
    run make -C research help
    [ "$status" -eq 0 ]
    for target in "${DIST_ANALYSIS_TARGETS[@]}"; do
        assert_output_contains "$target"
    done
}

@test "research make help lists dist-all" {
    run make -C research help
    [ "$status" -eq 0 ]
    assert_output_contains "dist-all"
}

@test "dist-all dry-run reaches every family config in sequence" {
    run make -n -C research dist-all
    [ "$status" -eq 0 ] || {
        echo "make -n -C research dist-all failed (status $status)" >&2
        echo "$output" >&2
        return 1
    }
    # All six family configs must appear, in the spec order
    # (Family A api/db/burner/stress, then B weight, then C qos).
    assert_first_occurrences_in_order "${DIST_EXPERIMENT_CONFIGS[@]}"
}

@test "dist-all passes the three required flags for every family" {
    run make -n -C research dist-all
    [ "$status" -eq 0 ] || {
        echo "make -n -C research dist-all failed (status $status)" >&2
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

@test "validate-configs target still present and wired to --dry-run" {
    # Regression guard: the dry-run validation gate must survive the dist
    # additions. Dry-run only — the six new dist configs are separate
    # deliverables and do not exist yet, so a full validate run cannot cover
    # them.
    run make -n -C research validate-configs
    [ "$status" -eq 0 ]
    assert_output_contains "run-experiment.sh"
    assert_output_contains "--dry-run"
}
