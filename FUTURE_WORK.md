# Future Work

This document records approved design direction for findings discovered after
Milestone 7 host acceptance. It is a planning record, not a claim that the
work below has been implemented. The required implementation order is:
`3 -> 4 -> 1 -> 8 -> 9 -> 5 -> 2 -> 7 -> 6`.

## 1. Symbol Identity

Duplicate declarations in one file must be independently indexable. Symbol
identity is:

```text
(path, qualified_name, kind, occurrence)
symbol_id = sha1(f"{path}:{qualified_name}:{kind}:{occurrence}")[:16]
```

`occurrence` is the zero-based source-order index among declarations sharing
the same `(path, qualified_name, kind)` tuple. For example, three module-level
`FLAG` declarations have occurrences `0`, `1`, and `2`. Ordinary declarations
continue to have `occurrence = 0`, preserving their identity semantics.

Implementation must assign occurrences deterministically in every supported
parser, migrate materialization safely, and add Python and JavaScript duplicate
declaration regressions through `rune update`. Store start line/byte as debug
metadata but not as identity. Only an identity-algorithm migration followed by
re-indexing is normal symbol-ID churn; `rebuild-cache` alone does not redefine
symbol identity.

## 2. Remove Git Blob Hashing

Remove the unused `git_blob_hash` field and its per-file `git hash-object`
subprocess. This eliminates an approximately one-process-per-file cost from
scan-related commands. If Git clean-filter-aware blob identity becomes a real
feature requirement, redesign it from that requirement rather than retaining
write-only data.

## 3. Canonical Schema Is Fail-Closed

Canonical Pydantic models must use `ConfigDict(extra="forbid", ...)`. Unknown
canonical fields are never silently ignored or dropped during rewrite.

Schema version is a mutation safety boundary:

```text
record.schema_version > supported schema version
-> raw inspection may be allowed
-> mutation and rewrite are forbidden
-> Rune reports that an upgrade is required
```

Example error:

```text
Canonical schema version 4 is newer than this Rune version supports (3).
Refusing to modify this repository to prevent data loss.
```

Every canonical field addition must bump `schema_version`. Schema changes use
explicit migrations from old schema to new schema; optional fields must not be
used to bypass versioning. Corrupt JSON, invalid canonical records, and
unsupported versions must produce domain errors rather than raw parser or
Pydantic tracebacks.

## 4. Project-Level Canonical Write Lock

Multi-agent operation requires one exclusive project-level canonical write
lock at:

```text
.rune/locks/canonical.lock
```

Use a small portable locking dependency such as `portalocker`, not handwritten
`O_CREAT | O_EXCL` stale-PID logic. Every logical canonical mutation acquires
the same exclusive lock:

```text
acquire lock
re-read canonical state
validate preconditions
write all canonical files in the logical mutation
update or invalidate the SQLite projection
release lock
```

Examples include note add/update, decision/constraint proposal, approval,
rejection, semantic publish, and scope mutation. An approval that changes both
proposals and decisions/constraints holds the same lock across the whole
operation; locks are not per JSONL file.

The lock is complemented by optimistic record checks. A mutation records an
expected revision/source hash, rechecks current canonical state after acquiring
the lock, and returns an explicit conflict if another writer changed the record.
The lock prevents lost updates; optimistic checks prevent semantic overwrite of
the same record.

Expensive prepare work stays outside the writer lock. For example, update scan,
parse, semantic generation, and network calls prepare a candidate mutation
outside the lock. The commit phase then acquires the lock, re-reads schema and
preconditions, verifies the candidate remains valid, commits canonical state,
updates or dirties the projection, and releases the lock. V1 uses a 30-second
default timeout with this error:

```text
Could not acquire Rune canonical writer lock within 30s.
Another Rune writer may be modifying this repository.
Lock: <path>
```

Required tests: competing subprocess writes preserve both independent records;
competing edits of one record produce a conflict; timeout is actionable; crash
does not permanently block later writers; canonical/cache state remains safe.

## 5. Windows Atomic Replace Retry

Keep temp-file plus `os.replace`; never fall back to truncate-and-rewrite.
On Windows sharing violation or access-denied `PermissionError`, retry bounded
backoff of roughly 50ms, 100ms, 200ms, 400ms, and so on up to about 2-3 seconds.
On final failure, retain the original canonical file, clean the temp file, and
raise an actionable domain error. Add an integration test where replace fails
once or twice before succeeding.

## 6. Reference Observation and Resolution

Long-term index design separates extraction from resolution:

```text
parse caller -> store ReferenceObservation
symbol table changes -> resolver re-resolves observations
```

Each observation stores raw name, source symbol, context/kind, and source
location. SQLite gains `reference_observations`. Changed symbol names can
re-resolve only observations for those names, replacing all-source reparsing
with work close to changed symbols plus affected observations. This is a larger
schema/index design and comes after correctness and cache safety work.

## 7. Incremental Cache Projection

Canonical JSONL remains authoritative. Successful mutations update only their
own SQLite projection:

```text
note add -> note revisions, current notes, FTS notes
decision propose -> proposals
approve -> proposal current, decision/constraint revision, current pointer, FTS
reject -> rejected proposal revision and proposal projection
```

They do not re-materialize unrelated files, symbols, edges, scopes, or semantic
data. SQLite `schema_meta` stores a `canonical_manifest_hash`. The authoritative
input list is centralized in `CANONICAL_PROJECTION_INPUTS`, rather than copied
among call sites. For every listed canonical file, compute SHA-256 over its raw
bytes and combine sorted `(path, file_hash)` pairs into one SHA-256 manifest.
Thus equal manifest hashes prove identical canonical input bytes. `mtime_ns` and
size may later be used only as a fast-path optimization before content hashing,
never as correctness proof. Every cache read compares the stored manifest with
current canonical state. A mismatch makes the cache unusable and requires
rebuild rather than serving silently stale data.

Invariant:

```text
Every successful canonical mutation with a SQLite projection either updates that
projection before returning or marks the cache unusable/dirty.
```

Incremental projection is an optimization, never a correctness dependency:
the cache remains disposable.

## 8. Authoritative Visibility Policy

Create `rune.core.memory.policy` as the only visibility-policy authority. It
provides current revision lookup, visibility classification, visibility
warnings, and SQL-ready status sets.

Use an enum rather than a bool:

```text
NORMAL
WARNING
HIDDEN
```

For example, active is NORMAL; review-required and stale are WARNING; inactive
and orphaned are HIDDEN. Notes may use a separate policy table. Search, check,
context, doctor, CLI, MCP, and adapter-facing core paths call this module.
SQL filters import the same exported status sets rather than duplicating string
literals. Add table-driven tests covering every status for each record type.

## 9. Proposal Rejection and Projection Invariant

When incremental projection from section 7 exists, rejection appends its
rejected revision and incrementally materializes proposal state before return.
The section 7 invariant applies to rejection and every future canonical write,
preventing a write path from silently forgetting to update or invalidate the
cache.

## Approved CLI/MCP Parity

Add thin CLI wrappers for existing core/MCP retrieval operations:

```text
rune scope-read
rune decision get
rune constraint get
rune note get
```

They must remain thin `--json` CLI adapters over existing core retrieval
functions. The previous deferment of `scope-read` is superseded by this
decision.

## Remaining Findings Outside This Sequence

The following remain separately tracked: ConfigError command handling, Windows
Unicode output, read-only SQLite creation, provider cost coercion, negative
limits, check query batching, HTTP client closure, marker escaping, session
bootstrap race, bash activation cap, endpoint JSON validation, NTFS case
handling, src-layout import resolution, and parser traversal consistency.
