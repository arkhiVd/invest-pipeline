import json
import stat
from pathlib import Path

import duckdb
import pytest

from invest import db, ranking_migrate, tracking


def v18(path):
    conn = db.connect(str(path))
    db.init_schema(conn)
    tracking.install_schema(conn)
    conn.close()


def test_verified_v18_backup_disposable_restore_and_v19_migration(tmp_path, monkeypatch):
    production = tmp_path / "invest.duckdb"
    v18(production)
    monkeypatch.setattr(ranking_migrate, "PRODUCTION_DB", production)
    monkeypatch.setattr(ranking_migrate, "migration_units_are_inactive", lambda: None)
    metadata = ranking_migrate.create_backup(production, tmp_path / "backups")
    payload = json.loads(metadata.read_text())
    backup = Path(payload["backup"])
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert ranking_migrate.schema_version(backup) == 18

    ranking_migrate.migrate(metadata, production)

    assert ranking_migrate.schema_version(production) == 19
    assert ranking_migrate.schema_version(backup) == 18
    assert not list((tmp_path / "backups").glob(".*.proof.duckdb"))
    conn = duckdb.connect(str(production), read_only=True)
    assert {row[0] for row in conn.execute("SHOW TABLES").fetchall()} >= {
        "ranking_run",
        "ranking_symbol",
        "ranking_component",
        "ranking_input",
    }
    conn.close()


def test_active_unit_and_tampered_backup_block_migration(tmp_path, monkeypatch):
    production = tmp_path / "invest.duckdb"
    v18(production)
    monkeypatch.setattr(ranking_migrate, "PRODUCTION_DB", production)
    monkeypatch.setattr(
        ranking_migrate,
        "migration_units_are_inactive",
        lambda: (_ for _ in ()).throw(RuntimeError("invest-mf.timer is active")),
    )
    with pytest.raises(RuntimeError, match="timer is active"):
        ranking_migrate.create_backup(production, tmp_path / "backups")
    monkeypatch.setattr(ranking_migrate, "migration_units_are_inactive", lambda: None)
    metadata = ranking_migrate.create_backup(production, tmp_path / "backups")
    payload = json.loads(metadata.read_text())
    with Path(payload["backup"]).open("ab") as handle:
        handle.write(b"tamper")
    with pytest.raises(RuntimeError, match="metadata verification"):
        ranking_migrate.migrate(metadata, production)
    assert ranking_migrate.schema_version(production) == 18


def test_post_replace_verification_failure_restores_v18(tmp_path, monkeypatch):
    production = tmp_path / "invest.duckdb"
    v18(production)
    monkeypatch.setattr(ranking_migrate, "PRODUCTION_DB", production)
    monkeypatch.setattr(ranking_migrate, "migration_units_are_inactive", lambda: None)
    metadata = ranking_migrate.create_backup(production, tmp_path / "backups")
    original = ranking_migrate.tracking_migrate.verify_integrity

    def fail_v19_production(path):
        if path == production and ranking_migrate.schema_version(path) == 19:
            raise RuntimeError("injected post-replace failure")
        original(path)

    monkeypatch.setattr(ranking_migrate.tracking_migrate, "verify_integrity", fail_v19_production)
    with pytest.raises(RuntimeError, match="injected"):
        ranking_migrate.migrate(metadata, production)
    assert ranking_migrate.schema_version(production) == 18
    ranking_migrate.verify_restored_database(metadata, production)


def test_maintenance_lock_blocks_concurrent_backup(tmp_path, monkeypatch):
    production = tmp_path / "invest.duckdb"
    v18(production)
    monkeypatch.setattr(ranking_migrate, "PRODUCTION_DB", production)
    monkeypatch.setattr(ranking_migrate, "migration_units_are_inactive", lambda: None)
    with ranking_migrate.maintenance_lock(production):
        with pytest.raises(RuntimeError, match="maintenance lock"):
            ranking_migrate.create_backup(production, tmp_path / "backups")
