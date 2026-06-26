"""Agent setup (Ctrl-A): panel checkboxes add/remove agents and build the roster."""

from __future__ import annotations

import pytest
from textual.app import App
from textual.widgets import Button

from agentpanel.core import config as cfg
from agentpanel.core import ftu
from agentpanel.core.config import AgentConfig, Config, JudgeConfig, Settings
from agentpanel.core.ftu import DetectedAgent
from agentpanel.tui.agent_setup import AgentSetupScreen


class _Acct:
    line = "me@example.com"


def _fake_agents() -> list:
    return [
        DetectedAgent(name="claude", kind="claude_code", label="Claude Code",
                      installed=True, drivable=True, version="9"),
        DetectedAgent(name="cursor", kind="cursor_agent", label="Cursor",
                      installed=True, drivable=True, version="9"),
        DetectedAgent(name="codex", kind="codex", label="OpenAI Codex CLI",
                      installed=True, drivable=False, version="1"),
    ]


@pytest.mark.asyncio
async def test_panel_checkboxes_build_roster(tmp_path, monkeypatch):
    async def fake_detect():
        return _fake_agents()

    async def fake_account(agent, timeout=20.0):
        return _Acct()

    monkeypatch.setattr(ftu, "detect", fake_detect)
    monkeypatch.setattr(ftu, "account_status", fake_account)
    monkeypatch.setattr(ftu, "write_brief", lambda *a, **k: [])

    config = Config(
        roster=[AgentConfig(name="claude", kind="claude_code", enabled=True, verified=True),
                AgentConfig(name="cursor", kind="cursor_agent", enabled=True, verified=True)],
        judge=JudgeConfig(backend="designated_agent", agent="cursor"), settings=Settings())
    cfg_path = tmp_path / "c.toml"
    screen = AgentSetupScreen(config, cfg_path)

    class _Host(App):
        async def on_mount(self) -> None:
            await self.push_screen(screen)

    async with _Host().run_test() as pilot:
        await pilot.pause(0.3)
        assert screen._panel == {"claude", "cursor"}  # seeded from the enabled roster
        codex_btn = next(b for b in screen.query(Button) if b.id == "panel__codex")
        assert codex_btn.disabled  # not drivable → can't join a panel
        cursor_btn = next(b for b in screen.query(Button) if b.id == "panel__cursor")
        await pilot.click(cursor_btn)  # uncheck cursor
        await pilot.pause(0.1)
        assert screen._panel == {"claude"}
        await screen._save()
        await pilot.pause(0.1)

    saved = cfg.load(cfg_path)
    assert {a.name for a in saved.roster} == {"claude"}        # roster = checked drivable agents
    assert saved.judge.agent == "claude"                       # judge repointed off removed cursor
