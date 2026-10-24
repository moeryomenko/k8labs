# cpu-limits — Kubernetes CPU requests and limits research

This series measures how Kubernetes CPU requests and limits become cgroup-v2
settings. It records their effect on four workload shapes: CPU-bound, I/O-bound,
memory-bound, and hybrid. It separates request-share effects under contention
from limit-quota effects.

## Final status

The series is **partially measured**.

- The live capability gate passed on the selected custom-kernel worker.
- The pilot completed three full blocks: 108 of 108 planned cells.
- Policy calibration failed for `cpu-bound`, `io-bound`, and `hybrid`.
- The confirmatory campaign is blocked and did not run.

The pilot is descriptive. It does not provide a confirmatory result or a policy
recommendation. See [RESULTS.md](RESULTS.md), [PILOT.md](PILOT.md),
[FREEZE.md](FREEZE.md), and [CONFIRMATORY.md](CONFIRMATORY.md).

## Commands

Run these commands from the repository root.

```sh
# Inspect and validate the fixed 36-cell matrix.
task cpu-limits-plan
task cpu-limits-validate

# Run all offline tests and shell contracts.
task cpu-limits-test

# Run the offline quality checks used for this release.
uv run pytest -q research/cpu-limits/tests
uv run ruff check research/cpu-limits
find research/cpu-limits -type f -name '*.py' -print0 | xargs -0 -r python3 -m py_compile
find research/cpu-limits -type f -name '*.sh' -print0 | xargs -0 -r -n1 bash -n
```

If `shellcheck` is installed, also run:

```sh
find research/cpu-limits -type f -name '*.sh' -print0 | xargs -0 -r shellcheck
```

The following commands need a selected worker, a collector endpoint, and a
valid kubeconfig. They can change the live cluster. Do not run them as part of
the offline checks.

```sh
task cpu-limits-gate
task cpu-limits-smoke
task cpu-limits-pilot
task cpu-limits-freeze
task cpu-limits-sample-size
```

`task cpu-limits-confirm` must remain blocked until a new pilot produces
successful `policies.json` and `sample-size.json` records.

## Layout

```text
cpu-limits/
├── DESIGN.md          # fixed experiment matrix and timing
├── GATES.md           # live and campaign gate record
├── PILOT.md           # completed pilot inventory
├── FREEZE.md          # policy calibration and sample-size decision
├── CONFIRMATORY.md    # blocked confirmatory status
├── RESULTS.md         # pilot descriptive report and figures
├── GUIDE.md           # developer and administrator guide
├── SOURCES.md         # source annotations
├── Taskfile.yml       # series commands
├── experiments/       # design, gate, runner, and freeze tools
├── workloads/         # workload manifests and renderer
├── observers/         # collector manifest and implementation
├── capture/           # trace capture and parsing
├── analysis/          # statistics, reports, and figures
├── metrics/           # Prometheus and Grafana assets
├── figures/           # committed report figures
└── tests/             # offline tests and shell contracts
```

## Data root

Raw and derived run data is not committed. The data root is:

```text
${K8LABS_RESEARCH_DATA_ROOT:-<repo>/build}/research/cpu-limits/
```

Pilot data is under `pilot/`. Confirmatory data is absent because the
confirmatory campaign did not start. Each complete block has 36 cells. Missing
cells must remain incomplete and must never be filled with placeholder values.

## Results pointer

[RESULTS.md](RESULTS.md) reports the three-block pilot. Its measurements apply
only to the recorded worker, workload images, and measurement windows. The
report records descriptive paired estimates and cgroup mapping evidence. It
does not make confirmatory, focused-kernel, or sched_ext claims.

## Known limitations

- The pilot has three blocks and one worker only.
- Calibration did not produce valid low limits for all workloads.
- The confirmatory campaign did not run.
- Throttle-gap contrasts remain incomplete where the cgroup had zero CFS
  periods.
- The raw trace CSV inputs are not in the report data. Trace figures are an
  artifact inventory and do not make numerical trace findings.
- No focused cgroup-mode, base-slice, or sched_ext measurement was run.
