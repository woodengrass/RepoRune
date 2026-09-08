from __future__ import annotations

import pytest
from pydantic import ValidationError

from rune.core.storage.models import (
    MemoryRevision,
    RecordStatus,
    RecordType,
    RevisionAuthor,
    Scope,
    ScopeSource,
)


@pytest.mark.parametrize("scope_id", ["Auth", "auth_api", "-auth", "auth-", "auth--api", ""])
def test_scope_id_must_be_lower_kebab_case(scope_id: str) -> None:
    with pytest.raises(ValidationError):
        Scope(id=scope_id, name="Auth", source=ScopeSource.human)


def test_record_id_uses_a_stable_lower_kebab_case_format() -> None:
    def build(record_id: str) -> MemoryRevision:
        return MemoryRevision(
            record_id=record_id,
            revision=1,
            type=RecordType.decision,
            status=RecordStatus.active,
            content="Use PostgreSQL.",
            created_by=RevisionAuthor.human,
            created_at="2026-01-01T00:00:00Z",
        )

    valid = build("use-postgres-for-primary-store")
    assert valid.record_id == "use-postgres-for-primary-store"

    for record_id in ("Bad_ID", "-bad", "bad-", "bad--id", ""):
        with pytest.raises(ValidationError):
            build(record_id)
