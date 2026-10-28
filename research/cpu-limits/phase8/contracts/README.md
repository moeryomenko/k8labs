# Phase 8 contracts: CPU-limit autosizing

Frozen interfaces between the benchmark (`k8labs`) and the autosizer (`fitter`).
Both files are the single source of truth; neither project restates them in
prose-only form.

| File | Purpose |
|---|---|
| `model-bundle.schema.json` | Versioned artifact `fitter` consumes. Bounds every cell to measured conditions and forbids extrapolation. |
| `telemetry-contract.json` | Signal names, units, semantics, freshness rules, and the workload identity that makes a workload eligible. |

## Versioning

- Model schema: `cpu-limit-model/v1`. A breaking change requires `v2` and a
  migration note; `fitter` rejects unknown major versions.
- Telemetry contract: `cpu-limit-telemetry/v1`. Adding a signal is compatible;
  renaming or changing units is not.
- Workload contract: `db-cache-mixed/v1`. A workload is eligible only when its
  in-service profile and contract version equal the model's values, and the
  model declares an explicit entry for every container the policy selects.

## Integrity

`model_hash` is `sha256:` plus the hex SHA-256 of the canonical JSON of the
bundle with `model_hash` removed. Canonical JSON means sorted keys, no
insignificant whitespace, and UTF-8. Both projects implement the same helper
and test against the same fixture, so a corrupted ConfigMap entry is rejected
rather than silently applied.

## Scope of authority

A model authorizes only:

- CPU limits for containers named in its `workload.containers`.
- HPA `minReplicas`, `maxReplicas`, and CPU utilization target, only inside
  explicit policy ranges and only while every response cell stays in bounds.

A model never authorizes a Deployment template change, a container restart, or
a metric it does not declare in `telemetry.required_signals`.
