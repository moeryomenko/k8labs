# CPU-limits confirmatory campaign

## Status

**Skipped.** The freeze gate blocked this campaign. No confirmatory workload
ran and this file makes no measured claim.

## Freeze records

- `build/research/cpu-limits/policies.json`: status `FAIL`.
- `build/research/cpu-limits/sample-size.json`: status `FAIL`.
- `research/cpu-limits/FREEZE.md`: blocked because valid low limits were not
  frozen for `cpu-bound`, `io-bound`, and `hybrid`.

## Counts

No frozen confirmatory N is authorized. Block count, complete-cell count, and
missing-cell count are not applicable because the campaign did not start.

## Data paths

- Pilot input: `build/research/cpu-limits/pilot/`.
- Confirmatory status record:
  `build/research/cpu-limits/confirmatory/SKIPPED.md`.
- No confirmatory raw measurement data exists.

## Next step

Run a new pilot calibration and create successful `policies.json` and
`sample-size.json` freeze records before starting this campaign.
