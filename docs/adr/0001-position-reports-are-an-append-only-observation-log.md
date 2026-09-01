# Position Reports are an append-only observation log keyed by Report ID

A Position Report records what arrived from AIS, not where a vessel truly was, so reports
are stored append-only and never updated or reconciled. Identity is the Report ID the feed
supplies (its `stationId` field), which is unique across the dataset and ascending in
receipt order; that gives the consumer an idempotent write, making at-least-once delivery
and full replay safe without deduplication logic.

## Considered Options

Keying on `(mmsi, reported_time)` was the obvious alternative and is wrong here: 142 such
pairs in the source data carry *conflicting* positions — the same vessel, the same second,
up to 515 km apart, with zero exact duplicates among them. Last-write-wins would have
silently discarded 5% of the data while the ingest counter reported every record written.

## Consequences

Conflicting Reports are a normal, expected state of the store, and any consumer reading a
vessel's track must be prepared for two contradictory reports at one Reported Time. The
system offers no "current position of vessel X" answer without a caller-chosen rule for
picking between them.

Ordering by Report ID recovers a physically coherent voyage for two of the three vessels in
the source data (median hop 0.5 km, versus 7-79 km when ordered by Reported Time). Report ID
is therefore the trustworthy sequence; Reported Time is not.
