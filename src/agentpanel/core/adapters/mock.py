"""Deterministic mock adapter.

Stands in for not-yet-installed agents and powers the engine's unit tests without
spending real model calls. Behavior is scripted entirely through ``extra_args`` as
``key=value`` tokens so a test can compose any panel it likes:

- ``plan=A``         the approach label this agent proposes in round 0 (default: its name)
- ``switch_to=B``    starting at ``switch_turn``, adopt approach ``B`` (models persuasion)
- ``switch_turn=2``  turn at which the switch happens (default 1)
- ``fit=0.9``        self-reported fitness to execute (0..1; default 0.5) — feeds election
- ``delay=0.0``      seconds to sleep before responding (to exercise the barrier timeout)
- ``fail=1``         emit an error instead of a plan (to exercise dead-panelist handling)

Plans are emitted with a stable first line ``APPROACH: <label>`` so the deterministic
judge can cluster them exactly, mirroring what the real semantic judge does fuzzily.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator, Dict

from ..adapter import AdapterEvent, AgentAdapter, HealthStatus, RunContext


class MockAdapter(AgentAdapter):
    kind = "mock"
    default_binary = ""

    def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
        super().__init__(config)
        self.spec = _parse_spec(config.extra_args)

    async def health(self) -> HealthStatus:
        return HealthStatus(
            name=self.name,
            kind=self.kind,
            installed=True,
            binary="(mock)",
            version="mock-1.0",
            authed=True,
            detail="mock agent — always available",
        )

    def _label_for_turn(self, turn: int) -> str:
        base = self.spec.get("plan", self.name)
        switch_to = self.spec.get("switch_to")
        switch_turn = int(self.spec.get("switch_turn", "1"))
        if switch_to and turn >= switch_turn:
            return switch_to
        return base

    async def _emit(self, label: str, body: str, ctx: RunContext) -> AsyncIterator[AdapterEvent]:
        delay = float(self.spec.get("delay", "0"))
        if delay:
            await asyncio.sleep(delay)
        if self.spec.get("fail") == "1":
            yield AdapterEvent.error(f"{self.name}: simulated failure")
            return
        fit = self.spec.get("fit", "0.5")
        text = f"APPROACH: {label}\n{body}\nFIT: {fit}\n"
        # Stream it in a couple of chunks so the UI/event path is exercised.
        yield AdapterEvent.token(f"APPROACH: {label}\n")
        yield AdapterEvent.token(f"{body}\n")
        yield AdapterEvent.done(text, session_ref=f"mock-{self.name}")

    def plan(self, prompt: str, ctx: RunContext) -> AsyncIterator[AdapterEvent]:
        label = self._label_for_turn(0)
        return self._emit(label, f"{self.name}'s isolated plan for: {prompt[:60]}", ctx)

    def critique(self, prompt: str, peers: str, ctx: RunContext) -> AsyncIterator[AdapterEvent]:
        label = self._label_for_turn(ctx.turn)
        return self._emit(label, f"{self.name} stands by {label} after reviewing peers", ctx)

    async def execute(self, plan: str, ctx: RunContext) -> AsyncIterator[AdapterEvent]:
        # A real agent edits files in its worktree; the mock writes a marker file so the
        # execution -> diff -> keep flow is exercisable end to end without real agents.
        try:
            (ctx.workdir / f"{self.name}_change.txt").write_text(
                f"{self.name} implemented: {plan[:80]}\n", encoding="utf-8"
            )
            yield AdapterEvent.tool_use("write", f"{self.name}_change.txt")
        except Exception as exc:  # pragma: no cover - workdir issues
            yield AdapterEvent.error(f"{self.name}: execute write failed: {exc}")
            return
        yield AdapterEvent.done(f"{self.name} executed plan.", session_ref=f"mock-{self.name}")


def _parse_spec(extra_args) -> Dict[str, str]:  # type: ignore[no-untyped-def]
    spec: Dict[str, str] = {}
    for token in extra_args or []:
        if "=" in token:
            key, _, value = token.partition("=")
            spec[key.strip()] = value.strip()
    return spec
