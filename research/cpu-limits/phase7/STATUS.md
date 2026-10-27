# Phase 7: Status

**Date**: 2026-10-27  
**State**: all eight topics implemented, seven measured on the cluster, 7.8 computed from Phase 4 data

All measurements ran on one 4-core node (`kernel 7.3.0-k8labs`) through a
reader pod that mounts the host cgroupfs. 7.7 additionally drained one 2-core
worker and uncordoned it afterwards. 7.6 and 7.7 run last because they take
the node over for their duration.

---

## Topic Status

| Topic | State | Script | Evidence |
|-------|-------|--------|----------|
| 7.1 Real application (nginx) | Measured | `7.1-real-apps/test-nginx-throttling.py` | `evidence/phase7/real-apps/nginx-throttling-20261026T191420Z.json` |
| 7.2 QoS class comparison | Measured | `7.2-qos-comparison/test-qos-classes.py` | `evidence/phase7/qos-comparison/qos-classes-20261026T191440Z.json` |
| 7.3 Noisy neighbour | Measured | `7.3-interference/test-noisy-neighbor.py` | `evidence/phase7/interference/noisy-neighbor-20261026T191852Z.json` |
| 7.4 Live BPF scheduler trace | Measured | `7.4-live-bpf/trace-scheduler.py` | `evidence/phase7/live-bpf/scheduler-trace-20261026T192056Z.json` |
| 7.5 cgroup v2 CPU parameters | Measured | `7.5-cgroup-deep-dive/analyze-cgroup-v2.py` | `evidence/phase7/cgroup-v2/cgroup-v2-analysis-20261026T190908Z.json` |
| 7.6 Kernel scheduler comparison | Measured | `7.6-kernel-comparison/test-kernel-comparison.py` | `evidence/phase7/kernel-comparison/kernel-comparison-20261026T212246Z.json`, `cpu-bandwidth-20261026T214327Z.json`, `preempt-20261026T214749Z.json`, `survey-clean-20261027T030516Z.json`, `cells-mitosis-20261027T031216Z.json` |
| 7.7 Incident simulation | Measured | `7.7-incidents/test-incidents.py` | `evidence/phase7/incidents/incidents-20261027T000538Z.json`, `incidents-run2-20261027T002520Z.json`, `drain-pdb-20261026T235150Z.json` |
| 7.8 Cost / TCO model | Modelled | `7.8-cost-analysis/calculate-tco.py` | `evidence/phase7/cost-analysis/tco-model-20261026T190918Z.json` |

---

## Headline Numbers

| Topic | Result |
|-------|--------|
| 7.1 | No limit 7216 req/s p99 13.98 ms; 120m limit 3481 req/s p99 69.43 ms; 70m limit 2071 req/s p99 98.87 ms |
| 7.2 | Weights read from pod cgroups: BestEffort 1 / nice 19, Burstable 59 / 2, Guaranteed 100 / 0. p99 65.44 / 31.28 / 30.99 ms |
| 7.3 | Capping four burners at 200m cut their CPU 75% (149.1 s -> 37.8 s); victim p50 -79%, p99 -2.9% |
| 7.4 | Mean wake-to-run wait 6.4 us -> 9.6 us -> 13.7 us as the limit tightens; max wait up to 71.7 ms |
| 7.5 | 100m request -> weight 17 / nice 8; 100m limit -> `10000 100000`; `cpu.max.burst` 0 |
| 7.6 | `scx_lavd` ignores `cpu.max`: 12.22 CPU-s against a 4.0 CPU-s quota; `--enable-cpu-bw` restores 4.01 CPU-s. Thread count beats cgroup weight in all five CFS modes. `scx_bpfland` and `scx_mitosis` ignore it too (12.48 and 12.91 CPU-s); mitosis isolates two cells 50/50 where CFS splits them 75/25 by weight |
| 7.7 | A 120m pod served 1453 of 3000 req/s at p99 102 ms under a spike; a 3-replica service lost a pod with no client errors and took 42 s to replace it |
| 7.8 | Upper bound ~$2,755/month wasted capacity per throttled scenario; revenue model not supplied |

Full detail, including the caveats, is in `../PHASE7_COMPLETE.md`.

---

## Notes For The Next Run

- 7.6 requires the `7.3.0-k8labs` node; the CFS group modes and sched_ext do
  not exist on the stock kernels of the other two nodes.
- 7.7 uses `phase7/lib/httpd.c` rather than the nginx image, because the node
  garbage-collected that image and Docker Hub rate-limits anonymous pulls.
  The harness publishes the binary as two ConfigMaps and assembles it in an
  init container; it never writes to the node's filesystem.
- Two attempts at 7.7 were interrupted by cluster problems, not by the
  scripts: a required `nodeSelector` in the drain scenario (fixed) and a
  control-plane TLS handshake timeout. Both are visible in the evidence
  directory as earlier files; the ones cited above are the clean runs.
- The HPA in 7.7 never scaled. That is a measurement of this cluster's metrics
  path, not a claim about HPAs in general.
- `scx_mavd` and `scx_nitosis` build and are staged, but do not load on
  `7.3.0-k8labs`: they need `bpf_call_rcu`/`bpf_rcu_head` and the
  `sub_cid_sched_updated` ops field, which that kernel does not have. A
  `next-20261016` kernel with them is built at `build/kernel-custom-next`, but
  installing a kernel requires the signed-image bake (PKCS#11 token, host image
  staging, CAPI machines), none of which is available here.
- PREEMPT_RT and sched_ext configure and compile together; `olddefconfig` only
  drops sched_ext when `pahole` is missing, which is a toolchain accident, not
  a kernel conflict. The runtime combination is not booted.
