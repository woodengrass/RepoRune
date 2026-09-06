"""Atomic read/write for canonical JSON and JSONL files.

Every write goes through a temp-file-then-rename so a crash mid-write never
leaves a corrupted canonical file (spec §63). V1 assumes a single writer
(DATA_MODEL.md §1/§8): JSONL "append" is implemented as read-all + rewrite
atomically, which is simplest and still crash-safe for our write volume.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from pydantic import BaseModel

from rune.core.storage.schema_versions import check_schema_version


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def read_json_model[ModelT: BaseModel](path: Path, model_cls: type[ModelT]) -> ModelT | None:
    """Returns None if the file doesn't exist (caller decides the default)."""
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    schema_version = data.get("schema_version", 1)
    check_schema_version(str(path), schema_version)
    return model_cls.model_validate(data)


def write_json_model(path: Path, model: BaseModel) -> None:
    content = model.model_dump_json(indent=2) + "\n"
    _atomic_write_text(path, content)


def read_jsonl[ModelT: BaseModel](path: Path, model_cls: type[ModelT]) -> list[ModelT]:
    """Reads every line of a canonical JSONL file. Missing file -> []."""
    if not path.exists():
        return []
    records: list[ModelT] = []
    text = path.read_text(encoding="utf-8")
    for line_no, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        data = json.loads(line)
        schema_version = data.get("schema_version", 1)
        check_schema_version(f"{path}:{line_no}", schema_version)
        records.append(model_cls.model_validate(data))
    return records


def append_jsonl(path: Path, model: BaseModel) -> None:
    """Appends one record. V1 single-writer assumption: implemented as
    read full file + rewrite atomically, not a raw O_APPEND write, so a
    crash mid-write can never leave a half-written line.
    """
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    new_line = model.model_dump_json() + "\n"
    _atomic_write_text(path, existing + new_line)


def rewrite_jsonl(path: Path, models: list[BaseModel]) -> None:
    """Rewrites the whole JSONL file from scratch. Used for --force repair
    paths and tests; not used for normal append flows.
    """
    content = "".join(m.model_dump_json() + "\n" for m in models)
    _atomic_write_text(path, content)
