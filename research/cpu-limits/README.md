# cpu-limits — Kubernetes CPU requests and limits research

This series measures how Kubernetes CPU requests and limits become cgroup-v2
settings, and how those settings affect four workload shapes. It separates
request share effects under contention from limit quota effects.

## Scope

The series owns Kubernetes CPU request and limit behaviour on the selected
worker. It records the kubelet, CRI-O, and crun cgroup hierarchy and reads the
resulting cgroup-v2 counters. It does not change the worker image, cluster
configuration, or scheduler source.

Sibling series retain ownership of placement, flat Linux scheduler comparisons,
raw cgroup-mode comparisons, kernel attribution, and sched_ext delegation.

## Planned commands

```sh
task cpu-limits-plan       # print one seeded 36-cell block
task cpu-limits-validate   # validate the frozen design
uv run pytest -q research/cpu-limits/tests/test_design.py
```

## Layout

```text
cpu-limits/
├── DESIGN.md              # frozen experiment design
├── GATES.md               # capability and runtime gate record
├── Taskfile.yml           # offline series commands
├── experiments/design.py  # matrix and seeded block schedule
└── tests/test_design.py   # offline design checks
```

Later tasks add workloads, cgroup capture, metrics, traces, runner, analysis,
and the measurement guides.

## Data root

Raw and derived run data is not committed. The data root is:

```text
${K8LABS_RESEARCH_DATA_ROOT:-<repo>/build}/research/cpu-limits/
```

Pilot, confirmatory, and smoke data remain separate. A complete block contains
all 36 cells. Missing cells are reported as incomplete and are never filled.

## Current status

The design foundation is frozen and offline validation is available. The live
capability gates and measurements are pending. No measurement result is claimed
by this document.
