"""Session persistence: save a session, restore it (agents keep their resume handles)."""

from __future__ import annotations

import subprocess

import pytest

from agentpanel.core.session import SessionManager
from agentpanel.core.store import PanelistRecord, SessionRecord, SessionStore
from tests.conftest import make_config, mock_agent


def test_record_round_trips(tmp_path):
    store = SessionStore(tmp_path)
    rec = SessionRecord(
        id="s001", question="do it", repo=str(tmp_path), status="converged",
        elected="claude", plan="APPROACH: A\nFIT: 0.8",
        panelists=[PanelistRecord(name="claude", kind="claude_code", session_ref="abc", plan="A")],
    )
    store.save(rec)
    again = store.load("s001")
    assert again.question == "do it" and again.elected == "claude"
    assert again.panelists[0].session_ref == "abc"
    assert [r.id for r in store.load_all()] == ["s001"]


def _git(path):
    def g(*a):
        subprocess.run(["git", "-C", str(path), *a], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    g("init", "-b", "main")
    g("config", "user.email", "t@t.co")
    g("config", "user.name", "t")
    (path / "R.md").write_text("hi")
    g("add", "-A")
    g("commit", "-qm", "i")


@pytest.mark.asyncio
async def test_session_persists_and_restores(tmp_path):
    _git(tmp_path)
    cfg = make_config([mock_agent("a", plan="A", fit=0.9), mock_agent("b", plan="A")], threshold=0.5)
    mgr = SessionManager(cfg)
    session = mgr.create("build a thing", repo=tmp_path, use_worktrees=False)
    await session.run()
    assert session.outcome.status == "converged"

    # a record was written
    saved = SessionStore(tmp_path).load_all()
    assert saved and saved[0].question == "build a thing"
    assert saved[0].elected == "a"
    # the mock's session_ref was captured for resume
    refs = {p.name: p.session_ref for p in saved[0].panelists}
    assert refs["a"] == "mock-a"

    # a fresh manager restores it — panelists carry their session_ref, outcome rebuilt
    mgr2 = SessionManager(cfg)
    restored = mgr2.load_saved(tmp_path)
    assert len(restored) == 1
    r = restored[0]
    assert r.id == session.id and r.status == "converged"
    assert r.outcome.elected == "a"
    by = {p.name: p for p in r.panelists}
    assert by["a"].session_ref == "mock-a"  # the agent can be resumed
    assert by["a"].record.text.startswith("APPROACH: A")
