#!/usr/bin/env bats
# test-perfetto-view.bats — pins the `make cpu-sched-perfetto-view` target
#
# cpu-sched-perfetto-view must exist in the research makefile tree
# (research/Makefile aggregator + research/cpu-sched/cpu-sched.mk) and its
# recipe must:
#   1. reference the ui.perfetto.dev UI (the detailed scheduler analysis UI);
#   2. serve the RAW trace file via the CORS static server script
#      (research/cpu-sched/perfetto/bin/perfetto-serve.py — path contains
#      "perfetto-serve");
#   3. open a ui.perfetto.dev URL whose ?url= parameter points at the trace
#      FILE served by that script (?url=http://127.0.0.1:<port>/<basename>),
#      NOT a bare ?url=http://127.0.0.1:<port>.
#
# The bare-port form is the bug this pins: the Perfetto UI treats a bare
# localhost URL as a raw trace file, while the trace_processor httpd root
# returns a text help page, so the UI fails with ERR:fmt. Only the file URL
# served by perfetto-serve (with CORS headers) renders scheduler events.
#
# The test invokes `make -n` (dry-run), which prints the recipe without
# executing it — so it never starts a server, never opens a browser, and never
# requires a live cluster.
#
# When there is no *.perfetto-trace under research/cpu-sched/experiments/data there is
# nothing to view, so the test SKIPS cleanly instead of failing.
# This mirrors the skip convention in test-perfetto-capture-e2e.bats:
# absence of the prerequisite skips, never fails.

setup() {
    export PROJECT_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../../../.." && pwd -P)"
    export RESEARCH_DIR="$PROJECT_ROOT/research"
    export EXPERIMENTS_DATA_DIR="$PROJECT_ROOT/research/cpu-sched/experiments/data"
}

# =============================================================================
# cpu-sched-perfetto-view serves the raw trace and opens a file URL
# =============================================================================

@test "make cpu-sched-perfetto-view serves the raw trace via perfetto-serve and opens a file URL" {
    # Skip (never fail) when there are no traces to view.
    local trace
    trace="$(find "$EXPERIMENTS_DATA_DIR" -name '*.perfetto-trace' -type f 2>/dev/null | head -1)"
    if [[ -z "$trace" ]]; then
        skip "no *.perfetto-trace under $EXPERIMENTS_DATA_DIR — perfetto-view has nothing to open"
    fi

    # Dry-run: proves the target exists (make errors on a missing target) and
    # pins the recipe, without executing anything.
    run make -n -C "$RESEARCH_DIR" cpu-sched-perfetto-view
    [ "$status" -eq 0 ]

    # 1. The Perfetto UI is still the target.
    [[ "$output" == *"ui.perfetto.dev"* ]]

    # 2. The recipe references the CORS static server script that serves the
    #    RAW trace file (absent from the old trace_processor --httpd recipe).
    [[ "$output" == *"perfetto-serve"* ]]

    # 3. The ?url= parameter points at the trace FILE path, not a bare port.
    #    Extract the ui.perfetto.dev URL line; it must carry a path after the
    #    port that is derived from the trace (its basename).
    local url_line
    url_line="$(printf '%s\n' "$output" | grep -F 'ui.perfetto.dev' | head -1)"
    [[ -n "$url_line" ]]
    [[ "$url_line" == *"?url=http://127.0.0.1:"* ]]
    [[ "$url_line" == *"/"*"trace"* ]]
}
