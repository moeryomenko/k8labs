# CPU-limits freeze record

## Status

**Blocked.** The pilot has three complete 36-cell blocks. The policy calibration
cannot freeze a valid low limit for every workload. The confirmatory campaign
must not start.

This record uses only `build/research/cpu-limits/pilot/`. It does not use
confirmatory data or `SKIPPED.md`.

## Pilot input

- Protocol: `cpu-limits-v1`
- Run: `cpu-limits-20261024T123954Z`
- Worker: `k8labs-md-custom-4vcpu-qq498-zlz6p-6n655`
- Blocks: 1, 2, and 3
- Cell records: 108 complete records, 36 per block
- Calibration target: 10% to 40% throttled periods
- Policy record: `build/research/cpu-limits/policies.json`, status `FAIL`
- Sample-size record: `build/research/cpu-limits/sample-size.json`, status `FAIL`

## Policy calibration

`fit` and `low` below are values recorded by the policy tool. A `FAIL` row is
not a frozen policy. It retains the pre-pilot protocol value only to identify
the failed calibration. It must not start a confirmatory run.

| workload | fit (m) | low (m) | policy status | pilot evidence | result |
|---|---:|---:|---|---|---|
| cpu-bound | 2000 | 500 | FAIL | fit: 6 observations at 0%; burstable-low: 6 observations at 98.51% to 100%; guaranteed-low: 3 observations at 100% | No finite low quota was in the target band. |
| io-bound | 1000 | 250 | FAIL | fit: 6 observations at 0%; burstable-low: 6 observations at 0%; guaranteed-low: 3 observations at 0% | No finite low quota was in the target band. |
| memory-bound | 1000 | 500 | PASS | `contention-memory-bound-burstable-low`, block 1, `container_leaf`: 500m and 23.27% throttled periods | This low value is inside the target band. |
| hybrid | 1000 | 500 | FAIL | fit: 6 observations at 0% to 100%; burstable-low: 6 observations at 0% to 100%; guaranteed-low: 3 observations at 0% to 100% | No finite low quota was in the target band. |

The only valid selected low value is 500m for `memory-bound`. The other three
workloads have no selected low value. The required resource relation remains
unchanged: `burstable-low` uses a 100m request, and `guaranteed-low` uses a
request equal to its limit.

## Sample size

The calculation uses 90% power, a one-sided 95% upper bound for pilot standard
deviation, and a cap of six confirmatory blocks. `N` is the capped count. A row
with `underpowered` set to `yes` requires more than six blocks. The throttle-gap
rows have no valid N because the pilot contrast is incomplete.

| family | workload | upper SD | required blocks | N | underpowered |
|---|---|---:|---:|---:|---|
| limit_effect | cpu-bound | 0.092459 | 45 | 6 | yes |
| limit_effect | hybrid | 0.025476 | 4 | 4 | no |
| limit_effect | io-bound | 0.487430 | 1239 | 6 | yes |
| limit_effect | memory-bound | 0.016093 | 3 | 3 | no |
| request_weight | cpu-bound | 0.141986 | 106 | 6 | yes |
| request_weight | hybrid | 0.971964 | 4926 | 6 | yes |
| request_weight | io-bound | 0.785708 | 3219 | 6 | yes |
| request_weight | memory-bound | 0.088528 | 41 | 6 | yes |
| guaranteed_vs_burstable | cpu-bound | 0.104692 | 58 | 6 | yes |
| guaranteed_vs_burstable | hybrid | 0.937148 | 4579 | 6 | yes |
| guaranteed_vs_burstable | io-bound | 0.751357 | 2944 | 6 | yes |
| guaranteed_vs_burstable | memory-bound | 0.082070 | 36 | 6 | yes |
| throttle_gap | cpu-bound | — | — | — | no; incomplete |
| throttle_gap | hybrid | — | — | — | no; incomplete |
| throttle_gap | io-bound | — | — | — | no; incomplete |
| throttle_gap | memory-bound | — | — | — | no; incomplete |

## Confirmatory gate

The runner refuses a confirmatory request when either `policies.json` or
`sample-size.json` is missing. This check was verified without running a live
confirmatory workload.

The records exist, but their status is `FAIL`. The policy calibration failure
and missing throttle-gap N prevent this task from claiming that a confirmatory
campaign is ready. A new pilot calibration is required before a new freeze.
