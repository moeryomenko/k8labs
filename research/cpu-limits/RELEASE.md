# CPU-limits release record

## Status

**Partially measured.**

The live gate passed on the selected custom-kernel worker. The pilot completed
three blocks with 36 complete cells in each block. The pilot has 108 complete
cells.

Policy calibration failed for `cpu-bound`, `io-bound`, and `hybrid`. The
calibration process blocked the confirmatory campaign. The campaign collected no
confirmatory workload, block, cell, metric, cgroup snapshot, or trace. This
release does not claim a confirmatory result.

## Reproduce the recorded release

Run the commands from the repository root. These commands check the fixed
matrix, the offline suite, and the stored pilot analysis.

```sh
task cpu-limits-plan
task cpu-limits-validate
task cpu-limits-test
uv run pytest -q research/cpu-limits/tests
uv run ruff check research/cpu-limits
find research/cpu-limits -type f -name '*.py' -print0 | xargs -0 -r python3 -m py_compile
find research/cpu-limits -type f -name '*.sh' -print0 | xargs -0 -r -n1 bash -n
find research/cpu-limits -type f -name '*.sh' -print0 | xargs -0 -r shellcheck
```

Regenerate the stored pilot analysis in a temporary directory. The comparison
commands must produce no output.

```sh
output_dir="$(mktemp -d /tmp/opencode/cpu-limits-release.XXXXXX)"
PYTHONHASHSEED=4 PYTHONPATH=research/cpu-limits python3 research/cpu-limits/analysis/analyze.py \
  --phase pilot \
  --blocks build/research/cpu-limits/pilot/blocks.csv \
  --cgroups build/research/cpu-limits/pilot/cgroups.csv \
  --metrics build/research/cpu-limits/pilot/metrics.csv \
  --trace-rows build/research/cpu-limits/pilot/analysis/trace-rows.json \
  --output-dir "$output_dir"
cmp build/research/cpu-limits/pilot/analysis/summary.csv "$output_dir/summary.csv"
cmp build/research/cpu-limits/pilot/analysis/summary.json "$output_dir/summary.json"
cmp build/research/cpu-limits/pilot/analysis/summary.md "$output_dir/summary.md"
PYTHONHASHSEED=4 PYTHONPATH=research/cpu-limits python3 research/cpu-limits/analysis/plots.py \
  --summary "$output_dir/summary.json" \
  --output-dir "$output_dir/plots"
diff -r build/research/cpu-limits/pilot/analysis/plots "$output_dir/plots"
```

## Reproduce a new live campaign

A new live run needs a Ready worker, `build/kubeconfig`, a collector Pod, and
a local port forward to the collector metrics endpoint. Set the worker and
collector values before the commands below.

```sh
export CPU_LIMITS_NODE="k8labs-md-custom-4vcpu-qq498-zlz6p-6n655"
export CPU_LIMITS_COLLECTOR_POD="<collector-pod-name>"
export CPU_LIMITS_COLLECTOR_URL="http://127.0.0.1:9100/metrics"

KUBECONFIG=build/kubeconfig research/cpu-limits/observers/apply.sh apply
kubectl --kubeconfig build/kubeconfig -n cpu-limits port-forward pod/$CPU_LIMITS_COLLECTOR_POD 9100:9100
task cpu-limits-gate
task cpu-limits-pilot
task cpu-limits-freeze
task cpu-limits-sample-size
```

Keep the port-forward command running in a separate terminal. Stop after the
freeze and sample-size records. Do not run `task cpu-limits-confirm` for this
release. Both records have status `FAIL`, so the confirmatory campaign remains
blocked.

## Data paths and retention

The series stores run data outside Git. The data root is:

```text
${K8LABS_RESEARCH_DATA_ROOT:-<repo>/build}/research/cpu-limits/
```

- Pilot records: `pilot/blocks.csv`, `pilot/cgroups.csv`, and
  `pilot/metrics.csv`.
- Pilot analysis: `pilot/analysis/`, including `trace-rows.json`, summaries,
  and generated plots.
- Freeze records: `policies.json` and `sample-size.json`.
- Confirmatory record: `confirmatory/SKIPPED.md`.
- Confirmatory raw data: absent. The campaign did not start.

Keep the pilot data root and its raw trace inputs outside Git for the lifetime
of this release. Do not use `task cpu-limits-clean` for the recorded pilot data.
The repository retains release documents and figures. It does not retain raw run
data. This series has no time-based retention period.

## Block counts and sample size

- Pilot blocks: 3 complete blocks.
- Pilot cells: 108 complete cells. Each block has 36 complete cells.
- Confirmatory blocks: 0. No confirmatory cell exists.
- Confirmatory sample size: not authorized. `policies.json` and
  `sample-size.json` have status `FAIL`.

## Known limitations

- The pilot is descriptive. It has one worker and three blocks.
- Calibration did not freeze valid low limits for `cpu-bound`, `io-bound`,
  and `hybrid`.
- The confirmatory campaign did not run.
- Throttle-gap contrasts are incomplete where the cgroup had zero CFS periods.
- The trace values are pilot-descriptive evidence from selected five-second
  slices. They do not describe every cell or a full measurement window.
- The campaign collected no cgroup-mode, base-slice, or `sched_ext` measurement.

## Final verifier verdict

The final verifier verdict is **PASS**. See
`.agent/tasks/cpu-limits-21-final-verify/verdict.json`.

The verifier passed VC-01 through VC-08. It verified the evidence records, the
offline suite, pilot reproducibility, trace classification, protected paths,
commit metadata, guide limits, and fail-closed confirmatory status.

The verifier PASS means that the recorded release is consistent and reproducible.
It does not change the campaign status. The campaign remains partially measured.

## Next steps

1. Run a new pilot calibration for the three workloads without a valid low limit.
2. Create passing `policies.json` and `sample-size.json` records.
3. Run confirmatory blocks only after both records pass.
4. Run focused cgroup-mode, base-slice, and `sched_ext` experiments only after
   successful freeze records authorize the confirmatory campaign.
