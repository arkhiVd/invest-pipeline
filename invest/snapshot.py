"""Consistent DuckDB file snapshot while holding the database writer lock."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import duckdb


def snapshot_database(source: str | Path, destination: str | Path) -> Path:
    """Checkpoint, copy under an exclusive connection, then verify readability."""
    source = Path(source)
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(source))
    try:
        conn.execute("CHECKPOINT")
        shutil.copy2(source, destination)
    finally:
        conn.close()
    verify = duckdb.connect(str(destination), read_only=True)
    try:
        verify.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()
    finally:
        verify.close()
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="invest-snapshot")
    parser.add_argument("source")
    parser.add_argument("destination")
    args = parser.parse_args(argv)
    try:
        path = snapshot_database(args.source, args.destination)
    except (duckdb.Error, OSError) as exc:
        print(f"snapshot failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"snapshot verified: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
