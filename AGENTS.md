# Contributor agent rules

## Scope

This repository contains educational, read-only investment research software.
It computes metrics and research signals. It must never place, modify, or cancel
orders or transfer funds.

## Privacy

Use synthetic data in tests, examples, screenshots, and bug reports. Never add
credentials, account identifiers, broker exports, holdings, positions, orders,
trades, statements, databases, logs, or reports made from private data.

## Safety

- Keep `INVEST_MODE=demo` for development and tests.
- Mock broker and external-data clients in tests.
- Do not weaken the Kite read allowlist or broker-write scan.
- Store secrets only in environment variables or ignored protected files.
- Do not add outbound network calls to unit tests.

## Local gate

```bash
uv sync --all-groups --locked
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
python scripts/check_private_markers.py .
python scripts/check_broker_safety.py .
```

Use a branch for changes. Keep commits small and use conventional commit
messages. Do not merge until CI and independent review pass.
