# linux-sched OTel profiling campaign

A **separate** campaign from the scheduler comparison: it profiles all five
workloads under all three arms so a comparison result can be explained, not just
reported. It never runs while a comparison block runs — the runner refuses, and
the plan forbids overlap.

## Where each piece runs, and why

| Piece | Runs on | Why there |
|---|---|---|
| Alloy, with `pyroscope.ebpf` | each research worker (DaemonSet) | the profiler sees the node's processes, so it must be on the node running the workload |
| Pyroscope | the host, host networking, `profile-data/` volume | the plan wants a dedicated 10 GiB store separate from the worker disks; every node here has a 5 GiB filesystem |
| `profilecli` | the host, from the Pyroscope image | it is the CLI the plan names for the per-session queries |

Profiler Pods reach Pyroscope on the host's LAN address (verified reachable from
a worker Pod). The host reaches Alloy's HTTP API through `kubectl port-forward`,
because the pod-to-host direction works but the reverse does not.

## What the pipeline is, and what it is not

The plan's design was: the OTel eBPF profiler exports OTLP profiles to Alloy,
Alloy's OTLP exporter writes them to Pyroscope, and the gate asserts on Alloy's
`otelcol_exporter_*_profile_samples` counters. **That pipeline cannot be built**,
and both halves were run rather than assumed:

- **Alloy cannot carry the profiles signal.** `otelcol.receiver.otlp` rejects it
  outright — `unrecognized attribute name "profiles"` — on v1.20.1 *and* on
  v1.21.0-rc.0, with `--stability.level=experimental` set. There is no
  `otelcol.exporter.pyroscope` component in either release.
- **Pyroscope 2.4.0 rejects the profiler distribution's payload.** Running the
  profiler (`otel/opentelemetry-collector-ebpf-profiler:0.163.1`) against
  Pyroscope's OTLP path `/v1development/profiles` (the path Pyroscope serves;
  `/v1/profiles` is 404) returns HTTP 400:
  `invalid_labels '{="1791330", ... }' with error: invalid label name ''` — the
  2026 profiles payload and this Pyroscope disagree on the label set.

What runs instead keeps every other property the plan asks for: Alloy stays in
the data path, and the profiler is the same OTel eBPF profiler —
`pyroscope.ebpf` embeds `grafana/opentelemetry-ebpf-profiler`, a fork of
`open-telemetry/opentelemetry-ebpf-profiler`. Alloy writes profiles to Pyroscope
in Pyroscope's own format, which is native ingest and needs no translation.

**One counter assertion differs.** The gate requires
`pyroscope_ebpf_pprofs_total` to advance and
`pyroscope_ebpf_profiling_sessions_failing_total` to stay put, instead of
Alloy's `otelcol_exporter_*_profile_samples`: with profiles not travelling over
OTLP, there is no OTLP exporter to count them. Both names, their meaning and this
substitution are recorded in the plan's revision note.

## Session labels

Every session is labelled `service_name=linux-sched-profile` plus `workload`,
`scheduler`, `node`, `kernel`, `image` and `run_id`. The labels live in the
Alloy ConfigMap, which the runner renders per session and applies with a rollout,
so a stored profile carries exactly the labels `sessions.json` records for it —
which is what makes the gate's exact-label, exact-time-range query meaningful.

## Filesystem budget

`PROFILE_VOLUME` holds Pyroscope's data on a host-owned filesystem with a
**10 GiB budget**, verified with `df -B1`; `du -sb` is recorded after every
session. Ingest stops at **9 GiB**. Pyroscope retains **336h** (14 days) and
nothing inside that window is deleted. This is separate from the confirmatory
data root and from every worker disk.

## Running it

```sh
task linux-sched-otel-up        # host: Pyroscope
task linux-sched-otel-apply     # workers: the Alloy profiling DaemonSet
task linux-sched-otel-profiles  # 15 labelled sessions, one at a time
task linux-sched-otel-check     # the gate
task linux-sched-otel-down
```

`linux-sched-otel-profiles` writes `sessions.json` beside the data root: for each
session the UTC start and end, workload, scheduler arm, node, kernel, workload
image and `run_id`. `linux-sched-otel-validate` checks the artefacts offline:
digests pinned, no `privileged: true`, the documented capability set, every
session label present, and the gate wired to the command the plan's acceptance
names.

## Validated before the campaign

The pipeline was run end to end on this host with Alloy's embedded profiler:
Alloy reported `All Alloy components are healthy.`, its
`pyroscope_ebpf_pprofs_total{service_name="linux-sched-profile"}` counter
advanced with `profiling_sessions_failing_total` at zero, and a
`profilecli query top` over the session's exact labels and time range returned
nonzero samples. The campaign wraps that validated pipeline; it does not rely on
a design that was never executed.
