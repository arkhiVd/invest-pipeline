import json
import stat
from pathlib import Path

import duckdb
import pytest

from invest import accounting_migrate, db, ranking, tracking


def v19(path):
    conn = db.connect(str(path))
    db.init_schema(conn)
    tracking.install_schema(conn)
    ranking.install_schema(conn)
    conn.close()


def setup(tmp_path, monkeypatch):
    production = tmp_path / "invest.duckdb"
    v19(production)
    monkeypatch.setattr(accounting_migrate, "PRODUCTION_DB", production)
    monkeypatch.setattr(accounting_migrate, "migration_units_are_inactive", lambda: None)
    return production


def test_verified_backup_disposable_restore_and_v20_migration(tmp_path, monkeypatch):
    production = setup(tmp_path, monkeypatch)
    metadata = accounting_migrate.create_backup(production, tmp_path / "backups")
    payload = json.loads(metadata.read_text())
    backup = Path(payload["backup"])
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert accounting_migrate.schema_version(backup) == 19

    accounting_migrate.migrate(metadata, production)

    assert accounting_migrate.schema_version(production) == 20
    assert accounting_migrate.schema_version(backup) == 19
    conn = duckdb.connect(str(production), read_only=True)
    assert {row[0] for row in conn.execute("SHOW TABLES").fetchall()} >= {
        "portfolio_account",
        "portfolio_performance_result",
        "managed_product_membership",
    }
    conn.close()


def test_active_unit_and_tampered_backup_block_migration(tmp_path, monkeypatch):
    production = tmp_path / "invest.duckdb"
    v19(production)
    monkeypatch.setattr(accounting_migrate, "PRODUCTION_DB", production)
    monkeypatch.setattr(
        accounting_migrate,
        "migration_units_are_inactive",
        lambda: (_ for _ in ()).throw(RuntimeError("invest-mf.timer is active")),
    )
    with pytest.raises(RuntimeError, match="timer is active"):
        accounting_migrate.create_backup(production, tmp_path / "backups")
    monkeypatch.setattr(accounting_migrate, "migration_units_are_inactive", lambda: None)
    metadata = accounting_migrate.create_backup(production, tmp_path / "backups")
    with Path(json.loads(metadata.read_text())["backup"]).open("ab") as handle:
        handle.write(b"tamper")
    with pytest.raises(RuntimeError, match="metadata verification"):
        accounting_migrate.migrate(metadata, production)
    assert accounting_migrate.schema_version(production) == 19


def test_post_replace_failure_restores_v19(tmp_path, monkeypatch):
    production = setup(tmp_path, monkeypatch)
    metadata = accounting_migrate.create_backup(production, tmp_path / "backups")
    original = accounting_migrate.tracking_migrate.verify_integrity

    def fail_v20_production(path):
        if path == production and accounting_migrate.schema_version(path) == 20:
            raise RuntimeError("injected post-replace failure")
        original(path)

    monkeypatch.setattr(
        accounting_migrate.tracking_migrate, "verify_integrity", fail_v20_production
    )
    with pytest.raises(RuntimeError, match="injected"):
        accounting_migrate.migrate(metadata, production)
    assert accounting_migrate.schema_version(production) == 19
    accounting_migrate.verify_restored_database(metadata, production)


def test_maintenance_lock_blocks_concurrent_backup(tmp_path, monkeypatch):
    production = setup(tmp_path, monkeypatch)
    with accounting_migrate.maintenance_lock(production):
        with pytest.raises(RuntimeError, match="maintenance lock"):
            accounting_migrate.create_backup(production, tmp_path / "backups")
