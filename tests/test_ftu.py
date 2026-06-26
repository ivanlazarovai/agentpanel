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
    # Claude + Cursor are the drivable ones; they get a real health probe.
    assert by_name["claude"].drivable is True
    assert by_name["claude"].health is not None
    # Codex is catalogued as installable-but-not-drivable-yet.
    assert by_name["codex"].drivable is False
    assert by_name["codex"].install_cmd  # has an install hint


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
