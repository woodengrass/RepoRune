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

from rune.core.storage.schema_versions import (
    UnknownSchemaVersionError,
    check_schema_version,
)


class CanonicalReadError(Exception):
    """A canonical file exists but cannot safely be read or validated."""


def atomic_write_text(path: Path, content: str) -> None:
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
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        schema_version = data.get("schema_version", 1)
        check_schema_version(str(path), schema_version)
        return model_cls.model_validate(data)
    except UnknownSchemaVersionError:
        raise
    except Exception as exc:
        raise CanonicalReadError(f"cannot read canonical file {path}: {exc}") from exc


def write_json_model(path: Path, model: BaseModel) -> None:
    content = model.model_dump_json(indent=2) + "\n"
    atomic_write_text(path, content)


def validated_copy[ModelT: BaseModel](model: ModelT, updates: dict) -> ModelT:
    """`model.model_copy(update=updates)` writes `updates` straight into
    `__dict__` and skips every validator -- Pydantic v2 documents this
    explicitly. That's fine for updates a caller can't get wrong (e.g. a
    system-computed `revision`/`status`/timestamp), but every canonical
    "append a new revision" writer in this codebase that lets a *string*
    from a CLI flag (`--expires-at`, `--importance`) flow into `updates`
    was using plain `model_copy` regardless, which let a value that fails
    the field's own validator (an `AfterValidator`, a `Field(ge=..., le=
    ...)`) get written straight into a canonical JSONL line -- confirmed
    by hand: `note update --expires-at` with a naive (non-UTC) timestamp
    wrote the bad line successfully, and every subsequent `note`
    command then crashed reading it back, since `read_jsonl` DOES
    validate. Round-tripping the update through `model_dump()` +
    `model_validate()` re-runs every validator before anything is ever
    handed to `append_jsonl`, so a bad value fails loudly right here
    instead of poisoning the canonical file.
    """
    return type(model).model_validate(model.model_dump() | updates)


def read_jsonl[ModelT: BaseModel](path: Path, model_cls: type[ModelT]) -> list[ModelT]:
    """Reads every line of a canonical JSONL file. Missing file -> []."""
    if not path.exists():
        return []
    records: list[ModelT] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CanonicalReadError(f"cannot read canonical file {path}: {exc}") from exc
    for line_no, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            schema_version = data.get("schema_version", 1)
            check_schema_version(f"{path}:{line_no}", schema_version)
            records.append(model_cls.model_validate(data))
        except UnknownSchemaVersionError:
            raise
        except Exception as exc:
            raise CanonicalReadError(f"cannot read canonical file {path}:{line_no}: {exc}") from exc
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
    atomic_write_text(path, existing + new_line)


def append_jsonl_many(path: Path, models: list[BaseModel]) -> None:
    """Appends multiple records as a single atomic write. Callers that need
    to append N records from one logical operation (e.g. `core.update`
    appending every scope's new `ScopeSummary` revision after one
    `rebuild_cache` transaction) must use this instead of calling
    `append_jsonl` N times — N separate atomic writes are each individually
    crash-safe, but a failure partway through the Nth call would leave
    canonical with only some of the records a single already-committed
    SQLite transaction expects, which is worse than any one write failing
    outright (confirmed by hand: two scopes refreshed in one run, the
    second's canonical append simulated to fail, left `semantic_objects`
    reporting both as current while `semantic.jsonl` only had one).
    """
    if not models:
        return
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    new_lines = "".join(m.model_dump_json() + "\n" for m in models)
    atomic_write_text(path, existing + new_lines)


def rewrite_jsonl(path: Path, models: list[BaseModel]) -> None:
    """Rewrites the whole JSONL file from scratch. Used for --force repair
    paths and tests; not used for normal append flows.
    """
    content = "".join(m.model_dump_json() + "\n" for m in models)
    atomic_write_text(path, content)
