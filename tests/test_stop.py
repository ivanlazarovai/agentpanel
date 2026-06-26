"""Stopping a runaway agent: terminate() kills its subprocess and clears is_busy."""

from __future__ import annotations

import asyncio

from agentpanel.core.adapters.codex import CodexAdapter
from agentpanel.core.config import AgentConfig


class _FakeProc:
    def __init__(self) -> None:
        self.killed = False

    def kill(self) -> None:
        self.killed = True


def test_terminate_kills_tracked_procs_and_clears_busy():
    a = CodexAdapter(AgentConfig(name="codex", kind="codex"))
    assert a.is_busy is False
    proc = _FakeProc()
    a._procs.add(proc)
    assert a.is_busy is True
    asyncio.run(a.terminate())
    assert proc.killed is True
    assert a.is_busy is False
