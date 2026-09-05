# Normalise AIS units and types at ingest

The raw feed encodes Speed as an integer ten times the value in knots, Reported Time as Unix
epoch seconds, and Rate of Turn as a string that is empty in every supplied record. The
consumer converts all three to their natural types on write, so the datastore and the API
speak knots, real timestamps, and nullable numbers, and no downstream reader has to know the
wire encoding.

## Consequences

The wire format is lost: a stored report cannot be replayed byte-identically to what the
producer published. That is acceptable because the feed file is retained as the source of
truth, but it means round-tripping through the datastore is not lossless.

Rate of Turn's column type comes from the AIS specification (signed, -128..127) rather than
from the supplied data, which never populates it. A non-nullable column would reject the
entire dataset.
