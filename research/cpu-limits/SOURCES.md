# Sources and evidence classification

This file classifies the sources used by [GUIDE.md](GUIDE.md). It separates official platform documentation, this repository's measured pilot report, and external articles. An external article can provide a hypothesis or an operating trade-off. It does not prove a result for this campaign.

## Evidence labels

- **Measured**: Data collected by this repository. The scope and limitations are in the report.
- **Official**: Documentation published by the relevant project or vendor.
- **Opinion**: External engineering article, vendor article, or individual post. It can include case-study data, but it is not a controlled result for this campaign.
- **Campaign relationship**: **supports**, **contradicts**, or **does not test** describes only the recorded CPU-limits pilot. It does not grade source quality.

## Local campaign

### RESULTS

- **URL:** [RESULTS.md](RESULTS.md)
- **Type:** Measured.
- **Claim:** Three complete pilot blocks produced 108 complete cells on one 4-vCPU custom-kernel worker. Requests mapped to observed `cpu.weight`. Limits mapped to observed `cpu.max`. Low-limit CPU-bound and memory-bound cells showed descriptive throughput reductions and high throttled-period ratios in several cases.
- **Campaign relationship:** Supports directly. This is the campaign evidence.
- **Limit:** This report is pilot descriptive, not confirmatory. Calibration failed for CPU-bound, I/O-bound, and hybrid low policies. The confirmatory campaign did not run. It contains no sched_ext measurement. Its numerical run-queue result is pilot-descriptive trace evidence from selected five-second slices only.

## Official Kubernetes documentation

### K-Resources

- **URL:** https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/
- **Type:** Official.
- **Claim:** Containers declare CPU requests and limits. The scheduler uses requests for placement. Kubernetes uses operating-system mechanisms to enforce limits.
- **Campaign relationship:** Supports the request-to-weight and limit-to-quota mapping observed in the pilot. The document does not test this worker, runtime, or workload.

### K-QoS

- **URL:** https://kubernetes.io/docs/concepts/workloads/pods/pod-qos/
- **Type:** Official.
- **Claim:** Kubernetes assigns Guaranteed, Burstable, or BestEffort QoS from the resource declarations of all containers in a Pod.
- **Campaign relationship:** Supports QoS interpretation. The campaign did not test eviction behavior.

### K-Cgroups

- **URL:** https://kubernetes.io/docs/concepts/architecture/cgroups/
- **Type:** Official.
- **Claim:** Kubernetes uses Linux cgroups to manage resources and describes cgroup-v2 requirements and migration concerns.
- **Campaign relationship:** Supports the hierarchy context. The campaign measured one Kubernetes, CRI-O, and crun hierarchy only.

### K-Kubelet-config

- **URL:** https://kubernetes.io/docs/reference/config-api/kubelet-config.v1beta1/
- **Type:** Official.
- **Claim:** The kubelet configuration API defines fields including CFS quota controls and node resource reservation fields. Field behavior can vary by Kubernetes release.
- **Campaign relationship:** Does not test. The pilot did not change kubelet configuration.

### K-CPU-manager

- **URL:** https://kubernetes.io/docs/tasks/administer-cluster/cpu-management-policies/
- **Type:** Official.
- **Claim:** CPU Manager offers policies such as `static` for eligible workloads and documents configuration and allocation behavior.
- **Campaign relationship:** Does not test. The pilot did not test CPU Manager policy changes.

### K-Topology

- **URL:** https://kubernetes.io/docs/tasks/administer-cluster/topology-manager/
- **Type:** Official.
- **Claim:** Topology Manager coordinates resource alignment policies on a node.
- **Campaign relationship:** Does not test. The pilot did not compare topology policies.

### K-Reserve

- **URL:** https://kubernetes.io/docs/tasks/administer-cluster/reserve-compute-resources/
- **Type:** Official.
- **Claim:** `systemReserved` and `kubeReserved` reserve node resources for the operating system and Kubernetes components.
- **Campaign relationship:** Does not test. The pilot did not change reservations.

### K-Scheduler

- **URL:** https://kubernetes.io/docs/concepts/scheduling-eviction/kube-scheduler/
- **Type:** Official.
- **Claim:** The Kubernetes scheduler assigns Pods to nodes and uses resource requests in its placement decisions.
- **Campaign relationship:** Supports the placement role of requests. The campaign does not test scheduler scoring or placement policy.

### K-HPA

- **URL:** https://kubernetes.io/docs/tasks/run-application/horizontal-pod-autoscale/
- **Type:** Official.
- **Claim:** For per-Pod CPU resource metrics, HPA uses CPU utilization relative to the CPU request.
- **Campaign relationship:** Does not test. The pilot did not run HPA.

### K-VPA

- **URL:** https://kubernetes.io/docs/concepts/workloads/autoscaling/vertical-pod-autoscale/
- **Type:** Official.
- **Claim:** VPA recommends and can apply resource requests for Pods.
- **Campaign relationship:** Does not test. The pilot did not run VPA.

## Official Linux documentation

### L-Cgroup-v2

- **URL:** https://docs.kernel.org/admin-guide/cgroup-v2.html
- **Type:** Official.
- **Claim:** Cgroup v2 provides a hierarchical CPU controller. `cpu.weight` distributes active-child CPU cycles. `cpu.max` contains a maximum quota and period. `cpu.stat` reports usage and, for the documented fair-class path, quota statistics. `cpu.pressure` reports CPU pressure.
- **Campaign relationship:** Supports the cgroup controls recorded in the pilot. It does not establish an application outcome for this workload.

### L-Sched-ext

- **URL:** https://docs.kernel.org/scheduler/sched-ext.html
- **Type:** Official.
- **Claim:** sched_ext is a BPF-defined scheduler class. It receives cgroup controller changes through callbacks. A BPF scheduler can choose how to implement those semantics. Its ABI has no cross-version stability guarantee.
- **Campaign relationship:** Does not test. The pilot did not run sched_ext.

### L-EEVDF

- **URL:** https://docs.kernel.org/scheduler/sched-eevdf.html
- **Type:** Official.
- **Claim:** EEVDF documentation describes the fair scheduler's virtual-deadline behavior and tunable context.
- **Campaign relationship:** Does not test. The pilot did not compare EEVDF tuning values.

## Requested external sources

### B-Robusta

- **URL:** https://home.robusta.dev/blog/stop-using-cpu-limits
- **Type:** Opinion.
- **Claim:** The article recommends requests without CPU limits for many workloads to avoid throttling and preserve burst access to idle CPU.
- **Campaign relationship:** Partly supports. The pilot observed high throttling and descriptive throughput reductions in several low-limit CPU-bound and memory-bound cells. It does not test the article's general no-limit policy, safety conditions, or other clusters.

### B-Indeed

- **URL:** https://engineering.indeedblog.com/blog/2019/12/unthrottled-fixing-cpu-limits-in-the-cloud/
- **Type:** Opinion. It reports an engineering case study and explains CFS bandwidth behavior.
- **Claim:** A quota can cause a runnable application to wait until the next period. It proposes the ratio of throttled periods to periods as a useful diagnostic.
- **Campaign relationship:** Supports the campaign's use of throttled-period ratio and the observed finite-limit throttling. It does not test the historical kernel regression or reproduce the case study.

### B-Dnastacio

- **URL:** https://dnastacio.medium.com/why-you-should-keep-using-cpu-limits-on-kubernetes-60c4e50dfc61
- **Type:** Opinion.
- **Claim:** The article argues that CPU limits retain value for control and predictability.
- **Campaign relationship:** Does not test the article's policy claim. The pilot did not compare node-level safety or multi-tenant capacity outcomes.
- **Retrieval note:** Medium returned an access-denied response during source review. This annotation is limited to the article's stated position and does not treat it as measured evidence.

### B-Sysdig

- **URL:** https://www.sysdig.com/blog/kubernetes-cpu-requests-limits-autoscaling
- **Type:** Opinion.
- **Claim:** The article distinguishes requests used for scheduling and shares from limits used for quotas. It discusses HPA, capacity, and application concurrency.
- **Campaign relationship:** Supports the cgroup mapping and the need to measure concurrency. It does not test HPA, cluster autoscaling, or language-runtime defaults.

### B-PerfectScale

- **URL:** https://www.perfectscale.io/blog/kubernetes-cpu-limit-best-practises
- **Type:** Opinion.
- **Claim:** The article recommends measuring requests, considering concurrency, and avoiding unneeded CPU limits when performance is more important than a hard ceiling.
- **Campaign relationship:** Partly supports. The pilot supports caution with finite low limits for selected workload shapes. It does not establish the article's general recommendation.

### B-CockroachDB

- **URL:** https://docs.cockroachlabs.com/docs/stable/kubernetes-performance#resource-requests-and-limits
- **Type:** Official vendor documentation for CockroachDB, not Kubernetes upstream documentation.
- **Claim:** The page recommends resource requests for database performance and describes limits as a trade-off between predictable ceilings and access to spare capacity.
- **Campaign relationship:** Does not test CockroachDB. It supports the guide's requirement to choose limits from an explicit trade-off.

### B-Kubernetes-limits

- **URL:** https://kubernetes.io/blog/2023/11/16/the-case-for-kubernetes-resource-limits/
- **Type:** Opinion published on the Kubernetes project blog. It is not a normative API reference.
- **Claim:** The article argues that resource limits can provide predictable performance and protect cluster operation, while acknowledging efficiency trade-offs.
- **Campaign relationship:** Does not test the policy claim. The pilot measured application-level CPU behavior on one worker, not cluster-wide predictability or failure containment.

### B-Directeam

- **URL:** https://medium.com/directeam/kubernetes-resources-under-the-hood-part-3-6ee7d6015965
- **Type:** Opinion.
- **Claim:** The article explains Kubernetes resource behavior under the hood, including cgroup resource mechanisms.
- **Campaign relationship:** Supports only as explanatory context. The official Linux and Kubernetes documentation is the authority for behavior in this guide.
- **Retrieval note:** Medium returned an access-denied response during source review. This source is not used for a campaign conclusion.

### B-ErickHun

- **URL:** https://erickhun.com/posts/kubernetes-faster-services-no-cpu-limits/
- **Type:** Opinion. It reports a Buffer production case study.
- **Claim:** The article reports latency improvements after removing CPU limits. It also describes safeguards such as accurate requests, monitoring, HPA, and workload isolation.
- **Campaign relationship:** Partly supports the need to inspect throttling and protect capacity before removing limits. It does not test Buffer's workload, historical kernel condition, or reported speedup.

## Source-use rule

Use `RESULTS.md` only for the recorded pilot worker and workload cells. Use official documentation for Kubernetes and kernel behavior. Use external articles to identify hypotheses and operational trade-offs. Do not convert an external recommendation into a measured campaign fact. [RESULTS](RESULTS.md#limitations) [L-Cgroup-v2] [K-Resources]
