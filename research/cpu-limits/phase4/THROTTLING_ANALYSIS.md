# Phase 4: CPU Throttling Analysis

**Generated:** 2026-10-26 14:04:29 UTC  
**Experiments:** 4

---

## Executive Summary

This phase intentionally triggers CPU throttling by setting aggressive CPU limits
to demonstrate the impact on application performance. Unlike Phase 2 (where no 
throttling occurred with appropriate limits), these experiments show what happens 
when containers exceed their CPU quota.

### Key Findings


- **Maximum throttling:** cpu-bound-heavy-throttle at 69.8%
- **Minimum throttling:** cpu-bound-no-throttle at 0.0%
- **Performance range:** 19.2 to 139.1 ops/sec
- **Phase 2 baseline:** 51.5 ops/sec (2000m limit, 0% throttling)
- **Maximum performance loss:** 62.7% (heavy throttle)
- **Performance improvement:** 170.0% (no throttle, 4 workers vs 2)

---

## Experiment Results

### Comparison Table

| Configuration | Request | Limit | Throttling | Performance | vs Phase 2 |
|---------------|---------|-------|------------|-------------|------------|
| cpu-bound-moderate-throttle | 500m | 1500m | 33.2% | 53.6 ops/s | +4.0% |
| cpu-bound-light-throttle | 1000m | 3000m | 4.7% | 108.8 ops/s | +111.2% |
| cpu-bound-no-throttle | 2000m | 4000m | 0.0% | 139.1 ops/s | +170.0% |
| cpu-bound-heavy-throttle | 100m | 500m | 69.8% | 19.2 ops/s | -62.7% |

---

## Detailed Analysis

### cpu-bound-moderate-throttle

**Configuration:**
- CPU Request: `500m`
- CPU Limit: `1500m`
- Workers: 4
- Duration: 115.9s

**Expected Behavior:**
Moderate throttling - 4 workers, 1.5 CPU

**Results:**
- **Throttling Events:** 72 times
- **Throttled Duration:** 5.39s
- **Throttling Percentage:** 33.2% of desired CPU time blocked
- **CPU Usage:** 10.83s
- **Performance:** 53.6 bogo ops/sec
- **Total Operations:** 6,430

**Analysis:**
- ⚠️ **Moderate throttling** - 33.2% of desired CPU time was blocked
- Performance: 53.6 ops/sec (+4.0% vs Phase 2)
- Recommendation: Consider increasing CPU limit if performance is critical

---

### cpu-bound-light-throttle

**Configuration:**
- CPU Request: `1000m`
- CPU Limit: `3000m`
- Workers: 4
- Duration: 116.1s

**Expected Behavior:**
Light throttling - 4 workers, 3 CPU

**Results:**
- **Throttling Events:** 57 times
- **Throttled Duration:** 0.87s
- **Throttling Percentage:** 4.7% of desired CPU time blocked
- **CPU Usage:** 17.44s
- **Performance:** 108.8 bogo ops/sec
- **Total Operations:** 13,055

**Analysis:**
- ✅ **No significant throttling** - 4.7% of desired CPU time was blocked
- Performance optimal: 108.8 ops/sec (+111.2% vs Phase 2)
- Recommendation: Current limits are well-sized

---

### cpu-bound-no-throttle

**Configuration:**
- CPU Request: `2000m`
- CPU Limit: `4000m`
- Workers: 4
- Duration: 116.1s

**Expected Behavior:**
No throttling - 4 workers, 4 CPU

**Results:**
- **Throttling Events:** 0 times
- **Throttled Duration:** 0.00s
- **Throttling Percentage:** 0.0% of desired CPU time blocked
- **CPU Usage:** 26.14s
- **Performance:** 139.1 bogo ops/sec
- **Total Operations:** 16,687

**Analysis:**
- ✅ **No significant throttling** - 0.0% of desired CPU time was blocked
- Performance optimal: 139.1 ops/sec (+170.0% vs Phase 2)
- Recommendation: Current limits are well-sized

---

### cpu-bound-heavy-throttle

**Configuration:**
- CPU Request: `100m`
- CPU Limit: `500m`
- Workers: 4
- Duration: 120.9s

**Expected Behavior:**
Heavy throttling - 4 workers, only 0.5 CPU

**Results:**
- **Throttling Events:** 57 times
- **Throttled Duration:** 6.65s
- **Throttling Percentage:** 69.8% of desired CPU time blocked
- **CPU Usage:** 2.87s
- **Performance:** 19.2 bogo ops/sec
- **Total Operations:** 2,308

**Analysis:**
- ❌ **Severe throttling** - 69.8% of desired CPU time was blocked
- Performance severely degraded to 19.2 ops/sec (-62.7% vs Phase 2)
- Container wanted 9.5s but only got 2.9s
- Recommendation: Increase CPU limit significantly or reduce workload intensity

---

## Understanding CPU Throttling

### What is CPU Throttling?

CPU throttling occurs when a container's CPU usage exceeds its configured limit.
The Linux kernel's cgroup CPU controller enforces limits by:

1. Tracking CPU time used in 100ms periods
2. Comparing usage to the quota (limit × period)
3. Suspending the process when quota is exhausted
4. Resuming in the next period

### cgroup CPU Statistics

Key metrics from `/sys/fs/cgroup/cpu.stat`:

- `usage_usec`: Total CPU time actually consumed (user + system)
- `throttled_usec`: Total CPU time that was **denied** due to limits
- `nr_periods`: Number of enforcement periods elapsed
- `nr_throttled`: Number of periods where throttling occurred

### Calculating Throttling Percentage

```
throttling_pct = throttled_usec / (usage_usec + throttled_usec) × 100
```

This tells you what percentage of the container's **desired** CPU time was blocked.

**Example:** Heavy throttle configuration
- Container wanted: 16.2s of CPU
- Container got: 10.8s of CPU
- Container was denied: 5.4s (33.2%)

### Impact on Applications

**Latency-sensitive applications:**
- HTTP servers see P99 latency spikes
- Database queries take longer
- Real-time systems miss deadlines

**Throughput-focused applications:**
- Batch jobs take longer to complete
- Data processing throughput drops
- Background workers queue up

**Bursty applications:**
- Cannot burst above limit even with idle CPU
- Cold starts are slower
- Peak load handling is degraded

---

## Comparison: Phase 2 vs Phase 4

### Phase 2 (No Throttling)
- CPU-bound workload with 2000m request, 2000m limit, 2 workers
- Performance: **51.5 ops/sec**
- Throttling: **0%** (limits matched workload)

### Phase 4 Results (4 workers, varying limits)

**cpu-bound-moderate-throttle** (1500m limit)
- Performance: **53.6 ops/sec** (+4.0% vs Phase 2)
- Throttling: **33.2%**
- Throttled: 5.4s out of 16.2s desired

**cpu-bound-light-throttle** (3000m limit)
- Performance: **108.8 ops/sec** (+111.2% vs Phase 2)
- Throttling: **4.7%**
- Throttled: 0.9s out of 18.3s desired

**cpu-bound-no-throttle** (4000m limit)
- Performance: **139.1 ops/sec** (+170.0% vs Phase 2)
- Throttling: **0.0%**
- Throttled: 0.0s out of 26.1s desired

**cpu-bound-heavy-throttle** (500m limit)
- Performance: **19.2 ops/sec** (-62.7% vs Phase 2)
- Throttling: **69.8%**
- Throttled: 6.7s out of 9.5s desired


### Key Insights

1. **Heavy throttling (500m limit)** reduced performance by 62.7% compared to Phase 2
2. **Light throttling (3000m limit)** actually **improved** performance by 111.3% due to 4 workers vs 2
3. **No throttle (4000m limit)** shows maximum performance: 139.1 ops/sec (+170.1%)
4. **Throttling directly correlates with performance loss** within the same workload type

---

## Recommendations

### For Developers

1. **Monitor throttling in production**
   ```promql
   rate(container_cpu_cfs_throttled_seconds_total[5m]) > 0
   ```

2. **Size limits based on actual usage**
   - Measure P95/P99 CPU usage over time
   - Set limit = P99 + 20% headroom
   - Or use no limit and only set requests

3. **Test with limits before production**
   - Run load tests with production limits
   - Measure P99 latency and throughput
   - Adjust limits if throttling > 5%

4. **Consider request-only pattern**
   - Only set CPU requests, no limits
   - Allows bursting when cluster has capacity
   - Scheduler still ensures placement

### For Administrators

1. **Detect throttling cluster-wide**
   ```promql
   topk(10, 
     rate(container_cpu_cfs_throttled_seconds_total[5m]) /
     rate(container_cpu_cfs_periods_total[5m]) * 100
   )
   ```

2. **Alert on excessive throttling**
   - Throttling > 10% for > 5 minutes
   - Indicates undersized limits or resource contention

3. **Review resource quotas**
   - Namespace quotas may force developers to set aggressive limits
   - Balance cost control with application performance

4. **Consider node-level CPU management**
   - cpuset for CPU pinning
   - CPU isolation for latency-sensitive workloads
   - Custom schedulers for specialized needs

---

## Next Steps

### Deploy Prometheus Monitoring
```bash
cd research/cpu-limits/phase4
./deploy-prometheus.sh
kubectl port-forward -n monitoring svc/kube-prometheus-stack-grafana 3000:80
```

### Import Grafana Dashboard
1. Open http://localhost:3000
2. Import `monitoring/grafana-dashboard-throttling.json`
3. View real-time throttling metrics

### Optional: BPF Scheduler Analysis
If you have SSH access to nodes:
```bash
ssh root@<node>
sudo cp phase3/bpf-tracer.service /etc/systemd/system/
sudo cp phase3/bpf-tracer-daemon.py /usr/local/bin/
sudo systemctl enable --now bpf-tracer
```

### Optional: Scheduler Comparison
If kernel 6.12+ with scx support:
```bash
# Install scx tools
git clone https://github.com/sched-ext/scx.git
cd scx && make && sudo make install

# Run comparison
python3 analyze-scheduler-comparison.py
```

---

## References

### Kubernetes CPU Management
- [Resource Management](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/)
- [Pod QoS Classes](https://kubernetes.io/docs/concepts/workloads/pods/pod-qos/)

### Linux cgroup CPU Controller
- [cgroup v2 Documentation](https://www.kernel.org/doc/Documentation/cgroup-v2.txt)
- [CPU Controller](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html#cpu)

### Industry Perspectives
- [Stop Using CPU Limits (Robusta)](https://home.robusta.dev/blog/stop-using-cpu-limits)
- [Unthrottled: Fixing CPU Limits (Indeed)](https://engineering.indeedblog.com/blog/2019/12/unthrottled-fixing-cpu-limits-in-the-cloud/)
- [The Case for Resource Limits (Kubernetes)](https://kubernetes.io/blog/2023/11/16/the-case-for-kubernetes-resource-limits/)

---

**Experiment data:** `research/cpu-limits/phase4/evidence/`  
**Phase 2 comparison:** `research/cpu-limits/GUIDE.md`  
**Visual dashboard:** `research/cpu-limits/research-dashboard.html`
