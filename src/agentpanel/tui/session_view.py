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

import time
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
        self._active = False  # currently thinking/working this turn
        self._started = 0.0
        self._steps = 0  # tool calls observed this turn (a legibility signal)
        self._spend = {"cost": 0.0, "in": 0, "out": 0}  # accumulated across all passes
        super().__init__(self._turns, title=self._render_title(), collapsed=True)

    def add_spend(self, cost, tokens) -> None:
        """Accumulate this agent's token usage / cost from a finished pass."""
        if cost:
            self._spend["cost"] += float(cost)
        if isinstance(tokens, dict):
            self._spend["in"] += int(tokens.get("input") or 0)
            self._spend["out"] += int(tokens.get("output") or 0)
        self.title = self._render_title()

    def _spend_str(self) -> str:
        s = self._spend
        if not (s["in"] or s["out"] or s["cost"]):
            return ""
        k = lambda n: f"{n/1000:.1f}k" if n >= 1000 else str(n)  # noqa: E731
        money = f" ${s['cost']:.3f}" if s["cost"] else ""
        return f"   ⛁ {k(s['in'])}↑{k(s['out'])}↓{money}"

    @staticmethod
    def _summary_line(status: str, approach: str, fit) -> str:
        fit_s = f"FIT {fit:.2f}" if isinstance(fit, (int, float)) else ""
        return f"{status} {approach}    {fit_s}".rstrip()

    def _render_title(self) -> str:
        if self._active:
            el = int(time.monotonic() - self._started)
            spinner = "◐◓◑◒"[int(el) % 4]
            return (f"{self._agent}   {spinner} working · {self._steps} steps · {el}s"
                    + self._spend_str())
        tag = {"proceed": "  ▶ PROCEED", "stand_down": "  ■ stand down",
               "monitor": "  👁 observing", "candidate": "  ? candidate",
               "benched": "  ⛔ benched"}.get(self._decision, "")
        return f"{self._agent}   " + self._summary_line(*self._last) + self._spend_str() + tag

    # -- live progress -----------------------------------------------------

    def begin(self) -> None:
        self._active = True
        self._started = time.monotonic()
        self._steps = 0
        self.title = self._render_title()

    def bump(self) -> None:
        self._steps += 1
        if self._active:
            self.title = self._render_title()

    def refresh_elapsed(self) -> None:
        if self._active:
            self.title = self._render_title()

    def set_summary(self, status: str, approach: str, fit) -> None:
        self._active = False
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

    async def populate_restored(self, session) -> None:
        """Render a restored session: prior plans, the outcome, and resumable agent sessions."""
        o = getattr(session, "outcome", None)
        if o is not None:
            self._elected = o.elected or ""
            self._set_bar(f"↩ restored · {o.status} · elected {o.elected or '—'}")
        else:
            self._set_bar("↩ restored session")
        for p in session.panelists:
            card = self._cards.get(p.name)
            if card is None:
                continue
            if p.record and p.record.text.strip():
                card.set_summary("✓", extract_label(p.record.text) or "saved plan", p.record.fit)
            cmd = p.adapter.open_command(p.session_ref, p.workdir)
            if cmd:
                self._open[p.name] = cmd
                await card.mount(Static(f"⮑ Ctrl-O to resume {p.name}'s session  ({cmd})",
                                        classes="open-cmd"))
        if self._elected in self._cards:
            self._cards[self._elected].collapsed = False

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
        yield Static("", id="spend-bar", classes="open-cmd")
        for agent in self._agents:
            card = AgentCard(agent)
            self._cards[agent] = card
            yield card

    def _add_spend(self, agent: str, d) -> None:
        """Accumulate an agent's tokens/cost into its card and the session total bar."""
        card = self._cards.get(agent)
        if card is None:
            return
        card.add_spend(d.get("cost_usd"), d.get("tokens"))
        tot = {"cost": 0.0, "in": 0, "out": 0}
        for c in self._cards.values():
            tot["cost"] += c._spend["cost"]
            tot["in"] += c._spend["in"]
            tot["out"] += c._spend["out"]
        if tot["in"] or tot["out"] or tot["cost"]:
            k = lambda n: f"{n/1000:.1f}k" if n >= 1000 else str(n)  # noqa: E731
            money = f" · ${tot['cost']:.3f}" if tot["cost"] else ""
            try:
                self.query_one("#spend-bar", Static).update(
                    f"Σ session: {k(tot['in'])} in ↑ {k(tot['out'])} out ↓{money}")
            except Exception:
                pass

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
        if d["agent"] in self._cards:
            self._cards[d["agent"]].begin()  # start the live "working · N steps · Ns" clock

    def refresh_progress(self) -> None:
        """Tick the elapsed clock on any agents currently working (called on a timer)."""
        for card in self._cards.values():
            card.refresh_elapsed()

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
        if agent in self._cards:
            self._cards[agent].bump()  # live step counter
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
        suffix = f"  ·{d['duration_ms'] // 1000}s" if d.get("duration_ms") else ""
        self._add_spend(agent, d)  # accumulate this pass's tokens/cost (card + session bar)
        # fit comes from the engine (parsed from the full result, not the streamed tokens).
        self._cards[agent].set_summary("✓", approach + suffix, d.get("fit"))

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

    async def _on_execution_done(self, d) -> None:
        self._add_spend(d["agent"], d)  # execution tokens/cost count toward the agent + total

    async def _on_diff_ready(self, d) -> None:
        card = self._cards.get(d["agent"])
        if card is not None:
            stat = d.get("diffstat") or "(no changes)"
            await card.mount(Static(f"📦 executed → {d['branch']}\n{stat}", classes="diffstat"))

    async def _on_permission_decision(self, d) -> None:
        icon = "✓" if d.get("behavior") == "allow" else "✗"
        note = " (remembered)" if d.get("remembered") else ""
        self._set_bar(f"{self.bar_text}   ·   {icon} {d.get('tool')} {d.get('behavior')}{note}")

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
