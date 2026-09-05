# Public release audit

## Scope

The audit covered the full private working tree, ignored runtime material,
tracked files, and all 123 private commits. The export did not copy `.git`, so
none of that history can reach the public candidate.

## Classification

| Class | Material | Decision |
|---|---|---|
| Safe after review | Calculation engines, parsers, schemas, fixed thresholds, read-only adapters | Copied, then scanned |
| Sanitized | Machine-specific paths, operator references, protected defaults, documentation claims | Rewritten in the export |
| Excluded | Private Git history, databases, backups, logs, caches, environments, tokens, protected config, workbooks, exports, production screenshots and evidence | Not copied |
| Replaced | Portfolio fixtures, example holdings and transactions, dashboard images | Synthetic-only public fixtures |

The original contracts and evidence mixed reusable design with dates, host
observations, portfolio totals, account reconciliation, local paths, and private
operational details. New public documents describe the implementation without
those observations.

## Release gates

Publication stays blocked until the candidate has zero findings from the custom
private-marker scan and Gitleaks, passes the broker-write gate, passes tests and
Ruff from the lockfile, and receives an independent review.

A zero-result scan does not prove that private data is absent. The separate
history, denylist file policy, synthetic fixtures, manual diff review, and
independent review are all required.
