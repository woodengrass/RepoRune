# RepoRune Agent Instructions

RepoRune (`rune`) is a repository intelligence and persistent project-memory system for coding agents.

These rules apply to all code changes unless a more specific nested `AGENTS.md` overrides them.

Modifying `AGENTS.md` itself requires explicit user approval — it is the governance document, not a working file.

## 1. Instruction priority

Before non-trivial work, read relevant files in this order:

1. `AGENTS.md`
2. `ARCHITECTURE.md`
3. `DATA_MODEL.md`
4. `IMPLEMENTATION_PLAN.md`
5. `HANDOFF.md`
6. `FUTURE_WORK.md`
7. affected code
8. affected tests

`FUTURE_WORK.md` records the approved implementation order and the open problems that still require design — it defines what §3 means by "future milestones".

If implementation and confirmed design disagree, do not silently choose one. Identify the mismatch. If fixing it would change architecture, schema, lifecycle, governance, public compatibility, or agent-injection semantics, stop and ask.

Never change confirmed semantics merely to simplify implementation.

## 2. Engineering priorities

Prefer, in order:

1. correctness
2. preservation of project knowledge
3. explicit behavior
4. architectural integrity
5. safety
6. maintainability
7. observability
8. cross-platform correctness
9. performance
10. brevity

Fix root causes rather than symptoms when reasonably possible.

Prefer deterministic facts from Git, filesystem state, Tree-sitter, hashes, canonical data, or SQLite over model inference.

LLM-generated semantic information is never code truth.

Fail loudly rather than silently guessing when incorrect behavior could affect authoritative state, protocol interpretation, paths, persistence, or agent instructions.

## 3. Change scope

Make the smallest coherent change required.

Do not opportunistically:

- rename unrelated APIs;
- reorganize unrelated code;
- reformat unrelated files;
- replace established libraries;
- change schemas;
- change lifecycle semantics;
- change public CLI contracts;
- implement future milestones;
- add abstractions unrelated to the task.

Do not discard user changes or run destructive Git commands unless explicitly requested.

## 4. Architecture boundaries

Business logic belongs under:

`src/rune/core/`

Core owns:

- canonical storage semantics;
- indexing;
- scopes;
- graph logic;
- semantic validation;
- revisions;
- current/visible selection;
- staleness;
- proposals;
- Decision/Constraint/Note lifecycle;
- retrieval;
- status;
- update orchestration.

`src/rune/cli/` is a thin interface layer. It may parse input, call core, serialize results, render output, and map domain errors to exit codes. Business rules must not be reimplemented there.

`src/rune/mcp/server.py` is likewise a thin wrapper layer. It calls `core` functions in-process (it must not shell out to the CLI) and must not add business logic. Read operations there must not trigger updates or refreshes as a side effect.

`adapters/opencode/` is a thin host adapter. It must not:

- read or write canonical `.rune/*.json` / `.jsonl` directly;
- open `.rune/cache/memory.db`;
- import Python implementation code;
- duplicate current/visible or staleness logic;
- decide Decision/Constraint authority;
- parse source ASTs;
- call semantic providers;
- parse shell commands semantically;
- auto-run `rune init`;
- implicitly trigger `rune update`, `rebuild-cache`, or a semantic refresh (expensive and LLM-backed updates are explicit user actions only).

The adapter/core boundary is:

`rune ... --json`

Python core must remain host-agnostic. OpenCode hooks, prompts, session objects, and host-specific rendering belong in the adapter.

## 5. Python style

Target Python 3.12+.

Prefer modern typing:

```python
list[str]
dict[str, int]
str | None
tuple[str, ...]
```

Prefer PEP 695 generics where clearer.

Use:

* modules/functions/variables: `snake_case`
* classes: `PascalCase`
* exceptions: `PascalCase`, usually ending in `Error`
* constants: `UPPER_SNAKE_CASE`
* private helpers: leading `_`

Names must describe domain meaning. Avoid vague names such as `data`, `thing`, `obj`, `tmp`, `res2`, or `flag` unless context is trivial.

## 6. Python formatting

Ruff is authoritative for lint.

Required check:

```text
ruff check .
```

`I` (import sorting) is enabled: keep import blocks sorted and let `ruff check --fix` do it. `B008` stays ignored (Typer's option idiom) and `UP042` stays ignored (`str, Enum` is load-bearing — `StrEnum` would change canonical/CLI output; see the `pyproject.toml` comment).

`ruff format --check .` is not a gate: it currently fails on most of the existing codebase, and adopting a formatting baseline is a separate change requiring explicit user approval — not something to smuggle into unrelated work. Until then, do not reformat files you did not otherwise need to touch.

For code you do write or touch, follow these rules:

* 4-space indentation;
* no tabs;
* UTF-8;
* one final newline;
* no trailing whitespace;
* standard Ruff import ordering;
* no manual alignment with spaces;
* avoid dense one-line logic;
* avoid unrelated formatting diffs.

Do not disable lint rules broadly. Suppressions must be narrow and justified.

## 7. Type annotations

All public Python functions and methods must be fully typed.

New internal functions should normally also be typed.

Do not use `Any` only to silence typing issues.

Prefer `object` or explicit unions at uncertain external boundaries.

Do not use undocumented sentinel values such as empty strings for normal failure states when `None` or a typed result is clearer.

Avoid casts unless program logic proves them valid.

## 8. Function design

Functions should have one coherent responsibility.

Prefer separating:

1. computation;
2. validation;
3. persistence;
4. presentation.

Avoid functions that parse CLI input, perform domain logic, write canonical state, rebuild SQLite, and print output all at once.

Prefer pure functions where practical.

Use guard clauses and explicit state transitions instead of deeply nested control flow.

Do not create tiny helpers merely to reduce line count.

## 9. Pydantic and structured data

Use Pydantic v2 for important boundaries such as:

* canonical models;
* persisted schema;
* configuration;
* model structured output;
* machine-facing protocol DTOs where appropriate.

Do not rely on accidental coercion for correctness-critical data.

Invalid required/core fields should fail validation.

Treat normalization and validation separately:

* normalization converts explicitly supported forms;
* validation rejects unsupported forms.

Do not silently convert malformed values into convenient types.

## 10. Canonical storage

Canonical files are the source of truth for persistent project knowledge.

SQLite is derived state only.

All canonical knowledge represented in SQLite must be rebuildable from canonical files without LLM calls.

Canonical writes must go through designated storage abstractions.

Do not directly append or rewrite `.rune/*.jsonl` from arbitrary business modules.

Append-only revision history must not be mutated in place during normal lifecycle changes.

Preserve valid canonical state when validation or materialization fails.

Never silently drop conflicting or malformed authoritative records to make an operation succeed.

## 11. Revision semantics

For revisioned records:

```text
current = highest revision number
```

regardless of status.

Visibility is evaluated after selecting current.

Never substitute “latest active revision” for current.

Example:

```text
rev1 = active
rev2 = inactive
```

Current is rev2.

System-generated lifecycle revisions must preserve the previous full snapshot except for explicitly documented changed fields.

Lifecycle transitions that land in a non-terminal status must advance their comparison snapshots (`source_hashes`, `scope_hashes`, or equivalent) to the currently observed values. Otherwise every subsequent `rune update` re-triggers an identical revision and canonical files grow without bound.

Do not invent new revision semantics without updating the authoritative data model and obtaining approval.

## 12. SQLite

Use central connection/materialization helpers.

Required behavior:

* WAL mode;
* configured busy timeout;
* one logical writer;
* consistent reader snapshots;
* materialization inside one transaction;
* no half-materialized visible state.

Do not replace a live SQLite DB file using `os.replace()` as a cross-platform update strategy.

SQLite schema changes must respect `CACHE_SCHEMA_VERSION`.

Use parameterized SQL.

Avoid `SELECT *` in stable interfaces.

Keep transaction ownership explicit.

## 13. Git and subprocess

Use argument arrays with `cwd=`, matching the existing convention (repo-relative invocation, not `git -C`):

```python
subprocess.run(
    ["git", "status", "--porcelain"],
    cwd=repo_path,
    ...
)
```

Avoid shell command string construction.

Avoid `shell=True` unless actual shell semantics are required and input is fully controlled.

Support:

* spaces in paths;
* Unicode;
* Chinese filenames;
* Windows drive letters;
* CRLF;
* subprocess encoding.

Do not assume POSIX-only behavior.

## 14. Path handling

Repo-internal stored paths use documented repo-relative POSIX representation.

Native absolute paths belong at host/filesystem boundaries.

Normalize paths explicitly.

Never blindly do:

```python
str(value)
```

or:

```ts
String(value)
```

on unknown host values and assume the result is a path.

External host input must be runtime validated even when declarations claim a narrower type.

Unknown shapes must fail safely and must never become values such as:

```text
[object Object]
```

passed to Git, Rune CLI, or filesystem APIs.

## 15. Tree-sitter

`ParserAdapter` defines an output contract, not identical algorithms across languages.

Language adapters may use language-specific logic.

For the same logical symbol, `qualified_name(node)` must agree with symbol extraction.

Tree-sitter returning a tree does not imply valid syntax. Check parser error state explicitly.

Best-effort symbols may be retained where allowed, but parse-error state must be accurate.

Do not treat recovered parse output as complete information.

## 16. Import/reference confidence

Resolved imports are high-confidence structural evidence.

Best-effort references are weaker evidence.

Weak reference evidence must not independently create authoritative state unless explicitly permitted.

Unresolvable imports/references should normally degrade gracefully instead of aborting deterministic indexing.

## 17. Semantic worker

Model output is untrusted structured input.

Semantic output must pass:

1. schema validation;
2. file reference validation;
3. symbol reference validation;
4. required redaction;
5. documented strip-vs-reject policy.

Never persist hallucinated file or symbol references as valid evidence.

Provider transport errors and semantic validation failures are different error classes.

Do not feed raw provider errors into repair prompts.

Do not add unbounded retries.

Do not silently replace a previously valid semantic summary with invalid model output.

## 18. Security

Never persist or log:

* API keys;
* access tokens;
* passwords;
* authorization headers;
* credentials;
* full environment dumps.

Secrets belong in environment variables or credential storage.

The repository root may contain a `token.env` file for local shell convenience. `build_provider` reads API keys only from environment variables — never wire project files into credential loading.

Required content must pass redaction before canonical persistence.

When debugging runtime objects, prefer logging type, constructor, keys, sanitized identifiers, and normalized paths rather than entire objects.

## 19. Error handling

Use domain-specific exceptions where failures have domain meaning.

Core raises domain errors.

CLI maps expected domain errors to clean user-facing messages and exit codes.

Expected configuration/user failures should not emit raw tracebacks.

Never write:

```python
try:
    ...
except Exception:
    pass
```

Broad catches are acceptable only at deliberate isolation boundaries where correctness is preserved and failure is properly classified or logged.

Use errors when continuing would be incorrect or unsafe.

Use warnings only when behavior remains correct but degraded or incomplete.

## 20. CLI and JSON output

Human-readable CLI output and machine JSON output are separate contracts.

For `--json` commands:

* stdout must contain valid JSON only;
* diagnostics go to stderr;
* no banners;
* no progress text around JSON;
* no prose after JSON.

Machine consumers must never scrape human output.

Adapter-facing responses must use the documented `protocol_version`.

Unsupported protocol versions must fail loudly.

Do not best-effort parse incompatible protocol shapes.

## 21. TypeScript

Keep TypeScript strict.

Avoid:

* implicit `any`;
* unnecessary `any`;
* `as unknown as X` to silence uncertainty;
* unnecessary non-null assertions;
* unchecked host runtime values.

At external boundaries, runtime validation is required.

Prefer:

```ts
function pluginDirectoryPath(value: unknown): string | null {
  ...
}
```

when declarations are not trustworthy.

Do not weaken compiler settings globally because one external API has a bad type declaration.

## 22. TypeScript formatting and naming

No formatter is currently configured for the adapter; `tsc` (via `npm test`) is the gate. Match surrounding code style and do not reformat unrelated files.

Rules:

* 2-space indentation;
* no tabs;
* UTF-8;
* one final newline;
* no trailing whitespace;
* consistent quote and semicolon style;
* organized imports;
* multiline formatting for complex objects/calls;
* no manual alignment.

Naming:

* variables/functions/properties: `camelCase`
* types/classes/interfaces: `PascalCase`
* constants: `UPPER_SNAKE_CASE`

Boolean names should read clearly:

```ts
softPending
isRuneProject
hasActiveScope
fallbackUsed
```

Avoid vague names such as `flag`, `state2`, `tmp`, or `done`.

## 23. OpenCode adapter rules

Always distinguish:

1. declared host contract;
2. observed runtime contract;
3. RepoRune compatibility workaround.

Host quirks belong in the adapter, never Python core.

Session-local state must be keyed by actual session ID.

No scope or context state may leak across sessions.

Keep separate state for:

* hard bootstrap generation;
* active scopes;
* soft bootstrap;
* pending host events.

Do not collapse them into one generic injected flag.

Hard, soft, and scoped context have distinct lifecycle semantics and must remain distinct.

## 24. Bash handling

Do not parse arbitrary shell command semantics to infer changed files.

After bash execution, inspect actual Git/repository state.

The shell command text is not authoritative evidence of filesystem effects.

## 25. Dependencies

Do not add runtime dependencies casually.

Before adding one, check whether:

* standard library is sufficient;
* an existing dependency is sufficient;
* maintenance/security/platform cost is justified.

New runtime dependencies require a clear reason.

Do not introduce major architecture dependencies such as ORM, web framework, Redis, Postgres, task queues, vector databases, or daemons unless architecture explicitly changes.

## 26. Comments and docstrings

Comments should explain:

* why;
* invariants;
* host quirks;
* non-obvious failure modes;
* security implications;
* intentional limitations.

Do not restate obvious code.

Bad:

```python
# Increment revision
revision += 1
```

Good:

```python
# Current selection depends only on revision order; visibility is evaluated
# separately from status.
```

Public APIs and non-obvious domain operations should have useful docstrings.

Trivial private helpers do not need docstrings.

TODOs must be actionable and scoped.

## 27. Testing

Tests are part of implementation.

Every behavior-changing change must include appropriate tests.

Every confirmed correctness bug should receive a regression test whenever practical.

A regression test should:

1. reproduce the original failure;
2. fail on the old implementation;
3. pass after the fix;
4. test behavior rather than incidental implementation detail.

Verify each regression test fails on the old implementation before trusting it (e.g. `git stash` the fix while keeping the test) — see §36.

Do not weaken or delete a valid existing test merely because new code fails it.

## 28. Test levels

Use unit tests for:

* pure transformations;
* hashing;
* validation;
* revision selection;
* lifecycle logic;
* path normalization;
* parser helpers;
* protocol parsing.

Use integration tests for:

* canonical ↔ SQLite materialization;
* Git subprocess behavior;
* init/update/rebuild workflows;
* parser/index interactions;
* filesystem behavior;
* SQLite transaction behavior.

Use real-host/provider acceptance when correctness depends on external runtimes, including:

* OpenCode hooks;
* plugin loading;
* host tool argument shapes;
* context injection lifecycle;
* provider-specific API behavior;
* Windows file locking.

Mocks alone do not prove external compatibility.

## 29. Test isolation

Tests must not depend on:

* execution order;
* developer home directory;
* global OpenCode config;
* leftover files;
* uncontrolled wall-clock timing;
* network access unless explicitly acceptance/integration.

Use temporary repos and directories.

Do not modify real user `.rune/` state.

Path-sensitive tests should include, when relevant:

* Unicode;
* Chinese filenames;
* spaces;
* CRLF;
* nested paths;
* deleted files;
* renamed files;
* untracked files.

## 30. Windows support

Windows is first-class.

Always consider:

* drive letters;
* path separators;
* Unicode paths;
* Chinese filenames;
* CRLF;
* executable discovery;
* subprocess encoding;
* file locking;
* SQLite readers/writers;
* atomic file behavior.

A default implementation known to fail on Windows is not acceptable.

Text files written by tooling or scripts must be UTF-8 without BOM with LF
line endings, matching the existing repo convention. PowerShell's default
file-writing cmdlets (`Add-Content`, `Set-Content`, `Out-File`) emit CRLF
(and, on Windows PowerShell 5.1, a UTF-8 BOM) — never use them to write or
modify repo files. Prefer the file-editing tools or Python with explicit
`encoding="utf-8"` and `newline="\n"`.

## 31. Assertions and mocking

Use precise assertions.

Prefer:

```python
assert result.status == RecordStatus.active
assert result.record_id == expected_id
```

over:

```python
assert result
```

Mock external boundaries, not the logic under test.

Do not mock OpenCode behavior and then claim host compatibility is validated.

## 32. Performance

Avoid accidental:

* repeated full-repo scans;
* N+1 Git subprocess calls;
* N+1 DB queries;
* reparsing unchanged files;
* repeated model calls;
* unnecessary serialization.

Deliberate exception: unchanged files' references are re-extracted on every update (`core/update.py`), because reference correctness depends on the *targets'* symbol tables, not just the caller's own content. Do not "optimize" this away.

Do not introduce complex caching for hypothetical performance issues.

Correctness comes first.

## 33. Public contracts

Treat these as compatibility-sensitive contracts:

* documented CLI commands;
* machine JSON shape;
* `protocol_version`;
* canonical schema;
* revision semantics;
* lifecycle semantics.

Before changing them:

1. identify consumers;
2. analyze compatibility;
3. update versioning if required;
4. update tests;
5. update documentation.

Do not hide breaking changes inside bug fixes.

## 34. Governance

Scope definitions and membership are persistent project knowledge.

Do not trigger full-repo reclassification as an unrelated side effect.

Protect locked or human-authoritative scope state according to architecture.

Do not implement future membership-provenance schema without explicit approval.

Agent-generated Decision/Constraint content does not become authoritative automatically.

Human approval flow must not be bypassed.

## 35. Logging and generated artifacts

Do not scatter `print()` calls throughout core.

CLI presentation and diagnostic logging are separate responsibilities.

Temporary acceptance instrumentation must not remain enabled in normal operation.

Do not manually edit generated build artifacts when source/build tooling owns them.

When debugging stale plugin builds, distinguish:

* source file;
* build output;
* package cache;
* actual loaded runtime module.

## 36. Bug-fix workflow

For correctness bugs:

1. reproduce;
2. collect evidence;
3. identify root cause;
4. determine whether design or implementation is wrong;
5. add or prepare regression coverage;
6. verify the new test fails on the old implementation (e.g. `git stash` the fix while keeping the test);
7. apply the smallest correct fix;
8. rerun targeted tests;
9. rerun full quality gates;
10. record the bug, fix, and rationale in IMPLEMENTATION_PLAN.md's decision log;
11. update docs when host/design facts changed.

Do not start with speculative refactoring.

## 37. External contract debugging

For OpenCode/provider/runtime mismatches:

1. record runtime version;
2. record SDK/declaration version;
3. inspect actual runtime input/output;
4. distinguish declaration from observed behavior;
5. normalize only at the boundary;
6. add regression tests;
7. repeat real acceptance.

Do not treat static declarations as proof of runtime behavior.

## 38. Quality gates

Before Python work is complete (using the project's virtualenv interpreter):

```text
python -m ruff check .
python -m ty check src tests
python -m pytest
```

(`pytest` runs the full suite, which is also the coverage gate: `core/memory`, `core/storage`, and `core/scopes` must stay at or above 90% statement coverage — see §27. Check with `python -m pytest --cov=src/rune/core/memory --cov=src/rune/core/storage --cov=src/rune/core/scopes --cov-report=term-missing`.)

`ruff format --check .` is not currently a gate (see §6). Do not use it to block work or justify unrelated reformatting.

`python -m` form matters: a bare `pytest`/`ruff` may resolve outside the project's virtualenv. If `ty` reports diagnostics in files you did not touch, fix only yours and report the rest — do not silence the gate.

Before OpenCode adapter work is complete, run the authoritative scripts from `adapters/opencode/package.json`:

```text
npm test
```

(`npm test` already runs `tsc` plus the `node:test` suite; there is no separate `typecheck` script.)

Run the configured formatter/linter if present.

Do not invent parallel commands when package scripts already define the authoritative ones.

## 39. High-risk changes

Changes affecting these areas require extra review and failure-path tests:

* canonical writes;
* revision selection;
* lifecycle transitions;
* staleness;
* approvals;
* scope authority;
* SQLite transactions;
* cache schema;
* path normalization;
* Git invocation;
* adapter/core protocol;
* host context injection;
* redaction/security.

## 40. Final self-review

Before declaring substantial work complete, inspect your own diff and verify:

* only necessary code changed;
* architecture boundaries remain intact;
* no business logic leaked into CLI/adapter;
* no external runtime value was blindly trusted;
* canonical/revision/governance semantics remain correct;
* valid project knowledge cannot be silently lost;
* persistent updates cannot become partial;
* malformed model/host data cannot silently pass;
* session state cannot leak;
* Windows behavior was considered;
* machine JSON output remains clean;
* errors are explicit;
* secrets are protected;
* regression coverage exists;
* lint and tests pass;
* documentation still matches implementation.

## 41. Critical invariants

Never violate these for implementation convenience:

1. Business logic belongs in `src/rune/core`.
2. OpenCode adapter communicates with core only through documented `rune ... --json` interfaces.
3. Canonical files are the persistent source of truth.
4. SQLite is derived and rebuildable.
5. Authoritative revision history is never silently rewritten.
6. Current revision selection is independent of visibility.
7. Model-derived semantics are never deterministic code truth.
8. External host inputs require runtime validation.
9. Incompatible protocol versions fail loudly.
10. Bash effects are discovered from actual repository state, not shell parsing.
11. Windows is a first-class supported platform.
12. Correctness bugs receive regression coverage whenever practical.
13. External host compatibility cannot be established from mocks alone.
14. Secrets never belong in canonical data or unsafe logs.
15. Confirmed architecture/data-model semantics must not be changed merely to simplify implementation.
16. Decision and Constraint authority must not bypass human approval.
17. RepoRune must not destroy or silently replace valid project knowledge during error recovery.

## 42. Code quality

Consistent, high-density code that a stranger can maintain in three months without asking the author. These rules complement §§5–9 (style/typing) and §8 (function design); where they overlap, this section states the *judgment*, the earlier sections state the *mechanics*.

**Abstractions must earn their place.** Every interface, service, factory, manager, or shared helper must map to a real domain concept, a stable repeated pattern, an external boundary, or an important invariant — never added because it "looks professional". Prefer short-term duplication over a wrong abstraction: a premature `ScopeMembership`-style generalization that the schema cannot support is worse than two similar call sites. Classes need state, lifecycle, polymorphism, or an invariant; a bag of functions does not deserve a class. Do not build repository/service/manager/factory layers around complexity that does not exist.

**One function, one coherent concept.** A function owns a complete concept or a single state transition — not an arbitrary line-count slice. Do not split a 60-line state machine into six meaningless helpers, and do not merge CLI parsing, business logic, DB writes, and printing into one body (§8). Prefer separating computation, validation, persistence, and presentation; keep side effects at explicit boundaries, not hidden in mutations, globals, or callback chains.

**Invariants over defensive repetition.** Internal code should build on already-established invariants, not re-check them at every layer. A state that cannot happen must fail loudly, never be patched with a silent default. Relatedly: no implicit fallbacks in correctness-sensitive code — no `x or []`, no default enum, no silent coercion to make a type error go away. Missing data is handled explicitly; a fallback is only acceptable when it is part of the design, documented as such.

**Data structures carry meaning.** Durable domain data uses named models, not nested dicts passed through three layers. But do not wrap every small tuple in a dataclass either — use judgment: if a shape crosses a module boundary or persists, name it; if it lives inside one function, a tuple is fine.

**Naming is precise, comments are scarce and valuable.** Names express domain meaning so a reader rarely needs to jump to the definition (§5). Comments explain why, invariants, tradeoffs, host quirks, dangerous alternatives — never translate code into English. A few high-value comments beat many low-value ones; a comment that can be deleted without losing correctness or clarity should be deleted.

**Complexity has a ceiling.** Deeply nested control flow, five-wrapper call chains, and clever one-liners are rejected in review. Code should read as direct but not crude: guard clauses over nesting, explicit state transitions over flags. `ty` (§38) and the enabled Ruff rules (§6) enforce the mechanical floor; reviewer judgment enforces the rest.

**Review-friendly diffs.** Good code lets a reviewer quickly answer: what changed, why, what the invariant is, what the failure modes are, and whether there are side effects. Keep one commit focused on one thing; do not rename, reformat, or refactor neighboring code along the way (§3).
