from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from rune.core.project import RuneLayout
from rune.core.storage.sqlite.materialize import CACHE_SCHEMA_VERSION, CacheUnusableError, connect_for_read


def _write_schema_meta(layout: RuneLayout, value: str) -> None:
    layout.memory_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(layout.memory_db))
    try:
        conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?)", (value,))
        conn.commit()
    finally:
        conn.close()


def test_connect_for_read_closes_connection_when_schema_metadata_is_invalid(tmp_path: Path) -> None:
    layout = RuneLayout(tmp_path / "repo")
    layout.repo_root.mkdir()
    _write_schema_meta(layout, "garbage")

    with pytest.raises(CacheUnusableError, match="usable cache"):
        connect_for_read(layout)

    # On Windows this also verifies the failed read did not leave the DB open.
    layout.memory_db.unlink()


def test_connect_for_read_escapes_uri_fragment_characters(tmp_path: Path) -> None:
    layout = RuneLayout(tmp_path / "repo#fragment")
    layout.repo_root.mkdir()
    _write_schema_meta(layout, str(CACHE_SCHEMA_VERSION))

    conn = connect_for_read(layout)
    try:
        assert conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()[0] == str(
            CACHE_SCHEMA_VERSION
        )
    finally:
        conn.close()
