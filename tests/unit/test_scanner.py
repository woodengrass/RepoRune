from __future__ import annotations

from pathlib import Path

from rune.core.index.scanner import (
    ScannedFile,
    diff_against_previous,
    scan_files,
    scan_files_with_issues,
)
from rune.core.storage.models import IndexConfig


def _write(path: Path, content: str = "x = 1\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_scan_files_finds_top_level_and_nested_files(tmp_path: Path) -> None:
    _write(tmp_path / "a.py")
    _write(tmp_path / "pkg" / "b.py")
    config = IndexConfig()
    results = scan_files(tmp_path, config)
    assert {r.path for r in results} == {"a.py", "pkg/b.py"}


def test_scan_files_skips_unsupported_extensions(tmp_path: Path) -> None:
    _write(tmp_path / "readme.md", "# hi\n")
    _write(tmp_path / "a.py")
    results = scan_files(tmp_path, IndexConfig())
    assert {r.path for r in results} == {"a.py"}


def test_scan_files_respects_exclude_pattern(tmp_path: Path) -> None:
    _write(tmp_path / "a.py")
    _write(tmp_path / "vendor" / "b.py")
    config = IndexConfig(include=["**/*"], exclude=["**/vendor/**"])
    results = scan_files(tmp_path, config)
    assert {r.path for r in results} == {"a.py"}


def test_scan_files_always_prunes_git_and_node_modules(tmp_path: Path) -> None:
    _write(tmp_path / ".git" / "objects" / "fake.py")
    _write(tmp_path / "node_modules" / "pkg" / "index.js")
    _write(tmp_path / "a.py")
    results = scan_files(tmp_path, IndexConfig())
    assert {r.path for r in results} == {"a.py"}


def test_scan_files_detects_language_by_extension(tmp_path: Path) -> None:
    _write(tmp_path / "a.py")
    _write(tmp_path / "b.ts", "const x = 1;\n")
    _write(tmp_path / "c.js", "const x = 1;\n")
    results = {r.path: r.language for r in scan_files(tmp_path, IndexConfig())}
    assert results == {"a.py": "python", "b.ts": "typescript", "c.js": "javascript"}


def test_scan_files_reports_an_unreadable_source_without_aborting(tmp_path: Path, monkeypatch) -> None:
    import rune.core.index.scanner as scanner_module

    _write(tmp_path / "good.py")
    _write(tmp_path / "blocked.py")
    real_hash = scanner_module.content_hash_of_file

    def failing_hash(path: Path) -> str:
        if path.name == "blocked.py":
            raise PermissionError("simulated sharing violation")
        return real_hash(path)

    monkeypatch.setattr(scanner_module, "content_hash_of_file", failing_hash)

    real_exists = Path.exists

    def inaccessible_exists(path: Path) -> bool:
        if path.name == "blocked.py":
            return False
        return real_exists(path)

    monkeypatch.setattr(Path, "exists", inaccessible_exists)

    result = scan_files_with_issues(tmp_path, IndexConfig())

    assert [file.path for file in result.files] == ["good.py"]
    assert result.unreadable_paths == ["blocked.py"]


def _scanned(path: str, content_hash: str) -> ScannedFile:
    return ScannedFile(
        path=path,
        absolute_path=Path(path),
        language="python",
        content_hash=content_hash,
        git_blob_hash=None,
        size=1,
        mtime=0.0,
    )


def test_diff_against_previous_classifies_added_modified_unchanged_deleted() -> None:
    current = [
        _scanned("a.py", "hash-a-new"),
        _scanned("b.py", "hash-b"),
        _scanned("c.py", "hash-c-new"),
    ]
    previous_hashes = {"a.py": "hash-a-old", "b.py": "hash-b", "d.py": "hash-d"}

    changeset = diff_against_previous(current, previous_hashes)

    assert [f.path for f in changeset.added] == ["c.py"]
    assert [f.path for f in changeset.modified] == ["a.py"]
    assert [f.path for f in changeset.unchanged] == ["b.py"]
    assert changeset.deleted_paths == ["d.py"]


def test_diff_against_previous_empty_previous_state_means_everything_added() -> None:
    current = [_scanned("a.py", "h1"), _scanned("b.py", "h2")]
    changeset = diff_against_previous(current, previous_hashes={})
    assert {f.path for f in changeset.added} == {"a.py", "b.py"}
    assert changeset.modified == []
    assert changeset.unchanged == []
    assert changeset.deleted_paths == []
