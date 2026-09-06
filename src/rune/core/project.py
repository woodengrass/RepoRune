"""Project discovery and `.rune/` initialization.

Implements the refuse/--force semantics confirmed in ARCHITECTURE.md §10:
`--force` never clears or rebuilds any canonical memory file that already
exists (project.json, scopes.json, semantic.jsonl, decisions.jsonl,
constraints.jsonl, notes.jsonl, proposals.jsonl) — it only creates missing
canonical files, repairs the config skeleton, and rebuilds the SQLite cache
and deterministic index.
"""

from __future__ import annotations

import subprocess
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from rune.core.config import write_default_config
from rune.core.storage.canonical import atomic_write_text, write_json_model
from rune.core.storage.models import ProjectFile, ScopesFile

RUNE_DIR_NAME = ".rune"


class NotAGitRepoError(Exception):
    pass


class AlreadyInitializedError(Exception):
    pass


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def find_repo_root(start: Path) -> Path:
    """Asks git itself whether `start` is inside a working tree, and if so
    where its root is. Deliberately does not just look for a `.git`
    directory/file — that only checks for a filesystem marker, which a
    stray or fake `.git` entry would satisfy without there being a real,
    usable git repo underneath (spec §48: "確認為 git repo").
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=start,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    except OSError as exc:
        raise NotAGitRepoError(f"git is not available to verify {start}: {exc}") from exc
    if result.returncode != 0:
        raise NotAGitRepoError(
            f"{start} is not inside a git working tree "
            f"(git rev-parse --show-toplevel failed: {result.stderr.strip()})"
        )
    return Path(result.stdout.strip())


@dataclass(frozen=True)
class RuneLayout:
    repo_root: Path

    @property
    def rune_dir(self) -> Path:
        return self.repo_root / RUNE_DIR_NAME

    @property
    def config_path(self) -> Path:
        return self.rune_dir / "config.toml"

    @property
    def project_json(self) -> Path:
        return self.rune_dir / "project.json"

    @property
    def scopes_json(self) -> Path:
        return self.rune_dir / "scopes.json"

    @property
    def decisions_jsonl(self) -> Path:
        return self.rune_dir / "decisions.jsonl"

    @property
    def constraints_jsonl(self) -> Path:
        return self.rune_dir / "constraints.jsonl"

    @property
    def notes_jsonl(self) -> Path:
        return self.rune_dir / "notes.jsonl"

    @property
    def semantic_jsonl(self) -> Path:
        return self.rune_dir / "semantic.jsonl"

    @property
    def proposals_jsonl(self) -> Path:
        return self.rune_dir / "proposals.jsonl"

    @property
    def cache_dir(self) -> Path:
        return self.rune_dir / "cache"

    @property
    def memory_db(self) -> Path:
        return self.cache_dir / "memory.db"

    @property
    def gitignore(self) -> Path:
        return self.rune_dir / ".gitignore"

    def canonical_memory_files(self) -> list[Path]:
        """The seven canonical files `init --force` must never clobber
        (ARCHITECTURE.md §10). project.json/scopes.json are structural but
        still protected once they contain real content.
        """
        return [
            self.project_json,
            self.scopes_json,
            self.semantic_jsonl,
            self.decisions_jsonl,
            self.constraints_jsonl,
            self.notes_jsonl,
            self.proposals_jsonl,
        ]


def _write_gitignore(layout: RuneLayout, commit_proposals_to_git: bool) -> None:
    lines = ["cache/"]
    if not commit_proposals_to_git:
        lines.append("proposals.jsonl")
    atomic_write_text(layout.gitignore, "\n".join(lines) + "\n")


def _touch_empty_jsonl_files(layout: RuneLayout) -> None:
    for path in (
        layout.decisions_jsonl,
        layout.constraints_jsonl,
        layout.notes_jsonl,
        layout.semantic_jsonl,
        layout.proposals_jsonl,
    ):
        if not path.exists():
            atomic_write_text(path, "")


def init_project(repo_root: Path, force: bool = False) -> RuneLayout:
    """Creates or repairs `.rune/` under `repo_root`.

    Raises AlreadyInitializedError if `.rune/` already exists and
    `force=False`. With `force=True`, only missing canonical files are
    created and the SQLite cache is scheduled for a rebuild by the caller
    (this function does not touch memory.db itself — see cli.main / M2+
    for the full rebuild-cache pipeline).
    """
    layout = RuneLayout(repo_root=repo_root)
    already_initialized = layout.rune_dir.exists()

    if already_initialized and not force:
        raise AlreadyInitializedError(
            f"{layout.rune_dir} already exists. Use `rune update` to refresh "
            f"memory, or `rune init --force` to repair missing structural "
            f"files (canonical memory is never touched by --force)."
        )

    layout.rune_dir.mkdir(parents=True, exist_ok=True)

    if not layout.project_json.exists():
        project = ProjectFile(
            project_id=str(uuid.uuid4()),
            name=repo_root.name,
            created_at=utc_now_iso(),
        )
        write_json_model(layout.project_json, project)

    if not layout.scopes_json.exists():
        write_json_model(layout.scopes_json, ScopesFile())

    if not layout.config_path.exists():
        write_default_config(layout.config_path)

    _touch_empty_jsonl_files(layout)

    from rune.core.config import load_config

    config = load_config(layout.config_path)
    _write_gitignore(layout, config.proposals.commit_to_git)

    return layout


def is_git_available() -> bool:
    try:
        result = subprocess.run(
            ["git", "--version"], capture_output=True, check=False
        )
    except OSError:
        return False
    return result.returncode == 0
