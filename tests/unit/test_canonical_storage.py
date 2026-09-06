from __future__ import annotations

from pathlib import Path

import pytest

from rune.core.storage.canonical import (
    append_jsonl,
    read_json_model,
    read_jsonl,
    write_json_model,
)
from rune.core.storage.models import ProjectFile
from rune.core.storage.schema_versions import UnknownSchemaVersionError


def test_write_then_read_json_model_roundtrips(tmp_path: Path) -> None:
    path = tmp_path / "project.json"
    original = ProjectFile(project_id="p1", name="demo", created_at="2026-01-01T00:00:00Z")
    write_json_model(path, original)
    loaded = read_json_model(path, ProjectFile)
    assert loaded == original


def test_read_json_model_missing_file_returns_none(tmp_path: Path) -> None:
    assert read_json_model(tmp_path / "nope.json", ProjectFile) is None


def test_read_json_model_refuses_unknown_future_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "project.json"
    path.write_text(
        '{"schema_version": 999, "project_id": "p", "name": "n", "created_at": "x"}',
        encoding="utf-8",
    )
    with pytest.raises(UnknownSchemaVersionError):
        read_json_model(path, ProjectFile)


def test_atomic_write_leaves_no_tmp_file_behind(tmp_path: Path) -> None:
    path = tmp_path / "project.json"
    write_json_model(path, ProjectFile(project_id="p", name="n", created_at="x"))
    tmp_files = list(tmp_path.glob(".*.tmp"))
    assert tmp_files == []


def test_append_jsonl_accumulates_lines(tmp_path: Path) -> None:
    path = tmp_path / "things.jsonl"
    append_jsonl(path, ProjectFile(project_id="1", name="a", created_at="t1"))
    append_jsonl(path, ProjectFile(project_id="2", name="b", created_at="t2"))
    records = read_jsonl(path, ProjectFile)
    assert [r.project_id for r in records] == ["1", "2"]


def test_read_jsonl_missing_file_returns_empty_list(tmp_path: Path) -> None:
    assert read_jsonl(tmp_path / "nope.jsonl", ProjectFile) == []


def test_read_jsonl_skips_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "things.jsonl"
    path.write_text(
        '{"schema_version":1,"project_id":"1","name":"a","created_at":"t1"}\n'
        "\n"
        '{"schema_version":1,"project_id":"2","name":"b","created_at":"t2"}\n',
        encoding="utf-8",
    )
    records = read_jsonl(path, ProjectFile)
    assert len(records) == 2
