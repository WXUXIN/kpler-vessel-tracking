# Effectively-once ingest via at-least-once delivery and idempotent writes

The consumer disables auto-commit and commits Kafka offsets only after the database
transaction commits. Combined with the Report ID primary key from ADR-0001, this yields
at-least-once delivery with idempotent writes, which is effectively-once ingest without
requiring distributed transactions between Kafka and PostgreSQL. A crash between the
database commit and the offset commit replays messages that are then discarded by
`ON CONFLICT DO NOTHING`, so replay and restart are always safe.

## Consequences

Two failure classes are handled deliberately differently, and conflating them is the failure
mode this decision exists to prevent:

- A **poison message** — one that fails validation and can never succeed — is published to a
  dead-letter topic with a reason, counted as a Rejected Report, and its offset committed so
  the pipeline continues.
- A **transient failure** — the database being unreachable, a reset connection — is retried
  with backoff and never dead-lettered. The consumer stalls rather than discarding data it
  could have written.

Dead-lettering on a transient outage would silently drop valid reports; retrying a malformed
record forever would wedge ingest. The taxonomy is the point.

Messages are keyed by MMSI so that one vessel's reports stay ordered within a partition. With
three vessels in the source data this produces severe partition skew — three hot partitions
regardless of the topic's partition count — which is a property of the sample, not of the
design.
