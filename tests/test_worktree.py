"""Worktree manager tests against a throwaway git repo."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agentpanel.core.worktree import WorktreeManager


def _init_repo(path: Path) -> None:
    def git(*args):
        subprocess.run(["git", "-C", str(path), *args], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    git("init", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (path / "README.md").write_text("hello\n")
    git("add", "-A")
    git("commit", "-m", "init")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    _init_repo(tmp_path)
    return tmp_path


@pytest.mark.asyncio
async def test_create_diff_commit_cleanup(repo: Path):
    wm = WorktreeManager(repo, "s001")
    handle = await wm.create("claude")
    assert handle.path.exists()
    assert handle.branch == "ap/s001/claude"
    assert (handle.path / "README.md").exists()

    # Make a change in the worktree and commit it.
    (handle.path / "feature.txt").write_text("new feature\n")
    assert await wm.has_changes("claude")
    sha = await wm.commit_all("claude", "add feature")
    assert sha

    stat = await wm.diffstat("claude")
    assert "feature.txt" in stat
    patch = await wm.diff("claude")
    assert "new feature" in patch

    await wm.cleanup()
    assert not handle.path.exists()


@pytest.mark.asyncio
async def test_two_agents_isolated(repo: Path):
    wm = WorktreeManager(repo, "s002")
    a = await wm.create("claude")
    b = await wm.create("cursor")
    (a.path / "a.txt").write_text("A\n")
    (b.path / "b.txt").write_text("B\n")
    await wm.commit_all("claude", "a")
    await wm.commit_all("cursor", "b")
    # Each branch sees only its own file.
    assert "a.txt" in await wm.diffstat("claude")
    assert "b.txt" not in await wm.diffstat("claude")
    assert "b.txt" in await wm.diffstat("cursor")
    await wm.cleanup()


@pytest.mark.asyncio
async def test_keep_merges_winner(repo: Path):
    wm = WorktreeManager(repo, "s003")
    a = await wm.create("claude")
    (a.path / "win.txt").write_text("winner\n")
    await wm.commit_all("claude", "win")
    await wm.keep("claude")
    # Winner's file is now on main.
    assert (repo / "win.txt").read_text() == "winner\n"
    await wm.cleanup()
