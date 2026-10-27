# cpu-limits gate record

## Final campaign status

**PARTIALLY MEASURED — PILOT COMPLETE, CONFIRMATORY BLOCKED.**

The live capability gate passed on the selected custom-kernel worker. The pilot
completed three full 36-cell blocks. Policy calibration then failed for
`cpu-bound`, `io-bound`, and `hybrid`, so the confirmatory campaign did not
start. This record does not claim a confirmatory result.

| final gate | status | evidence |
|---|---|---|
| Offline design and test commands | PASS | `task cpu-limits-validate` and `task cpu-limits-test` pass. |
| Live capability gate | PASS | The retry record below passed on the selected worker. |
| Pilot completeness | PASS | `PILOT.md` records 3 complete blocks and 108 complete cells. |
| Policy calibration | FAIL | `FREEZE.md` records no valid low limit for three workloads. |
| Sample-size authorization | FAIL | `sample-size.json` has status `FAIL`; throttle-gap rows are incomplete. |
| Confirmatory campaign | BLOCKED | `CONFIRMATORY.md` records that no confirmatory workload ran. |
| Focused kernel and sched_ext experiments | NOT RUN | They require a successful confirmatory authorization. |

> The Phase 7.6 scheduler comparison and the Phase 7.7 incident simulation were
> run later as an exploratory track, outside this confirmatory protocol. They
> are reported in `PHASE7_COMPLETE.md` and are not confirmatory results; the
> gate above still describes the confirmatory campaign.

Do not run `task cpu-limits-confirm`, focused experiments, or report a
confirmatory result until a new pilot creates successful `policies.json` and
`sample-size.json` records for every required workload and contrast.

## Live capability gate

- Task: `cpu-limits-12-live-gate`
- Retry: `2026-10-24T12:19:39Z`
- Kubeconfig: `build/kubeconfig`
- Selected node: `k8labs-md-custom-4vcpu-qq498-zlz6p-6n655`
- Kernel: `7.3.0-k8labs`
- Collector: `cpu-limits-collector` DaemonSet, Service, and ConfigMap
- Overall status: **PASS (retry)**

## Initial attempt: FAIL

The first attempt failed before any workload ran. The collector manifest did
not create the `cpu-limits` namespace, and the DaemonSet referenced an image
that did not exist in the lab registry. No collector Pod became Ready, so every
host snapshot, metric, image-pull, noise-floor, and trace gate was blocked. The
failure is recorded in commit `0778e80`.

## Retry: PASS

The retry fixed the packaging and verified the live path end to end:

| gate | status | evidence |
|---|---|---|
| Workload cluster is reachable and selected worker is Ready | PASS | `k8labs-md-custom-4vcpu-qq498-zlz6p-6n655` is `Ready`. |
| Selected worker name and required custom kernel | PASS | Kernel is `7.3.0-k8labs`. |
| Only the selected worker has `cpu-limits/research=collector` | PASS | The selector returns one node. The stock worker and control plane have no label. |
| Namespace, ConfigMap, Service, and collector apply | PASS | `apply.sh apply` creates namespace `cpu-limits`, ConfigMap `cpu-limits-collector`, Service `cpu-limits-collector`, and the DaemonSet. |
| Collector Pod becomes Ready | PASS | `1/1 Running` on the selected worker. |
| cgroup v2 and the CPU controller | PASS | `cgroup.controllers` contains `cpu`. |
| `cpu.max` and `cpu.weight` are readable | PASS | Read from `kubepods.slice`; the root cgroup does not expose these files. |
| `sched/cgroup_mode` and `sched/base_slice_ns` are readable | PASS | `cgroup_mode` reports `up smp (concur) max tasks`; `base_slice_ns` is `2100000`. |
| `bpftrace`, `perf`, `wrk2`, and `/fixtures/input.mp4` are available | PASS | All commands resolve; the fixture is 62 MiB. |
| Collector `/metrics` exposes required families | PASS | 10 families, including usage, periods, throttled periods, throttled seconds, memory, PSI, weight, quota, and period. |
| Ten-second CPU pressure noise floor | PASS | `cpu.pressure some avg10=0.81`; `full avg10=0.00`. |
| Workload images pull | PASS | nginx, stress-ng, ffmpeg, and wrk images pulled and a 4/4 image-check Pod became Ready. |
| Short probe reads scheduler events | PASS | `sched_switch` and `sched_cfs_period_timer` attach; `throttle_cfs_rq` is not traceable on this kernel, so the attachable workqueue event is used. |
| Control plane unchanged | PASS | The control-plane node has no collector label. |
| Smoke cell path | PASS | Four `cpu-bound / solo` cells completed; `blocks.csv`, `cgroups.csv`, and `metrics.csv` were written with non-zero counters. |

### Commands and raw output

```text
$ KUBECONFIG=build/kubeconfig research/cpu-limits/observers/apply.sh apply
namespace/cpu-limits created
configmap/cpu-limits-collector created
serviceaccount/cpu-limits-collector created
role.rbac.authorization.k8s.io/cpu-limits-collector created
rolebinding.rbac.authorization.k8s.io/cpu-limits-collector created
daemonset.apps/cpu-limits-collector created
service/cpu-limits-collector created

$ kubectl --kubeconfig build/kubeconfig -n cpu-limits get pods -o wide
NAME                         READY   STATUS    RESTARTS   AGE   IP              NODE
cpu-limits-collector-fgpd7   1/1     Running   0          3m    192.168.124.3   k8labs-md-custom-4vcpu-qq498-zlz6p-6n655

$ kubectl --kubeconfig build/kubeconfig -n cpu-limits exec <collector> -- \
    python3 /opt/cpu-limits/runtime_gate.py --root /tmp/gate-root --json /tmp/runtime-gate.json
PASS cgroup_v2: /sys/fs/cgroup/cgroup.controllers
PASS cpu_controller: cpu controller is enabled
PASS kernel_release: 7.3.0-k8labs
PASS cgroup_mode_readable: /sys/kernel/debug/sched/cgroup_mode
PASS base_slice_ns_readable: /sys/kernel/debug/sched/base_slice_ns
PASS command_bpftrace: /usr/bin/bpftrace
PASS command_perf: /usr/bin/perf
PASS command_wrk2: /usr/local/bin/wrk2
PASS fixture_input_mp4: /fixtures/input.mp4
PASS cpu_max_readable: /sys/fs/cgroup/kubepods.slice/cpu.max
PASS cpu_weight_readable: /sys/fs/cgroup/kubepods.slice/cpu.weight
```

The runtime gate uses a temporary symlink root because the collector mounts the
host cgroup and debugfs under `/host`, while `runtime_gate.py` expects absolute
host paths. The symlink root does not change the host.

### Probe capability

```text
$ bpftrace -e 'tracepoint:sched:sched_switch { @sw = count(); } interval:s:2 { exit(); }'
Attached 2 probes
@sw: 6211

$ bpftrace -e 'kprobe:sched_cfs_period_timer { @p = count(); } interval:s:2 { exit(); }'
Attached 2 probes
@p: 6

# Not attachable on 7.3.0-k8labs:
# kprobe:throttle_cfs_rq -> "not traceable"
# The series uses kprobe:throttle_cfs_rq_work as the throttle marker.
```

### Smoke run

```text
$ research/cpu-limits/experiments/run-block.sh --phase smoke \
    --workload cpu-bound --condition solo --blocks 1 ...
{"status": "complete", "teardown": "complete"}   # best-effort
{"status": "complete", "teardown": "complete"}   # request-low
{"status": "complete", "teardown": "complete"}   # fit
{"status": "complete", "teardown": "complete"}   # burstable-low
```

The smoke run is pipeline evidence only. It is not part of the pilot or
confirmatory verdict.

## Residual limitations

- The collector installs `bpftrace`, `perf`, and `libbpf-tools` at start. The
  first start takes about 70 seconds on this node.
- The collector image is Fedora with the collector code supplied by a ConfigMap.
  No private image is required, but the node must reach the Fedora registry.
- `throttle_cfs_rq` is not traceable on this kernel. The trace uses
  `throttle_cfs_rq_work`, `unthrottle_cfs_rq`, and `sched_cfs_period_timer`.
- The host `nsenter` cannot enter PID 1's mount namespace from the collector, so
  the noise floor uses the host cgroup `cpu.pressure` counters instead of
  `/proc/loadavg`.
