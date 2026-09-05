# Architecture

## Data path

```mermaid
sequenceDiagram
    participant S as Source adapter
    participant V as Validator
    participant E as Deterministic engine
    participant D as DuckDB writer
    participant P as Projection publisher
    participant U as Dashboard
    S->>V: bounded source payload
    V->>E: normalized records
    E->>D: versioned metrics and events
    D->>P: read transaction
    P->>P: allowlist, verify, atomic replace
    U->>P: bounded read-only queries
```

Adapters validate response size, schema, dates, and numeric values before data
reaches an engine. Engines have no broker-write capability. Immutable natural
keys and content fingerprints make replays deterministic and expose conflicting
duplicates.

## Trust boundaries

```mermaid
flowchart TB
    X[Untrusted external text] --> N[Parser and limits]
    N --> Q[Deterministic prefilter]
    Q --> L[Optional model classification]
    L --> R[Untrusted narrative field]
    D[(Private runtime database)] --> P[Allowlisted projection publisher]
    P --> U[Read-only local dashboard]
```

The UI escapes narrative and uses fixed SQL. It never accepts raw SQL. The
projection omits credentials and direct account identifiers.

## Failure isolation

Each adapter returns validated records or an explicit failure. A failed source
does not turn missing data into zero, does not advance a successful watermark,
and does not permit later narrative or alert stages to invent a result.

## Broker access

Kite support is read-only. Tests pass injected openers. A real opener is denied
in demo and test modes and requires a separate live-read acknowledgement.
