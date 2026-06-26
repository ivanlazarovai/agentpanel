"""The deliberation engine — AgentPanel's turn-synchronized state machine.

Mirrors the handydocs3 pattern: a *deterministic* engine driving *swappable* brains
(the agent adapters + the judge). The flow for one request:

    turn 0  ISOLATED_PLANNING   every panelist plans blind, in parallel, in its worktree
            BARRIER             wait for all (per-turn deadline) — agents get time to answer
            CONSENSUS_CHECK     judge clusters plans; if a cluster >= X% -> CONVERGED
    turn n  CRITIQUE            each sees the whole panel, revises + judges peers (resumes
            BARRIER             its own session so it keeps its own understanding)
            CONSENSUS_CHECK     re-evaluate; converge, or continue while n <= Y
    end     CONVERGED -> elect best-positioned agent      (-> execution happens upstream)
            or ESCALATE -> top-N options for the user to decide

The engine is UI-agnostic: progress flows out as :mod:`events`; it returns a
:class:`SessionOutcome`. Worktrees are optional (mock panels run without them).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .adapter import AgentAdapter, RunContext
from .config import AgentConfig, Settings
from .consensus import (
    ConsensusResult,
    PlanRecord,
    evaluate,
    extract_fit,
    top_options,
)
from .events import EventBus, EventKind
from .judge import Judge


@dataclass
class Panelist:
    """A panel member: an adapter + its config + its evolving state."""

    config: AgentConfig
    adapter: AgentAdapter
    workdir: Path
    session_ref: Optional[str] = None  # the agent's own resume handle, carried across turns
    record: Optional[PlanRecord] = None  # latest plan
    emitted_ref: Optional[str] = None  # last session_ref surfaced via AGENT_SESSION

    @property
    def name(self) -> str:
        return self.config.name


@dataclass
class SessionOutcome:
    """What the engine concluded."""

    status: str  # "converged" | "escalated"
    result: ConsensusResult
    turns_used: int
    # converged:
    plan: str = ""
    elected: Optional[str] = None
    runners_up: List[str] = field(default_factory=list)
    # escalated:
    options: List[Dict] = field(default_factory=list)


class DeliberationEngine:
    """Runs one panel session from question to verdict."""

    def __init__(
        self,
        question: str,
        panelists: List[Panelist],
        judge: Judge,
        settings: Settings,
        bus: EventBus,
    ) -> None:
        self.question = question
        self.panelists = panelists
        self.judge = judge
        self.settings = settings
        self.bus = bus

    # -- public ------------------------------------------------------------

    async def run(self) -> SessionOutcome:
        """Drive the state machine to convergence or escalation."""
        await self._panel_turn(turn=0, mode="plan")
        result = await self._consensus(turn=0)

        turn = 1
        while not result.converged and turn <= self.settings.max_turns:
            await self._panel_turn(turn=turn, mode="critique")
            result = await self._consensus(turn=turn)
            turn += 1

        turns_used = result.turn
        if result.converged and result.leading:
            outcome = self._converged_outcome(result, turns_used)
        else:
            outcome = self._escalated_outcome(result, turns_used)
        return outcome

    def records(self) -> List[PlanRecord]:
        return [p.record for p in self.panelists if p.record is not None]

    # -- one synchronized turn (the BARRIER) -------------------------------

    async def _panel_turn(self, turn: int, mode: str) -> None:
        phase = "isolated_planning" if mode == "plan" else "critique"
        self.bus.publish(EventKind.TURN_STARTED, turn=turn)
        self.bus.publish(EventKind.PHASE_CHANGED, phase=phase, turn=turn)

        peers = self._render_panel(exclude=None) if mode == "critique" else ""

        # Launch every panelist concurrently; gather is the barrier.
        await asyncio.gather(
            *(self._invoke(p, mode, turn, peers) for p in self.panelists),
            return_exceptions=True,
        )

        responded = [p.name for p in self.panelists if _responded_this_turn(p, turn)]
        missing = [p.name for p in self.panelists if p.name not in responded]
        self.bus.publish(
            EventKind.BARRIER_REACHED, turn=turn, responded=responded, missing=missing
        )

    async def _invoke(self, p: Panelist, mode: str, turn: int, peers: str) -> None:
        """Run one verb for one panelist, with the per-turn deadline, updating its record."""
        ctx = RunContext(
            workdir=p.workdir,
            session_ref=p.session_ref,
            model=p.config.model,
            turn=turn,
            timeout_s=self.settings.barrier_timeout_s,
        )
        gen = p.adapter.plan(self.question, ctx) if mode == "plan" else p.adapter.critique(
            self.question, peers, ctx
        )
        self.bus.publish(EventKind.PANELIST_STARTED, agent=p.name, mode=mode, turn=turn)
        started = time.monotonic()
        try:
            full, sref, failed, err, cost, tokens = await asyncio.wait_for(
                self._consume(p, gen), timeout=self.settings.barrier_timeout_s
            )
        except asyncio.TimeoutError:
            self.bus.publish(EventKind.PANELIST_TIMEOUT, agent=p.name, turn=turn)
            p.record = PlanRecord(
                agent=p.name, text="", turn=turn, failed=True, weight=p.config.weight,
                session_ref=p.session_ref,
            )
            return

        if sref:
            p.session_ref = sref
            # Surface the agent's own native session so the user can open and watch it.
            if sref != p.emitted_ref:
                cmd = p.adapter.open_command(sref, p.workdir)
                self.bus.publish(
                    EventKind.AGENT_SESSION, agent=p.name, session_ref=sref, open_command=cmd
                )
                p.emitted_ref = sref
        if failed:
            p.record = PlanRecord(
                agent=p.name, text="", turn=turn, failed=True, weight=p.config.weight,
                session_ref=p.session_ref,
            )
            return
        p.record = PlanRecord(
            agent=p.name,
            text=full,
            turn=turn,
            session_ref=p.session_ref,
            fit=extract_fit(full),
            weight=p.config.weight,
        )
        self.bus.publish(
            EventKind.PANELIST_DONE, agent=p.name, mode=mode, turn=turn,
            summary=_first_line(full), fit=p.record.fit,
            duration_ms=int((time.monotonic() - started) * 1000),
            cost_usd=cost, tokens=tokens,
        )

    async def _consume(self, p: Panelist, gen) -> tuple:
        """Drain an adapter's event stream, forwarding to the bus. Returns
        (full_text, session_ref, failed, error, cost_usd, tokens)."""
        collected: List[str] = []
        sref = p.session_ref
        full = ""
        failed = False
        err = ""
        cost = None
        tokens = None
        async for ev in gen:  # type: AdapterEvent
            if ev.session_ref:
                sref = ev.session_ref
            if ev.type == "token":
                collected.append(ev.text)
                self.bus.publish(EventKind.PANELIST_TOKEN, agent=p.name, text=ev.text)
            elif ev.type == "tool":
                self.bus.publish(
                    EventKind.PANELIST_TOOL, agent=p.name, tool=ev.tool, detail=ev.detail
                )
            elif ev.type == "error":
                failed = True
                err = ev.detail
                self.bus.publish(EventKind.PANELIST_ERROR, agent=p.name, message=ev.detail)
            elif ev.type == "done":
                full = ev.full_text or "".join(collected)
                cost = ev.cost_usd
                tokens = ev.tokens
        if not full:
            full = "".join(collected)
        return full, sref, failed, err, cost, tokens

    # -- consensus ---------------------------------------------------------

    async def _consensus(self, turn: int) -> ConsensusResult:
        plans = self.records()
        started = time.monotonic()
        clusters = await self.judge.cluster(self.question, plans)
        self.bus.publish(
            EventKind.JUDGE, turn=turn, backend=type(self.judge).__name__,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        result = evaluate(plans, clusters, self.settings.consensus_threshold, turn)
        self.bus.publish(
            EventKind.CONSENSUS_COMPUTED,
            turn=turn,
            agreement=round(result.agreement, 3),
            threshold=self.settings.consensus_threshold,
            converged=result.converged,
            clusters=[
                {"label": c.label, "members": c.members, "weight": c.weight}
                for c in result.clusters
            ],
            dissenters=result.dissenters,
            silent=result.silent,
            elected=result.elected,
        )
        return result

    # -- outcomes ----------------------------------------------------------

    def _converged_outcome(self, result: ConsensusResult, turns_used: int) -> SessionOutcome:
        leading = result.leading
        rep = leading.representative if leading else None
        plan_text = ""
        if rep:
            plan_text = next((p.text for p in self.records() if p.agent == rep), "")
        runners_up = [a for a, _ in result.ranking if a != result.elected][:2]
        self.bus.publish(
            EventKind.CONVERGED,
            agreement=round(result.agreement, 3),
            elected=result.elected,
            runners_up=runners_up,
            plan=plan_text,
        )
        # Relay the per-session proceed/stand-down decision (the agent's native session
        # can show this — "the panel selected X, so this session will proceed/stand down").
        for rec in self.records():
            if not rec.responded:
                continue
            if rec.agent == result.elected:
                self.bus.publish(EventKind.DECISION, agent=rec.agent, decision="proceed",
                                 reason="elected by the panel to implement the agreed plan")
            else:
                self.bus.publish(EventKind.DECISION, agent=rec.agent, decision="stand_down",
                                 reason=f"panel converged on {leading.label if leading else 'a plan'}; "
                                        f"{result.elected} will implement")
        return SessionOutcome(
            status="converged",
            result=result,
            turns_used=turns_used,
            plan=plan_text,
            elected=result.elected,
            runners_up=runners_up,
        )

    def _escalated_outcome(self, result: ConsensusResult, turns_used: int) -> SessionOutcome:
        options = top_options(self.records(), result.clusters, self.settings.escalation_top_n)
        self.bus.publish(
            EventKind.ESCALATED,
            reason=f"no cluster reached {int(self.settings.consensus_threshold * 100)}% "
            f"within {self.settings.max_turns} turns",
            options=options,
        )
        return SessionOutcome(
            status="escalated",
            result=result,
            turns_used=turns_used,
            options=options,
        )

    # -- helpers -----------------------------------------------------------

    def _render_panel(self, exclude: Optional[str]) -> str:
        """Render every panelist's current plan for a critique turn."""
        blocks = []
        for p in self.panelists:
            if p.name == exclude or not p.record or not p.record.text.strip():
                continue
            blocks.append(f"### Panelist: {p.name}\n{p.record.text.strip()}")
        return "\n\n".join(blocks) if blocks else "(no plans yet)"


def _responded_this_turn(p: Panelist, turn: int) -> bool:
    return bool(p.record and p.record.turn == turn and p.record.responded)


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()[:120]
    return ""
