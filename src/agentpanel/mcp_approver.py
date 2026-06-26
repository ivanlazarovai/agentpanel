"""AgentPanel permission gate — a tiny MCP stdio server Claude calls to ask permission.

When an elected agent runs a tool that needs approval (a Bash command like ``gh``, a write,
a fetch), Claude Code — invoked with ``--permission-prompt-tool mcp__agentpanel__approve`` —
calls the ``approve`` tool here instead of stalling. We grade the request with AgentPanel's
risk-graded policy (``core.permissions``) and answer allow/deny, so the agent keeps going
within the rules the user set. Every decision is appended to the metrics log.

This is intentionally dependency-free and self-contained: it loads the saved policy from the
config and decides locally (no live IPC). The *interactive* "user clicks allow/deny in the
panel" channel is a follow-up; here the policy answers (auto-allowing up to a configurable
risk ceiling in the isolated, reviewed worktree; denying above and always denying critical).

Run as: ``python -m agentpanel.mcp_approver`` (Claude spawns it via ``--mcp-config``).
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict

PROTOCOL_VERSION = "2024-11-05"

APPROVE_TOOL = {
    "name": "approve",
    "description": "AgentPanel permission gate. Given a tool request, returns whether it "
                   "is allowed under the user's risk-graded policy.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "tool_name": {"type": "string"},
            "input": {"type": "object"},
        },
    },
}


def _decide(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Grade the requested tool call and return Claude's permission-tool result shape."""
    from .core import config as cfg
    from .core.permissions import (
        PermissionPolicy,
        RiskLevel,
        headless_resolve,
        map_tool_request,
    )

    tool_name = arguments.get("tool_name") or arguments.get("toolName") or ""
    tool_input = arguments.get("input") or arguments.get("tool_input") or {}
    req = map_tool_request("worker", tool_name, tool_input)

    policy = PermissionPolicy.from_dict(cfg.load().permissions)
    decision = policy.decide(req)
    if decision.outcome == "allow":
        behavior = "allow"
    elif decision.outcome == "deny":
        behavior = "deny"
    else:
        # 'ask' — give the live panel first refusal (the user decides), else fall back to
        # the headless policy ceiling.
        live = _ask_broker({
            "tool": tool_name, "target": req.target, "action": req.action,
            "risk": decision.risk.label, "reason": decision.reason,
        })
        if live and live.get("behavior") in ("allow", "deny"):
            behavior = live["behavior"]
        else:
            ceiling = _risk_from_env(RiskLevel)
            behavior, _ = headless_resolve(policy, req, max_auto_risk=ceiling)
    _log(tool_name, req.target, decision, behavior)

    if behavior == "allow":
        return {"behavior": "allow", "updatedInput": tool_input}
    return {
        "behavior": "deny",
        "message": f"AgentPanel denied {tool_name} ({decision.risk.label} risk): "
                   f"{decision.reason}. Adjust the policy or run it yourself.",
    }


def _ask_broker(payload: Dict[str, Any]) -> Dict[str, Any] | None:
    """Forward a request to the live panel's approval broker; None if unreachable."""
    sock = os.environ.get("AGENTPANEL_APPROVE_SOCK")
    if not sock:
        return None
    import socket

    try:
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(600)
        c.connect(sock)
        c.sendall((json.dumps(payload) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = c.recv(4096)
            if not chunk:
                break
            buf += chunk
        c.close()
        return json.loads(buf.decode()) if buf.strip() else None
    except Exception:
        return None


def _risk_from_env(RiskLevel) -> Any:
    name = (os.environ.get("AGENTPANEL_MAX_AUTO_RISK") or "medium").upper()
    return getattr(RiskLevel, name, RiskLevel.MEDIUM)


def _log(tool_name: str, target: str, decision, behavior: str) -> None:
    path = os.environ.get("AGENTPANEL_METRICS")
    if not path:
        return
    try:
        from datetime import datetime, timezone

        rec = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": "permission",
            "tool": tool_name,
            "target": target[:200],
            "risk": decision.risk.label,
            "outcome": behavior,
            "reason": decision.reason[:200],
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
    except Exception:
        pass


def handle(msg: Dict[str, Any]) -> Dict[str, Any] | None:
    """Map one JSON-RPC request to a response (or None for notifications)."""
    method = msg.get("method")
    mid = msg.get("id")
    if method == "initialize":
        return _ok(mid, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "agentpanel-approver", "version": "0.1.0"},
        })
    if method == "tools/list":
        return _ok(mid, {"tools": [APPROVE_TOOL]})
    if method == "tools/call":
        params = msg.get("params") or {}
        if params.get("name") == "approve":
            result = _decide(params.get("arguments") or {})
            return _ok(mid, {"content": [{"type": "text", "text": json.dumps(result)}]})
        return _err(mid, -32602, f"unknown tool: {params.get('name')}")
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None  # notifications get no response
    if mid is not None:
        return _err(mid, -32601, f"unknown method: {method}")
    return None


def _ok(mid, result) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(mid, code, message) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = handle(msg)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":  # pragma: no cover
    main()
