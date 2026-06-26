"""Cursor adapter (``cursor-agent`` CLI).

- plan:    ``cursor-agent -p <prompt> --output-format stream-json --plan``
- critique:``... --plan --resume <chatId>`` (keeps this agent's own context across turns)
- execute: ``cursor-agent -p <prompt> --output-format stream-json --force`` (auto-approve)

cursor-agent's stream-json schema differs from Claude's and is not pinned across
versions, so :meth:`_parse_line` is deliberately tolerant: it pulls text out of several
plausible shapes and harvests a chat/session id from whichever key carries it. The exact
schema is validated against real output during the "real planning" build step.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional

from ..adapter import AdapterEvent, CliAdapter, HealthStatus, RunContext

_SESSION_KEYS = ("chat_id", "chatId", "session_id", "sessionId", "thread_id", "threadId")


class CursorAgentAdapter(CliAdapter):
    kind = "cursor_agent"
    default_binary = "cursor-agent"
    version_flag = ["--version"]

    async def health(self) -> HealthStatus:
        path = self.resolved_binary()
        if not path:
            return HealthStatus(
                name=self.name,
                kind=self.kind,
                installed=False,
                detail="`cursor-agent` not found on PATH",
            )
        version = await self._probe_version()
        return HealthStatus(
            name=self.name,
            kind=self.kind,
            installed=True,
            binary=path,
            version=version,
            authed=None,  # confirmed by the FTU handshake (CURSOR_API_KEY or login)
            detail="ready (auth verified at handshake)",
        )

    def open_command(self, session_ref: Optional[str], workdir: Path) -> Optional[str]:
        if not session_ref:
            return None
        return f"(cd {workdir} && {self.binary} --resume {session_ref})"

    def _args(self, mode: str, prompt: str, ctx: RunContext) -> List[str]:
        # `--trust` is essential: without it cursor-agent prints a "Workspace Trust
        # Required" prompt and exits with no output (so it never contributes to the panel).
        args = ["-p", prompt, "--output-format", "stream-json", "--trust"]
        if mode == "execute":
            args += ["--force"]  # auto-approve edits/commands in the worktree
        else:
            args += ["--plan"]  # isolated planning, no edits
        model = ctx.model or self.model
        if model:
            args += ["--model", model]
        if ctx.session_ref:
            args += ["--resume", ctx.session_ref]
        args += ["--workspace", str(ctx.workdir)]
        args += list(self.config.extra_args)
        return args

    def _parse_line(self, obj: Any) -> List[AdapterEvent]:
        if not isinstance(obj, dict):
            return []
        sid = _find_session(obj)
        events: List[AdapterEvent] = []
        kind = obj.get("type")

        # Terminal result line — carries the final text AND token usage.
        if kind in ("result", "final", "done"):
            final = _coerce_text(obj.get("result") or obj.get("text") or obj.get("content"))
            if obj.get("is_error") or obj.get("error"):
                return [AdapterEvent.error(final or "cursor-agent reported an error")]
            usage = obj.get("usage") or {}
            tokens = {"input": usage.get("inputTokens"), "output": usage.get("outputTokens"),
                      "cache_read": usage.get("cacheReadTokens")} if usage else None
            return [AdapterEvent.done(final, sid, tokens=tokens)]

        # Partial text delta (with --stream-partial-output) or a plain text event.
        if kind in ("text", "delta", "assistant_delta"):
            text = _coerce_text(obj.get("text") or obj.get("delta") or obj.get("content"))
            if text:
                events.append(AdapterEvent.token(text))

        # Assistant message with content blocks (Claude-like shape).
        elif kind in ("assistant", "message"):
            message = obj.get("message", obj)
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") in (None, "text"):
                        text = _coerce_text(block.get("text") or block.get("content"))
                        if text:
                            events.append(AdapterEvent.token(text))
                    elif block.get("type") == "tool_use":
                        events.append(AdapterEvent.tool_use(block.get("name", "tool")))
            else:
                text = _coerce_text(content)
                if text:
                    events.append(AdapterEvent.token(text))

        # Tool / command activity. Cursor nests the tool under `tool_call.<name>ToolCall`
        # and emits both "started" and "completed" — surface only the start, named, so the
        # turn body doesn't fill with empty ⚙ bullets.
        elif kind in ("tool_call", "tool_use", "command"):
            if obj.get("subtype") != "completed":
                name, detail = _cursor_tool(obj)
                if name:
                    events.append(AdapterEvent.tool_use(name, detail))

        if sid:
            events.append(AdapterEvent.meta(session_ref=sid))
        return events


def _cursor_tool(obj: dict) -> tuple:
    """Pull a readable (name, detail) out of cursor's nested tool_call shape:
    ``{"tool_call": {"globToolCall": {"args": {...}}}}`` -> ("glob", "<pattern/path>")."""
    name = _coerce_text(obj.get("name") or obj.get("tool"))
    if name:
        return name, ""
    tc = obj.get("tool_call")
    if isinstance(tc, dict):
        for key, val in tc.items():
            if key.endswith("ToolCall") and isinstance(val, dict):
                short = key[: -len("ToolCall")] or key
                args = val.get("args") if isinstance(val.get("args"), dict) else {}
                detail = _coerce_text(args.get("globPattern") or args.get("path")
                                      or args.get("targetFile") or args.get("command") or "")
                return short, detail[:80]
    return "", ""


def _find_session(obj: dict) -> Any:
    for key in _SESSION_KEYS:
        if obj.get(key):
            return obj[key]
    return None


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_coerce_text(v) for v in value)
    if isinstance(value, dict):
        return _coerce_text(value.get("text") or value.get("content"))
    return str(value)
