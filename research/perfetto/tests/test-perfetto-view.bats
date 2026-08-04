#!/usr/bin/env bats
# test-perfetto-view.bats — pins the `make perfetto-view` target (REQ-004)
#
# perfetto-view must exist in research/Makefile and its recipe must reference
# a ui.perfetto.dev URL (the Perfetto UI used for detailed scheduler analysis,
# see TASK-007). The test invokes `make -n` (dry-run), which prints the recipe
# without executing it — so it never starts trace_processor --httpd, never
# opens a browser, and never requires a live cluster.
#
# When there is no *.perfetto-trace under research/experiments/data there is
# nothing to view, so the test SKIPS cleanly (REQ-004) instead of failing.
# This mirrors the skip convention in test-perfetto-capture-e2e.bats
# (E2E-11/E2E-12): absence of the prerequisite skips, never fails.

setup() {
    export PROJECT_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../../.." && pwd -P)"
    export RESEARCH_DIR="$PROJECT_ROOT/research"
    export EXPERIMENTS_DATA_DIR="$PROJECT_ROOT/research/experiments/data"
}

# =============================================================================
# REQ-004 — perfetto-view target exists and references ui.perfetto.dev
# =============================================================================

@test "VW-01: make perfetto-view target exists and references ui.perfetto.dev (REQ-004)" {
    # Skip (never fail) when there are no traces to view (REQ-004).
    local trace
    trace="$(find "$EXPERIMENTS_DATA_DIR" -name '*.perfetto-trace' -type f 2>/dev/null | head -1)"
    if [[ -z "$trace" ]]; then
        skip "no *.perfetto-trace under $EXPERIMENTS_DATA_DIR — perfetto-view has nothing to open"
    fi

    # Dry-run: proves the target exists (make errors on a missing target) and
    # pins the ui.perfetto.dev URL in the recipe, without executing anything.
    run make -n -C "$RESEARCH_DIR" perfetto-view
    [ "$status" -eq 0 ]
    [[ "$output" == *"ui.perfetto.dev"* ]]
}
