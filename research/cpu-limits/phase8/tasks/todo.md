# CPU-limit autosizing: benchmark and model (phase 8)

Plan: `/home/eryoma/.plannotator/plans/plan-cpu-limit-benchmark-and-a-2026-10-27-approved.md`

Baseline pinned by live probe: Kubernetes v1.37.0, CRI-O 1.37.0, Cilium,
in-place pod resize verified (`ResizeStarted`/`ResizeCompleted` events).

## Phase 1: Contracts (shared by k8labs and fitter)

- [ ] T1: Freeze telemetry schema, workload identity, model bundle schema
- [ ] T2: Freeze experiment design (factors, pilot, budget, validity gates)
- [ ] T3: Freeze joint CPU/HPA objective, policy ranges, rollback semantics

## Checkpoint A
- [ ] Schemas validate in both projects
- [ ] Diff review with user

## Phase 2: Benchmark foundation (k8labs)

- [x] T4: Rust reference service (PostgreSQL + Redis + CPU work, OTel)
- [x] T5: Kubernetes manifests: service, PostgreSQL, Redis, OTel collector
  - Evidence: `.agent/tasks/dcccc2fa-8025-4829-8a58-71d0dbdce2be/`
  - Postgres 17.11, Redis, collector all Ready; 2-container cell running;
    all required signals exported with pod/namespace/deployment attribution.
  - Resolved: no StorageClass (emptyDir for the measurement backend) and
    Docker Hub rate limiting (images served from the host registry).
- [x] T6: Load generator, open-loop and closed-loop
  - Evidence: `.agent/tasks/4c05338b-1f31-4d0a-8640-bbdec0eb0366/`
  - Open loop holds its declared rate (50 -> 49.99 rps); closed loop saturates.
  - Throttling is observable: same load, p99 72ms at 500m vs 161ms at 100m.

## Checkpoint B
- [ ] Both load modes produce valid telemetry
- [ ] Dependency attribution present in traces

## Phase 3: Experiment and model pipeline

- [ ] T7: Bracketed CPU pilot with node-capacity bounds
- [x] T8: Factorial campaign runner with validity gates and budget preview
  - Evidence: `.agent/tasks/a954f79e-d0e6-4c3a-a3e1-f00f2685ff17/`
  - Deterministic full-factorial planner, explicit over-cap confirmation,
    bounded fractional fallback, baseline preservation, and JSON cost preview.
- [x] T9: Fit bounded monotonic model with confidence bounds
  - Evidence: `.agent/tasks/67960fc3-91d2-4377-81d8-f41222261b04/`
  - Measured-range-only interpolation, isotonic fitting, response-table
    validity filtering, repeated-sample gates, and confidence bounds pass 66
    phase8 tests.
- [ ] T10: Emit versioned bundle, fixtures, compatibility tests

## Checkpoint C
- [ ] Clean run reproduces model
- [ ] Artifact validates against fitter fixtures

## Phase 4: Autosizer foundation (fitter)

- [ ] T11: Operator scaffold, CRDs, RBAC
- [ ] T12: ConfigMap model ingestion, identity match, signal collection
- [ ] T13: Joint CPU/HPA evaluator

## Checkpoint D
- [ ] Unit and integration tests pass without mutation

## Phase 5: Guarded control

- [ ] T14: Resize detection, mutation rules, audit, guardrails
- [ ] T15: Apply resize, known-good rollback, fail closed
- [ ] T16: End-to-end tests on pinned baseline

## Checkpoint E
- [ ] No mutation without valid model + fresh measurements

## Phase 6: Documentation

- [ ] T17: Operations docs, failure modes, disablement, recovery
