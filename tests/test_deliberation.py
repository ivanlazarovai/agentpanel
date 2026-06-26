"""End-to-end engine tests with mock panels (deterministic, no model calls)."""

from __future__ import annotations

import pytest

from agentpanel.core.events import EventKind
from tests.conftest import mock_agent, run_panel


@pytest.mark.asyncio
async def test_converges_when_all_agree():
    session = await run_panel(
        [mock_agent("a", plan="A", fit=0.5),
         mock_agent("b", plan="A", fit=0.8),
         mock_agent("c", plan="A", fit=0.6)],
        threshold=0.5,
    )
    out = session.outcome
    assert out.status == "converged"
    assert out.turns_used == 0  # agreed straight from isolated planning
    assert out.elected == "b"  # highest fit
    assert "APPROACH: A" in out.plan


@pytest.mark.asyncio
async def test_escalates_when_split_and_no_consensus():
    session = await run_panel(
        [mock_agent("a", plan="A"), mock_agent("b", plan="B"), mock_agent("c", plan="C")],
        threshold=0.9,
        max_turns=2,
    )
    out = session.outcome
    assert out.status == "escalated"
    assert len(out.options) == 3
    labels = {o["label"] for o in out.options}
    assert labels == {"A", "B", "C"}


@pytest.mark.asyncio
async def test_convergence_over_turns():
    # c starts on B, switches to A at turn 2 -> converges only after deliberation.
    session = await run_panel(
        [mock_agent("a", plan="A"),
         mock_agent("b", plan="A"),
         mock_agent("c", plan="B", switch_to="A", switch_turn=2)],
        threshold=0.75,
        max_turns=3,
    )
    out = session.outcome
    assert out.status == "converged"
    assert out.turns_used == 2  # converged on the 2nd critique turn


@pytest.mark.asyncio
async def test_barrier_timeout_excludes_slow_agent():
    # 'slow' exceeds the per-turn deadline -> marked silent; the rest still converge.
    session = await run_panel(
        [mock_agent("a", plan="A"),
         mock_agent("b", plan="A"),
         mock_agent("slow", plan="B", delay=2.0)],
        threshold=0.9,
        barrier_timeout_s=0.3,
    )
    out = session.outcome
    assert out.status == "converged"
    assert "slow" in out.result.silent
    # A timeout event was emitted for the slow agent.
    kinds = [(e.kind, e.data.get("agent")) for e in session.bus.history()]
    assert (EventKind.PANELIST_TIMEOUT, "slow") in kinds


@pytest.mark.asyncio
async def test_dead_panelist_does_not_stall():
    session = await run_panel(
        [mock_agent("a", plan="A"),
         mock_agent("b", plan="A"),
         mock_agent("broken", plan="B", fail=1)],
        threshold=0.9,
    )
    out = session.outcome
    assert out.status == "converged"
    assert "broken" in out.result.silent


@pytest.mark.asyncio
async def test_election_picks_highest_fit_in_leading_cluster():
    session = await run_panel(
        [mock_agent("low", plan="A", fit=0.2),
         mock_agent("high", plan="A", fit=0.95),
         mock_agent("mid", plan="A", fit=0.5)],
        threshold=0.5,
    )
    assert session.outcome.elected == "high"


@pytest.mark.asyncio
async def test_mediation_emits_sessions_and_decisions():
    # The panel surfaces each agent's native session handle and relays proceed/stand-down.
    session = await run_panel(
        [mock_agent("claude", plan="A", fit=0.9),
         mock_agent("cursor", plan="A", fit=0.3)],
        threshold=0.5,
    )
    h = session.bus.history()
    # Every agent's own session was surfaced (so the user could open it).
    sessions = {e.data["agent"] for e in h if e.kind == EventKind.AGENT_SESSION}
    assert sessions == {"claude", "cursor"}
    # The elected agent is told to proceed; the other stands down.
    decisions = {e.data["agent"]: e.data["decision"] for e in h if e.kind == EventKind.DECISION}
    assert decisions == {"claude": "proceed", "cursor": "stand_down"}


@pytest.mark.asyncio
async def test_parallel_sessions_are_isolated():
    # One manager, two sessions running concurrently: distinct ids, buses, and outcomes.
    import asyncio

    from agentpanel.core.session import SessionManager
    from tests.conftest import make_config

    cfg = make_config([mock_agent("a", plan="A"), mock_agent("b", plan="A")], threshold=0.5)
    mgr = SessionManager(cfg)
    s1 = mgr.create("q1", repo=None, use_worktrees=False)
    s2 = mgr.create("q2", repo=None, use_worktrees=False)
    await asyncio.gather(mgr.start(s1), mgr.start(s2))
    assert s1.id != s2.id
    assert s1.bus is not s2.bus
    assert s1.outcome.status == "converged"
    assert s2.outcome.status == "converged"
    # Each bus only saw its own question.
    q1 = [e.data.get("question") for e in s1.bus.history() if e.kind == EventKind.SESSION_CREATED]
    assert q1 == ["q1"]
