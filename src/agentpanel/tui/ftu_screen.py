"""The first-time-user wizard (Textual).

A single scrollable form — detection at the top, then per-agent config, judge choice,
thresholds, repo + brief, and Verify / Save. The heavy lifting is in
:mod:`agentpanel.core.ftu`; this screen just collects choices and calls it.

Run standalone via ``agentpanel setup`` (or automatically on first launch when no config
exists). ``app.run()`` returns the saved :class:`Config`, or ``None`` if cancelled.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Header,
    Input,
    Label,
    RadioButton,
    RadioSet,
    Static,
)

from ..core import config as cfg
from ..core import ftu
from ..core.config import Config, JudgeConfig, Settings
from ..core.ftu import AgentChoice, DetectedAgent

# Default judge model. Opus by default (don't downgrade for cost — the user can pick
# Haiku/Sonnet in this field if they prefer a cheaper neutral judge).
DEFAULT_JUDGE_MODEL = "claude-opus-4-8"


class AgentRow(Static):
    """One agent's controls. Drivable+installed agents get enable+model; installable
    agents get an Install button."""

    def __init__(self, agent: DetectedAgent) -> None:
        super().__init__()
        self.agent = agent
        self.verified = False
        self.enable: Optional[Checkbox] = None
        self.model: Optional[Input] = None
        self.status = Static("", classes="agent-status")

    def compose(self) -> ComposeResult:
        a = self.agent
        if a.drivable and a.installed:
            self.enable = Checkbox(f"{a.label}", value=True, id=f"en-{a.name}")
            self.model = Input(placeholder=f"model (default) — {a.binary or a.name}",
                               id=f"model-{a.name}")
            with Horizontal(classes="agent-line"):
                yield self.enable
                yield self.model
                if a.auth_cmd:
                    yield Button("Log in", id=f"login-{a.name}")
            ver = f"  ✓ installed {a.version}".rstrip()
            self.status.update(ver)
            yield self.status
        elif a.installable:
            with Horizontal(classes="agent-line"):
                yield Label(f"· {a.label} — not installed")
                yield Button("Install", id=f"install-{a.name}", variant="primary")
            self.status.update(f"  $ {a.install_cmd}")
            yield self.status
        else:
            note = "installed (adapter coming soon)" if a.installed else "not available"
            yield Label(f"· {a.label} — {note}")


class FtuApp(App):
    CSS = """
    VerticalScroll { padding: 1 2; }
    .section { color: $accent; text-style: bold; margin: 1 0 0 0; }
    .agent-line { height: 3; }
    .agent-status { color: $text-muted; margin: 0 0 1 3; }
    #buttons { height: 3; align-horizontal: left; }
    #buttons Button { margin: 0 2 0 0; }
    .ok { color: $success; }
    .bad { color: $error; }
    Input { width: 60; }
    #threshold, #turns { width: 16; }
    """
    BINDINGS = [("ctrl+q", "quit", "Cancel")]

    def __init__(self, config_path: Optional[Path] = None,
                 detected_override: Optional[List[DetectedAgent]] = None,
                 repo: Optional[Path] = None) -> None:
        super().__init__()
        self.config_path = config_path or cfg.GLOBAL_CONFIG
        self._detected_override = detected_override
        self.repo = repo or Path.cwd()
        self.rows: Dict[str, AgentRow] = {}
        self.result: Optional[Config] = None

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="form"):
            yield Static("Welcome to AgentPanel — let's set up your panel.", classes="section")
            yield Static("Detecting agents…", id="detect-note")
            yield Static("Agents", classes="section", id="agents-header")
            # agent rows mounted in on_mount after detection
            yield Static("Neutral judge", classes="section")
            with RadioSet(id="judge"):
                yield RadioButton("Neutral model (dedicated, independent of panelists)",
                                  value=True, id="judge-neutral")
                yield RadioButton("Designated chair agent (one panelist judges neutrally)",
                                  id="judge-agent")
            yield Input(value=DEFAULT_JUDGE_MODEL, id="judge-target",
                        placeholder="judge model id (neutral) or agent name (chair)")
            yield Static("Consensus settings", classes="section")
            with Horizontal():
                yield Label("Agree threshold X% ")
                yield Input(value="50", id="threshold")
                yield Label("  Max turns Y ")
                yield Input(value="3", id="turns")
            yield Static("Shared repository", classes="section")
            yield Input(value=str(self.repo), id="repo", placeholder="path to the repo")
            yield Checkbox("Write the dev-cycle brief into the repo (AGENTS.md + CLAUDE.md)",
                           value=True, id="write-brief")
            yield Static("", id="verify-results")
            with Horizontal(id="buttons"):
                yield Button("Cold start (install + log in + verify all)", id="coldstart",
                             variant="primary")
                yield Button("Verify enabled agents", id="verify")
                yield Button("Save & Finish", id="save", variant="success")
        yield Footer()

    async def on_mount(self) -> None:
        self.title = "AgentPanel · Setup"
        agents = self._detected_override if self._detected_override is not None else await ftu.detect()
        note = self.query_one("#detect-note", Static)
        n_inst = sum(1 for a in agents if a.installed)
        note.update(f"Found {n_inst} installed of {len(agents)} known agents.")
        header = self.query_one("#agents-header", Static)
        # Configurable agents (drivable + installed) first, then installable, then the rest.
        order = sorted(agents, key=lambda a: (not (a.drivable and a.installed), not a.installable))
        # mount(after=header) inserts each right after the header, so mount in reverse to
        # preserve `order` top-to-bottom.
        for a in reversed(order):
            row = AgentRow(a)
            self.rows[a.name] = row
            await self.mount(row, after=header)

    # -- actions -----------------------------------------------------------

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid.startswith("install-"):
            await self._install(bid[len("install-"):])
        elif bid.startswith("login-"):
            await self._login(bid[len("login-"):])
        elif bid == "coldstart":
            await self._coldstart()
        elif bid == "verify":
            await self._verify()
        elif bid == "save":
            await self._save()

    async def _login(self, name: str) -> None:
        row = self.rows.get(name)
        if not row or not row.agent.auth_cmd:
            return
        # Suspend the TUI so the agent's interactive/browser login owns the terminal.
        with self.suspend():
            print(f"\nLaunching `{row.agent.auth_cmd}` — complete the login, then return.\n")
            res = await ftu.login(row.agent)
        row.status.update("  ✓ logged in — verify to confirm" if res.ok
                          else f"  login: {res.output[-80:]}")

    async def _coldstart(self) -> None:
        """One click: install + log in + verify every drivable agent, then finish."""
        repo = Path(self.query_one("#repo", Input).value or ".")
        with self.suspend():
            print("\n== AgentPanel cold start ==\n")
            config = await ftu.auto_bootstrap(repo, emit=lambda m: print(f"  {m}"))
            input("\nDone. Press Enter to return to AgentPanel…")
        cfg.save(config, self.config_path)
        self.result = config
        self.exit(config)

    async def _install(self, name: str) -> None:
        row = self.rows.get(name)
        if not row:
            return
        row.status.update(f"  installing… ($ {row.agent.install_cmd})")
        result = await ftu.install(row.agent)
        if result.ok:
            row.status.update("  ✓ installed — re-run setup to configure it")
            row.status.add_class("ok")
        else:
            row.status.update(f"  ✗ install failed: {result.output.strip()[-200:]}")
            row.status.add_class("bad")

    def _enabled_choices(self) -> List[AgentChoice]:
        choices: List[AgentChoice] = []
        for name, row in self.rows.items():
            if row.enable is None:  # not a configurable (drivable+installed) row
                continue
            model = (row.model.value.strip() if row.model else "") or None
            choices.append(AgentChoice(
                name=name, kind=row.agent.kind, enabled=row.enable.value,
                model=model, verified=row.verified,
            ))
        return choices

    async def _verify(self) -> None:
        out = self.query_one("#verify-results", Static)
        repo = Path(self.query_one("#repo", Input).value or ".")
        if self.query_one("#write-brief", Checkbox).value:
            ftu.write_brief(repo)
        lines = ["Verification handshake:"]
        out.update("\n".join(lines + ["  running…"]))
        for choice in self._enabled_choices():
            if not choice.enabled:
                continue
            from ..core.config import AgentConfig
            res = await ftu.verify(
                AgentConfig(name=choice.name, kind=choice.kind, model=choice.model), repo
            )
            self.rows[choice.name].verified = res.ok
            mark = "✓" if res.ok else "✗"
            lines.append(f"  {mark} {choice.name}: {res.detail}")
            out.update("\n".join(lines))

    async def _save(self) -> None:
        choices = self._enabled_choices()
        judge_backend = "neutral_model" if self.query_one("#judge-neutral", RadioButton).value \
            else "designated_agent"
        target = self.query_one("#judge-target", Input).value.strip()
        judge = JudgeConfig(
            backend=judge_backend,
            model=target if judge_backend == "neutral_model" else None,
            agent=target if judge_backend == "designated_agent" else None,
        )
        threshold = _pct(self.query_one("#threshold", Input).value, 0.5)
        turns = _int(self.query_one("#turns", Input).value, 3)
        settings = Settings(consensus_threshold=threshold, max_turns=turns)
        repo = Path(self.query_one("#repo", Input).value or ".")
        if self.query_one("#write-brief", Checkbox).value:
            ftu.write_brief(repo)
        config = ftu.assemble_config(choices, judge, settings, repo)
        cfg.save(config, self.config_path)
        self.result = config
        self.exit(config)


def _pct(value: str, default: float) -> float:
    try:
        v = float(value)
        return max(0.0, min(1.0, v / 100 if v > 1 else v))
    except (ValueError, TypeError):
        return default


def _int(value: str, default: int) -> int:
    try:
        return max(1, int(float(value)))
    except (ValueError, TypeError):
        return default


def run(config_path: Optional[Path] = None, repo: Optional[Path] = None) -> Optional[Config]:
    return FtuApp(config_path=config_path, repo=repo).run()
