"""Canonical schema version constants and a minimal compatibility check.

V1 does not implement migrations (per spec §64, "V1 不需要複雜 migration，但不要假設 schema 永遠不變").
This module only centralizes the current version number and a check that
`rune doctor` / materialize can use to refuse unknown-future versions instead
of silently misreading them.
"""

from __future__ import annotations

CURRENT_SCHEMA_VERSION = 1


class UnknownSchemaVersionError(Exception):
    def __init__(self, file_label: str, found: int):
        super().__init__(
            f"{file_label}: schema_version={found} is newer than this rune build "
            f"supports ({CURRENT_SCHEMA_VERSION}). Refusing to guess how to read it."
        )
        self.file_label = file_label
        self.found = found


def check_schema_version(file_label: str, schema_version: int) -> None:
    if schema_version > CURRENT_SCHEMA_VERSION:
        raise UnknownSchemaVersionError(file_label, schema_version)
