"""SessionView — renders one panel session with progressive disclosure.

Layout:
    ┌ consensus bar: phase · turn · agreement% · status · elected ┐
    │ ▸ AgentCard (collapsed by default — one-line summary)        │
    │ ▾ AgentCard (expanded — TabbedContent: one tab per turn)     │
    │ … escalation options appear here when the panel can't agree  │

One agent's output is rich and N agents is N×, so panelist cards collapse to a single
summary line (agent · approach · FIT · status) and expand to the full plan. Inside an
expanded card, each turn is its own tab — so you can step through how an agent's thinking
evolved across the deliberation.
"""

from __future__ import annotations

from typing import Dict, Tuple

from textual.containers import VerticalScroll
from textual.widgets import Collapsible, Static, TabbedContent, TabPane

from ..core.consensus import extract_label
from ..core.events import Event


class AgentCard(Collapsible):
    """A collapsible card for one panelist. Body holds per-turn tabs."""

    def __init__(self, agent: str) -> None:
        self._agent = agent
        self._turns = TabbedContent(id=f"turns-{agent}")
        self._last = ("·", "—", None)  # status, approach, fit
        self._decision = ""  # proceed | stand_down | candidate
        super().__init__(self._turns, title=self._render_title(), collapsed=True)

    @staticmethod
    def _summary_line(status: str, approach: str, fit) -> str:
        fit_s = f"FIT {fit:.2f}" if isinstance(fit, (int, float)) else ""
        return f"{status} {approach}    {fit_s}".rstrip()

    def _render_title(self) -> str:
        tag = {"proceed": "  ▶ PROCEED", "stand_down": "  ■ stand down",
               "monitor": "  👁 observing", "candidate": "  ? candidate"}.get(self._decision, "")
        return f"{self._agent}   " + self._summary_line(*self._last) + tag

    def set_summary(self, status: str, approach: str, fit) -> None:
        self._last = (status, approach, fit)
        self.title = self._render_title()

    def set_decision(self, decision: str) -> None:
        self._decision = decision
        self.title = self._render_title()


class SessionView(VerticalScroll):
    """Reactive view of one Session, driven by its event bus."""

    def __init__(self, agents, **kwargs) -> None:
        super().__init__(**kwargs)
        self._agents = list(agents)
        self._cards: Dict[str, AgentCard] = {}
        self._turn_text: Dict[Tuple[str, int], str] = {}
        self._turn_static: Dict[Tuple[str, int], Static] = {}
        self.bar_text: str = ""  # last consensus-bar text (also handy for tests)
        self._open: Dict[str, str] = {}  # agent -> command to open its native session
        self._elected: str = ""

    def primary_open(self):
        """The most relevant agent session to open: the elected one, else any available.
        Returns (agent, command) or None."""
        if self._elected in self._open:
            return self._elected, self._open[self._elected]
        for agent, cmd in self._open.items():
            return agent, cmd
        return None

    def compose(self):
        yield Static("● initializing…", id="consensus-bar", classes="consensus-bar")
        for agent in self._agents:
            card = AgentCard(agent)
            self._cards[agent] = card
            yield card

    # -- event application -------------------------------------------------

    async def apply(self, e: Event) -> None:
        handler = getattr(self, f"_on_{e.kind.value}", None)
        if handler is not None:
            await handler(e.data)

    def _set_bar(self, text: str) -> None:
        self.bar_text = text
        self.query_one("#consensus-bar", Static).update(text)

    async def _ensure_turn(self, agent: str, turn: int) -> Static:
        key = (agent, turn)
        if key in self._turn_static:
            return self._turn_static[key]
        static = Static("", classes="turn-body")
        self._turn_static[key] = static
        self._turn_text[key] = ""
        tabs = self._cards[agent]._turns
        await tabs.add_pane(TabPane(f"turn {turn}", static, id=f"t-{agent}-{turn}"))
        tabs.active = f"t-{agent}-{turn}"
        return static

    async def _on_session_created(self, d) -> None:
        self._set_bar(f"● {d['id']}: {d['question']}")

    async def _on_phase_changed(self, d) -> None:
        label = "isolated planning" if d["phase"] == "isolated_planning" else f"critique · turn {d['turn']}"
        self._set_bar(f"◷ {label} …")

    async def _on_panelist_started(self, d) -> None:
        await self._ensure_turn(d["agent"], d["turn"])
        self._cards[d["agent"]].set_summary("◷", "thinking…", None)

    async def _on_panelist_token(self, d) -> None:
        # We don't know the active turn from the token event; append to the agent's latest.
        agent = d["agent"]
        turn = max((t for (a, t) in self._turn_static if a == agent), default=0)
        key = (agent, turn)
        if key not in self._turn_static:
            await self._ensure_turn(agent, turn)
        self._turn_text[key] += d["text"]
        self._turn_static[key].update(self._turn_text[key])

    async def _on_panelist_tool(self, d) -> None:
        agent = d["agent"]
        turn = max((t for (a, t) in self._turn_static if a == agent), default=0)
        key = (agent, turn)
        if key in self._turn_static:
            self._turn_text[key] += f"\n  ⚙ {d['tool']} {d.get('detail','')}\n"
            self._turn_static[key].update(self._turn_text[key])

    async def _on_panelist_done(self, d) -> None:
        agent = d["agent"]
        turn = max((t for (a, t) in self._turn_static if a == agent), default=0)
        text = self._turn_text.get((agent, turn), "")
        approach = extract_label(text) or (d.get("summary") or "done")
        # fit comes from the engine (parsed from the full result, not the streamed tokens).
        self._cards[agent].set_summary("✓", approach, d.get("fit"))

    async def _on_agent_session(self, d) -> None:
        # Remember the agent's own native session command (Ctrl-O opens it interactively).
        cmd = d.get("open_command")
        if not cmd:
            return
        self._open[d["agent"]] = cmd
        card = self._cards.get(d["agent"])
        if card is not None and not getattr(card, "_session_shown", False):
            await card.mount(Static(f"⮑ Ctrl-O to open {d['agent']}'s live session  ({cmd})",
                                    classes="open-cmd"))
            card._session_shown = True

    async def _on_decision(self, d) -> None:
        card = self._cards.get(d["agent"])
        if card is not None:
            card.set_decision(d["decision"])

    async def _on_observation(self, d) -> None:
        # Coopetition feedback from an observing teammate, shown under the worker's card.
        card = self._cards.get(d["target"])
        if card is not None:
            first = (d["text"].strip().splitlines() or [""])[0][:160]
            await card.mount(Static(f"💬 {d['observer']} (r{d['round']}): {first}",
                                    classes="observation"))

    async def _on_panelist_error(self, d) -> None:
        self._cards[d["agent"]].set_summary("✗", d["message"][:40], None)

    async def _on_panelist_timeout(self, d) -> None:
        self._cards[d["agent"]].set_summary("⏱", "timed out", None)

    async def _on_consensus_computed(self, d) -> None:
        pct, thr = int(d["agreement"] * 100), int(d["threshold"] * 100)
        verdict = "✓ CONVERGED" if d["converged"] else "… deliberating"
        clusters = "  ".join(
            f"[{c['label']}×{len(c['members'])}]" for c in d["clusters"] if c["members"]
        )
        elected = f" · elected {d['elected']}" if d.get("elected") and d["converged"] else ""
        self._set_bar(
            f"turn {d['turn']} · agreement {pct}% (need {thr}%) · {verdict}{elected}   {clusters}"
        )

    async def _on_converged(self, d) -> None:
        # Expand the elected agent's card so the winning plan is front-and-center.
        elected = d.get("elected")
        self._elected = elected or ""
        if elected in self._cards:
            self._cards[elected].collapsed = False

    async def _on_escalated(self, d) -> None:
        lines = ["⚠ ESCALATED — the panel could not agree. Your options:"]
        for i, opt in enumerate(d["options"], 1):
            lines.append(f"  [{i}] {opt['label']}  (backed by: {', '.join(opt['backers'])})")
        await self.mount(Static("\n".join(lines), classes="escalation"))
