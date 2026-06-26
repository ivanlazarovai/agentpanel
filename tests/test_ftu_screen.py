"""Headless smoke test for the FTU wizard."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentpanel.core import config as cfg
from agentpanel.core.ftu import DetectedAgent
from agentpanel.tui.ftu_screen import FtuApp
from textual.widgets import Button, Input


def _fake_detection() -> list:
    return [
        DetectedAgent(name="claude", kind="claude_code", label="Claude Code",
                      installed=True, drivable=True, binary="/usr/bin/claude", version="9.9"),
        DetectedAgent(name="mockone", kind="mock", label="Mock One",
                      installed=True, drivable=True, binary="(mock)", version="1.0"),
        DetectedAgent(name="codex", kind="codex", label="OpenAI Codex CLI",
                      installed=False, drivable=False, install_cmd="npm i -g @openai/codex"),
    ]


@pytest.mark.asyncio
async def test_ftu_builds_form_and_saves(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    repo = tmp_path / "repo"
    repo.mkdir()
    app = FtuApp(config_path=config_path, detected_override=_fake_detection(), repo=repo)

    async with app.run_test() as pilot:
        await pilot.pause()
        # Set threshold/turns and repo via the inputs.
        app.query_one("#threshold", Input).value = "60"
        app.query_one("#turns", Input).value = "2"
        app.query_one("#repo", Input).value = str(repo)
        await pilot.pause()
        # Press Save & Finish.
        for btn in app.query(Button):
            if btn.id == "save":
                btn.press()
                break
        await pilot.pause()

    # Config was written and reflects the form.
    assert config_path.exists()
    saved = cfg.load(config_path)
    names = {a.name for a in saved.roster}
    assert {"claude", "mockone"} <= names  # drivable+installed agents are configurable
    assert "codex" not in names            # not-yet-drivable agent isn't added to the roster
    assert saved.settings.consensus_threshold == 0.6
    assert saved.settings.max_turns == 2
    # The dev-cycle brief was written into the repo.
    assert (repo / "AGENTS.md").exists()
    assert (repo / "CLAUDE.md").exists()
