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
from textual.containers import Horizontal
from textual.widgets import Footer, Header, Input, Static, TabbedContent, TabPane

from ..core import config as cfg
from ..core.config import Config
from ..core.session import Session, SessionManager
from .session_view import SessionView

WELCOME = """
[bold cyan]      ╭───────────────────────────────────────────────╮
      │   ◆   A G E N T   P A N E L   ◆                 │
      ╰───────────────────────────────────────────────╯[/]
[dim]        a council of superagents — mediated, not commanded[/]


  [bold]Ask once.[/]  Each agent plans it [italic]alone[/], as if it were the only
  mind in existence. Then they meet in the open — judging,
  conceding, converging on a plan they can defend.

  The one best positioned [bold green]builds[/].  The others don't rest:
  they [bold]watch and coach[/].  Competition [italic]and[/] cooperation, together.

  Each keeps its own mind, its own tools, its own session.
  You don't drive them. [bold cyan]You convene them.[/]

[dim]  ─────────────────────────────────────────────────────────[/]
  [bold cyan]▸[/]  Type a request above and press [bold]Enter[/] to convene the panel.
"""


class Welcome(Static):
    """The first screen — shown until the first session convenes."""

    def __init__(self) -> None:
        super().__init__(WELCOME, id="welcome", markup=True)


class AgentPanelApp(App):
    CSS = """
    #ask { dock: top; height: 3; }
    #ask-input { width: 1fr; }
    #sessions { height: 1fr; }
    SessionView { height: 1fr; padding: 1; }
    .consensus-bar { background: $boost; color: $text; padding: 0 1; height: auto; margin: 0 0 1 0; }
    .turn-body { padding: 0 1; height: auto; }
    .open-cmd { color: $text-muted; padding: 0 1; height: auto; }
    .observation { color: $warning; padding: 0 1; height: auto; }
    .escalation { background: $warning-darken-2; color: $text; padding: 1; margin: 1 0; }
    Collapsible { border: round $primary-darken-2; margin: 0 0 1 0; }
    #welcome { height: 1fr; content-align: center middle; padding: 1 2; }
    """
    BINDINGS = [
        ("ctrl+n", "focus_ask", "New session"),
        ("ctrl+o", "open_session", "Open agent's session"),
        ("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self, config: Optional[Config] = None, repo: Optional[Path] = None,
                 demo_question: Optional[str] = None) -> None:
        super().__init__()
        self.manager = SessionManager(config or cfg.load())
        self.repo = repo or Path.cwd()
        self._demo_question = demo_question

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="ask"):
            yield Input(placeholder="Ask the panel…  (Enter to start a new session)", id="ask-input")
        yield Welcome()
        yield TabbedContent(id="sessions")
        yield Footer()

    async def on_mount(self) -> None:
        self.title = "AgentPanel"
        self.sub_title = f"{len(self.manager.config.panel() or self.manager.config.enabled_agents())} panelists"
        self.query_one("#sessions", TabbedContent).display = False  # welcome until first session
        if self._demo_question:
            await self.start_session(self._demo_question)

    def action_focus_ask(self) -> None:
        self.query_one("#ask-input", Input).focus()

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
            self.query_one("#welcome", Welcome).display = False
            self.query_one("#sessions", TabbedContent).display = True
        except Exception:
            pass

    async def start_session(self, question: str) -> None:
        """Create + prepare a session, add its tab, then run it in the background."""
        self._reveal_panel()
        session = self.manager.create(question, repo=self.repo, use_worktrees=False)
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
