# cpu-limits live gate record

- Task: `cpu-limits-12-live-gate`
- Timestamp: `2026-10-24T11:51:19Z`
- Kubeconfig: `build/kubeconfig`
- Selected node: `k8labs-md-custom-4vcpu-qq498-zlz6p-6n655`
- Kernel: `7.3.0-k8labs`
- Collector image: `ghcr.io/eryoma/k8labs-cpu-limits-collector:0.1.0`
- Overall status: **FAIL**

The `cpu-limits` namespace is absent. The collector manifest could not create
its ServiceAccount, Role, RoleBinding, or DaemonSet. The collector was not
Ready. The failed collector gate blocks all host snapshot, metrics, image-pull,
noise-floor, and trace gates. No workload was started.

| gate | status | evidence |
|---|---|---|
| Workload cluster is reachable and selected worker is Ready | PASS | Selected node is `Ready`. |
| Selected worker name and required custom kernel | PASS | Node name contains `md-custom`; kernel is `7.3.0-k8labs`. |
| Only the selected worker has `cpu-limits/research=collector` | PASS | The label selector returned one node. The stock worker and control-plane node had no collector label. |
| Collector manifest applies and collector is Ready | FAIL | The namespace `cpu-limits` does not exist. The DaemonSet was not created. |
| cgroup v2 and the CPU controller are available | FAIL | Blocked: no collector Pod exists for the read-only host snapshot. |
| `cpu.max` and `cpu.weight` are readable | FAIL | Blocked: no collector Pod exists for the read-only host snapshot. |
| `sched/cgroup_mode` and `sched/base_slice_ns` are readable | FAIL | Blocked: no collector Pod exists for the read-only host snapshot. |
| `bpftrace`, `perf`, `wrk2`, and `/fixtures/input.mp4` are available | FAIL | Blocked: no collector Pod exists for the read-only host snapshot. |
| Collector `/metrics` exposes all required metric families | FAIL | Blocked: no collector Pod exists. |
| Ten-second steal and PSI noise floor is below the frozen threshold | FAIL | Blocked: no collector Pod exists for the read-only host snapshot. |
| nginx, stress-ng, and ffmpeg images pull | FAIL | Blocked by the failed collector gate. No workload or image-pull Pod was created. |
| Short probe reads `sched_switch` and `throttle_cfs_rq` events | FAIL | Blocked: no collector Pod exists for the read-only host snapshot. |
| No control-plane node is modified | PASS | The collector label is present only on the selected custom worker. The control-plane node has no collector label. |
| Target Pods become Ready before the timeout | FAIL | Blocked by the failed collector gate. No target Pod was created. |
| Every recorded block has 36 cells | FAIL | Blocked by the failed collector gate. No block was created. |
| `policies.json` and `sample-size.json` exist before confirmatory runs | FAIL | Blocked by the failed collector gate. No confirmatory run was attempted. |

## Raw command output

### Label selected worker

```text
$ kubectl --kubeconfig build/kubeconfig label node k8labs-md-custom-4vcpu-qq498-zlz6p-6n655 cpu-limits/research=collector
node/k8labs-md-custom-4vcpu-qq498-zlz6p-6n655 labeled
```

### Verify label scope and selected kernel

```text
$ kubectl --kubeconfig build/kubeconfig get nodes -l cpu-limits/research=collector -o wide
NAME                                       STATUS   ROLES    AGE    VERSION   INTERNAL-IP     EXTERNAL-IP   OS-IMAGE                          KERNEL-VERSION         CONTAINER-RUNTIME
k8labs-md-custom-4vcpu-qq498-zlz6p-6n655   Ready    <none>   4h2m   v1.37.0   192.168.124.3   <none>        Fedora Linux 44 (Cloud Edition)   7.3.0-k8labs (amd64)   cri-o://1.37.0

$ kubectl --kubeconfig build/kubeconfig get nodes -o custom-columns=NAME,READY,KERNEL,COLLECTOR
NAME                                       READY   KERNEL                                           COLLECTOR
k8labs-md-0-hbgwg-5z54n-stxvt              True    7.3.0-65.fc46.x86_64                             <none>
k8labs-md-custom-4vcpu-qq498-zlz6p-6n655   True    7.3.0-k8labs                                     collector
k8labs-xzc6j-0                             True    7.3.0-0.rc7.261014g4056b3c9d3c9.60.fc46.x86_64   <none>
```

### Apply collector DaemonSet

```text
$ KUBECONFIG=build/kubeconfig research/cpu-limits/observers/apply.sh apply
Error from server (NotFound): error when creating "/home/eryoma/workspace/blog/k8labs/research/cpu-limits/observers/collector-daemonset.yaml": namespaces "cpu-limits" not found
Error from server (NotFound): error when creating "/home/eryoma/workspace/blog/k8labs/research/cpu-limits/observers/collector-daemonset.yaml": namespaces "cpu-limits" not found
Error from server (NotFound): error when creating "/home/eryoma/workspace/blog/k8labs/research/cpu-limits/observers/collector-daemonset.yaml": namespaces "cpu-limits" not found
Error from server (NotFound): error when creating "/home/eryoma/workspace/blog/k8labs/research/cpu-limits/observers/collector-daemonset.yaml": namespaces "cpu-limits" not found
```

The apply command exited with status `1`.

### Collector status after the apply failure

```text
$ kubectl --kubeconfig build/kubeconfig -n cpu-limits get daemonset,pods -l app.kubernetes.io/name=cpu-limits-collector -o wide
No resources found in cpu-limits namespace.
```
