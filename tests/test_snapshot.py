from pathlib import Path

import duckdb
import pytest

from invest.snapshot import snapshot_database


def test_snapshot_is_readable_and_refuses_overwrite(tmp_path: Path):
    source = tmp_path / "source.duckdb"
    destination = tmp_path / "copy.duckdb"
    conn = duckdb.connect(str(source))
    conn.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TIMESTAMP)"
    )
    conn.execute("INSERT INTO schema_migrations VALUES (1, current_timestamp)")
    conn.close()

    assert snapshot_database(source, destination) == destination
    copy = duckdb.connect(str(destination), read_only=True)
    assert copy.execute("SELECT version FROM schema_migrations").fetchone() == (1,)
    copy.close()
    with pytest.raises(FileExistsError):
        snapshot_database(source, destination)
