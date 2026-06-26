"""Sessions and the manager that runs many of them in parallel.

A :class:`Session` bundles one question with its event bus, worktrees, engine, and final
outcome. :class:`SessionManager` owns multiple sessions concurrently (the TUI shows them
as tabs), each fully isolated: its own worktrees, its own bus, its own asyncio task.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .adapter import RunContext
from .adapters import build as build_adapter
from .config import Config
from .deliberation import DeliberationEngine, Panelist, SessionOutcome
from .events import EventBus, EventKind
from .judge import build_judge
from .metrics import MetricsRecorder, MetricsSink, repo_metrics_path
from .worktree import WorktreeManager


def _short_id(seq: int) -> str:
    """Stable, human-friendly session id (no clock/random — engine must be reproducible)."""
    return f"s{seq:03d}"


@dataclass
class Session:
    """One panel session."""

    id: str
    question: str
    repo: Optional[Path]
    bus: EventBus
    config: Config
    use_worktrees: bool = True
    worktrees: Optional[WorktreeManager] = None
    panelists: List[Panelist] = field(default_factory=list)
    engine: Optional[DeliberationEngine] = None
    outcome: Optional[SessionOutcome] = None
    status: str = "pending"  # pending | running | converged | escalated | error
    # Interactive permission channel: async (request) -> {"behavior": ...}. When set, an
    # executing agent's gated requests are surfaced to it live (TUI modal / stdin prompt).
    approval_resolver: Optional[object] = None
    _gate_sock: Optional[str] = None
    _task: Optional[asyncio.Task] = None

    async def prepare(self) -> None:
        """Build the panel: adapters, worktrees, judge, engine. No model calls yet."""
        # Append-only metrics (per-repo, git-ignored). Tap the bus before anything is
        # published. Skipped for repo-less/ephemeral sessions so nothing leaks to $HOME.
        if self.repo is not None:
            MetricsRecorder(MetricsSink(repo_metrics_path(self.repo)), self.id).register(self.bus)
        self.bus.publish(EventKind.SESSION_CREATED, id=self.id, question=self.question)

        members = self.config.panel() or self.config.enabled_agents()
        if not members:
            raise ValueError("no enabled/verified agents in roster — run `agentpanel setup`")

        adapters = {m.name: build_adapter(m) for m in members}

        if self.use_worktrees and self.repo is not None:
            self.worktrees = WorktreeManager(self.repo, self.id)

        self.panelists = []
        for m in members:
            workdir = self.repo or Path.cwd()
            if self.worktrees is not None:
                handle = await self.worktrees.create(m.name)
                workdir = handle.path
            self.panelists.append(
                Panelist(config=m, adapter=adapters[m.name], workdir=workdir)
            )

        judge = build_judge(self.config.judge, adapters)
        self.engine = DeliberationEngine(
            question=self.question,
            panelists=self.panelists,
            judge=judge,
            settings=self.config.settings,
            bus=self.bus,
        )

    async def run(self) -> SessionOutcome:
        """Run deliberation to convergence/escalation."""
        if self.engine is None:
            await self.prepare()
        assert self.engine is not None
        self.status = "running"
        try:
            self.outcome = await self.engine.run()
            self.status = self.outcome.status
        except Exception as exc:  # pragma: no cover - surfaced to the UI
            self.status = "error"
            self.bus.publish(EventKind.LOG, message=f"session error: {exc}", level="error")
            raise
        finally:
            self.bus.publish(
                EventKind.LOG, message=f"session {self.id} -> {self.status}", level="info"
            )
        return self.outcome

    async def execute(self, agent_names: List[str], review_rounds: int = 0) -> Dict[str, str]:
        """Have the named agents carry out the agreed plan in their own worktrees — with
        **coopetition**: the agents that weren't selected don't go idle. They observe the
        working agent(s) and feed back encouragement and/or criticism, which the panel relays
        into the worker's own session for the next round. The team works as superagents.

        ``review_rounds=0`` runs each worker once with no observation. With ``review_rounds>=1``
        the loop is: worker round → observers review the worker's diff → relay feedback →
        worker revises, repeated ``review_rounds`` times. Returns ``{agent: diffstat}``.
        """
        if self.worktrees is None:
            raise RuntimeError("execution requires worktrees (use_worktrees=True + a repo)")
        if self.outcome is None:
            raise RuntimeError("nothing to execute — run deliberation first")

        by_name = {p.name: p for p in self.panelists}
        workers = [n for n in agent_names if n in by_name]
        observers = [
            p.name for p in self.panelists
            if p.record and p.record.responded and p.name not in workers
        ]
        for name in workers:
            self.bus.publish(EventKind.DECISION, agent=name, decision="proceed",
                             reason="selected to implement the agreed plan")
        for name in observers:
            self.bus.publish(EventKind.DECISION, agent=name, decision="monitor",
                             reason="not selected — now observing and coaching the worker(s)")

        # Live permission channel: stand up the broker so gated requests reach the user.
        broker = await self._start_broker()
        feedback: Dict[str, str] = {w: "" for w in workers}
        results: Dict[str, str] = {}

        try:
            return await self._run_rounds(workers, observers, review_rounds, feedback, results)
        finally:
            if broker is not None:
                await broker.stop()
                self._gate_sock = None

    async def _run_rounds(self, workers, observers, review_rounds, feedback, results):
        by_name = {p.name: p for p in self.panelists}
        for rnd in range(review_rounds + 1):
            # 1) Each worker runs this round (resuming its own session across rounds).
            for name in workers:
                results[name] = await self._run_worker(by_name[name], feedback[name], rnd)
            # 2) If more rounds remain, observers review each worker and we relay feedback.
            if rnd < review_rounds and observers:
                for w in workers:
                    progress = await self.worktrees.diff(w)
                    notes = []
                    for obs in observers:
                        text = await self._run_observer(by_name[obs], w, progress, rnd)
                        if text:
                            self.bus.publish(EventKind.OBSERVATION, observer=obs, target=w,
                                             round=rnd, text=text)
                            notes.append(f"[{obs}] {text}")
                    feedback[w] = "\n".join(notes)
        return results

    async def _run_worker(self, panelist: Panelist, feedback: str, rnd: int) -> str:
        """Run one worker round in its own worktree/session; return its diffstat."""
        name = panelist.name
        handle = await self.worktrees.create(name)
        self.bus.publish(EventKind.EXECUTION_STARTED, agent=name, branch=handle.branch)
        prompt = self._mediation_prompt(name)
        if rnd > 0 and feedback:
            prompt += (
                "\n\n=== Peer feedback from the panel (coopetition — your teammates are "
                "watching and want you to succeed; incorporate or rebut) ===\n" + feedback
            )
        # The agent owns its edits, tools, and commits; the panel only relays + reads.
        # Execution gets full effort (the real work) unless the agent overrides it, and
        # its gated tool requests are answered by AgentPanel's permission policy.
        ctx = RunContext(workdir=handle.path, session_ref=panelist.session_ref,
                         model=panelist.config.model, effort=panelist.config.effort,
                         gate_env=self._gate_env())
        failed = False
        cost = None
        started = time.monotonic()
        async for ev in panelist.adapter.execute(prompt, ctx):
            if ev.session_ref:
                panelist.session_ref = ev.session_ref  # chain the worker's own session
            if ev.type == "token":
                self.bus.publish(EventKind.PANELIST_TOKEN, agent=name, text=ev.text)
            elif ev.type == "tool":
                self.bus.publish(EventKind.PANELIST_TOOL, agent=name, tool=ev.tool, detail=ev.detail)
            elif ev.type == "error":
                failed = True
                self.bus.publish(EventKind.PANELIST_ERROR, agent=name, message=ev.detail)
            elif ev.type == "done":
                cost = ev.cost_usd
        committed = None
        if not failed and await self.worktrees.has_changes(name):
            committed = await self.worktrees.commit_all(
                name, f"{name} (via AgentPanel): {self.question[:60]}"
            )
        diffstat = await self.worktrees.diffstat(name)
        self.bus.publish(EventKind.EXECUTION_DONE, agent=name, branch=handle.branch,
                         committed=bool(committed), role="worker", round=rnd,
                         duration_ms=int((time.monotonic() - started) * 1000), cost_usd=cost)
        self.bus.publish(EventKind.DIFF_READY, agent=name, branch=handle.branch, diffstat=diffstat)
        return diffstat

    async def _run_observer(self, panelist: Panelist, worker: str, progress: str, rnd: int) -> str:
        """An observing agent reviews the worker's progress and returns coaching feedback.

        Runs in the observer's own session in *plan mode* (it advises, it does not edit)."""
        handle = await self.worktrees.create(panelist.name)
        prompt = (
            f"=== AgentPanel coopetition — you are observing, not implementing ===\n"
            f"You weren't selected to do this task, but you're on the team and your job now is "
            f"to help {worker} succeed. Below is {worker}'s progress on the request. Give "
            f"concise, specific feedback: encourage what's right, and flag what's wrong, risky, "
            f"or missing. Do NOT edit files — advise only.\n\n"
            f"REQUEST:\n{self.question}\n\nPLAN:\n{self._plan_text(worker)}\n\n"
            f"{worker}'s PROGRESS (diff):\n{(progress or '(no changes yet)')[:4000]}"
        )
        # Observing/coaching is advisory — keep it cheap and fast.
        ctx = RunContext(workdir=handle.path, session_ref=panelist.session_ref,
                         model=panelist.config.model,
                         effort=panelist.config.effort or self.config.settings.plan_effort)
        collected: List[str] = []
        async for ev in panelist.adapter.plan(prompt, ctx):
            if ev.session_ref:
                panelist.session_ref = ev.session_ref
            if ev.type == "token":
                collected.append(ev.text)
            elif ev.type == "done" and ev.full_text:
                collected = [ev.full_text]
        return "".join(collected).strip()

    def _gate_env(self) -> Optional[Dict[str, str]]:
        """Env for the permission-gate MCP server an executing agent will consult.
        None when there's no repo (no metrics/consent scope)."""
        if self.repo is None:
            return None
        from . import config as cfg
        from .metrics import repo_metrics_path
        max_auto = str((self.config.permissions or {}).get("max_auto_risk", "medium"))
        env: Dict[str, str] = {
            "AGENTPANEL_HOME": str(cfg.GLOBAL_DIR),
            "AGENTPANEL_METRICS": str(repo_metrics_path(self.repo)),
            "AGENTPANEL_MAX_AUTO_RISK": max_auto,
        }
        if self._gate_sock:  # live interactive approvals reach the panel through this socket
            env["AGENTPANEL_APPROVE_SOCK"] = self._gate_sock
        if os.environ.get("PATH"):
            env["PATH"] = os.environ["PATH"]  # so the agent can find gh/git/etc.
        return env

    async def _start_broker(self):
        """Stand up the approval broker if an interactive resolver is attached."""
        if self.approval_resolver is None or self.repo is None:
            return None
        from .approval_broker import ApprovalBroker, socket_path_for

        sock = socket_path_for(self.id)
        broker = ApprovalBroker(self._broker_resolve, sock)
        await broker.start()
        self._gate_sock = sock
        return broker

    async def _broker_resolve(self, req: Dict) -> Dict:
        """Surface a gated request to the user (via the resolver) and relay the decision."""
        self.bus.publish(EventKind.PERMISSION_REQUEST,
                         **{k: req.get(k) for k in ("tool", "target", "action", "risk", "reason")})
        try:
            decision = await self.approval_resolver(req)  # type: ignore[misc]
        except Exception:
            decision = {"behavior": "deny", "reason": "resolver error"}
        self.bus.publish(EventKind.PERMISSION_DECISION, tool=req.get("tool"),
                         behavior=decision.get("behavior"), risk=req.get("risk"),
                         remembered=decision.get("remembered", False))
        return decision

    def _plan_text(self, name: str) -> str:
        """The agreed plan (converged), else this agent's own plan."""
        if self.outcome and self.outcome.plan:
            return self.outcome.plan
        rec = next((p.record for p in self.panelists if p.name == name and p.record), None)
        return rec.text if rec else self.question

    def _mediation_prompt(self, name: str) -> str:
        """What the panel relays into the agent's own session.

        AgentPanel does not do the work and does not constrain the agent — it hands over the
        mediated decision and defers entirely to the agent's native capabilities, tools, and
        judgement (which only get better over time). The agent owns the implementation, its
        context, and its commits.
        """
        return (
            f"=== AgentPanel mediation — relayed into your own session ===\n"
            f"The panel deliberated on the request below and selected YOU to implement it. "
            f"This is the panel's decision; the work, the tools, and the commits are yours. "
            f"Use your full capabilities — do it the best way you see fit.\n\n"
            f"REQUEST:\n{self.question}\n\n"
            f"AGREED PLAN (a starting point, not a cage — improve on it as you see fit):\n"
            f"{self._plan_text(name)}\n\n"
            f"Implement it in this worktree and commit your changes when done."
        )

    async def keep(self, agent: str, into: Optional[str] = None) -> str:
        """Merge the chosen agent's branch into the working branch (the winner)."""
        if self.worktrees is None:
            raise RuntimeError("no worktrees to keep from")
        return await self.worktrees.keep(agent, into=into)

    async def cleanup(self, keep_agent: Optional[str] = None) -> None:
        """Tear down worktrees. If ``keep_agent`` is set, merge it first, then clean."""
        if self.worktrees is None:
            return
        if keep_agent:
            await self.worktrees.keep(keep_agent)
        await self.worktrees.cleanup()


class SessionManager:
    """Owns concurrent sessions."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.sessions: Dict[str, Session] = {}
        self._seq = 0

    def create(
        self, question: str, repo: Optional[Path] = None, use_worktrees: bool = True
    ) -> Session:
        self._seq += 1
        sid = _short_id(self._seq)
        session = Session(
            id=sid,
            question=question,
            repo=Path(repo) if repo else None,
            bus=EventBus(),
            config=self.config,
            use_worktrees=use_worktrees,
        )
        self.sessions[sid] = session
        return session

    def start(self, session: Session) -> asyncio.Task:
        """Launch a session as a background task (enables parallel sessions)."""
        task = asyncio.create_task(session.run(), name=f"session-{session.id}")
        session._task = task
        return task

    async def run_to_completion(
        self, question: str, repo: Optional[Path] = None, use_worktrees: bool = True
    ) -> Session:
        """Convenience: create + run one session, awaiting its outcome."""
        session = self.create(question, repo=repo, use_worktrees=use_worktrees)
        await session.run()
        return session

    def get(self, sid: str) -> Optional[Session]:
        return self.sessions.get(sid)

    def list(self) -> List[Session]:
        return list(self.sessions.values())
