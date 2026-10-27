# Evidence: production incident simulation (7.7)

The files the documentation cites:

| File | What it holds |
|------|---------------|
| `incidents-20261027T000538Z.json` | Traffic spike: 800 -> 3000 -> 800 req/s against an unlimited pod and a 120m pod. |
| `incidents-run2-20261027T002520Z.json` | Gradual ramp (7 steps, both arms), the HPA race, pod eviction, and the node drain without a PodDisruptionBudget. |
| `drain-pdb-20261026T235150Z.json` | The same node drain with `minAvailable: 2`. |

The 7.7 run is split across two files because the cluster's API server dropped a
connection once, which invalidated the ramp, eviction and drain in the run that
contains the spike. The numbers reproduced in both runs.

## Files that are not results

- `incidents-20261026T215254Z.json`, `incidents-20261026T220541Z.json`,
  `incidents-20261026T220815Z.json`, `incidents-20261026T221300Z.json`,
  `incidents-20261026T221346Z.json`, `incidents-20261026T221846Z.json`,
  `incidents-20261026T222423Z.json`: harness development, before the target
  server had a CPU cost that a 120m quota could bind.
- `incidents-20261026T224357Z.json`, `incidents-20261026T230711Z.json`,
  `incidents-20261026T233648Z.json`, `drain-nopdb-20261026T234710Z.json`: the
  drain scenario still pinned replicas to the drained node with a required
  `nodeSelector`, so their replacements could never be scheduled and the drain
  recorded an artificial outage. Fixed; the replacement used here attracts
  replicas to the node without pinning them.

They are kept rather than deleted so the history is inspectable, but nothing
cites them.

## Reproducing

```bash
cd research/cpu-limits/phase7/7.7-incidents
python3 test-incidents.py                                   # all four
python3 test-incidents.py --scenarios drain --drain-pdb \
        --label drain-pdb                                   # the PDB arm
```

The target is `phase7/lib/httpd.c`, built on the workstation and published as
two ConfigMaps that an init container concatenates. Nothing is written to the
node's filesystem, and no image is pulled: the nginx image used by 7.1-7.3 was
garbage-collected from the node and Docker Hub rate-limits anonymous pulls.
