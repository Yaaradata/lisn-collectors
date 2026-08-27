# Prior local findings (reference only)

Copied from `LiSN_Collector_Acceptance_Report_v1.md` before its deletion.
These are inputs for deployed-GCP validation, not deployed results.

## Defect list by priority (local run)

### Blocking

1. Garbage truncated JSON accepted without loud terminal surfacing (`E-6`): `truncated_json` page-0 final status is `pending` (not `dead`), loudness assertion fails, while sibling page completed.
2. Dead-letter surface exposes credential-bearing DSN text (`G-5`): forced DSN appears in `/v1/dead-letter` while endpoint is unauthenticated.
3. Null-thread identity loss in enrichment output (`B-1`): truth null identities `150`, observed `0`.
4. Silent discovery gap not surfaced (`H-3`): missing identities `60` while health/reconcile/dead-letter remain zero.
5. Unsupported discovery key silently accepted (`H-14`): status `200`, no caller-visible dropped-key signal.
6. Forced destructive reset leaves non-zero GCS raw objects (`G-7`): `gcs_objects_raw=1521`.
7. Planner type-confusion in `incident_ids` handling (`C-4`):
   - case 4 accepted string scalar and iterated it as six IDs (`keys=6`);
   - case 5 accepted `None`, `1`, and `{}` as planned IDs.

### P1

1. 30-minute capacity below DS-2 population (`F-3`): margin `-281286`.
2. See deployment findings finding 2 (deployed workers do not survive database unavailability).

### P2

1. Forced reset returns warnings with partial GCS delete behavior while SQL/BQ clear (`G-7` warning payload).

### P3

1. No additional P3 defect recorded from executed evidence set.

## Deployment findings (observed from production logs; not test-produced locally)

1. **Workers terminate at the 24-hour Cloud Run task ceiling with no restart. (P1)**
   - Executions `col-sentinel-j4wkc` and `col-sentinel-discovery-z82fd` logged max-timeout termination and worker stop.
   - No automatic restart logic in worker deploy script.
2. **Deployed workers do not survive database unavailability. (P1)**
   - `col-maintenance-qmd84` failed after Cloud SQL `invalidState` / pool init timeout / `exit(1)`.
   - Cloud SQL start via activation policy patch occurred after failure; failed task did not restart.
3. **Periodic sweep is deferred by every worker, not only maintenance worker. (P2)**
   - Sweep defer logs observed in both `col-sentinel` and `col-sentinel-discovery` with interleaved sequence IDs.
