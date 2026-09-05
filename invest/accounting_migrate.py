"""Approved schema-v19 to v20 accounting migration with verified rollback metadata."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import sys
from contextlib import contextmanager
from datetime import UTC
from datetime import datetime as dt
from pathlib import Path

import duckdb

from invest import accounting, snapshot, tracking_migrate

PRODUCTION_DB = Path("data/invest.duckdb")
BACKUP_DIR = Path("data/backups")


def schema_version(path: Path) -> int:
    return tracking_migrate.schema_version(path)


def migration_units_are_inactive() -> None:
    tracking_migrate.migration_units_are_inactive()


@contextmanager
def maintenance_lock(source: Path):
    path = source.with_name(".invest-maintenance.lock")
    handle = path.open("a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise RuntimeError("invest maintenance lock is held") from exc
    try:
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def create_backup(source: Path, backup_dir: Path = BACKUP_DIR) -> Path:
    with maintenance_lock(source):
        if source.resolve() != PRODUCTION_DB.resolve():
            raise ValueError("backup source must be the fixed production database")
        migration_units_are_inactive()
        found = schema_version(source)
        if found != 19:
            raise RuntimeError(f"v20 migration requires a v19 source, found v{found}")
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = dt.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = backup_dir / f"invest-pre-v20-{stamp}.duckdb"
        snapshot.snapshot_database(source, backup)
        os.chmod(backup, 0o600)
        metadata = backup.with_suffix(".json")
        payload = {
            "source": str(source.resolve()),
            "backup": str(backup.resolve()),
            "sha256": tracking_migrate.sha256_file(backup),
            "schema_version": 19,
            "created_at": dt.now(UTC).isoformat(),
            "rollback_procedure": [
                "systemctl stop invest-mf.timer",
                "systemctl stop invest-mf.service",
                f".venv/bin/python -m invest.accounting_migrate restore {metadata.resolve()}",
                "systemctl start invest-mf.timer",
                "systemctl status --no-pager invest-mf.timer",
            ],
        }
        metadata.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        os.chmod(metadata, 0o600)
        return metadata


def verify_metadata(metadata: Path, source: Path) -> dict:
    if source.resolve() != PRODUCTION_DB.resolve():
        raise ValueError("migration target must be the fixed production database")
    payload = json.loads(metadata.read_text())
    backup = Path(payload["backup"])
    if (
        payload.get("source") != str(source.resolve())
        or payload.get("schema_version") != 19
        or not backup.is_file()
        or (backup.stat().st_mode & 0o777) != 0o600
        or tracking_migrate.sha256_file(backup) != payload.get("sha256")
        or schema_version(backup) != 19
    ):
        raise RuntimeError("backup metadata verification failed")
    return payload


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _atomic_restore(payload: dict, source: Path) -> None:
    staged = source.with_name(f".{source.name}.restore.{os.getpid()}")
    if staged.exists():
        staged.unlink()
    try:
        shutil.copy2(payload["backup"], staged)
        os.chmod(staged, 0o600)
        _fsync_file(staged)
        os.replace(staged, source)
    finally:
        if staged.exists():
            staged.unlink()


def verify_restored_database(metadata: Path, source: Path = PRODUCTION_DB) -> None:
    payload = verify_metadata(metadata, source)
    if (source.stat().st_mode & 0o777) != 0o600:
        raise RuntimeError("restored database must have mode 0600")
    if tracking_migrate.sha256_file(source) != payload["sha256"]:
        raise RuntimeError("restored database does not match the verified backup hash")
    if schema_version(source) != 19:
        raise RuntimeError("restored database is not at schema v19")
    tracking_migrate.verify_integrity(source)


def restore(metadata: Path, source: Path = PRODUCTION_DB) -> None:
    with maintenance_lock(source):
        migration_units_are_inactive()
        payload = verify_metadata(metadata, source)
        _atomic_restore(payload, source)
        verify_restored_database(metadata, source)


def _apply(path: Path) -> None:
    conn = duckdb.connect(str(path))
    try:
        accounting.install_schema(conn)
    finally:
        conn.close()


def migrate(metadata: Path, source: Path = PRODUCTION_DB) -> dict:
    with maintenance_lock(source):
        migration_units_are_inactive()
        payload = verify_metadata(metadata, source)
        if schema_version(source) != 19:
            raise RuntimeError("production database is not at schema v19")
        proof = metadata.with_name(f".{metadata.stem}.proof.duckdb")
        staged = source.with_name(f".{source.name}.v20.{os.getpid()}")
        for path in (proof, staged):
            if path.exists():
                path.unlink()
        try:
            shutil.copy2(payload["backup"], proof)
            _apply(proof)
            if schema_version(proof) != 20:
                raise RuntimeError("disposable v19 to v20 proof failed")
            tracking_migrate.verify_integrity(proof)
            shutil.copy2(payload["backup"], proof)
            os.chmod(proof, 0o600)
            if tracking_migrate.sha256_file(proof) != payload["sha256"]:
                raise RuntimeError("disposable restore bytes do not match backup")
            tracking_migrate.verify_integrity(proof)
            verify_metadata(metadata, source)
            shutil.copy2(source, staged)
            os.chmod(staged, 0o600)
            _apply(staged)
            if schema_version(staged) != 20:
                raise RuntimeError("staged production schema verification failed")
            tracking_migrate.verify_integrity(staged)
            _fsync_file(staged)
            migration_units_are_inactive()
            os.replace(staged, source)
            try:
                if schema_version(source) != 20:
                    raise RuntimeError("production schema verification failed")
                tracking_migrate.verify_integrity(source)
            except Exception:
                _atomic_restore(payload, source)
                verify_restored_database(metadata, source)
                raise
        finally:
            for path in (proof, staged):
                if path.exists():
                    path.unlink()
        return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="invest-accounting-migrate")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("backup")
    for command in ("verify-restore", "restore", "migrate"):
        item = sub.add_parser(command)
        item.add_argument("metadata", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            print(create_backup(PRODUCTION_DB))
        elif args.command == "verify-restore":
            verify_restored_database(args.metadata)
            print("restored v19 database matches verified backup")
        elif args.command == "restore":
            restore(args.metadata)
            print("atomically restored verified v19 database")
        else:
            migrate(args.metadata)
            print("schema v20 applied; rollback steps recorded in backup metadata")
    except (duckdb.Error, OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"accounting migration failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
