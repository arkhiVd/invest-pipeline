"""Approved v17 to v18 tracking migration with verified rollback metadata."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC
from datetime import datetime as dt
from pathlib import Path

import duckdb

from invest import snapshot, tracking

PRODUCTION_DB = tracking.PRODUCTION_DB
BACKUP_DIR = Path("data/backups")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def schema_version(path: Path) -> int:
    conn = duckdb.connect(str(path), read_only=True)
    try:
        return int(conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0])
    finally:
        conn.close()


def verify_integrity(path: Path) -> None:
    """Open a disposable database and force DuckDB to read its catalog and tables."""
    conn = duckdb.connect(str(path), read_only=True)
    try:
        conn.execute("PRAGMA database_size").fetchone()
        conn.execute("SELECT count(*) FROM schema_migrations").fetchone()
        for (table,) in conn.execute("SHOW TABLES").fetchall():
            conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()
    finally:
        conn.close()


def unit_is_active(unit: str) -> bool:
    result = subprocess.run(
        ["systemctl", "is-active", "--quiet", unit],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def migration_units_are_inactive() -> None:
    active = [unit for unit in ("invest-mf.timer", "invest-mf.service") if unit_is_active(unit)]
    if active:
        raise RuntimeError(f"refusing v18 migration while {', '.join(active)} is active")


def create_backup(source: Path, backup_dir: Path = BACKUP_DIR) -> Path:
    if source.resolve() != PRODUCTION_DB.resolve():
        raise ValueError("backup source must be the fixed production database")
    migration_units_are_inactive()
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = backup_dir / f"invest-pre-v18-{stamp}.duckdb"
    snapshot.snapshot_database(source, backup)
    os.chmod(backup, 0o600)
    version = schema_version(backup)
    if version != 17:
        raise RuntimeError(f"v18 migration requires a v17 source, found v{version}")
    metadata = backup.with_suffix(".json")
    payload = {
        "source": str(source.resolve()),
        "backup": str(backup.resolve()),
        "sha256": sha256_file(backup),
        "schema_version": version,
        "created_at": dt.now(UTC).isoformat(),
        "rollback_procedure": [
            "systemctl stop invest-mf.timer",
            "systemctl stop invest-mf.service",
            f"cp --preserve=mode,timestamps {backup.resolve()} {source.resolve()}",
            f"chmod 600 {source.resolve()}",
            f".venv/bin/python -m invest.tracking_migrate verify-restore {metadata.resolve()}",
            "systemctl start invest-mf.service",
            "systemctl start invest-mf.timer",
            "systemctl status --no-pager invest-mf.service",
        ],
    }
    metadata.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(metadata, 0o600)
    return metadata


def verify_metadata(metadata: Path, source: Path) -> dict:
    if source.resolve() != PRODUCTION_DB.resolve():
        raise ValueError("migration target must be the fixed production database")
    payload = json.loads(metadata.read_text(encoding="utf-8"))
    backup = Path(payload["backup"])
    if (
        payload.get("source") != str(source.resolve())
        or payload.get("schema_version") != 17
        or not backup.is_file()
        or (backup.stat().st_mode & 0o777) != 0o600
        or sha256_file(backup) != payload.get("sha256")
        or schema_version(backup) != 17
    ):
        raise RuntimeError("backup metadata verification failed")
    return payload


def verify_restored_database(metadata: Path, source: Path = PRODUCTION_DB) -> None:
    payload = verify_metadata(metadata, source)
    if (source.stat().st_mode & 0o777) != 0o600:
        raise RuntimeError("restored database must have mode 0600")
    if sha256_file(source) != payload["sha256"]:
        raise RuntimeError("restored database does not match the verified backup hash")
    if schema_version(source) != 17:
        raise RuntimeError("restored database is not at schema v17")
    verify_integrity(source)


def _apply(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("BEGIN TRANSACTION")
    try:
        for statement in tracking._DDL:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_migrations VALUES (?,?) ON CONFLICT DO NOTHING",
            [tracking.SCHEMA_VERSION, dt.now(UTC)],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def migrate(metadata: Path, source: Path = PRODUCTION_DB) -> dict:
    migration_units_are_inactive()
    payload = verify_metadata(metadata, source)
    if schema_version(source) != 17:
        raise RuntimeError("production database is not at schema v17")
    proof = metadata.with_name(f".{metadata.stem}.proof.duckdb")
    for path in (proof,):
        if path.exists():
            path.unlink()
    shutil.copy2(payload["backup"], proof)
    try:
        proof_conn = duckdb.connect(str(proof))
        try:
            _apply(proof_conn)
        finally:
            proof_conn.close()
        if schema_version(proof) != 18:
            raise RuntimeError("disposable v17 to v18 proof failed")
        verify_integrity(proof)
        shutil.copy2(payload["backup"], proof)
        os.chmod(proof, 0o600)
        if sha256_file(proof) != payload["sha256"]:
            raise RuntimeError("disposable restore bytes do not match backup")
        if schema_version(proof) != 17:
            raise RuntimeError("disposable restore schema verification failed")
        verify_integrity(proof)
    finally:
        for path in (proof,):
            if path.exists():
                path.unlink()
    verify_metadata(metadata, source)
    conn = duckdb.connect(str(source))
    try:
        migration_units_are_inactive()
        _apply(conn)
    finally:
        conn.close()
    if schema_version(source) != 18:
        raise RuntimeError("production schema verification failed")
    verify_integrity(source)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="invest-tracking-migrate")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("backup")
    restore_parser = sub.add_parser("verify-restore")
    restore_parser.add_argument("metadata", type=Path)
    migrate_parser = sub.add_parser("migrate")
    migrate_parser.add_argument("metadata", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            print(create_backup(PRODUCTION_DB))
        elif args.command == "verify-restore":
            verify_restored_database(args.metadata)
            print("restored v17 database matches verified backup")
        else:
            migrate(args.metadata)
            print("schema v18 applied; rollback steps recorded in backup metadata")
    except (duckdb.Error, OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"tracking migration failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
