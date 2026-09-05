# Position Reports are an append-only observation log keyed by Report ID

A Position Report records what arrived from AIS, not where a vessel truly was, so reports
are stored append-only and never updated or reconciled. Identity is the Report ID the feed
supplies (its `stationId` field), which is unique across the dataset and ascending in
receipt order; that gives the consumer an idempotent write, making at-least-once delivery
and full replay safe without deduplication logic.

## Considered Options

Keying on `(mmsi, reported_time)` was the obvious alternative and is wrong here. The source
data holds only 345 distinct such pairs across 2,696 records; 142 of those keys carry more
than one report, none of them an exact duplicate, and the busiest single key carries 119.
Where they disagree they disagree wildly — the same vessel, the same second, up to 515 km
apart. Last-write-wins would have silently discarded 2,351 records, 87% of the feed, while
the ingest counter reported every record written.

The figures above are reproduced by `analysis/explore_feed.py`; see
`docs/dataset-observations.md` for the text version, or `analysis/explore_feed.ipynb`
for the same analysis with the track plots that make the ordering finding visible.

## Consequences

Conflicting Reports are a normal, expected state of the store, and any consumer reading a
vessel's track must be prepared for two contradictory reports at one Reported Time. The
system offers no "current position of vessel X" answer without a caller-chosen rule for
picking between them.

Ordering by Report ID recovers a physically coherent voyage for two of the three vessels in
the source data (median hop 0.5 km, versus 7-79 km when ordered by Reported Time). Report ID
is therefore the trustworthy sequence; Reported Time is not.
