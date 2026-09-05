# The rate limiter fails open

When Redis cannot be reached, the API serves the request rather than refusing it. The
failure is logged at error level, and the request is recorded in the request log like any
other, so the period during which no limit was applied is visible afterwards.

## Considered Options

**Failing closed** — refusing every request while the limiter is unavailable — protects
capacity absolutely, and is the safer choice for a limiter whose purpose is to stop
abuse. It was rejected because the limiter here exists to stop one caller exhausting
capacity for everyone, and refusing everyone achieves that by making the outage total. It
turns a fault in the thing that protects the service into a fault in the service, which
is a worse answer for every caller in order to give a better one to none.

**Falling back to an in-process counter** keeps a limit during an outage, but a per-process
limit is the thing this design deliberately avoids: two API instances each allowing ten
requests a minute allow twenty, which is not the limit anyone configured. A fallback that
silently changes what the limit means is harder to reason about than not having one.

## Consequences

For as long as Redis is unreachable, there is no rate limit. Anyone able to make Redis
unreachable can remove the limit, and this is a real cost of the choice rather than an
oversight: the limiter is not a security control here, because the system has no
authentication and client IP is the only identity it has (see the Out of Scope notes in
the README).

The error log is the only signal that the limit has lapsed. An operator who wants to know
should alert on `rate_limiter_unavailable`, because nothing else about the system's
behaviour changes — requests are served exactly as they would be under the limit.

`/healthz` is exempt from the limit for a separate reason: the container health probe
polls every five seconds, twelve times a minute against an allowance of ten, so counting
it would have the API refuse its own liveness check and be marked unhealthy for enforcing
its own limit. It is still recorded in the request log, because the log is a record of
traffic and that is traffic.
