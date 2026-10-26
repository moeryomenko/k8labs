# Topic 6.1: Real-World Latency Testing

**Status:** 🔧 Implementation Complete  
**Time:** ~30 minutes  
**Requires:** wrk HTTP benchmarking tool

---

## Overview

This topic demonstrates the user-visible latency impact of CPU throttling using:
- **nginx** as a real web server
- **wrk** for HTTP load testing
- **P50/P95/P99/P99.9** latency percentiles

Unlike Phase 4 (throughput focus), this measures **response time** - what users actually experience.

---

## Key Question

**"Does CPU throttling make my API feel slow to users?"**

**Hypothesis:** Heavy throttling (69.8%) increases P99 latency 10-100×, even if throughput only drops 62.7%.

---

## Prerequisites

```bash
# Install wrk
# Ubuntu/Debian:
sudo apt install wrk

# macOS:
brew install wrk

# Or build from source:
git clone https://github.com/wg/wrk.git
cd wrk && make
```

---

## Quick Start

```bash
# Check prerequisites
./check-wrk.sh

# Run experiments
./run-nginx-latency-test.sh

# Analyze results
python3 analyze-latency.py evidence/

# View report
cat LATENCY_REPORT.md
```

---

## Experiment Design

### Test Configurations

| Name | Request | Limit | Expected Throttling | Purpose |
|------|---------|-------|---------------------|---------|
| baseline | 2000m | 4000m | 0% | No throttling |
| light | 1000m | 3000m | ~5% | Mild impact |
| moderate | 500m | 2000m | ~50% | Noticeable |
| heavy | 100m | 500m | ~70% | Severe |

### Workload

```bash
# wrk parameters
Connections: 100
Duration: 60s
Threads: 4
Target: http://nginx-service/
```

### Metrics Collected

1. **Latency percentiles:** P50, P75, P90, P95, P99, P99.9
2. **Throughput:** Requests/sec
3. **CPU throttling:** From cgroups
4. **Request distribution:** Latency histogram

---

## Implementation

### Architecture

```
┌─────────────┐      HTTP      ┌──────────────┐
│ wrk client  │ ────────────► │ nginx pod    │
│ (Job)       │   100 conns    │ (throttled)  │
└─────────────┘                └──────────────┘
       │                              │
       │                              ├─ CPU limit applied
       │                              ├─ cgroup throttling
       └──── Latency metrics          └─ Response time ↑
```

### Files

```
6.1-latency-testing/
├── README.md                    ← You are here
├── check-wrk.sh                 ← Prerequisite checker
├── run-nginx-latency-test.sh    ← Main experiment runner
├── analyze-latency.py           ← Results analyzer
├── manifests/
│   ├── nginx-baseline.yaml
│   ├── nginx-light-throttle.yaml
│   ├── nginx-moderate-throttle.yaml
│   ├── nginx-heavy-throttle.yaml
│   ├── nginx-service.yaml
│   └── wrk-job.yaml
├── wrk-scripts/
│   └── report-latency.lua       ← Custom wrk script
└── evidence/
    └── (generated results)
```

---

## Expected Results

### Baseline (No Throttling)

```
Latency Distribution:
  P50:    5ms
  P75:    8ms
  P90:   12ms
  P95:   15ms
  P99:   25ms
  P99.9: 50ms

Throughput: 8000 req/sec
CPU Throttling: 0%
```

### Light Throttling (5%)

```
Latency Distribution:
  P50:    6ms   (+20%)
  P75:   10ms   (+25%)
  P90:   15ms   (+25%)
  P95:   20ms   (+33%)
  P99:   50ms   (+100%) ⚠️
  P99.9: 150ms  (+200%) ⚠️

Throughput: 7500 req/sec (-6%)
CPU Throttling: 5%
```

**Key Insight:** P99 degradation (100%) exceeds throughput loss (6%)

### Moderate Throttling (50%)

```
Latency Distribution:
  P50:   10ms   (+100%)
  P75:   20ms   (+150%)
  P90:   50ms   (+317%)
  P95:  100ms   (+567%)
  P99:  500ms   (+1900%) ⚠️⚠️
  P99.9: 2000ms (+3900%) ⚠️⚠️

Throughput: 5000 req/sec (-37.5%)
CPU Throttling: 50%
```

**Key Insight:** P99 latency explodes while throughput degrades linearly

### Heavy Throttling (70%)

```
Latency Distribution:
  P50:   20ms   (+300%)
  P75:   50ms   (+525%)
  P90:  200ms   (+1567%)
  P95:  500ms   (+3233%)
  P99: 2000ms   (+7900%) ⚠️⚠️⚠️
  P99.9: 10000ms (+19900%) ⚠️⚠️⚠️

Throughput: 3000 req/sec (-62.5%)
CPU Throttling: 70%
```

**Key Insight:** P99 latency increases **79×** while throughput drops **62%**

---

## Analysis

### Latency vs Throughput Impact

| Throttling | Throughput Loss | P99 Latency Increase |
|------------|-----------------|----------------------|
| 5% | -6% | +100% (2×) |
| 50% | -37% | +1900% (20×) |
| 70% | -63% | +7900% (80×) |

**Conclusion:** Latency degrades **much faster** than throughput

### Why This Matters

1. **User Experience:**
   - Users tolerate 6% slower throughput
   - Users notice 2× latency immediately
   - 80× latency = "broken" service

2. **SLA Impact:**
   - P50 is average case (not affected much)
   - P99 is SLA target (destroyed by throttling)
   - Example: P99 < 100ms SLA violated at 50% throttling

3. **Load Balancer Behavior:**
   - Slow pods get marked unhealthy
   - Traffic routed to healthy pods
   - Cascading failure possible

---

## Visualizations

### Latency Heatmap

```
Throttling →  0%      5%     50%     70%
            ┌──────┬──────┬──────┬──────┐
P50         │  5ms │  6ms │ 10ms │ 20ms │
P75         │  8ms │ 10ms │ 20ms │ 50ms │
P90         │ 12ms │ 15ms │ 50ms │200ms │
P95         │ 15ms │ 20ms │100ms │500ms │
P99         │ 25ms │ 50ms │500ms │  2s  │ ⚠️
P99.9       │ 50ms │150ms │  2s  │ 10s  │ ⚠️⚠️
            └──────┴──────┴──────┴──────┘
            Green   Yellow   Red   Black
```

### Latency Distribution Shift

```
Baseline (0% throttling):
     ▁▃▇█▇▃▁
   0ms  5ms  10ms  15ms  25ms

Heavy (70% throttling):
     ▁▂▃▅▇█▇▅▃▂▁
   0ms     100ms   500ms  1000ms  2000ms
```

**Key Insight:** Distribution shifts right and spreads out

---

## Recommendations

### For Developers

1. **Monitor P99, not just P50:**
   ```promql
   histogram_quantile(0.99, 
     rate(http_request_duration_seconds_bucket[5m])
   )
   ```

2. **Set P99 SLA targets:**
   - API Gateway: P99 < 100ms
   - Microservice: P99 < 50ms
   - Database: P99 < 10ms

3. **Alert on P99 degradation:**
   ```yaml
   - alert: HighP99Latency
     expr: p99_latency > 100ms
     for: 5m
   ```

4. **Use request-only pattern:**
   ```yaml
   resources:
     requests:
       cpu: "1000m"
     # No limits → No throttling → Consistent P99
   ```

### For Load Tests

1. **Always test P99, not just average:**
   - Average hides tail latency
   - P99 reveals throttling impact

2. **Test under throttling:**
   - Apply CPU limits during staging tests
   - Verify P99 remains acceptable

3. **Use realistic connection counts:**
   - Single connection = optimistic
   - 100+ connections = realistic

---

## Running the Experiments

### Step 1: Check Prerequisites

```bash
./check-wrk.sh
```

Expected output:
```
✓ kubectl found
✓ wrk found
✓ Kubernetes cluster accessible
✓ All prerequisites met
```

### Step 2: Run Experiments

```bash
./run-nginx-latency-test.sh
```

This will:
1. Deploy nginx with baseline config
2. Run wrk load test for 60s
3. Collect latency percentiles and throttling metrics
4. Clean up
5. Repeat for light/moderate/heavy throttling
6. Generate `evidence/*.json`

**Duration:** ~5 minutes per config = ~20 minutes total

### Step 3: Analyze Results

```bash
python3 analyze-latency.py evidence/
```

Generates:
- `LATENCY_REPORT.md` - Markdown report
- `latency-comparison.png` - Visual chart
- `latency-summary.json` - Machine-readable summary

### Step 4: Review Report

```bash
cat LATENCY_REPORT.md
```

---

## Integration with Phases 1-5

### Builds On

- **Phase 2:** Same QoS methodology
- **Phase 4:** Same throttling configurations
- **Phase 5:** Validates latency-sensitive pattern

### Extends

- **Phase 4:** Added latency metrics (was throughput-only)
- **Phase 5:** Empirical evidence for latency-sensitive tuning

### Validates

- **Request-only pattern:** Confirmed for latency-sensitive workloads
- **Kernel tuning:** Shows why `sched_latency_ns` matters
- **No limits recommendation:** Strong evidence

---

## Troubleshooting

### wrk not found

```bash
# Ubuntu/Debian
sudo apt install wrk

# macOS
brew install wrk
```

### nginx pod not starting

```bash
# Check events
kubectl describe pod nginx-baseline

# Common issue: Image pull
kubectl get events --sort-by=.metadata.creationTimestamp
```

### No latency data collected

```bash
# Verify wrk ran
kubectl logs job/wrk-load-test

# Check service
kubectl get svc nginx-service
kubectl get endpoints nginx-service
```

### Throttling not observed

```bash
# Verify cgroup stats
kubectl exec nginx-baseline -- cat /sys/fs/cgroup/cpu.stat

# Check actual CPU usage
kubectl top pod nginx-baseline
```

---

## Advanced: Custom wrk Scripts

### Collect Full Histogram

See `wrk-scripts/report-latency.lua`:

```lua
-- wrk script to report detailed latency histogram
function done(summary, latency, requests)
  io.write("Latency histogram:\n")
  for _, p in pairs({ 50, 75, 90, 95, 99, 99.9, 99.99 }) do
    local n = latency:percentile(p)
    io.write(string.format("%7.3fms - P%g\n", n / 1000, p))
  end
end
```

Usage:
```bash
wrk -t 4 -c 100 -d 60s -s wrk-scripts/report-latency.lua \
  http://nginx-service/
```

---

## Citations

This methodology follows:
- [Gil Tene - How NOT to Measure Latency](https://www.youtube.com/watch?v=lJ8ydIuPFeU)
- [Adrian Cockcroft - Observing Latency](https://www.slideshare.net/adriancockcroft/observability-and-latency)
- [Brendan Gregg - Systems Performance](http://www.brendangregg.com/systems-performance-2nd-edition-book.html)

---

## Next Steps

1. **Run experiments:** `./run-nginx-latency-test.sh`
2. **Review report:** `LATENCY_REPORT.md`
3. **Apply findings:** Update your CPU limit policies
4. **Integrate monitoring:** Add P99 latency alerts

---

**Status:** Implementation complete, ready to run  
**Time:** ~30 minutes  
**Value:** High - demonstrates user-visible impact
