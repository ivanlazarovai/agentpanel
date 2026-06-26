"""Shared test helpers: build mock panels and run the engine deterministically."""

from __future__ import annotations

from typing import List

from agentpanel.core.config import AgentConfig, Config, JudgeConfig, Settings
from agentpanel.core.session import Session, SessionManager


def mock_agent(name: str, **spec) -> AgentConfig:
    """An AgentConfig backed by the mock adapter, scripted via key=value extra_args.

    e.g. mock_agent("c", plan="B", switch_to="A", switch_turn=2, fit=0.9, delay=0, fail=0)
    """
    extra = [f"{k}={v}" for k, v in spec.items()]
    return AgentConfig(name=name, kind="mock", enabled=True, extra_args=extra)


def make_config(agents: List[AgentConfig], threshold: float = 0.5, max_turns: int = 3,
                barrier_timeout_s: float = 5.0) -> Config:
    return Config(
        roster=list(agents),
        judge=JudgeConfig(backend="deterministic"),  # falls back to DeterministicJudge
        settings=Settings(
            consensus_threshold=threshold,
            max_turns=max_turns,
            barrier_timeout_s=barrier_timeout_s,
        ),
    )


async def run_panel(agents: List[AgentConfig], question: str = "do the thing", **settings) -> Session:
    """Create + run one mock session without worktrees; return the finished Session."""
    config = make_config(agents, **settings)
    mgr = SessionManager(config)
    return await mgr.run_to_completion(question, repo=None, use_worktrees=False)
