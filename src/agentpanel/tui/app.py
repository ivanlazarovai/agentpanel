"""AgentPanelApp — the terminal interface.

Top: an input to put a request to the panel. Each request opens a **session tab**
(sessions run in parallel). Inside a tab, a :class:`SessionView` streams the deliberation
with collapsible per-agent cards and per-turn tabs.

The app is just another subscriber to each session's event bus — the same bus the
headless renderer and a future IDE/web frontend use. No engine logic lives here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Input, Static, TabbedContent, TabPane

from ..core import config as cfg
from ..core.config import Config
from ..core.session import Session, SessionManager
from .session_view import SessionView

def _banner() -> str:
    """A title box whose three lines are guaranteed equal width (so the corners always
    line up — hand-drawn ASCII boxes drift when the content line and borders differ)."""
    label = "◆   A G E N T   P A N E L   ◆"
    inner = len(label) + 8
    top = "╭" + "─" * inner + "╮"
    mid = "│" + label.center(inner) + "│"
    bot = "╰" + "─" * inner + "╯"
    return f"{top}\n{mid}\n{bot}"


PROSE = """[bold]Ask once.[/]  Each agent plans it [italic]alone[/], as if it were the only
mind in existence. Then they meet in the open — judging,
conceding, converging on a plan they can defend.

The one best positioned [bold green]builds[/].  The others don't rest:
they [bold]watch and coach[/].  Competition [italic]and[/] cooperation, together.

Each keeps its own mind, its own tools, its own session.
You don't drive them. [bold cyan]You convene them.[/]"""


class Welcome(Vertical):
    """The first screen — a centered block shown until the first session convenes.

    Each child is auto-width and the block is centered as a whole, so multi-line content
    (the banner, the prose) keeps its internal alignment instead of being justified
    line-by-line (which is what skewed the old box)."""

    def compose(self) -> ComposeResult:
        yield Static(_banner(), classes="w-banner")
        yield Static("a council of superagents — mediated, not commanded", classes="w-tag")
        yield Static(PROSE, classes="w-prose", markup=True)
        yield Static("[dim]· checking which agents are available …[/]",
                     id="welcome-status", classes="w-status", markup=True)
        yield Static("[bold cyan]▸[/]  Type a request [bold]below[/] and press "
                     "[bold]Enter[/] to convene the panel.", classes="w-cta", markup=True)


class AgentPanelApp(App):
    CSS = """
    #ask { height: 3; }
    #ask-input { width: 1fr; }
    #sessions { height: 1fr; }
    SessionView { height: 1fr; padding: 1; }
    .consensus-bar { background: $boost; color: $text; padding: 0 1; height: auto; margin: 0 0 1 0; }
    .turn-body { padding: 0 1; height: auto; }
    .open-cmd { color: $text-muted; padding: 0 1; height: auto; }
    .observation { color: $warning; padding: 0 1; height: auto; }
    .diffstat { color: $success; padding: 0 1; height: auto; }
    .escalation { background: $warning-darken-2; color: $text; padding: 1; margin: 1 0; }
    Collapsible { border: round $primary-darken-2; margin: 0 0 1 0; }
    Welcome { height: 1fr; align: center middle; padding: 1 2; }
    Welcome > Static { width: auto; height: auto; }
    .w-banner { color: $accent; text-style: bold; }
    .w-tag { color: $text-muted; margin-bottom: 1; }
    .w-status { margin-top: 1; }
    .w-cta { margin-top: 1; }
    """
    # priority=True so these fire even while the ask Input is focused (Ctrl-A/E/O would
    # otherwise be consumed by the input as cursor/edit keys).
    BINDINGS = [
        Binding("ctrl+n", "focus_ask", "New session"),
        Binding("ctrl+a", "agent_setup", "Agent setup", priority=True),
        Binding("ctrl+e", "execute", "Execute elected", priority=True),
        Binding("ctrl+o", "open_session", "Open agent's session", priority=True),
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]

    def __init__(self, config: Optional[Config] = None, repo: Optional[Path] = None,
                 demo_question: Optional[str] = None) -> None:
        super().__init__()
        self.manager = SessionManager(config or cfg.load())
        self.repo = repo or Path.cwd()
        self._demo_question = demo_question

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Welcome()
        yield TabbedContent(id="sessions")
        with Horizontal(id="ask"):  # in flow below the content → sits just above the footer
            yield Input(placeholder="Ask the panel…  (Enter to convene a new session)", id="ask-input")
        yield Footer()

    async def on_mount(self) -> None:
        self.title = "AgentPanel"
        self.sub_title = f"{len(self.manager.config.panel() or self.manager.config.enabled_agents())} panelists"
        self.query_one("#sessions", TabbedContent).display = False  # welcome until first session
        self.set_interval(1.0, self._tick_progress)  # live elapsed clocks on working agents
        restored = 0
        if self._is_git_repo():
            restored = await self._restore_saved_sessions()  # pick up where we left off
        if self._demo_question:
            await self.start_session(self._demo_question)
        elif restored == 0:
            # Welcome is showing: fill in agent status without blocking the UI, then nudge.
            self.run_worker(self._lazy_load_agents(), exclusive=False)

    async def _lazy_load_agents(self) -> None:
        """Detect agents in the background, show their status in the welcome, and steer a
        first-time user into setup (auto-open) or nudge a thin panel toward Ctrl-A."""
        from ..core import ftu

        try:
            agents = await ftu.detect()
        except Exception:
            return
        config = self.manager.config
        roster = {a.name for a in config.roster}
        ready = [p.name for p in (config.panel() or config.enabled_agents())]
        extra = [a.label for a in agents if a.installed and a.name not in roster]

        line = (f"[green]✓[/] panel ready: [bold]{', '.join(ready)}[/]" if ready
                else "[yellow]●[/] no agents configured yet")
        if extra:
            line += f"   ·   available: {', '.join(extra)}"
        line += "\n[dim]press [bold]^A[/] to set up agents · switch accounts · add more[/]"
        try:
            self.query_one("#welcome-status", Static).update(line)
        except Exception:
            return  # welcome already replaced by a session

        if not cfg.config_exists():  # genuine first run → open setup for them
            self.notify("Welcome — let's set up your panel.", timeout=6)
            self.set_timer(0.6, self.action_agent_setup)
        elif len(ready) < 2:  # has config but too thin to deliberate
            self.notify("Panel is thin — press ^A to add more agents.",
                        severity="warning", timeout=6)

    async def _restore_saved_sessions(self) -> int:
        sessions = self.manager.load_saved(self.repo)
        if not sessions:
            return 0
        self._reveal_panel()
        tabs = self.query_one("#sessions", TabbedContent)
        for session in sessions[:8]:  # most recent first
            agents = [p.name for p in session.panelists]
            if not agents:
                continue
            view = SessionView(agents, id=f"view-{session.id}")
            await tabs.add_pane(
                TabPane(f"↩ {session.id}: {session.question[:16]}", view, id=f"pane-{session.id}")
            )
            await view.populate_restored(session)
        self.notify(f"Restored {len(sessions)} previous session(s) — Ctrl-O to resume an agent.")
        return len(sessions)

    def _tick_progress(self) -> None:
        for view in self.query(SessionView):
            view.refresh_progress()

    def action_focus_ask(self) -> None:
        self.query_one("#ask-input", Input).focus()

    def action_agent_setup(self) -> None:
        """Open agent setup (Ctrl-A): add agents, sign in/out, switch accounts."""
        from .agent_setup import AgentSetupScreen

        def _done(updated) -> None:
            if updated is not None:
                self.manager.config = updated
                ready = updated.panel() or updated.enabled_agents()
                self.sub_title = f"{len(ready)} panelists"
                self.notify(f"Agent setup saved — {len(ready)} agents ready.")

        self.push_screen(AgentSetupScreen(self.manager.config, cfg.GLOBAL_CONFIG), _done)

    def _active_session(self) -> Optional[Session]:
        tabs = self.query_one("#sessions", TabbedContent)
        if not tabs.display or not tabs.active:
            return None
        return self.manager.get(str(tabs.active).removeprefix("pane-"))

    def action_execute(self) -> None:
        """Run the active session's elected agent (Ctrl-E). Gated commands prompt live."""
        session = self._active_session()
        if session is None or session.outcome is None:
            self.notify("Convene and converge a session first.", severity="warning")
            return
        if session.worktrees is None:
            self.notify("Execution needs a git repo (run AgentPanel inside one).",
                        severity="warning")
            return
        self.run_worker(self._execute(session), exclusive=False)

    async def _execute(self, session: Session) -> None:
        out = session.outcome
        if out.status == "converged" and out.elected:
            agents = [out.elected]
        elif out.status == "escalated" and out.options:
            agents = [out.options[0]["representative"]]
        else:
            agents = []
        if not agents:
            self.notify("Nothing to execute.")
            return
        self.notify(f"Executing {', '.join(agents)} — you'll be asked to approve gated commands.")
        try:
            await session.execute(agents, review_rounds=0)
            self.notify(f"{', '.join(agents)} finished — see the diff in its card.")
        except Exception as exc:  # pragma: no cover
            self.notify(f"execution error: {exc}", severity="error")

    async def _approve(self, req: dict) -> dict:
        """Interactive permission resolver: show the modal, relay the user's choice."""
        from .approval_modal import ApprovalModal

        choice = await self.push_screen_wait(ApprovalModal(req))
        if choice == "deny":
            return {"behavior": "deny", "remembered": False}
        remembered = self._remember(req) if choice == "allow_type" else False
        return {"behavior": "allow", "remembered": remembered}

    def _remember(self, req: dict) -> bool:
        """Add a remembered allow-rule for this category (never for critical)."""
        from ..core import config as cfg
        from ..core.permissions import PermissionPolicy, RiskLevel

        config = cfg.load()
        policy = PermissionPolicy.from_dict(config.permissions)
        risk = getattr(RiskLevel, str(req.get("risk", "medium")).upper(), RiskLevel.MEDIUM)
        try:
            policy.remember(str(req.get("action", "run_shell")), "allow", max_risk=risk)
        except ValueError:
            return False  # critical can never be auto-allowed
        config.permissions = policy.to_dict()
        cfg.save(config)
        return True

    def action_open_session(self) -> None:
        """Open the active session's agent in its OWN native CLI session — interactively —
        then return to the panel when you exit it. (Ctrl-O.)"""
        import subprocess

        tabs = self.query_one("#sessions", TabbedContent)
        if not tabs.display or not tabs.active:
            self.notify("Convene a session first.", severity="warning")
            return
        sid = str(tabs.active).removeprefix("pane-")
        try:
            view = self.query_one(f"#view-{sid}", SessionView)
        except Exception:
            return
        target = view.primary_open()
        if not target:
            self.notify("No agent session yet — wait for an agent to start.", severity="warning")
            return
        agent, cmd = target
        # Suspend the TUI so the native session owns the terminal; snap back on exit.
        with self.suspend():
            print(f"\n── {agent}'s live session ──  (exit it — Ctrl-D or /quit — to return to AgentPanel)\n")
            try:
                subprocess.run(cmd, shell=True)
            except Exception as exc:  # pragma: no cover
                print(f"could not open session: {exc}")
                input("press Enter to return…")

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        question = event.value.strip()
        if not question:
            return
        event.input.value = ""
        await self.start_session(question)

    def _reveal_panel(self) -> None:
        """Swap the welcome splash for the live session tabs (once)."""
        try:
            self.query_one(Welcome).display = False
            self.query_one("#sessions", TabbedContent).display = True
        except Exception:
            pass

    def _is_git_repo(self) -> bool:
        p = self.repo.resolve()
        return any((d / ".git").exists() for d in [p, *p.parents])

    async def start_session(self, question: str) -> None:
        """Create + prepare a session, add its tab, then run it in the background."""
        self._reveal_panel()
        # Worktrees (and thus execution) need a git repo; deliberation works without one.
        session = self.manager.create(question, repo=self.repo, use_worktrees=self._is_git_repo())
        session.approval_resolver = self._approve  # live permission channel for execution
        await session.prepare()
        agents = [p.name for p in session.panelists]
        view = SessionView(agents, id=f"view-{session.id}")
        tabs = self.query_one("#sessions", TabbedContent)
        await tabs.add_pane(TabPane(f"{session.id}: {question[:20]}", view, id=f"pane-{session.id}"))
        tabs.active = f"pane-{session.id}"
        self.run_worker(self._drive(session, view), name=f"drive-{session.id}", exclusive=False)

    async def _drive(self, session: Session, view: SessionView) -> None:
        """Run the session and pump its events into the view (same loop, no threads)."""
        import asyncio

        async def consume() -> None:
            async for event in session.bus.subscribe(replay=True):
                await view.apply(event)

        consumer = asyncio.create_task(consume())
        try:
            await session.run()
        finally:
            await asyncio.sleep(0)
            session.bus.close()
            await consumer


def run(config: Optional[Config] = None, repo: Optional[Path] = None,
        demo_question: Optional[str] = None) -> None:
    AgentPanelApp(config=config, repo=repo, demo_question=demo_question).run()
