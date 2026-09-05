# Release candidate evidence

Prepared from a separate export. The private repository and its services were
not changed.

## Private-source audit

- Private Git history: 123 commits inspected; Gitleaks reported 0 findings.
- Complete private working tree: Gitleaks reported 5 redacted findings across 3
  local-only files. The affected secret and environment material was excluded.
- Databases, backups, logs, caches, protected configuration, workbooks, exports,
  generated reports, and original screenshots were excluded as whole classes.
- The export contains none of the private Git objects.

## Candidate checks

- Ruff lint: pass
- Ruff format check: pass
- Pytest: 428 passed, 1 multiprocessing deprecation warning
- Custom private-marker scan: pass, 0 findings
- Broker safety scan: pass, 0 findings
- Gitleaks directory scan: pass, 0 findings across about 1.58 MB
- GitHub Actions immutable-SHA scan: pass, 0 findings
- Hashed dependency locks: `uv.lock` has 1,108 SHA-256 entries;
  `requirements.lock` has 1,080 SHA-256 entries
- Independent Herdr Pi review with Terra at low reasoning: pass after all
  findings were fixed

## Checks deferred until a remote exists

CodeQL, Dependabot, Gitleaks Actions, and the built-image Trivy scan are
configured but cannot produce GitHub evidence before a repository and pull
request exist. Creating the remote and pushing remain blocked pending approval.
