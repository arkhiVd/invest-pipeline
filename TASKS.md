# Release tasks

## Public baseline

- [x] Export source into a separate directory without private Git history
- [x] Exclude private databases, workbooks, exports, logs, and screenshots
- [x] Replace machine-specific paths and personal references
- [x] Add public repository documentation and synthetic examples
- [x] Add broker read opt-in and demo/test network denial
- [x] Run Ruff and pytest against the locked environment
- [x] Run private-marker, Gitleaks, and broker-safety scans
- [x] Add CodeQL, Dependabot, Gitleaks, and built-image container scan workflows
- [x] Complete a fresh independent review
- [x] Record exact local evidence for approval before publication
- [ ] Confirm GitHub-hosted checks after an approved remote and pull request exist

Publication, remote creation, pushing, tagging, deployment, and real broker
access remain blocked pending explicit approval.
