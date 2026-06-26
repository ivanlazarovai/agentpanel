"""Headless terminal renderer.

Subscribes to a session's event bus and prints the deliberation as it happens, then the
final verdict. This is the no-UI client used by ``agentpanel ask``; the Textual TUI is a
richer subscriber to the *same* bus (step 9).
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Dict, Optional

from .core.events import Event, EventKind
from .core.session import Session

# ANSI helpers (degrade gracefully — no hard dependency on color).
_DIM = "\033[2m"
_BOLD = "\033[1m"
_RESET = "\033[0m"
_CYAN = "\033[36m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"


def _c(text: str, code: str) -> str:
    return f"{code}{text}{_RESET}"


async def render(session: Session, after: Optional[Callable[[], Awaitable[None]]] = None) -> None:
    """Run the session while streaming a readable transcript to stdout.

    If ``after`` is given, it runs after deliberation while the consumer is still live, so
    execution + coopetition events stream too.
    """
    bus = session.bus
    printer = _Printer()

    async def consume() -> None:
        async for event in bus.subscribe(replay=True):
            printer.handle(event)

    consumer = asyncio.create_task(consume())
    try:
        await session.run()
        if after is not None:
            await after()
    finally:
        # Let the consumer drain remaining events, then stop it.
        await asyncio.sleep(0)
        bus.close()
        await consumer
    printer.summary(session)


class _Printer:
    """Stateful pretty-printer: collapses each agent's token stream onto its own line."""

    def __init__(self) -> None:
        self._active_turn = -1
        self._open_agent: str = ""
        self._line_len: Dict[str, int] = {}

    def handle(self, e: Event) -> None:
        k = e.kind
        d = e.data
        if k == EventKind.SESSION_CREATED:
            print(_c(f"\n● Session {d['id']}: ", _BOLD) + d["question"])
        elif k == EventKind.PHASE_CHANGED:
            label = "isolated planning" if d["phase"] == "isolated_planning" else f"critique · turn {d['turn']}"
            print(_c(f"\n┌─ {label} ", _CYAN))
        elif k == EventKind.PANELIST_STARTED:
            print(_c(f"│  {d['agent']} ", _BOLD) + _c(f"({d['mode']})", _DIM))
        elif k == EventKind.PANELIST_TOOL:
            print(_c(f"│    ⚙ {d['agent']} → {d['tool']} {d.get('detail','')}", _DIM))
        elif k == EventKind.AGENT_SESSION:
            cmd = d.get("open_command")
            if cmd:
                print(_c(f"│    ⮑ open {d['agent']}'s session: ", _DIM) + _c(cmd, _DIM))
        elif k == EventKind.DECISION:
            icon = {"proceed": "▶", "stand_down": "■", "monitor": "👁", "candidate": "?"}.get(
                d["decision"], "·")
            color = _GREEN if d["decision"] == "proceed" else _DIM
            print(_c(f"   {icon} {d['agent']}: {d['decision']} — {d.get('reason','')}", color))
        elif k == EventKind.OBSERVATION:
            head = _c(f"   💬 {d['observer']} → {d['target']} (round {d['round']}): ", _YELLOW)
            print(head + d["text"].strip().splitlines()[0][:120])
        elif k == EventKind.PANELIST_DONE:
            print(_c(f"│    ✓ {d['agent']}: ", _GREEN) + d.get("summary", ""))
        elif k == EventKind.PANELIST_ERROR:
            print(_c(f"│    ✗ {d['agent']}: {d['message']}", _RED))
        elif k == EventKind.PANELIST_TIMEOUT:
            print(_c(f"│    ⏱ {d['agent']} timed out (turn {d['turn']})", _YELLOW))
        elif k == EventKind.BARRIER_REACHED:
            miss = f" · silent: {', '.join(d['missing'])}" if d["missing"] else ""
            print(_c(f"└─ all responded: {', '.join(d['responded']) or '(none)'}{miss}", _CYAN))
        elif k == EventKind.EXECUTION_STARTED:
            print(_c(f"\n▶ {d['agent']} implementing in {d['branch']}", _BOLD))
        elif k == EventKind.DIFF_READY:
            print(_c(f"   ── {d['agent']} diff ──", _GREEN))
            for line in (d.get("diffstat") or "(no changes)").splitlines():
                print(f"     {line}")
        elif k == EventKind.CONSENSUS_COMPUTED:
            pct = int(d["agreement"] * 100)
            thr = int(d["threshold"] * 100)
            verdict = _c("CONVERGED", _GREEN) if d["converged"] else _c("no consensus", _YELLOW)
            clusters = "  ".join(
                f"[{c['label']}×{len(c['members'])}]" for c in d["clusters"] if c["members"]
            )
            print(f"   ⟹ agreement {pct}% (need {thr}%) — {verdict}   {clusters}")

    def summary(self, session: Session) -> None:
        out = session.outcome
        if out is None:
            print(_c("\nNo outcome (session errored).", _RED))
            return
        print()
        if out.status == "converged":
            print(_c("══ CONVERGED ", _GREEN) + _c(f"after {out.turns_used} turn(s)", _DIM))
            print(f"  agreement : {int(out.result.agreement * 100)}%")
            print(f"  elected   : {_c(out.elected or '(none)', _BOLD)} (best positioned to execute)")
            if out.runners_up:
                print(f"  runners-up: {', '.join(out.runners_up)}")
            print(_c("\n  agreed plan:", _BOLD))
            for line in out.plan.strip().splitlines():
                print(f"    {line}")
        else:
            print(_c("══ ESCALATED ", _YELLOW) + _c(f"after {out.turns_used} turn(s)", _DIM))
            print("  The panel could not agree. Your options:\n")
            for i, opt in enumerate(out.options, 1):
                backers = ", ".join(opt["backers"])
                print(_c(f"  [{i}] {opt['label']}", _BOLD) + _c(f"  (backed by: {backers})", _DIM))
                first = (opt["plan"].strip().splitlines() or [""])[0]
                print(f"      {first}")
            print(_c("\n  Pick one or more options to execute, or type your own direction.", _DIM))

        # Native session handles — the user can open these to watch each agent's real work.
        handles = []
        for p in session.panelists:
            cmd = p.adapter.open_command(p.session_ref, p.workdir)
            if cmd:
                handles.append((p.name, cmd))
        if handles:
            print(_c("\n  Open an agent's own session to watch the work:", _BOLD))
            for name, cmd in handles:
                print(f"    {name}: {_c(cmd, _DIM)}")
