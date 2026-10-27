# Evidence: kernel scheduler comparison (7.6)

The files the documentation cites:

| File | What it holds |
|------|---------------|
| `kernel-comparison-20261026T212246Z.json` | The full matrix: 4 scenarios x 5 CFS modes x 2 arms, 2 repetitions of 8 seconds. 110 summary rows in total (55 per arm), no failures; the two `-rows-*.jsonl` files hold 110 raw rows per arm. `cfs-rows-20261026T212246Z.jsonl` and `scx_lavd-performance-rows-20261026T212246Z.jsonl` are the raw per-cgroup rows behind it. |
| `cpu-bandwidth-20261026T214327Z.json` | The `quota` scenario under CFS, `scx_lavd --performance` and `scx_lavd --performance --enable-cpu-bw`. This is the evidence that `cpu.max` is not enforced without the flag. |
| `preempt-20261026T214749Z.json` | The `latency` scenario under `preempt=lazy` and `preempt=full`, switched at runtime, 3 repetitions per model. |
| `survey-clean-20261027T030516Z.json` | `scx_bpfland` and `scx_mitosis` against CFS on `7.3.0-k8labs`: weights, quota and wakeup latency, 2 repetitions. |
| `cells-mitosis-20261027T031216Z.json` | The two-cell isolation test: weights 400 and 100, four threads each, CFS against `scx_mitosis`. |
| `mavd-try-20261027T022959Z.json`, `nitosis-try-20261027T023315Z.json` | The two schedulers that cannot load on `7.3.0-k8labs`. The failure text is in `arms[].error` and in the pod logs; they need `bpf_call_rcu` and the `sub_cid_sched_updated` ops field. |

Not results, kept so the history is inspectable:

- `kernel-comparison-<older timestamp>.json` and `smoke-*.json`: harness
  development before the real runs.
- `mitosis-try-*.json`: the `--cell-parent-cgroup` path was wrong
  (`/sys/fs/cgroup` was prepended to an absolute path), so the scheduler never
  attached.
- `mitosis-73-20261027T024145Z.json`: discarded. A kernel build on the same
  host as the cluster VMs starved the node, halving every CPU-second; the
  per-scenario numbers are internally consistent but not comparable with
  `survey-clean`.
- `survey-73-20261027T020637Z.json`: a valid bpfland run, superseded by
  `survey-clean`, which measures bpfland and mitosis against the same CFS
  baseline in one go.

## Reproducing

```bash
cd research/cpu-limits/phase7/7.6-kernel-comparison
python3 test-kernel-comparison.py --reps 2 --duration 8          # the matrix
python3 test-kernel-comparison.py --schedulers lavd,lavd-bw --scenarios quota \
        --modes concur --reps 3 --skip-preempt --label cpu-bandwidth  # the quota check
python3 test-kernel-comparison.py --only-preempt --reps 3 --label preempt
python3 test-kernel-comparison.py --schedulers bpfland,mitosis \
        --scenarios weighted_threads,quota,latency --modes concur --reps 2 \
        --skip-preempt --label survey-clean
python3 test-kernel-comparison.py --schedulers mitosis --scenarios quota \
        --modes concur --reps 1 --skip-preempt --cells --label cells-mitosis
```

The scheduler binaries are not in this repository. Build them with
`cargo build --release -p scx_lavd -p scx_bpfland -p scx_mitosis -p scx_mavd
-p scx_nitosis` in the scx tree and point `PHASE7_SCX_BUILD` at its
`target/release` directory if it is not at the default path used by the
harness. `scx_mavd` and `scx_nitosis` need a 7.4-class kernel to load.
