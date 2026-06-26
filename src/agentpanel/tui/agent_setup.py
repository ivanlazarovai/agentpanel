"""Agent setup (Ctrl-A) — add agents and manage their accounts in one place.

For each known agent: install it if missing (codex, antigravity, …), and — for installed ones —
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
        self._accounts: dict = {}
        # Which agents are in the panel (membership the checkboxes edit). Seed from config;
        # newly-logged-in drivable agents get checked by default (see _rebuild).
        self._panel: set = {a.name for a in config.roster if a.enabled}
        self._seen: set = {a.name for a in config.roster}

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
        self._accounts = {name: line for name, line in gathered[1:]}
        # Default newly-connected drivable agents into the panel — checked on login.
        for a in self._agents:
            if a.name in self._seen:
                continue
            self._seen.add(a.name)
            if a.drivable and a.installed and self._accounts.get(a.name):
                self._panel.add(a.name)
        await self._render_rows()

    async def _render_rows(self) -> None:
        """(Re)build the rows from cached detection — no re-probe, so checkbox toggles are
        instant."""
        rows = self.query_one("#rows", VerticalScroll)
        await rows.remove_children()
        n_installed = sum(1 for a in self._agents if a.installed)
        self.query_one("#hint", Static).update(
            f"{n_installed} installed · [b]{len(self._panel)} in panel[/b]. ☑ = deliberates · "
            "👤 = signed-in account. Only agents with a headless adapter can join a panel.")
        for a in self._agents:
            await rows.mount(self._row(a, self._accounts.get(a.name, "")))

    def _row(self, a: DetectedAgent, account: str = "") -> Horizontal:
        selected = a.name in self._panel
        tag = " · in panel" if selected else ""
        if a.installed:
            who = f"   👤 {account}" if account else ""
            state = f"✓ {a.label} {a.version}{who}{tag}".rstrip()
            if not a.drivable:  # connected but no adapter → can't deliberate (answers "why not")
                state += "\n     no headless adapter yet — connected, but can't join a panel"
            if a.auth_note:  # e.g. Antigravity: sign in inside the app
                state += f"\n     {a.auth_note}"
            elif not a.auth_cmd:  # key-based agent: no login command
                var = ftu.KIND_KEYVAR.get(a.kind, "API key")
                state += f"\n     auth via {var} — `agentpanel account set {a.name} <KEY>`"
        elif a.installable:
            state = f"· {a.label} — not installed"
        else:
            state = f"· {a.label} — unavailable"
        buttons = []
        if a.installed:
            if a.drivable:  # panel checkbox — only agents we can actually drive
                buttons.append(Button("☑ In panel" if selected else "☐ Add",
                                      id=f"panel__{a.name}",
                                      variant="success" if selected else "default"))
            else:
                buttons.append(Button("⊘ no adapter", id=f"panel__{a.name}", disabled=True))
            if a.auth_cmd:
                label = "Open app" if a.auth_gui else "Log in"
                buttons.append(Button(label, id=f"login__{a.name}", variant="success"))
            if a.auth_logout_cmd:
                buttons.append(Button("Sign out", id=f"logout__{a.name}", variant="warning"))
            if a.auth_cmd and a.auth_logout_cmd:
                buttons.append(Button("Re-login", id=f"relogin__{a.name}", variant="primary"))
            # Status reads account/plan/renewal from each CLI or its stored creds — works
            # even for agents (codex, antigravity) that have no `status` subcommand.
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
        if action == "panel":  # toggle panel membership — instant, no re-detect
            if not agent.drivable:
                self.notify(f"{agent.label} has no headless adapter yet — it can't join a "
                            "panel.", severity="warning", timeout=5)
                return
            self._panel.discard(name) if name in self._panel else self._panel.add(name)
            await self._render_rows()
            return
        if action == "status":
            st = await ftu.account_status(agent)
            with self.app.suspend():
                print(f"\n── {agent.label} account ──\n")
                print(st.report(agent.label))
                input("\nPress Enter to return to AgentPanel…")
            await self._rebuild()
            return
        if action == "login" and agent.auth_gui:
            # Desktop app: launch it in its own window (don't suspend the TUI for a
            # terminal flow that the GUI would just hide), and acknowledge clearly.
            try:
                subprocess.Popen(agent.auth_cmd, shell=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.notify(f"Opened {agent.label} — sign in there, then reopen this "
                            "panel to refresh its account.", timeout=6)
            except Exception as exc:  # pragma: no cover - launch failure
                self.notify(f"couldn't open {agent.label}: {exc}", severity="error")
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
        # Build the roster straight from the checkboxes (the panel = checked drivable
        # agents). No blocking verify handshake — readiness is "enabled", so this is instant.
        from ..core.config import AgentConfig

        existing = {a.name: a for a in self.config.roster}
        roster = []
        for a in self._agents:
            if a.name in self._panel and a.drivable and a.installed:
                ac = existing.get(a.name) or AgentConfig(name=a.name, kind=a.kind)
                ac.enabled = True
                roster.append(ac)
        self.config.roster = roster
        # Keep the judge pointing at a panel member.
        names = {a.name for a in roster}
        if self.config.judge.backend == "designated_agent" and roster \
                and self.config.judge.agent not in names:
            self.config.judge.agent = roster[0].name
        ftu.write_brief(Path.cwd())  # ensure the dev-cycle brief is present for the agents
        cfg.save(self.config, self.config_path)
        self.notify(f"Panel saved — {len(roster)} agent(s) in the panel.")
        self.dismiss(self.config)

    def action_close(self) -> None:
        self.dismiss(None)
