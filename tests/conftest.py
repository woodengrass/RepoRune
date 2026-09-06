from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "integration" / "fixtures"


def _git_init_and_commit(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t",
         "add", "-A"],
        cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t",
         "commit", "-q", "-m", "init"],
        cwd=repo, check=True,
    )


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    """A fresh git repo with one empty commit, in an isolated tmp dir."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", "init"],
        cwd=repo, check=True,
    )
    return repo


def _fixture_repo(tmp_path: Path, fixture_name: str) -> Path:
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURES_DIR / fixture_name, repo)
    _git_init_and_commit(repo)
    return repo


@pytest.fixture()
def python_simple_repo(tmp_path: Path) -> Path:
    """A copy of tests/integration/fixtures/python-simple, git-initialized
    with one commit."""
    return _fixture_repo(tmp_path, "python-simple")


@pytest.fixture()
def ts_simple_repo(tmp_path: Path) -> Path:
    """A copy of tests/integration/fixtures/ts-simple, git-initialized
    with one commit."""
    return _fixture_repo(tmp_path, "ts-simple")
