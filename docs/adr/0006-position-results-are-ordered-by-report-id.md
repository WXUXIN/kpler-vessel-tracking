# Position results are ordered by Report ID, not Reported Time

The API orders results by Report ID and paginates by keyset on the same column. It does not
offer ordering by Reported Time.

This looks wrong at a glance — a time-series API that does not sort by time — which is
exactly why it is recorded. Reported Time in the source data takes only 219 distinct values
across 2,696 reports, with 272 reports sharing a single value, so ordering by it is close to
arbitrary. Report ID is monotonic in receipt order and recovers physically coherent voyages
(see ADR-0001). Keyset pagination also requires ordering by the column it seeks on, so the
two decisions are one.

## Consequences

Callers wanting chronological order must sort client-side and accept that Reported Time
cannot fully determine it. Adding a `sort=reported_at` parameter would break keyset
pagination and should not be treated as a small change.
