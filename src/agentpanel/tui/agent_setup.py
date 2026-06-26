"""Agent setup (Ctrl-A) — add agents and manage their accounts in one place.

For each known agent: install it if missing (codex, gemini, …), and — for installed ones —
log in, sign out (force re-auth so you can pick a different account), re-login (sign out +
in), or check status. Interactive commands run under ``app.suspend()`` so each CLI's
browser/device-code flow owns the terminal, then control returns to the panel.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
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


def _resolve_install_cmd(cmd: str) -> str:
    """Prepend sudo to a global npm install when the npm prefix isn't writable (EACCES),
    so the install actually succeeds — the interactive terminal handles the password prompt."""
    if "install -g" not in cmd and "i -g" not in cmd:
        return cmd
    try:
        prefix = subprocess.run(["npm", "config", "get", "prefix"], capture_output=True,
                                text=True, timeout=8).stdout.strip()
        node_modules = os.path.join(prefix, "lib", "node_modules")
        if prefix and os.path.isdir(node_modules) and not os.access(node_modules, os.W_OK):
            return "sudo " + cmd
    except Exception:
        pass
    return cmd


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
        self.query_one("#hint", Static).update("Detecting agents and accounts…")
        # Detection (health checks) and account lookups both spawn each CLI. Run them as a
        # single concurrent wave — account_status only needs the kind, so it doesn't wait
        # on detection — instead of detecting first and then fetching accounts.
        from ..core.adapters import KNOWN_AGENTS

        async def acct(entry) -> tuple:
            probe = DetectedAgent(name=str(entry["name"]), kind=str(entry["kind"]),
                                  label=str(entry["label"]), installed=True,
                                  drivable=bool(entry.get("adapter")))
            return str(entry["name"]), (await ftu.account_status(probe)).line

        gathered = await asyncio.gather(ftu.detect(),
                                        *(acct(e) for e in KNOWN_AGENTS))
        self._agents = gathered[0]
        accounts = {name: line for name, line in gathered[1:]}

        rows = self.query_one("#rows", VerticalScroll)
        await rows.remove_children()
        in_panel = {a.name for a in self.config.roster}
        n_installed = sum(1 for a in self._agents if a.installed)
        self.query_one("#hint", Static).update(
            f"{n_installed} installed · {len(self._agents)} known. 👤 = the account each is "
            "signed in as. Sign out to force re-auth, then log in with the account you want.")
        for a in self._agents:
            await rows.mount(self._row(a, a.name in in_panel, accounts.get(a.name, "")))

    def _row(self, a: DetectedAgent, in_panel: bool, account: str = "") -> Horizontal:
        tag = " · in panel" if in_panel else ""
        if a.installed:
            who = f"   👤 {account}" if account else ""
            state = f"✓ {a.label} {a.version}{who}{tag}".rstrip()
            if not a.auth_cmd:  # key-based agent (e.g. Gemini): no login command
                if a.auth_note:
                    state += f"\n     {a.auth_note}"
                else:
                    var = ftu.KIND_KEYVAR.get(a.kind, "API key")
                    state += f"\n     auth via {var} — `agentpanel account set {a.name} <KEY>`"
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
            # Status reads account/plan/renewal from each CLI or its stored creds — works
            # even for agents (codex, gemini) that have no `status` subcommand.
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
        if action == "status":
            st = await ftu.account_status(agent)
            with self.app.suspend():
                print(f"\n── {agent.label} account ──\n")
                print(st.report(agent.label))
                input("\nPress Enter to return to AgentPanel…")
            await self._rebuild()
            return
        # Run attached to the terminal so npm output / browser logins / prompts are visible.
        cmds = {"install": agent.install_cmd, "login": agent.auth_cmd,
                "logout": agent.auth_logout_cmd, "relogin": agent.auth_logout_cmd}
        cmd = cmds.get(action)
        if not cmd:
            return
        if action == "install":
            cmd = _resolve_install_cmd(cmd)
        with self.app.suspend():
            print(f"\n── {action} {agent.label} ──\n$ {cmd}\n")
            if cmd.startswith("sudo "):
                print("(global install needs admin rights — you'll be asked for your password)\n")
            res = await ftu.run_interactive(cmd)
            if action == "relogin" and res.ok:  # logout done → now log in
                print(f"\n$ {agent.auth_cmd}\n")
                res = await ftu.run_interactive(agent.auth_cmd)
            print(f"\n[{'ok' if res.ok else 'failed: ' + res.output}]")
            input("Press Enter to return to AgentPanel…")
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
