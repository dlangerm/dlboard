"""What `_context.collect` records about what produced a run, against a real (throwaway) git repository."""

from __future__ import annotations

import platform
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from dlboard.client import _context
from dlboard.client._context import ContextSettings, without_credentials

if TYPE_CHECKING:
    from pathlib import Path


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repository with one commit on `main`, an `origin` cloned with a token, as the working directory."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "train.py").write_text("lr = 0.1\n")
    _git(tmp_path, "add", "train.py")
    _git(tmp_path, "commit", "-q", "-m", "first")
    _git(tmp_path, "remote", "add", "origin", "https://me:ghp_secret@github.com/org/repo.git")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_a_clean_checkout_records_its_commit_branch_and_remote_and_no_diff(repo: Path) -> None:
    context, diff = _context.collect(1, 2, ContextSettings(diff=True))

    assert (context.git_commit, context.git_branch, context.git_dirty) == (_head(repo), "main", False)
    assert context.git_remote == "https://github.com/org/repo.git"
    assert diff is None
    assert (context.command, context.executable) == (sys.argv, sys.executable)
    assert context.python_version == platform.python_version()
    assert "pydantic" in context.packages


def test_uncommitted_changes_are_recorded_as_dirty_and_uploaded_as_the_diff(repo: Path) -> None:
    (repo / "train.py").write_text("lr = 0.2\n")

    context, diff = _context.collect(1, 2, ContextSettings(diff=True))

    assert context.git_dirty is True
    assert diff is not None
    assert b"+lr = 0.2" in diff.data


def test_a_diff_over_the_limit_is_left_out_and_warned_about(repo: Path) -> None:
    (repo / "train.py").write_text("x = 1\n" * 1000)

    with pytest.warns(UserWarning, match="over DLBOARD_CONTEXT_MAX_DIFF_KB"):
        context, diff = _context.collect(1, 2, ContextSettings(diff=True, max_diff_kb=1))

    assert (context.git_dirty, diff) == (True, None)


def test_what_is_turned_off_is_not_recorded(repo: Path) -> None:
    (repo / "train.py").write_text("lr = 0.2\n")

    context, diff = _context.collect(1, 2, ContextSettings(git=False, command=False, packages=False))

    assert (context.git_commit, context.git_dirty, diff) == (None, None, None)
    assert (context.command, context.cwd, context.packages) == ([], None, {})
    assert context.hostname  # where it ran is always recorded


def test_outside_a_repository_there_is_nothing_of_git_to_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))

    context, diff = _context.collect(1, 2, ContextSettings(diff=True))

    assert (context.git_commit, context.git_branch, context.git_remote, context.git_dirty, diff) == (
        None,
        None,
        None,
        None,
        None,
    )


@pytest.mark.parametrize(
    ("remote", "recorded"),
    [
        ("https://me:token@github.com/org/repo.git", "https://github.com/org/repo.git"),
        ("https://token@gitlab.example.com:8443/org/repo", "https://gitlab.example.com:8443/org/repo"),
        ("https://github.com/org/repo.git", "https://github.com/org/repo.git"),
        ("git@github.com:org/repo.git", "git@github.com:org/repo.git"),
        ("ssh://git@github.com/org/repo.git", "ssh://github.com/org/repo.git"),
    ],
)
def test_a_remote_is_recorded_without_credentials(remote: str, recorded: str) -> None:
    assert without_credentials(remote) == recorded
