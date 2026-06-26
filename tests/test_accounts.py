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


def _jwt(payload: dict) -> str:
    import base64
    import json

    def seg(d):
        raw = json.dumps(d).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{seg({'alg': 'RS256'})}.{seg(payload)}.sig"


def test_account_status_parses_each_provider(monkeypatch, tmp_path):
    """account_status pulls account + plan from each provider's distinct surface:
    Claude `auth status` JSON, Cursor `about` text, Codex's stored OAuth token."""
    import asyncio
    import json

    from agentpanel.core import ftu
    from agentpanel.core.ftu import DetectedAgent

    captures = {
        "claude auth status": json.dumps(
            {"loggedIn": True, "email": "me@example.com", "subscriptionType": "max"}),
        "cursor-agent about": (
            "About Cursor CLI\n"
            "Subscription Tier   Pro+\n"
            "User Email          work@example.com\n"),
    }
    monkeypatch.setattr(ftu, "_run_capture",
                        lambda cmd, timeout: _async(captures.get(cmd, "")))

    codex_creds = {
        "auth_mode": "chatgpt",
        "tokens": {
            "id_token": _jwt({
                "email": "codex@example.com",
                "https://api.openai.com/auth": {
                    "chatgpt_plan_type": "plus",
                    "chatgpt_subscription_active_until": "2026-07-04T07:20:01+00:00"},
            }),
            "account_id": "acc-1",
        },
    }
    auth = tmp_path / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(json.dumps(codex_creds))
    monkeypatch.setenv("HOME", str(tmp_path))

    def status(kind):
        a = DetectedAgent(name=kind, kind=kind, label=kind, installed=True, drivable=True)
        return asyncio.get_event_loop().run_until_complete(ftu.account_status(a))

    claude = status("claude_code")
    assert claude.account == "me@example.com" and claude.plan == "Max"
    assert "list" in claude.price and claude.usage  # price hint + usage pointer present

    cursor = status("cursor_agent")
    assert cursor.account == "work@example.com" and cursor.plan == "Pro+"

    codex = status("codex")
    assert codex.account == "codex@example.com" and codex.plan == "Plus"
    assert codex.renews == "renews 2026-07-04"
    assert codex.line == "codex@example.com · Plus"


async def _async(value):
    return value
