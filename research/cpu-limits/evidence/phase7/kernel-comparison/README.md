# Evidence: kernel scheduler comparison (7.6)

The files the documentation cites:

| File | What it holds |
|------|---------------|
| `kernel-comparison-20261026T212246Z.json` | The full matrix: 4 scenarios x 5 CFS modes x 2 arms, 2 repetitions of 8 seconds. 110 summary rows in total (55 per arm), no failures; the two `-rows-*.jsonl` files hold 110 raw rows per arm. `cfs-rows-20261026T212246Z.jsonl` and `scx_lavd-performance-rows-20261026T212246Z.jsonl` are the raw per-cgroup rows behind it. |
| `cpu-bandwidth-20261026T214327Z.json` | The `quota` scenario under CFS, `scx_lavd --performance` and `scx_lavd --performance --enable-cpu-bw`. This is the evidence that `cpu.max` is not enforced without the flag. |
| `preempt-20261026T214749Z.json` | The `latency` scenario under `preempt=lazy` and `preempt=full`, switched at runtime, 3 repetitions per model. |

Everything else in this directory is development scaffolding: small runs used to
validate the harness before the real ones, named `kernel-comparison-<older
timestamp>.json` or `smoke-*.json`. They are kept rather than deleted, but they
are not results and nothing cites them.

## Reproducing

```bash
cd research/cpu-limits/phase7/7.6-kernel-comparison
python3 test-kernel-comparison.py --reps 2 --duration 8          # the matrix
python3 test-kernel-comparison.py --scenarios quota --modes concur \
        --reps 3 --scx-bw --skip-preempt --label cpu-bandwidth   # the quota check
python3 test-kernel-comparison.py --only-preempt --reps 3 --label preempt
```

The scheduler binary is not in this repository. Build `scx_lavd` from the scx
tree and point `PHASE7_SCX_BUILD` at its `target/release` directory if it is not
at the default path used by the harness.
