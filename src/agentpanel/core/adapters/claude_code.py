"""Claude Code adapter.

Drives the ``claude`` CLI headlessly:
- plan:    ``claude -p <prompt> --output-format stream-json --permission-mode plan``
- critique:``claude -p <prompt> --output-format stream-json --permission-mode plan``
           (with ``--resume <session_id>`` to keep this agent's own context across turns)
- execute: ``claude -p <prompt> --output-format stream-json --permission-mode acceptEdits``

stream-json is JSONL: ``system`` (init; carries ``session_id``), ``assistant``
(message content blocks: ``text`` deltas and ``tool_use``), and a terminal ``result``
line (``result`` text, ``session_id``, ``is_error``). We normalize those to AdapterEvents.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, List, Optional

from ..adapter import AdapterEvent, CliAdapter, HealthStatus, RunContext


class ClaudeCodeAdapter(CliAdapter):
    kind = "claude_code"
    default_binary = "claude"
    version_flag = ["--version"]

    async def health(self) -> HealthStatus:
        path = self.resolved_binary()
        if not path:
            return HealthStatus(
                name=self.name,
                kind=self.kind,
                installed=False,
                detail="`claude` not found on PATH",
            )
        version = await self._probe_version()
        # Auth: claude uses a logged-in session or ANTHROPIC_API_KEY. We can't verify
        # cheaply offline without spending a call, so leave authed unknown (None) and let
        # the FTU verification handshake confirm it with a real probe.
        return HealthStatus(
            name=self.name,
            kind=self.kind,
            installed=True,
            binary=path,
            version=version,
            authed=None,
            detail="ready (auth verified at handshake)",
        )

    def open_command(self, session_ref: Optional[str], workdir: Path) -> Optional[str]:
        if not session_ref:
            return None
        # Resume the agent's own session interactively from its worktree.
        return f"(cd {workdir} && {self.binary} --resume {session_ref})"

    def _args(self, mode: str, prompt: str, ctx: RunContext) -> List[str]:
        permission = "acceptEdits" if mode == "execute" else "plan"
        args = [
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",  # required for stream-json with -p
            "--permission-mode",
            permission,
        ]
        model = ctx.model or self.model
        if model:
            args += ["--model", model]
        # Bound exploration: planning/critique runs at a lighter effort than Claude Code's
        # default (xhigh), which otherwise explores the whole repo for minutes per pass.
        if ctx.effort:
            args += ["--effort", ctx.effort]
        if ctx.budget_usd:
            args += ["--max-budget-usd", str(ctx.budget_usd)]  # hard cap on a planning pass
        if ctx.session_ref:
            args += ["--resume", ctx.session_ref]
        # Ensure the agent can read the worktree it's running in.
        args += ["--add-dir", str(ctx.workdir)]
        # Route gated tool requests (Bash/gh, writes, fetches) through AgentPanel's
        # permission gate so the agent doesn't stall asking — it's answered by policy.
        if mode == "execute" and ctx.gate_env is not None:
            mcp = {
                "mcpServers": {
                    "agentpanel": {
                        "command": sys.executable,
                        "args": ["-m", "agentpanel.mcp_approver"],
                        "env": ctx.gate_env,
                    }
                }
            }
            args += ["--mcp-config", json.dumps(mcp),
                     "--permission-prompt-tool", "mcp__agentpanel__approve"]
        args += list(self.config.extra_args)
        return args

    def _parse_line(self, obj: Any) -> List[AdapterEvent]:
        if not isinstance(obj, dict):
            return []
        kind = obj.get("type")
        sid = obj.get("session_id")

        if kind == "system":
            # init line — grab the session id for resume on later turns
            return [AdapterEvent.meta(session_ref=sid)] if sid else []

        if kind == "assistant":
            events: List[AdapterEvent] = []
            message = obj.get("message", {})
            for block in message.get("content", []) or []:
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text", "")
                    if text:
                        events.append(AdapterEvent.token(text))
                elif btype == "tool_use":
                    name = block.get("name", "tool")
                    events.append(AdapterEvent.tool_use(name, _short_input(block.get("input"))))
            if sid:
                events.append(AdapterEvent.meta(session_ref=sid))
            return events

        if kind == "result":
            final = obj.get("result", "") or ""
            if obj.get("is_error"):
                return [AdapterEvent.error(final or "claude reported an error")]
            # The result line carries real cost + token usage — capture for metrics.
            usage = obj.get("usage") or {}
            tokens = {
                "input": usage.get("input_tokens"),
                "output": usage.get("output_tokens"),
                "cache_read": usage.get("cache_read_input_tokens"),
            }
            return [AdapterEvent.done(final, sid, cost_usd=obj.get("total_cost_usd"),
                                      tokens=tokens)]

        return []


def _short_input(value: Any) -> str:
    """Compact one-line description of a tool_use input for the activity stream."""
    if isinstance(value, dict):
        for key in ("command", "file_path", "path", "pattern", "query"):
            if key in value:
                return f"{key}={str(value[key])[:80]}"
        return ", ".join(list(value.keys())[:3])
    return str(value)[:80]
