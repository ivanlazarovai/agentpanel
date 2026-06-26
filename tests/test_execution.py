"""End-to-end execution: deliberate -> elected agent executes in its worktree -> keep."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agentpanel.core.session import SessionManager
from tests.conftest import make_config, mock_agent


def _init_repo(path: Path) -> None:
    def git(*a):
        subprocess.run(["git", "-C", str(path), *a], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    git("init", "-b", "main")
    git("config", "user.email", "t@t.co")
    git("config", "user.name", "t")
    (path / "README.md").write_text("base\n")
    git("add", "-A")
    git("commit", "-m", "init")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    _init_repo(tmp_path)
    return tmp_path


@pytest.mark.asyncio
async def test_converge_execute_diff_keep(repo: Path):
    cfg = make_config(
        [mock_agent("claude", plan="A", fit=0.9), mock_agent("cursor", plan="A", fit=0.4)],
        threshold=0.5,
    )
    mgr = SessionManager(cfg)
    session = mgr.create("add a feature", repo=repo, use_worktrees=True)
    outcome = await session.run()
    assert outcome.status == "converged"
    assert outcome.elected == "claude"  # highest fit

    # Elected agent executes in its worktree and produces a committed diff.
    diffs = await session.execute([outcome.elected])
    assert "claude_change.txt" in diffs["claude"]

    # Keep the winner -> its change lands on main.
    await session.keep("claude")
    assert (repo / "claude_change.txt").exists()

    await session.cleanup()


@pytest.mark.asyncio
async def test_coopetition_observers_coach_the_worker(repo: Path):
    from agentpanel.core.events import EventKind

    cfg = make_config(
        [mock_agent("claude", plan="A", fit=0.9), mock_agent("cursor", plan="A", fit=0.4)],
        threshold=0.5,
    )
    mgr = SessionManager(cfg)
    session = mgr.create("build it", repo=repo, use_worktrees=True)
    outcome = await session.run()
    assert outcome.elected == "claude"

    # 1 coopetition round: cursor (stood down) observes + coaches claude (the worker).
    await session.execute([outcome.elected], review_rounds=1)
    h = session.bus.history()
    # cursor was put into monitoring, and gave feedback aimed at claude.
    monitors = {e.data["agent"] for e in h if e.kind == EventKind.DECISION
                and e.data["decision"] == "monitor"}
    assert "cursor" in monitors
    obs = [e for e in h if e.kind == EventKind.OBSERVATION]
    assert any(e.data["observer"] == "cursor" and e.data["target"] == "claude" for e in obs)
    await session.cleanup()


@pytest.mark.asyncio
async def test_escalation_user_picks_multiple_executors(repo: Path):
    cfg = make_config(
        [mock_agent("claude", plan="A"), mock_agent("cursor", plan="B")],
        threshold=0.9, max_turns=1,
    )
    mgr = SessionManager(cfg)
    session = mgr.create("do the thing", repo=repo, use_worktrees=True)
    outcome = await session.run()
    assert outcome.status == "escalated"

    # User chooses to run both and compare; each works in its own branch.
    diffs = await session.execute(["claude", "cursor"])
    assert "claude_change.txt" in diffs["claude"]
    assert "cursor_change.txt" in diffs["cursor"]
    await session.cleanup()
