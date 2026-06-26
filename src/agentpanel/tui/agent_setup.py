"""Agent setup (Ctrl-A) — add agents and manage their accounts in one place.

For each known agent: install it if missing (codex, gemini, …), and — for installed ones —
log in, sign out (force re-auth so you can pick a different account), re-login (sign out +
in), or check status. Interactive commands run under ``app.suspend()`` so each CLI's
browser/device-code flow owns the terminal, then control returns to the panel.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static

from ..core import config as cfg
from ..core import ftu
from ..core.config import Config
from ..core.ftu import DetectedAgent


class AgentSetupScreen(ModalScreen[Optional[Config]]):
    """Dismisses with the updated Config (if changed) or None."""

    CSS = """
    AgentSetupScreen { align: center middle; }
    #panel { width: 90; height: 80%; border: thick $primary; background: $surface; padding: 1 2; }
    #panel .title { text-style: bold; color: $accent; }
    #rows { height: 1fr; }
    .agent-row { height: auto; padding: 1 0; border-bottom: dashed $panel-darken-2; }
    .agent-row Label { width: 1fr; }
    .agent-row Button { margin: 0 1 0 0; min-width: 10; }
    #foot { height: 3; align-horizontal: right; }
    """
    BINDINGS = [("escape", "close", "Close")]

    def __init__(self, config: Config, config_path) -> None:
        super().__init__()
        self.config = config
        self.config_path = config_path
        self._agents: list[DetectedAgent] = []

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="panel"):
            yield Static("Agent setup — add agents · sign in / out · switch accounts",
                         classes="title")
            yield Static("Detecting…", id="hint")
            yield VerticalScroll(id="rows")
            with Horizontal(id="foot"):
                yield Button("Save to panel", id="save", variant="success")
                yield Button("Close", id="close")

    async def on_mount(self) -> None:
        await self._rebuild()

    async def _rebuild(self) -> None:
        self._agents = await ftu.detect()
        rows = self.query_one("#rows", VerticalScroll)
        await rows.remove_children()
        in_panel = {a.name for a in self.config.roster}
        n_installed = sum(1 for a in self._agents if a.installed)
        self.query_one("#hint", Static).update(
            f"{n_installed} installed · {len(self._agents)} known. "
            "Sign out to force re-auth, then log in with the account you want.")
        for a in self._agents:
            await rows.mount(self._row(a, a.name in in_panel))

    def _row(self, a: DetectedAgent, in_panel: bool) -> Horizontal:
        tag = " · in panel" if in_panel else ""
        if a.installed:
            state = f"✓ {a.label} {a.version}{tag}".rstrip()
        elif a.installable:
            state = f"· {a.label} — not installed"
        else:
            state = f"· {a.label} — unavailable"
        buttons = []
        if a.installed:
            if a.auth_cmd:
                buttons.append(Button("Log in", id=f"login__{a.name}", variant="success"))
            if a.auth_logout_cmd:
                buttons.append(Button("Sign out", id=f"logout__{a.name}", variant="warning"))
            if a.auth_cmd and a.auth_logout_cmd:
                buttons.append(Button("Re-login", id=f"relogin__{a.name}", variant="primary"))
            if a.auth_status_cmd:
                buttons.append(Button("Status", id=f"status__{a.name}"))
        elif a.installable:
            buttons.append(Button("Install", id=f"install__{a.name}", variant="primary"))
        return Horizontal(Label(state), *buttons, classes="agent-row")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "close":
            self.action_close()
            return
        if bid == "save":
            await self._save()
            return
        action, _, name = bid.partition("__")
        agent = next((a for a in self._agents if a.name == name), None)
        if agent is None:
            return
        ops = {"install": ftu.install, "login": ftu.login, "logout": ftu.logout,
               "relogin": ftu.relogin, "status": ftu.status}
        op = ops.get(action)
        if op is None:
            return
        with self.app.suspend():
            print(f"\n── {action} {agent.label} ──  (follow any prompts, then return)\n")
            res = await op(agent)
            if action == "status" or not res.ok:
                input("\nPress Enter to return to AgentPanel…")
        await self._rebuild()

    async def _save(self) -> None:
        # Reconcile the roster: add any newly installed + signed-in agents.
        with self.app.suspend():
            print("\n── verifying agents and updating the panel ──\n")
            updated = await ftu.auto_bootstrap(
                Path.cwd(), existing=self.config, emit=lambda m: print(f"  {m}"))
            input("\nDone. Press Enter to return…")
        cfg.save(updated, self.config_path)
        self.dismiss(updated)

    def action_close(self) -> None:
        self.dismiss(None)
