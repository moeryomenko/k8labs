# Kubernetes CPU Research - Complete Index

**Project:** k8labs CPU Limits Research  
**Duration:** October 1-26, 2026 (4 weeks)  
**Status:** ✅ COMPLETE

This document is your map to 7100+ lines of research code and documentation.

---

## Start Here

### New to This Research?

1. **Read:** `QUICK_REFERENCE.md` (2 min) - Decision tree and patterns
2. **Read:** `FINAL_SUMMARY.md` (15 min) - Phase-by-phase results
3. **Pick your path:**
   - Developer → `DEVELOPER_GUIDE.md`
   - Administrator → `ADMINISTRATOR_GUIDE.md`
   - Researcher → `PHASE7_COMPLETE.md` and `phase4/THROTTLING_ANALYSIS.md`

### Want to Run Experiments?

```bash
# Phase 2: QoS comparison
cd phase2
python3 run-experiments.py

# Phase 4: Throttling impact
cd phase4
python3 run-throttling-experiments.py

# Phase 5: Latency and BPF
cd phase5
./run-phase5.sh
```

---

## Document Map

### Executive Level (Quick Reads)

| Document | Time | Purpose |
|----------|------|---------|
| `QUICK_REFERENCE.md` | 2 min | Decision tree, formulas, anti-patterns |
| `README.md` | 5 min | Project overview and quick start |
| `FINAL_SUMMARY.md` | 15 min | Complete research summary (all phases) |

### Developer Guides

| Document | Lines | Purpose |
|----------|-------|---------|
| `GUIDE.md` | full guide | **Main deliverable** - How to set requests/limits |
| `phase5/admin-guide/PRODUCTION_PATTERNS.md` | 600+ | 5 deployment patterns with examples |
| `QUICK_REFERENCE.md` | 200+ | Quick sizing formulas and decision tree |

**Start with:** `GUIDE.md` → Then `PRODUCTION_PATTERNS.md` for your specific use case

### Administrator Guides

| Document | Lines | Purpose |
|----------|-------|---------|
| `phase5/admin-guide/KERNEL_CONFIG.md` | 350+ | CFS tuning, CPU isolation, UP/SMP |
| `phase5/admin-guide/CUSTOM_SCHEDULERS.md` | 350+ | scx guide and when to use |
| `phase5/admin-guide/PRODUCTION_PATTERNS.md` | 600+ | Deployment patterns |
| `phase4/monitoring/prometheus-alerts.yaml` | - | Production alert rules |
| `phase4/monitoring/grafana-dashboard.json` | - | 7-panel dashboard |

**Start with:** `KERNEL_CONFIG.md` → Then `CUSTOM_SCHEDULERS.md` if needed

### Research Deep-Dives

| Document | Lines | Purpose |
|----------|-------|---------|
| `phase4/THROTTLING_ANALYSIS.md` | 300+ | **Key finding:** 8x quota cut cost 7.2x throughput |
| `PHASE7_COMPLETE.md` | 230+ | Phase 7 measured results with evidence citations |
| `FINAL_SUMMARY.md` | 800+ | All experiment results and methodology |
| `phase2/evidence/*.json` | - | Raw QoS experiment data |
| `phase4/evidence/*.json` | - | Raw throttling experiment data |

**Start with:** `phase4/THROTTLING_ANALYSIS.md` for the key empirical finding

---

## By Phase

### Phase 1: Baseline Workloads

**Goal:** Establish 4 workload types  
**Status:** ✅ Complete

```
phase1/
├── workloads/
│   ├── cpu-bound.yaml          (stress-ng --cpu)
│   ├── io-bound.yaml           (stress-ng --hdd)
│   ├── memory-bound.yaml       (stress-ng --vm)
│   └── hybrid.yaml             (combined)
└── README.md
```

**Key Output:** 4 reproducible workload types

---

### Phase 2: QoS Comparison

**Goal:** Compare BestEffort, Burstable, Guaranteed  
**Status:** ✅ Complete  
**Key Finding:** a finite quota throttled 99.1-100.0% of periods in the runs
with an end snapshot; 9 of 14 runs have no end snapshot, so throttling there is
unknown

```
phase2/
├── workloads/                  (6 test manifests)
├── run-experiments.py          (Automation)
├── generate-guide.py           (GUIDE.md generator)
├── evidence/                   (6 JSON results)
├── GUIDE.md                    ⭐ main deliverable
└── README.md
```

**Key Outputs:**
- `GUIDE.md` - Comprehensive developer + admin guide
- 6 experiment evidence files
- `dashboard.html` - Visual results

**Read:** `GUIDE.md`

---

### Phase 3: BPF Infrastructure

**Goal:** Deploy kernel-level tracing  
**Status:** ✅ Complete

```
phase3/
├── bpf-tracer-daemon.py        (Collection daemon)
├── bpf-tracer.service          (Systemd unit)
├── deploy-bpf.sh               (Deployment)
└── README.md
```

**Key Output:** Systemd service collecting `sched:sched_switch`, `sched:sched_wakeup`, `sched:sched_wait`

**Usage:** Annotate pod with `bpf-trace=enabled`

---

### Phase 4: Throttling Analysis

**Goal:** Measure performance impact of CPU throttling  
**Status:** ✅ Complete  
**Key Finding:** an 8x quota cut cost 7.2x throughput (139.05 → 19.22 ops/s)

```
phase4/
├── workloads/                  (4 throttling manifests)
├── run-throttling-experiments.py
├── analyze-throttling.py
├── evidence/                   (4 JSON results) ⭐
│   ├── cpu-bound-no-throttle-*.json
│   ├── cpu-bound-light-throttle-*.json
│   ├── cpu-bound-moderate-throttle-*.json
│   └── cpu-bound-heavy-throttle-*.json ⭐
├── THROTTLING_ANALYSIS.md      ⭐ 300+ line analysis
├── monitoring/
│   ├── prometheus-alerts.yaml
│   ├── grafana-dashboard.json
│   └── deploy-prometheus.sh
└── README.md
```

**Key Outputs:**
- `THROTTLING_ANALYSIS.md` - **300+ lines** - Performance impact analysis
- 4 throttling evidence files (139.05 / 108.78 / 53.57 / 19.22 ops per sec)
- Prometheus + Grafana monitoring stack

**Read:** `THROTTLING_ANALYSIS.md` for the key research finding

---

### Phase 5: Production Readiness

**Goal:** Provide kernel tuning and deployment guidance  
**Status:** ✅ Complete

```
phase5/
├── admin-guide/
│   ├── KERNEL_CONFIG.md        ⭐ 350+ lines
│   ├── CUSTOM_SCHEDULERS.md    ⭐ 350+ lines
│   └── PRODUCTION_PATTERNS.md  ⭐ 600+ lines
├── bpf-correlation/
│   └── collect-during-throttle.py
├── experiments/
│   ├── latency-sensitive/
│   │   ├── run-latency-test.py
│   │   └── run-latency-test-simple.py
│   ├── scheduler-params/       (Optional)
│   └── scx-comparison/         (Optional)
├── evidence/
│   └── latency-impact-*.json
├── run-phase5.sh               (Interactive runner)
├── README.md
├── PHASE5_COMPLETE.md
└── IMPLEMENTATION_SUMMARY.md
```

**Key Outputs:**
- `KERNEL_CONFIG.md` - **350+ lines** - CFS tuning, CPU isolation
- `CUSTOM_SCHEDULERS.md` - **350+ lines** - scx guide
- `PRODUCTION_PATTERNS.md` - **600+ lines** - 5 deployment patterns

**Read:** All three `admin-guide/*.md` files

---

### Phase 6: Future Work (Optional Extensions)

**Goal:** Advanced research topics for specialized scenarios  
**Status:** 6.1 and 6.2 delivered as Phase 7.1 and 7.4; 6.3-6.6 not run

```
phase6/
├── README.md                   ⭐ Overview and prerequisites
├── PHASE6_SUMMARY.md           ⭐ Topic summaries
├── 6.1-latency-testing/        ✅ RESULTS SUPERSEDED BY PHASE 7.1
│   ├── README.md               ⭐ measured results + scripts
│   ├── check-wrk.sh
│   ├── run-nginx-latency-test.sh
│   ├── manifests/              (nginx configs)
│   └── wrk-scripts/            (latency reporting)
├── 6.2-bpf-traces/             ✅ RESULTS SUPERSEDED BY PHASE 7.4
│   └── README.md               (BPF correlation)
├── 6.3-kernel-tuning/          ❌ NOT RUN
│   └── README.md               (CFS experiments)
├── 6.4-scx-comparison/         ❌ NOT RUN
│   └── README.md               (Custom scheduler)
├── 6.5-multi-node/             📋 Design only
│   └── README.md               (Fairness testing)
└── 6.6-memory-limits/          📋 Design only
    └── README.md               (Parallel research)
```

**Key Topics:**
- **6.1 Latency Testing** ✅ - measured as Phase 7.1 (nginx + fixed-rate client, p50/p99)
- **6.2 BPF Traces** ✅ - measured as Phase 7.4 (bpftrace in reader pod, no node SSH)
- **6.3 Kernel Tuning** ❌ - not run (120m, would need root)
- **6.4 scx Comparison** ❌ - not run (180m, would need kernel 6.12+)
- **6.5 Multi-Node** 📋 - design only (210m, would need 3+ nodes)
- **6.6 Memory Limits** 📋 - design only (1 week)

**Read:** `phase6/PHASE6_SUMMARY.md` for complete overview and the per-topic gaps

---

## By Use Case

### "How do I size CPU requests and limits?"

1. **Read:** `QUICK_REFERENCE.md` (2 min) - Decision tree
2. **Read:** `GUIDE.md` section "Sizing Recommendations" (10 min)
3. **Read:** `phase5/admin-guide/PRODUCTION_PATTERNS.md` (20 min)
4. **Apply:** Monitor for 7 days → Use sizing formulas → Set up alerts

### "Why is my application slow?"

1. **Check:** Throttling rate
   ```promql
   rate(container_cpu_cfs_throttled_seconds_total[5m])
   ```
2. **If > 5%:** Read `phase4/THROTTLING_ANALYSIS.md`
3. **Solution:** Increase CPU limit or remove it (request-only pattern)
4. **Alternative:** Read `phase5/admin-guide/KERNEL_CONFIG.md` for tuning

### "Should I use CPU limits at all?"

1. **Read:** `GUIDE.md` section "Should You Use CPU Limits?"
2. **Read:** `phase4/THROTTLING_ANALYSIS.md` for evidence
3. **Decision:** 
   - Multi-tenant cluster → Yes (with monitoring)
   - Single-tenant cluster → Probably not (request-only)

### "How do I tune the kernel scheduler?"

1. **Read:** `phase5/admin-guide/KERNEL_CONFIG.md` (full guide)
2. **Measure first:** Collect P99 latency, context switch rate
3. **Tune:** Start with latency-sensitive preset
4. **Monitor:** Verify improvement

### "Should I use custom schedulers (scx)?"

1. **Read:** `phase5/admin-guide/CUSTOM_SCHEDULERS.md` 
2. **Section:** "When to Use scx" (spoiler: probably not)
3. **If yes:** Follow installation and migration guide

### "I want to understand the research methodology"

1. **Read:** `FINAL_SUMMARY.md` (complete overview)
2. **Read:** `phase4/THROTTLING_ANALYSIS.md` (key experiments)
3. **Review:** `phase*/evidence/*.json` (raw data)
4. **Reproduce:** Run `phase*/run-*.py` scripts

---

## By Audience

### Developers (Application Engineers)

**Priority reading order:**
1. `QUICK_REFERENCE.md` (2 min)
2. `GUIDE.md` (30 min) ⭐
3. `phase5/admin-guide/PRODUCTION_PATTERNS.md` (20 min)
4. `phase4/THROTTLING_ANALYSIS.md` (15 min) - Why throttling is bad

**Tools:**
- Sizing formulas in `QUICK_REFERENCE.md`
- PromQL queries in `GUIDE.md`
- Decision tree in `QUICK_REFERENCE.md`

### Platform/DevOps Engineers

**Priority reading order:**
1. `GUIDE.md` (30 min)
2. `phase5/admin-guide/PRODUCTION_PATTERNS.md` (20 min) ⭐
3. `phase5/admin-guide/KERNEL_CONFIG.md` (30 min)
4. `phase4/monitoring/prometheus-alerts.yaml` (5 min)

**Tools:**
- Prometheus alerts: `phase4/monitoring/prometheus-alerts.yaml`
- Grafana dashboard: `phase4/monitoring/grafana-dashboard.json`
- Deployment patterns: `phase5/admin-guide/PRODUCTION_PATTERNS.md`

### Infrastructure/SRE

**Priority reading order:**
1. `phase5/admin-guide/KERNEL_CONFIG.md` (30 min) ⭐
2. `phase5/admin-guide/CUSTOM_SCHEDULERS.md` (30 min) ⭐
3. `phase4/THROTTLING_ANALYSIS.md` (15 min)
4. `GUIDE.md` section "Administrator Guidelines" (20 min)

**Tools:**
- Kernel tuning presets: `phase5/admin-guide/KERNEL_CONFIG.md`
- scx evaluation: `phase5/admin-guide/CUSTOM_SCHEDULERS.md`
- Monitoring: `phase4/monitoring/`

### Researchers/Students

**Priority reading order:**
1. `FINAL_SUMMARY.md` (15 min) ⭐
2. `phase4/THROTTLING_ANALYSIS.md` (15 min)
3. `phase2/GUIDE.md` (30 min)
4. All `phase*/evidence/*.json` files

**Reproduce:**
```bash
# Phase 2
cd phase2 && python3 run-experiments.py

# Phase 4
cd phase4 && python3 run-throttling-experiments.py

# Phase 5
cd phase5 && ./run-phase5.sh
```

---

## Key Metrics

### Code Written

| Category | Files | Lines |
|----------|-------|-------|
| Experiment runners | 8 | 2000+ |
| Analysis scripts | 4 | 800+ |
| BPF infrastructure | 3 | 600+ |
| Monitoring | 3 | 400+ |
| **Total Code** | **18** | **3800+** |

### Documentation Written

| Category | Files | Lines |
|----------|-------|-------|
| Developer guides | 2 | 715 |
| Admin guides | 3 | 1300+ |
| Research analysis | 3 | 900+ |
| Phase READMEs | 5 | 800+ |
| **Total Docs** | **13** | **3700+** |

### Experiments Run

| Phase | Experiments | Evidence Files |
|-------|-------------|----------------|
| Phase 1 | 4 | - |
| Phase 2 | 6 | 6 JSON |
| Phase 3 | Infrastructure | - |
| Phase 4 | 4 | 4 JSON |
| Phase 5 | 2+ | 1+ JSON |
| **Total** | **16+** | **11+** |

---

## Evidence Archive

All experiment results stored in `phase*/evidence/*.json`:

### Phase 2: QoS Comparison
- `cpu-bound-besteffort-*.json` - 0.3s (cluster idle)
- `cpu-bound-burstable-*.json` - 122s, 1150/1150 periods throttled (100.0%)
- `cpu-bound-guaranteed-*.json` - 121s, no end snapshot (throttling unknown)
- `hybrid-burstable-*.json` - 121s, 1186/1186 periods throttled (100.0%)
- `io-bound-guaranteed-*.json` - 8.4s, no end snapshot (throttling unknown)
- `memory-bound-guaranteed-*.json` - 122s, 1170/1172 periods throttled (99.8%)

### Phase 4: Throttling Impact ⭐
- `cpu-bound-no-throttle-*.json` - 139.05 ops/s (baseline)
- `cpu-bound-light-throttle-*.json` - 108.78 ops/s (-21.8%)
- `cpu-bound-moderate-throttle-*.json` - 53.57 ops/s (-61.5%)
- `cpu-bound-heavy-throttle-*.json` - 19.22 ops/s (-86.2%)
- `throttling-summary.json` - the authoritative 4-config summary

These runs captured `nr_periods`/`nr_throttled` in the baseline snapshot only,
so cite the throughput figures. Throttle percentages for Phase 4 do not exist;
see the note in `THROTTLING_ANALYSIS.md`.

### Phase 5: Latency Impact
- `latency-impact-*.json` - Task duration under throttling

---

## Tools and Scripts

### Experiment Runners

```bash
# Phase 2: QoS experiments
phase2/run-experiments.py

# Phase 4: Throttling experiments
phase4/run-throttling-experiments.py

# Phase 5: Interactive menu
phase5/run-phase5.sh

# Phase 5: Specific experiments
phase5/bpf-correlation/collect-during-throttle.py
phase5/experiments/latency-sensitive/run-latency-test.py
```

### Analysis Scripts

```bash
# Phase 2: Generate GUIDE.md
phase2/generate-guide.py

# Phase 2: Generate dashboard
phase2/generate-dashboard.py

# Phase 4: Analyze throttling
phase4/analyze-throttling.py
```

### Monitoring Deployment

```bash
# Deploy Prometheus + Grafana
phase4/monitoring/deploy-prometheus.sh

# Deploy BPF tracing
phase3/deploy-bpf.sh
```

---

## Visual Artifacts

| File | Type | Purpose |
|------|------|---------|
| `dashboard.html` | HTML | Phase 2 interactive results |
| `complete-research-dashboard.html` | HTML | All phases visual summary |
| `phase4/monitoring/grafana-dashboard.json` | JSON | 7-panel Grafana dashboard |

Open in browser for visual exploration.

---

## FAQ

### "Which document should I read first?"

**If you have 2 minutes:** `QUICK_REFERENCE.md`  
**If you have 15 minutes:** `FINAL_SUMMARY.md`  
**If you have 1 hour:** `GUIDE.md` + `phase5/admin-guide/PRODUCTION_PATTERNS.md`

### "I just want to know how to size my pods"

Read `QUICK_REFERENCE.md` - Has decision tree and sizing formulas

### "I want the key research finding"

**Heavy throttle (Phase 4): 139.05 → 19.22 ops/sec, -86.2%**

Read `phase4/THROTTLING_ANALYSIS.md` for details

### "How do I cite this research?"

```
k8labs Kubernetes CPU Research
October 2026
https://github.com/<your-org>/k8labs/tree/main/research/cpu-limits
```

### "Can I reuse this methodology?"

Yes! All scripts are in `phase*/run-*.py`. Modify workloads in `phase*/workloads/*.yaml`.

### "Where's the raw data?"

`phase*/evidence/*.json` - All experiment results in JSON format

---

## Next Steps

### For Developers
1. Read `GUIDE.md`
2. Monitor your workloads for 7 days
3. Apply sizing formulas from `QUICK_REFERENCE.md`
4. Set up throttling alerts
5. Choose deployment pattern from `PRODUCTION_PATTERNS.md`

### For Administrators
1. Read all three `phase5/admin-guide/*.md` files
2. Deploy monitoring: `phase4/monitoring/deploy-prometheus.sh`
3. Set resource quotas per namespace
4. Document cluster CPU capacity
5. Tune kernel only if needed (measure first)

### For Researchers
1. Read `FINAL_SUMMARY.md` for methodology
2. Review `phase*/evidence/*.json` for raw data
3. Reproduce experiments with `phase*/run-*.py`
4. Extend with your own workloads

---

## Support

**Project:** k8labs CPU Limits Research  
**Status:** ✅ COMPLETE  
**Duration:** October 1-26, 2026 (4 weeks)  
**Total:** 7100+ lines of code and documentation  

**Questions?** Open a GitHub issue  
**Contributions?** Pull requests welcome

---

**This index is your map. Pick your path and dive in!** 🚀
