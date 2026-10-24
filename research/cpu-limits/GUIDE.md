# CPU requests, CPU limits, and scheduler operations

## Scope and evidence

This guide separates measured pilot results from platform documentation and blog opinions.

The local campaign ran three complete pilot blocks on one 4-vCPU custom-kernel worker. It recorded 108 complete cells. It is **pilot descriptive, not confirmatory**. The calibration failed because no low policy met the target throttled-period range for CPU-bound, I/O-bound, and hybrid workloads. The confirmatory campaign did not run. No sched_ext measurement ran. Do not generalize its figures to another node, image, kernel, or workload. [RESULTS](RESULTS.md#limitations) [RESULTS](RESULTS.md#calibration-outcome-and-campaign-status)

Source keys link to [SOURCES.md](SOURCES.md). A source key without `RESULTS` is platform documentation or an annotated external source. A blog does not establish a local result.

# Part 1. Developer guide

## 1. Set resources per container

Kubernetes schedules a Pod from resource requests. A CPU request is not a CPU reservation that prevents all contention. It is the scheduling input and, in this campaign, it mapped to cgroup-v2 `cpu.weight`. A CPU limit caps CPU bandwidth and, in this campaign, it mapped to `cpu.max`. [K-Resources] [RESULTS](RESULTS.md#cgroup-mapping)

Set CPU resources for every application container. Do not assume the Pod-level total protects an individual sidecar. Kubernetes resource requests and limits apply to containers. The resulting cgroup hierarchy can impose limits at more than one level. [K-Resources] [K-QoS] [RESULTS](RESULTS.md#cgroup-mapping)

### Sizing procedure

1. Measure normal and peak demand for the actual workload. Record throughput, latency, errors, CPU use, throttling, and CPU pressure. Do not size from a single average-use graph. [K-Resources] [K-HPA] [RESULTS](RESULTS.md#cpu-use-and-throttling-by-workload-and-policy)
2. Set an initial CPU request that lets the service meet its stated service objective under representative contention. The request affects placement and relative CPU share when work competes. [K-Resources] [L-Cgroup-v2]
3. Test a CPU limit only when a hard ceiling is a stated requirement. Test the production thread count and burst shape. A finite `cpu.max` can throttle a runnable workload before the period ends. [L-Cgroup-v2] [RESULTS](RESULTS.md#cpu-use-and-throttling-by-workload-and-policy)
4. Verify the cgroup files and outcome metrics after each change. Treat an absent or incomplete metric as a failed verification, not as zero throttling. [L-Cgroup-v2] [RESULTS](RESULTS.md#pilot-descriptive-contrast-availability)

The pilot showed a large descriptive throughput decrease for the low-limit CPU-bound and memory-bound cells. CPU-bound `burstable-low` had mean throttled-period ratios near 1.0 in both solo and contention cells. This evidence supports testing a finite limit carefully for a similar workload. It does not prove a general limit policy. [RESULTS](RESULTS.md#cpu-use-and-throttling-by-workload-and-policy) [RESULTS](RESULTS.md#application-outcomes-by-workload-and-policy)

## 2. Choose a QoS class deliberately

| Need | Resource declaration | Resulting QoS class | Rationale |
|---|---|---|---|
| Predictable request and limit values for every container | Equal CPU and memory requests and limits for every container | Guaranteed | Kubernetes assigns Guaranteed only when every container has equal requests and limits for CPU and memory. This adds a hard CPU ceiling when CPU is set. [K-QoS] [K-Resources] |
| A schedulable workload that may use spare CPU | Set a CPU request. Omit the CPU limit only after capacity and isolation verification passes. | Burstable | A request makes the Pod Burstable when its container does not satisfy Guaranteed rules. The request remains available to scheduling and CPU-share control. [K-QoS] [K-Resources] [L-Cgroup-v2] |
| Disposable work with no requested minimum | Omit requests and limits only when eviction and CPU-share consequences are acceptable. | BestEffort | BestEffort has no resource requests or limits. It is not a default for production latency-sensitive work. [K-QoS] |

**Recommendation:** use Guaranteed only when its memory and CPU ceilings are both intended. Use Burstable with explicit requests for services that need scheduler accounting and may need idle CPU bursts. Use BestEffort only for work that can tolerate its lower protection. [K-QoS] [K-Resources]

## 3. Workload-shape decision tree

1. Is the service CPU-bound during the measured busy window?
   - Yes. Start with an explicit request. Test any finite limit under the real concurrency level. The pilot observed high throttling and lower descriptive throughput in comparable low-limit CPU-bound cells. [RESULTS](RESULTS.md#cpu-use-and-throttling-by-workload-and-policy)
   - No. Continue.
2. Is the service I/O-bound with a latency objective?
   - Yes. Set a request from measured peak demand. Do not infer that a low average CPU use means that quota cannot affect latency. The pilot I/O workload had no measured throttling, but its three-block intervals were descriptive and not a universal result. [RESULTS](RESULTS.md#application-outcomes-by-workload-and-policy) [RESULTS](RESULTS.md#paired-pilot-descriptive-estimates-and-95-intervals)
   - No. Continue.
3. Is the service memory-bound?
   - Yes. Set CPU from measured demand and size memory separately. Test a finite CPU limit. The pilot memory-bound low-limit cells had descriptive throughput reductions and throttling. [RESULTS](RESULTS.md#cpu-use-and-throttling-by-workload-and-policy) [RESULTS](RESULTS.md#application-outcomes-by-workload-and-policy)
   - No. Continue.
4. Is the service hybrid, with I/O and CPU work in the same request path?
   - Yes. Measure p99 latency and throughput together. Do not select a limit from one outcome only. The pilot hybrid results are descriptive and show throttling in several finite-limit policies. [RESULTS](RESULTS.md#cpu-use-and-throttling-by-workload-and-policy) [RESULTS](RESULTS.md#application-outcomes-by-workload-and-policy)

## 4. Understand burst behaviour

A finite `cpu.max` contains a quota and a period. A cgroup can use its quota early in the period and then wait until the next period. Thus an interval average can look moderate while requests see delay. [L-Cgroup-v2] [B-Indeed]

**Recommendation:** use `nr_throttled / nr_periods` with `throttled_usec` and application latency. Do not use CPU utilization alone to clear a CPU limit. [L-Cgroup-v2] [RESULTS](RESULTS.md#cpu-use-and-throttling-by-workload-and-policy)

A no-limit cgroup can report `cpu.max` as `max 100000`. Its throttled-period ratio is not defined by the campaign data because no quota periods apply. Keep this state distinct from a measured zero ratio. [L-Cgroup-v2] [RESULTS](RESULTS.md#cpu-use-and-throttling-by-workload-and-policy)

## 5. Verify a deployed setting

These commands inspect a running Pod. Replace placeholders. They do not change cluster state.

```sh
kubectl -n <namespace> get pod <pod> -o jsonpath='{range .spec.containers[*]}{.name}{"\n"}{.resources}{"\n"}{end}'
kubectl -n <namespace> describe pod <pod>
kubectl get node <node> -o jsonpath='{.status.allocatable.cpu}{"\n"}'
```

Get the container PID and cgroup path with the node-approved support method. Then inspect every applicable level, not only the leaf.

```sh
cat /proc/<pid>/cgroup
cat /sys/fs/cgroup/<path>/cpu.weight
cat /sys/fs/cgroup/<path>/cpu.max
cat /sys/fs/cgroup/<path>/cpu.stat
cat /sys/fs/cgroup/<path>/cpu.pressure
```

`cpu.stat` reports usage. When the CPU controller is enabled for fair-class tasks, it also reports periods, throttled periods, and throttled time. `cpu.pressure` reports CPU pressure. [L-Cgroup-v2]

**Recommendation:** verify the manifest, Pod QoS, cgroup path, `cpu.weight`, `cpu.max`, `cpu.stat`, and application outcome in one time window. A resource declaration alone does not prove the runtime hierarchy. [K-QoS] [L-Cgroup-v2] [RESULTS](RESULTS.md#cgroup-mapping)

## 6. HPA and VPA

For CPU resource metrics, HPA computes utilization as CPU use divided by the relevant request. A request change can therefore change the observed utilization percentage without changing the application load. [K-HPA]

**Recommendation:** set and verify requests before choosing an HPA CPU target. Then verify replica changes against latency, queue depth, and throttling. CPU utilization is not a substitute for a service metric. [K-HPA] [RESULTS](RESULTS.md#cpu-use-and-throttling-by-workload-and-policy)

VPA recommends or changes resource requests. Coordinate VPA with HPA because a changed CPU request changes the HPA denominator. Freeze the policy during a controlled benchmark. [K-VPA] [K-HPA]

## 7. Multi-container Pods

A Pod can contain an application, proxy, metrics exporter, and log sidecar. Each container can consume CPU and can receive its own request and limit. The Pod QoS class depends on every container. [K-QoS] [K-Resources]

**Recommendation:** size each container from its own observed demand. Budget sidecars in the Pod total. Verify the pod and container cgroups because this campaign observed different values at those levels. [K-Resources] [RESULTS](RESULTS.md#cgroup-mapping)

Do not give only the main container a resource declaration and assume the Pod is Guaranteed. Do not set a small limit on a proxy without measuring its burst work. [K-QoS] [L-Cgroup-v2]

## 8. Anti-patterns

- **Set every CPU limit equal to the request by default.** This creates a quota. Use it only when the ceiling is intended and verified. [K-Resources] [L-Cgroup-v2]
- **Use a request as a limit.** Requests affect scheduling and relative distribution. Limits use a different cgroup control. [K-Resources] [L-Cgroup-v2] [RESULTS](RESULTS.md#cgroup-mapping)
- **Use a low average CPU graph as proof of no throttle.** Verify `cpu.stat` and latency. [L-Cgroup-v2] [B-Indeed]
- **Treat a three-block pilot interval as a confirmatory decision.** Calibration failed and the confirmatory campaign did not run. [RESULTS](RESULTS.md#calibration-outcome-and-campaign-status)
- **Copy a blog's no-limit rule without capacity policy.** The external articles contain different trade-offs. Apply their claims only after local measurement and an administrator-approved capacity policy. [B-Robusta] [B-Kubernetes-limits] [B-PerfectScale] [B-ErickHun]

# Part 2. Administrator guide

## 9. Cgroup-v2 hierarchy and controls

Cgroup v2 is a single hierarchy. CPU controller settings distribute or cap work hierarchically. A parent constraint can further restrict child cgroups. A child cannot override that parent restriction. [L-Cgroup-v2]

Kubernetes and the runtime commonly create a QoS slice, a Pod slice, and container scopes. Names and exact nesting depend on the cgroup driver and runtime. Inspect the node. Do not hard-code one path from this guide. [K-Cgroups] [L-Cgroup-v2] [RESULTS](RESULTS.md#cgroup-mapping)

`cpu.weight` distributes CPU cycles among active siblings. `cpu.max` defines maximum quota and period. The default `cpu.max` value is `max 100000`. [L-Cgroup-v2]

**Recommendation:** enable and delegate the CPU controller before workloads start. Keep the controller hierarchy stable during a campaign. Inspect parent and child values before attributing a throttle event to one container. [L-Cgroup-v2] [RESULTS](RESULTS.md#cgroup-mapping)

## 10. Kubelet and node policy

Review the effective kubelet configuration and node allocatable resources before changing workload policy.

```sh
kubectl get --raw /api/v1/nodes/<node>/proxy/configz
kubectl describe node <node>
```

Important fields include `cpuCFSQuota`, `cpuCFSQuotaPeriod`, `cpuManagerPolicy`, `systemReserved`, `kubeReserved`, and the topology-manager policy. Their availability and lifecycle depend on the Kubernetes release. Consult the target release reference before changing them. [K-Kubelet-config] [K-CPU-manager] [K-Reserve] [K-Topology]

**Recommendation:** reserve CPU for the OS and Kubernetes daemons before you calculate workload capacity. This protects node services from an application-only request sum. [K-Reserve]

**Recommendation:** use CPU Manager `static` only for workloads that meet its documented eligibility rules and require exclusive CPU placement. Do not use it as a general replacement for request sizing or quota verification. [K-CPU-manager]

**Recommendation:** change `cpuCFSQuota` or `cpuCFSQuotaPeriod` only through a tested node configuration rollout. These settings change CPU-limit enforcement behavior for every affected node. [K-Kubelet-config] [L-Cgroup-v2]

## 11. Kernel scheduler policy

The fair-class scheduler is the normal stock path for Kubernetes CPU control. In the current kernel documentation, fair-class tasks enforce `cpu.weight`, `cpu.max`, and `cpu.idle`. [L-Cgroup-v2] [L-Sched-ext]

Kernel parameters such as `sched_base_slice_ns`, `sched_migration_cost_ns`, `sched_cfs_bandwidth_slice_us`, `cgroup_mode`, and `sched_autogroup_enabled` can affect scheduling behavior. Treat them as node-level experiments. Read the effective value, record the kernel version, test a representative workload, and restore the prior value. This campaign did not produce a confirmatory tuning result for them. [RESULTS](RESULTS.md#limitations) [L-Cgroup-v2]

### EEVDF and sched_ext

EEVDF is part of the fair-class scheduling path. `sched_ext` is a BPF-defined scheduler class. A sched_ext scheduler can replace fair-class scheduling for eligible tasks and can revert to fair-class scheduling after an error or stop event. [L-Sched-ext]

**Recommendation:** use the stock fair-class path when cgroup CPU semantics must be known and no scheduler-specific validation exists. It is the path that the kernel documents as enforcing CPU controller settings. [L-Cgroup-v2] [L-Sched-ext]

**Recommendation:** use sched_ext only after validating the exact scheduler binary, kernel BTF interface, and callbacks on the target kernel. The sched_ext interface has no cross-version ABI stability guarantee. [L-Sched-ext]

### Quota enforcement limitation

For sched_ext tasks, the kernel reports `cpu.weight` and `cpu.max` changes to BPF scheduler callbacks. The BPF scheduler decides whether and how to implement them. A scheduler without `cgroup_set_bandwidth` support can ignore `cpu.max`. Therefore a Kubernetes CPU limit is not proven to enforce quota under an arbitrary sched_ext scheduler. [L-Cgroup-v2] [L-Sched-ext]

`scx_lavd --enable-cpu-bw` is not a recommendation from this campaign. Use it only after the exact scheduler exposes and passes a `cgroup_set_bandwidth` verification on the deployed kernel. The planned focused run did not occur, and RESULTS records no sched_ext measurement. [L-Cgroup-v2] [RESULTS](RESULTS.md#limitations)

## 12. Observability and incident verification

Collect these signals on the same time interval:

- Pod specification, QoS class, node, and container restarts. [K-QoS] [K-Resources]
- `cpu.weight`, `cpu.max`, `cpu.stat`, and `cpu.pressure` at the Pod and container levels. [L-Cgroup-v2]
- CPU use, CFS periods, throttled periods, and throttled seconds from cAdvisor-compatible metrics. [RESULTS](RESULTS.md#cpu-use-and-throttling-by-workload-and-policy)
- Application throughput, errors, and latency. [RESULTS](RESULTS.md#application-outcomes-by-workload-and-policy)
- Scheduler evidence from bounded `perf sched` and reviewed trace probes when the node policy permits it. The recorded run-queue p50 and p99 are pilot-descriptive trace evidence for selected cells only. [RESULTS](RESULTS.md#trace-evidence)

**Recommendation:** alert on a combination of service failure signals, throttling, and CPU pressure. Do not alert on a throttled-time value alone because workload concurrency changes its meaning. [L-Cgroup-v2] [B-Indeed]

## 13. Capacity and noisy-neighbour policy

The Kubernetes scheduler uses requests for resource-fit decisions. It does not promise that the runtime will have no CPU contention. Weight affects active sibling distribution, and a finite quota can leave capacity unused by the limited cgroup. [K-Scheduler] [L-Cgroup-v2]

**Recommendation:** admit workload requests against node allocatable CPU, not raw host CPU. Include `systemReserved` and `kubeReserved`. Keep headroom for daemon and burst demand. [K-Reserve] [K-Scheduler]

**Recommendation:** use requests, placement policy, and node isolation together for workloads that must not disturb other services. A CPU limit can be part of that policy, but it must be tested against the target service objective. [K-Scheduler] [K-Resources] [RESULTS](RESULTS.md#cpu-use-and-throttling-by-workload-and-policy)

## 14. Decision matrix

| Situation | Default choice | Required verification | Quota-enforcement limit |
|---|---|---|---|
| General Kubernetes node with fair-class scheduling | Explicit requests. Apply limits only where a ceiling is required. | Verify `cpu.weight`, `cpu.max`, `cpu.stat`, pressure, and application outcomes. [K-Resources] [L-Cgroup-v2] | Fair-class enforcement is documented. A finite limit can still throttle bursts. [L-Cgroup-v2] |
| Latency-sensitive CPU-bound service | Explicit request. Test no CPU limit and tested finite limits under contention. | Measure p99 latency, throughput, throttle ratio, and capacity headroom. [RESULTS](RESULTS.md#cpu-use-and-throttling-by-workload-and-policy) | The pilot supports caution for similar low-limit CPU-bound cells only. It is not confirmatory. [RESULTS](RESULTS.md#calibration-outcome-and-campaign-status) |
| Shared multi-tenant node that requires a hard CPU ceiling | Use a finite CPU limit as an explicit policy control. | Test each container and Pod hierarchy. Reserve system CPU. [K-Resources] [K-Reserve] | Fair-class quota applies only where the documented CPU controller path applies. [L-Cgroup-v2] |
| Guaranteed Pod with exclusive CPUs | Use documented CPU Manager static policy only if the Pod is eligible. | Verify CPU Manager, topology policy, reservations, and actual cpuset allocation. [K-CPU-manager] [K-Topology] | Guaranteed QoS does not remove a finite CPU quota. [K-QoS] [L-Cgroup-v2] |
| Custom kernel, fair-class scheduler | Keep stock resource semantics until a versioned local test passes. | Record kernel version and cgroup controls. Do not rely on unmeasured kernel knobs. [L-Cgroup-v2] [RESULTS](RESULTS.md#limitations) | The campaign did not measure cgroup-mode or base-slice results. [RESULTS](RESULTS.md#limitations) |
| sched_ext scheduler | Use only with an exact callback and workload validation. | Verify scheduler state, BTF, `cgroup_set_weight`, and `cgroup_set_bandwidth`. [L-Sched-ext] [L-Cgroup-v2] | `cpu.max` can be ignored when the scheduler does not implement bandwidth handling. No sched_ext result was measured here. [L-Cgroup-v2] [RESULTS](RESULTS.md#limitations) |

## 15. Limits of this guide

This guide does not set a universal CPU-limit policy. It converts official behavior and local pilot observations into actions. The pilot did not complete calibration or a confirmatory campaign. It did not measure sched_ext, cgroup mode, or base-slice effects. Apply the source rules to the installed Kubernetes and kernel versions, then measure the production workload. [RESULTS](RESULTS.md#calibration-outcome-and-campaign-status) [RESULTS](RESULTS.md#limitations)
