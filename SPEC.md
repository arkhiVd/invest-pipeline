# Specification

## Purpose

`invest-pipeline` is educational software for deterministic investment
research. It separates data ingestion, calculations, storage, and optional
narrative generation. Models do not calculate metrics or alter research state.

This software is not financial advice. Signals, scores, rankings, and backtests
are research outputs, not recommendations.

## Required behavior

- Store source time, calculation time, methodology version, lookback, frequency,
  and benchmark with calculated metrics where applicable.
- Make ingestion and event processing replay-safe.
- Distinguish generated signals, research positions, broker holdings, screen
  membership, and executed transactions.
- Rank candidates with visible inputs, transformations, missing-data status,
  weights, and contributions.
- Enforce point-in-time availability and explicit execution timing in signal
  research.
- Fail closed on stale, missing, malformed, duplicate-conflicting, or
  unresolved corporate-action data.
- Keep model output outside deterministic screening and accounting.

## Strategy contracts

### VBRS

The tactical cash fraction is:

```text
cash = base_cash + ((current_pe / median_pe) - 1) * sensitivity
```

Configuration defines the median, sensitivity, clamp, and informational bands.
Boundary tests pin equality and crossover behavior.

### EMA crossover

The swing signal uses separately SMA-seeded EMA10 and EMA21 series. An entry
requires EMA10 to move strictly from at-or-below EMA21 to above it on a completed
daily close. An exit is the inverse. Equality is not a crossover. Execution is
modeled at the next eligible session open. Same-close results, where produced,
are labeled upper-bound diagnostics.

### Mutual-fund metrics

The engine computes annualized returns, sample volatility, beta, Sharpe ratio,
and upside/downside capture from aligned observations. Insufficient history or
benchmark coverage produces an explicit missing result rather than imputation.

### Portfolio accounting

XIRR uses dated external cash flows and a terminal valuation. TWR requires
complete flow-boundary valuations. Exact, estimated, and unavailable results
are distinct. Estimated output records assumptions, exclusions, coverage, and
residuals.

## Security and privacy

- Commit synthetic data only.
- Keep secrets in environment variables or ignored protected files.
- Demo and test modes reject real broker connections.
- Live broker reads require explicit operator opt-in and use a fixed GET
  allowlist.
- The dashboard reads a separate, bounded projection rather than the writer's
  database.
- CI scans secrets, private markers, dependencies, Python code, and containers.

## Non-goals

- Order placement, cancellation, GTT mutation, fund transfer, or automated
  trading
- Personalized advice or recommendations
- Inferring a trade or research position from a holding
- Claiming long-horizon evidence from a short diagnostic sample
- Uploading or publishing real financial records

## Acceptance checks

- Ruff and pytest pass from the locked dependency set.
- Boundary, stale/missing data, timezone, duplicate, and failure-isolation tests
  pass.
- Private-marker and Gitleaks scans report zero findings.
- The broker safety scan reports zero write routes or methods.
- GitHub Actions use immutable commit SHAs.
- A reviewer who did not prepare the export checks privacy, broker safety,
  calculations, documentation claims, and CI.
