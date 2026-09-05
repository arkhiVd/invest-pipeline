# Roadmap

## 1. Public baseline

Publish the deterministic engines, synthetic demo, privacy scans, broker safety
gates, pinned CI, and release documentation with fresh Git history.

Exit checks:

- local tests and static checks pass
- secret and private-data scans return zero findings
- broker-write scan returns zero findings
- an independent review finds no unresolved release blocker

## 2. Reproducible demo

Add a one-command synthetic pipeline run that builds a disposable database and
renders every dashboard page without network access.

## 3. Methodology fixtures

Expand hand-worked fixtures for mutual-fund metrics, VBRS, ranking, portfolio
accounting, and point-in-time signal research.

## 4. Public feedback

Handle reported calculation or security defects. Real brokerage automation and
trading remain out of scope.
