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


def test_benched_agent_excluded_from_turns_and_consensus():
    """Benching drops an agent from the active set, consensus records, and future turns."""
    import tempfile
    from pathlib import Path

    from agentpanel.core.adapters import build
    from agentpanel.core.config import JudgeConfig, Settings
    from agentpanel.core.deliberation import DeliberationEngine, Panelist
    from agentpanel.core.events import EventBus
    from agentpanel.core.judge import build_judge

    def mk(name):
        cfg = AgentConfig(name=name, kind="mock")
        return Panelist(config=cfg, adapter=build(cfg), workdir=Path(tempfile.mkdtemp()))

    panelists = [mk("a"), mk("b"), mk("c")]
    eng = DeliberationEngine(question="do x", panelists=panelists, settings=Settings(),
                             judge=build_judge(JudgeConfig(backend="deterministic")), bus=EventBus())
    asyncio.run(eng._panel_turn(0, "plan"))
    assert {r.agent for r in eng.records()} == {"a", "b", "c"}

    panelists[1].benched = True  # bench "b"
    assert [p.name for p in eng.active_panelists()] == ["a", "c"]
    assert {r.agent for r in eng.records()} == {"a", "c"}  # dropped from consensus

    asyncio.run(eng._panel_turn(1, "critique"))
    assert panelists[1].record.turn == 0  # b did not run turn 1
