"""The permission gate: MCP protocol + risk-graded decisions for gated tool requests."""

from __future__ import annotations

import json

from agentpanel import mcp_approver as mcp
from agentpanel.core.permissions import (
    PermissionPolicy,
    RiskLevel,
    headless_resolve,
    map_tool_request,
)


def test_mcp_initialize_and_tools_list():
    init = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init["result"]["serverInfo"]["name"] == "agentpanel-approver"
    assert init["result"]["protocolVersion"] == mcp.PROTOCOL_VERSION

    listed = mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert listed["result"]["tools"][0]["name"] == "approve"

    # notifications get no response
    assert mcp.handle({"method": "notifications/initialized"}) is None


def test_tool_request_mapping():
    from agentpanel.core.permissions import GIT_PUSH, READ_PATH, RUN_SHELL, WRITE_PATH

    assert map_tool_request("w", "Bash", {"command": "gh issue list"}).action == RUN_SHELL
    assert map_tool_request("w", "Bash", {"command": "git push origin main"}).action == GIT_PUSH
    assert map_tool_request("w", "Write", {"file_path": "/x/a.py"}).action == WRITE_PATH
    assert map_tool_request("w", "Read", {"file_path": "/x/a.py"}).action == READ_PATH


def test_headless_resolve_allows_safe_denies_dangerous(tmp_path):
    pol = PermissionPolicy(granted_dirs=[str(tmp_path)])
    # gh in the worktree → medium → auto-allowed
    b, _ = headless_resolve(pol, map_tool_request("w", "Bash", {"command": "gh issue list"}))
    assert b == "allow"
    # destructive shell → critical → denied
    b, d = headless_resolve(pol, map_tool_request("w", "Bash", {"command": "sudo rm -rf /"}))
    assert b == "deny" and d.risk == RiskLevel.CRITICAL
    # git push → high → above the default medium ceiling → denied
    b, _ = headless_resolve(pol, map_tool_request("w", "Bash", {"command": "git push"}))
    assert b == "deny"


def test_mcp_tools_call_returns_claude_permission_shape(tmp_path, monkeypatch):
    # Point config at a temp home with a policy granting tmp_path.
    monkeypatch.setenv("AGENTPANEL_HOME", str(tmp_path / "home"))
    from agentpanel.core import config as cfg
    from agentpanel.core.config import Config

    cfg.save(Config(permissions={"granted_dirs": [str(tmp_path)], "rules": [], "default": "ask"}))

    resp = mcp.handle({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "approve",
                   "arguments": {"tool_name": "Bash", "input": {"command": "gh issue list"}}},
    })
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["behavior"] == "allow"
    assert payload["updatedInput"] == {"command": "gh issue list"}

    deny = mcp.handle({
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "approve",
                   "arguments": {"tool_name": "Bash", "input": {"command": "rm -rf /"}}},
    })
    assert json.loads(deny["result"]["content"][0]["text"])["behavior"] == "deny"
