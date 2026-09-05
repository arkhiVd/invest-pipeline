import json
import stat

import duckdb
import pytest

from invest import db, tracking_migrate


def test_verified_backup_disposable_proof_and_migration(tmp_path, monkeypatch):
    production = tmp_path / "invest.duckdb"
    conn = db.connect(str(production))
    db.init_schema(conn)
    conn.close()
    monkeypatch.setattr(tracking_migrate, "PRODUCTION_DB", production)
    monkeypatch.setattr(tracking_migrate.tracking, "PRODUCTION_DB", production)
    monkeypatch.setattr(tracking_migrate, "migration_units_are_inactive", lambda: None)
    metadata = tracking_migrate.create_backup(production, tmp_path / "backups")
    original_verify_integrity = tracking_migrate.verify_integrity
    integrity_reads = []

    def record_integrity_read(path):
        integrity_reads.append((path, tracking_migrate.schema_version(path)))
        original_verify_integrity(path)

    monkeypatch.setattr(tracking_migrate, "verify_integrity", record_integrity_read)
    payload = json.loads(metadata.read_text())
    backup = __import__("pathlib").Path(payload["backup"])
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert tracking_migrate.schema_version(backup) == 17
    tracking_migrate.migrate(metadata, production)
    assert tracking_migrate.schema_version(production) == 18
    assert tracking_migrate.schema_version(backup) == 17
    proof_reads = [(path, version) for path, version in integrity_reads if path != production]
    assert len({path for path, _version in proof_reads}) == 1
    assert [version for _path, version in proof_reads] == [18, 17]
    assert integrity_reads[-1] == (production, 18)
    assert not list((tmp_path / "backups").glob(".*.proof.duckdb"))
    assert not list((tmp_path / "backups").glob(".*.restore.duckdb"))
    assert payload["rollback_procedure"] == [
        "systemctl stop invest-mf.timer",
        "systemctl stop invest-mf.service",
        f"cp --preserve=mode,timestamps {backup} {production}",
        f"chmod 600 {production}",
        f".venv/bin/python -m invest.tracking_migrate verify-restore {metadata}",
        "systemctl start invest-mf.service",
        "systemctl start invest-mf.timer",
        "systemctl status --no-pager invest-mf.service",
    ]


def test_active_timer_or_service_refuses_before_production_schema_change(tmp_path, monkeypatch):
    production = tmp_path / "invest.duckdb"
    conn = db.connect(str(production))
    db.init_schema(conn)
    conn.close()
    monkeypatch.setattr(tracking_migrate, "PRODUCTION_DB", production)
    monkeypatch.setattr(tracking_migrate.tracking, "PRODUCTION_DB", production)
    monkeypatch.setattr(
        tracking_migrate,
        "migration_units_are_inactive",
        lambda: (_ for _ in ()).throw(RuntimeError("invest-mf.timer is active")),
    )
    with pytest.raises(RuntimeError, match="invest-mf.timer is active"):
        tracking_migrate.create_backup(production, tmp_path / "backups")
    assert not list((tmp_path / "backups").glob("*.duckdb"))
    monkeypatch.setattr(tracking_migrate, "migration_units_are_inactive", lambda: None)
    metadata = tracking_migrate.create_backup(production, tmp_path / "backups")
    monkeypatch.setattr(
        tracking_migrate,
        "migration_units_are_inactive",
        lambda: (_ for _ in ()).throw(RuntimeError("invest-mf.timer is active")),
    )
    with pytest.raises(RuntimeError, match="invest-mf.timer is active"):
        tracking_migrate.migrate(metadata, production)
    assert tracking_migrate.schema_version(production) == 17


def test_migration_unit_guard_mocks_timer_and_service_state(monkeypatch):
    checked = []
    monkeypatch.setattr(
        tracking_migrate,
        "unit_is_active",
        lambda unit: checked.append(unit) or False,
    )

    tracking_migrate.migration_units_are_inactive()

    assert checked == ["invest-mf.timer", "invest-mf.service"]
    monkeypatch.setattr(tracking_migrate, "unit_is_active", lambda unit: unit == "invest-mf.timer")
    with pytest.raises(RuntimeError, match="invest-mf.timer is active"):
        tracking_migrate.migration_units_are_inactive()


def test_tampered_backup_blocks_production_migration(tmp_path, monkeypatch):
    production = tmp_path / "invest.duckdb"
    conn = db.connect(str(production))
    db.init_schema(conn)
    conn.close()
    monkeypatch.setattr(tracking_migrate, "PRODUCTION_DB", production)
    monkeypatch.setattr(tracking_migrate.tracking, "PRODUCTION_DB", production)
    monkeypatch.setattr(tracking_migrate, "migration_units_are_inactive", lambda: None)
    metadata = tracking_migrate.create_backup(production, tmp_path / "backups")
    payload = json.loads(metadata.read_text())
    with open(payload["backup"], "ab") as handle:
        handle.write(b"tamper")
    with pytest.raises(RuntimeError, match="metadata verification"):
        tracking_migrate.migrate(metadata, production)
    conn = duckdb.connect(str(production), read_only=True)
    assert conn.execute("SELECT max(version) FROM schema_migrations").fetchone() == (17,)
    conn.close()


def test_migration_rechecks_units_immediately_before_production_ddl(tmp_path, monkeypatch):
    production = tmp_path / "invest.duckdb"
    conn = db.connect(str(production))
    db.init_schema(conn)
    conn.close()
    monkeypatch.setattr(tracking_migrate, "PRODUCTION_DB", production)
    monkeypatch.setattr(tracking_migrate.tracking, "PRODUCTION_DB", production)
    checks = iter((None, None, RuntimeError("invest-mf.service is active")))

    def inactive():
        outcome = next(checks)
        if outcome:
            raise outcome

    monkeypatch.setattr(tracking_migrate, "migration_units_are_inactive", inactive)
    metadata = tracking_migrate.create_backup(production, tmp_path / "backups")
    with pytest.raises(RuntimeError, match="invest-mf.service is active"):
        tracking_migrate.migrate(metadata, production)
    assert tracking_migrate.schema_version(production) == 17
