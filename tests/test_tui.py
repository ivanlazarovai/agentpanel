"""Smoke test for the Textual TUI via the headless pilot.

Drives a real (mock-backed) deliberation through the app and asserts the view reacts:
the consensus bar reaches CONVERGED and a collapsible card exists per agent with the
elected agent's card auto-expanded.
"""

from __future__ import annotations

import asyncio

import pytest

from agentpanel.tui.app import AgentPanelApp
from agentpanel.tui.session_view import AgentCard, SessionView
from agentpanel.core.config import AgentConfig, Config, JudgeConfig, Settings
from textual.widgets import TabPane


def _demo_config() -> Config:
    return Config(
        roster=[
            AgentConfig(name="claude", kind="mock", extra_args=["plan=A", "fit=0.8"]),
            AgentConfig(name="cursor", kind="mock", extra_args=["plan=A", "fit=0.6"]),
            AgentConfig(name="codex", kind="mock",
                        extra_args=["plan=B", "switch_to=A", "switch_turn=2", "fit=0.5"]),
        ],
        judge=JudgeConfig(backend="deterministic"),
        settings=Settings(consensus_threshold=0.75, max_turns=3),
    )


@pytest.mark.asyncio
async def test_tui_runs_and_converges():
    app = AgentPanelApp(config=_demo_config(), demo_question="Persist sessions")
    async with app.run_test() as pilot:
        # Wait for the demo session to finish (bounded).
        for _ in range(200):
            sessions = app.manager.list()
            if sessions and sessions[0].status in ("converged", "escalated"):
                break
            await pilot.pause()
            await asyncio.sleep(0.01)

        session = app.manager.list()[0]
        assert session.status == "converged"

        view = app.query_one(SessionView)
        cards = view.query(AgentCard)
        assert len(cards) == 3  # one card per panelist

        assert "CONVERGED" in view.bar_text

        # The elected agent's card auto-expanded.
        elected = session.outcome.elected
        elected_card = next(c for c in cards if c._agent == elected)
        assert elected_card.collapsed is False

        # Per-turn tabs were created (codex deliberated across multiple turns).
        panes = view.query(TabPane)
        assert len(panes) >= 3
