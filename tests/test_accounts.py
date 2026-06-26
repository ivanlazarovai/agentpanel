"""Per-agent accounts: credentials switchable + runnable side-by-side."""

from __future__ import annotations

from agentpanel.cli import _parse_creds
from agentpanel.core.adapters.cursor_agent import CursorAgentAdapter
from agentpanel.core.config import AgentConfig, Config, load, save


def test_env_roundtrips_through_config(tmp_path):
    c = Config(roster=[AgentConfig(name="cursor", kind="cursor_agent", account="work",
                                   env={"CURSOR_API_KEY": "key_a"})])
    p = tmp_path / "c.toml"
    save(c, p)
    got = load(p).get("cursor")
    assert got.account == "work"
    assert got.env == {"CURSOR_API_KEY": "key_a"}


def test_subprocess_env_applies_and_resolves_references(monkeypatch):
    monkeypatch.setenv("MY_WORK_KEY", "secret-123")
    a = CursorAgentAdapter(AgentConfig(name="c", kind="cursor_agent",
                                       env={"CURSOR_API_KEY": "key_a", "X": "env:MY_WORK_KEY"}))
    env = a.subprocess_env()
    assert env["CURSOR_API_KEY"] == "key_a"
    assert env["X"] == "secret-123"  # "env:VAR" resolved from the ambient environment
    # no per-agent env -> inherit unchanged (None)
    assert CursorAgentAdapter(AgentConfig(name="d", kind="cursor_agent")).subprocess_env() is None


def test_parse_creds_maps_bare_key_to_kind_default():
    assert _parse_creds(["sk-abc"], "cursor_agent") == {"CURSOR_API_KEY": "sk-abc"}
    assert _parse_creds(["sk-abc"], "claude_code") == {"ANTHROPIC_API_KEY": "sk-abc"}
    assert _parse_creds(["FOO=bar", "sk-x"], "codex") == {"FOO": "bar", "OPENAI_API_KEY": "sk-x"}
