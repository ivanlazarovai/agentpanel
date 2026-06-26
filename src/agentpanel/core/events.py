"""The typed event stream — AgentPanel's UI-agnostic contract.

The engine *publishes* events; clients *subscribe*. The TUI is just the first
subscriber; an IDE/web frontend can subscribe to the same bus later. Everything a
client needs to render a session flows through here, so the engine never imports a UI.

Design notes:
- One ``EventBus`` per session. ``publish`` is sync-callable from the engine; each
  subscriber drains an ``asyncio.Queue`` via ``async for``.
- Events are plain dataclasses with a ``kind`` (see :class:`EventKind`) and a free-form
  ``data`` dict. Keeping the envelope tiny means new event shapes don't churn the bus.
- Late subscribers can request replay of everything published so far (the TUI opening a
  session tab after work started still sees the backlog).
"""

from __future__ import annotations

import asyncio
import enum
import itertools
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List


class EventKind(str, enum.Enum):
    """Every event the engine can emit. String-valued so logs/JSON stay readable."""

    # Session + phase lifecycle
    SESSION_CREATED = "session_created"
    PHASE_CHANGED = "phase_changed"  # data: {phase, turn}
    TURN_STARTED = "turn_started"  # data: {turn}
    RED_TEAM = "red_team"  # data: {turn, critic, target} — round-robin critique assignment
    BARRIER_REACHED = "barrier_reached"  # data: {turn, responded, missing}

    # The agent's OWN native session — so the user can open it and watch the real work.
    # AgentPanel mediates; the agent owns its session/context/tools/diffs/commits.
    AGENT_SESSION = "agent_session"  # data: {agent, session_ref, open_command}
    DECISION = "decision"  # data: {agent, decision: proceed|stand_down|monitor|candidate, reason}
    OBSERVATION = "observation"  # data: {observer, target, round, text}  (coopetition feedback)
    PERMISSION_REQUEST = "permission_request"  # data: {tool, target, action, risk, reason}
    PERMISSION_DECISION = "permission_decision"  # data: {tool, behavior, risk, remembered?}

    # Per-panelist streaming (one agent's live output)
    PANELIST_STARTED = "panelist_started"  # data: {agent, mode, turn}
    PANELIST_TOKEN = "panelist_token"  # data: {agent, text}
    PANELIST_TOOL = "panelist_tool"  # data: {agent, tool, detail}
    PANELIST_DONE = "panelist_done"  # data: {agent, mode, turn, summary}
    PANELIST_ERROR = "panelist_error"  # data: {agent, message}
    PANELIST_TIMEOUT = "panelist_timeout"  # data: {agent, turn}

    # Deliberation results
    JUDGE = "judge"  # data: {turn, backend, duration_ms, cost_usd?}
    CONSENSUS_COMPUTED = "consensus_computed"  # data: {turn, agreement, clusters, dissents}
    CONVERGED = "converged"  # data: {plan, agreement, elected, runners_up}
    ESCALATED = "escalated"  # data: {options: [...], reason}

    # Execution + review
    EXECUTION_STARTED = "execution_started"  # data: {agent, branch}
    EXECUTION_DONE = "execution_done"  # data: {agent, branch, committed}
    DIFF_READY = "diff_ready"  # data: {agent, branch, diffstat}

    # Generic narration for the chat thread
    LOG = "log"  # data: {message, level}


@dataclass
class Event:
    """One thing that happened in a session."""

    kind: EventKind
    data: Dict[str, Any] = field(default_factory=dict)
    seq: int = 0  # monotonic per-bus; assigned at publish time

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return f"#{self.seq} {self.kind.value} {self.data}"


class EventBus:
    """In-memory async pub/sub for a single session.

    ``publish`` is synchronous (callable from anywhere in the engine). Each
    :meth:`subscribe` call returns an independent async iterator backed by its own
    queue, so multiple clients can watch the same session without stealing events.
    """

    def __init__(self, replay_buffer: int = 4096) -> None:
        self._subscribers: List[asyncio.Queue] = []
        self._history: List[Event] = []
        self._replay_buffer = replay_buffer
        self._seq = itertools.count(1)
        self._closed = False
        self._listeners: List = []  # synchronous taps (e.g. metrics) called on every publish

    def add_listener(self, fn) -> None:
        """Register a synchronous ``fn(Event)`` called for every published event. Used by
        the metrics recorder; kept fire-and-forget so a logging error never breaks a run."""
        self._listeners.append(fn)

    def publish(self, kind: EventKind, **data: Any) -> Event:
        """Emit an event to every current subscriber and the replay history."""
        event = Event(kind=kind, data=data, seq=next(self._seq))
        self._history.append(event)
        if len(self._history) > self._replay_buffer:
            self._history = self._history[-self._replay_buffer :]
        for q in list(self._subscribers):
            q.put_nowait(event)
        for fn in self._listeners:
            try:
                fn(event)
            except Exception:  # a metrics/logging failure must never break the run
                pass
        return event

    async def subscribe(self, replay: bool = True) -> AsyncIterator[Event]:
        """Yield events until the bus is closed.

        If ``replay`` is set, the iterator first re-emits buffered history so a late
        subscriber catches up before tailing live events.
        """
        q: asyncio.Queue = asyncio.Queue()
        if replay:
            for event in self._history:
                q.put_nowait(event)
        self._subscribers.append(q)
        try:
            while True:
                event = await q.get()
                if event is _SENTINEL:
                    return
                yield event
        finally:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def history(self) -> List[Event]:
        """Snapshot of buffered events (for non-streaming consumers/tests)."""
        return list(self._history)

    def close(self) -> None:
        """Signal all subscribers to stop iterating."""
        if self._closed:
            return
        self._closed = True
        for q in list(self._subscribers):
            q.put_nowait(_SENTINEL)


# Unique sentinel pushed onto subscriber queues to end iteration on close().
_SENTINEL: Any = object()
