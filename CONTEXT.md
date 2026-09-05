# Vessel Tracking

Ingests AIS vessel position broadcasts through a streaming pipeline and serves them
over a filtered HTTP API.

This glossary holds the terms this domain gives a particular meaning to. General
engineering vocabulary - cursors, batches, poison messages, bounding boxes - stays out
however often the code says it; where such a term carries a decision, it is defined in
the ADR that made it.

## Language

**AIS**:
The Automatic Identification System, a maritime VHF broadcast standard by which vessels
transmit their identity and movement. It is a lossy, unauthenticated medium: reports are
missed, duplicated, and occasionally wrong.

**Feed**:
The sequence of raw AIS messages this system ingests, supplied as a single file and
replayed one message at a time rather than loaded in bulk. Its order is the order the
messages were received, and the Report ID is what carries that order into the system.
_Avoid_: dataset, source data, input file

**Vessel**:
A ship, identified across the system by its MMSI. The system holds no vessel attributes
beyond that identifier — no name, type, or dimensions.

**MMSI**:
The nine-digit Maritime Mobile Service Identity a vessel broadcasts to identify itself.
Not a guaranteed-unique key: transponders can be misconfigured, so two vessels may share one.
_Avoid_: vessel id, ship id, IMO (an IMO number is a different, hull-lifetime identifier)

**Position Report**:
A single observation of a vessel received from AIS: where a vessel *said* it was, at a
moment it *said* it was there. It is a record of what arrived, never a claim about where
the vessel truly was. Immutable once received.
_Avoid_: position, ping, fix, sighting, event

**Report ID**:
The identifier a Position Report carries from the source feed, unique across all reports
and ascending in the order reports were received. Sourced from the raw feed's `stationId`
field, which despite its name does not identify a receiving station.
_Avoid_: station id, sequence number

**Reported Time**:
The timestamp a Position Report claims for itself. Untrusted: in the source data it is
coarse and frequently repeated, so many reports covering a wide area can share one value.
_Avoid_: timestamp, position time, observed at

**Conflicting Reports**:
Two or more Position Reports sharing an MMSI and a Reported Time but disagreeing about
position. Kept, not resolved — the system records observations rather than adjudicating them.

**Navigational Status**:
What a Vessel reports itself to be doing - under way, at anchor, moored, aground. Self-declared
and therefore only as reliable as the crew setting it.
_Avoid_: status, state, nav status

**Course**:
The direction a vessel is actually travelling over the ground. Distinct from Heading.
_Avoid_: bearing, direction, COG

**Heading**:
The direction a vessel's bow is pointing, which differs from its Course whenever wind or
current pushes it sideways.
_Avoid_: HDG

**Speed**:
A vessel's speed over the ground, in knots. The source feed transmits it as an integer ten
times larger; that encoding is a wire detail and never appears in this system's language.
_Avoid_: SOG, velocity, decikonts

**Rate of Turn**:
How fast a vessel is turning, in degrees per minute. Frequently absent from a report.
_Avoid_: ROT, turn rate

**Rejected Report**:
An inbound message that failed validation and was never written. Counted and set aside
rather than silently dropped.
_Avoid_: skipped record, bad message, error
