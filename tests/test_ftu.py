"""Tests for the non-UI FTU operations."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentpanel.core import ftu
from agentpanel.core.config import AgentConfig, JudgeConfig, Settings
from agentpanel.core.ftu import AgentChoice, BRIEF_BEGIN, BRIEF_END


@pytest.mark.asyncio
async def test_detect_returns_catalog_with_health_for_drivable():
    agents = await ftu.detect()
    assert len(agents) >= 4
    by_name = {a.name: a for a in agents}
    # Claude, Cursor and Codex are drivable; they get a real health probe.
    assert by_name["claude"].drivable is True
    assert by_name["claude"].health is not None
    assert by_name["codex"].drivable is True  # driven via `codex exec --json`
    # Antigravity is a desktop GUI agent — no headless adapter, so not drivable.
    assert by_name["antigravity"].drivable is False


def test_write_brief_creates_files_with_markers(tmp_path: Path):
    written = ftu.write_brief(tmp_path)
    names = {p.name for p in written}
    assert names == {"AGENTS.md", "CLAUDE.md"}
    text = (tmp_path / "AGENTS.md").read_text()
    assert BRIEF_BEGIN in text and BRIEF_END in text
    assert "APPROACH:" in text  # the brief's output convention


def test_write_brief_preserves_existing_user_content(tmp_path: Path):
    claude = tmp_path / "CLAUDE.md"
    claude.write_text("# My project rules\nDo the thing.\n")
    ftu.write_brief(tmp_path, filenames=["CLAUDE.md"])
    text = claude.read_text()
    assert "My project rules" in text  # user content kept
    assert BRIEF_BEGIN in text
    # Writing again must not duplicate the managed block.
    ftu.write_brief(tmp_path, filenames=["CLAUDE.md"])
    assert claude.read_text().count(BRIEF_BEGIN) == 1


@pytest.mark.asyncio
async def test_verify_mock_agent_acknowledges(tmp_path: Path):
    cfg = AgentConfig(name="m", kind="mock", extra_args=["plan=A"])
    result = await ftu.verify(cfg, tmp_path, timeout=5)
    assert result.ok
    assert "acknowledged" in result.detail  # mock output contains APPROACH/FIT


@pytest.mark.asyncio
async def test_verify_failed_agent(tmp_path: Path):
    cfg = AgentConfig(name="broken", kind="mock", extra_args=["fail=1"])
    result = await ftu.verify(cfg, tmp_path, timeout=5)
    assert not result.ok


def test_needs_login_detects_auth_failures():
    assert ftu.needs_login("Error: Authentication required. Please run login.")
    assert ftu.needs_login("set OPENAI_API_KEY or run `codex login`")
    assert not ftu.needs_login("SyntaxError: unexpected token")
    assert not ftu.needs_login("")


def test_recommend_judge_prefers_chair_then_deterministic():
    assert ftu.recommend_judge(["claude", "cursor"]).backend == "designated_agent"
    assert ftu.recommend_judge(["claude", "cursor"]).agent == "claude"
    assert ftu.recommend_judge([]).backend == "deterministic"


@pytest.mark.asyncio
async def test_detect_carries_auth_command():
    by_name = {a.name: a for a in await ftu.detect()}
    assert by_name["cursor"].auth_cmd == "cursor-agent login"
    # Antigravity is a desktop agent IDE: signed into via the app, with an auth_note.
    ag = by_name["antigravity"]
    assert ag.kind == "antigravity" and "Antigravity app" in ag.auth_note


@pytest.mark.asyncio
async def test_auto_bootstrap_preserves_existing_and_is_additive(tmp_path):
    # Both drivable agents already verified -> reused untouched, no install/login/verify calls.
    from agentpanel.core.config import AgentConfig, Config, JudgeConfig, Settings

    existing = Config(
        roster=[
            AgentConfig(name="claude", kind="claude_code", enabled=True, verified=True),
            AgentConfig(name="cursor", kind="cursor_agent", enabled=True, verified=True),
        ],
        judge=JudgeConfig(backend="designated_agent", agent="claude"),
        settings=Settings(consensus_threshold=0.7, max_turns=4),
        permissions={"granted_dirs": ["/somewhere"], "rules": [], "default": "ask"},
    )
    config = await ftu.auto_bootstrap(tmp_path, existing=existing,
                                      do_install=False, do_login=False)
    assert {a.name for a in config.panel()} >= {"claude", "cursor"}  # both kept
    assert config.judge.backend == "designated_agent"               # judge preserved
    assert config.settings.consensus_threshold == 0.7               # settings preserved
    granted = config.permissions["granted_dirs"]
    assert "/somewhere" in granted                                  # prior grant kept
    assert str(tmp_path.resolve()) in granted                       # base dir added


def test_assemble_config_roundtrips_choices():
    choices = [
        AgentChoice(name="claude", kind="claude_code", model="claude-opus-4-8", verified=True),
        AgentChoice(name="cursor", kind="cursor_agent", enabled=False),
    ]
    cfg = ftu.assemble_config(
        choices,
        JudgeConfig(backend="neutral_model", model="claude-haiku-4-5-20251001"),
        Settings(consensus_threshold=0.6, max_turns=4),
        repo=Path("/tmp/repo"),
    )
    assert cfg.get("claude").verified is True
    assert cfg.get("cursor").enabled is False
    assert cfg.judge.backend == "neutral_model"
    assert cfg.settings.consensus_threshold == 0.6
    assert cfg.repo == "/tmp/repo"
