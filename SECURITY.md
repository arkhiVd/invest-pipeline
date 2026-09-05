# Security policy

## Report a vulnerability

Open a private GitHub security advisory after publication. Do not include live
credentials, account identifiers, or financial records in an issue.

## Supported version

Security fixes target the current default branch.

## Data handling

The repository accepts synthetic fixtures only. Keep real broker exports,
databases, statements, screenshots, logs, and generated reports outside the
repository. `.env.example` contains blank placeholders. Real secrets belong in
environment variables or ignored files with restrictive permissions.

## Broker boundary

Broker integration is read-only. Demo and test modes reject real Kite network
access. Live reads require a separate explicit opt-in. Order placement,
cancellation, GTT changes, fund transfers, and portfolio mutation are not
supported and will not be accepted.
