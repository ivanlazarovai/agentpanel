"""Codex adapter (OpenAI ``codex`` CLI).

Drives ``codex exec`` headlessly (non-interactive) with JSONL event output:
- plan/critique: ``codex exec --json --sandbox read-only --ask-for-approval never <prompt>``
                 (read-only: produce a plan without touching the worktree; ``resume`` keeps
                 this agent's own context across critique turns)
- execute:       ``codex exec --json --full-auto <prompt>`` (workspace-write, auto-approve)

Codex's ``--json`` stream is JSONL but its schema is not pinned across versions (it has
shipped both a newer ``thread``/``turn``/``item`` envelope and an older ``msg`` envelope),
so :meth:`_parse_line` is deliberately tolerant — like ``cursor_agent.py`` it pulls text
out of several plausible shapes and harvests a thread/session id from whichever key carries
it. The exact schema is validated against real output during the "real planning" build step.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional

from ..adapter import AdapterEvent, CliAdapter, HealthStatus, RunContext

_SESSION_KEYS = (
    "thread_id",
    "threadId",
    "session_id",
    "sessionId",
    "conversation_id",
    "conversationId",
    "rollout_id",
)


class CodexAdapter(CliAdapter):
    kind = "codex"
    default_binary = "codex"
    version_flag = ["--version"]

    async def health(self) -> HealthStatus:
        path = self.resolved_binary()
        if not path:
            return HealthStatus(
                name=self.name,
                kind=self.kind,
                installed=False,
                detail="`codex` not found on PATH",
            )
        version = await self._probe_version()
        # Auth: codex uses `codex login` (ChatGPT session) or OPENAI_API_KEY. We can't
        # verify that cheaply offline, so leave authed unknown (None) and let the FTU
        # verification handshake confirm it with a real probe.
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
        # Resume the agent's own native session interactively from its worktree. Codex
        # resumes as a subcommand (`codex resume <id>`), not a `--resume` flag.
        return f"(cd {workdir} && {self.binary} resume {session_ref})"

    def _args(self, mode: str, prompt: str, ctx: RunContext) -> List[str]:
        args = ["exec"]
        if ctx.session_ref:
            # `codex exec resume <SESSION_ID> [PROMPT]` continues this agent's thread.
            args += ["resume", ctx.session_ref]
        args += ["--json", "--skip-git-repo-check"]
        if mode == "execute":
            args += ["--full-auto"]  # workspace-write sandbox, auto-approve edits/commands
        else:
            # Isolated planning: read-only, never block on an approval prompt.
            args += ["--sandbox", "read-only", "--ask-for-approval", "never"]
        model = ctx.model or self.model
        if model:
            args += ["--model", model]
        # _run already sets cwd; pass --cd too so codex roots the session in the worktree.
        args += ["--cd", str(ctx.workdir)]
        args += list(self.config.extra_args)
        args += [prompt]  # prompt is the trailing positional
        return args

    def _parse_line(self, obj: Any) -> List[AdapterEvent]:
        if not isinstance(obj, dict):
            return []
        # Codex wraps payloads in `item` (newer) or `msg` (older); unwrap to an effective
        # type + the dict that actually carries text/session fields.
        inner = obj
        if isinstance(obj.get("item"), dict):
            inner = obj["item"]
        elif isinstance(obj.get("msg"), dict):
            inner = obj["msg"]
        sid = _find_session(obj) or _find_session(inner)
        kind = obj.get("type") or inner.get("type")

        events: List[AdapterEvent] = []

        # Terminal result line.
        if kind in ("result", "done", "turn.completed", "task_complete"):
            final = _coerce_text(
                inner.get("last_agent_message")
                or inner.get("text")
                or inner.get("message")
                or inner.get("result")
                or inner.get("content")
            )
            if inner.get("is_error") or inner.get("error") or obj.get("error"):
                return [AdapterEvent.error(final or "codex reported an error")]
            return [AdapterEvent.done(final, sid)]

        # Assistant / agent message text.
        elif kind in ("agent_message", "agent_message_delta", "assistant", "message",
                      "item.completed", "item.updated", "text", "delta"):
            itype = inner.get("type")
            # item.* envelopes only carry text when the item is a message.
            if itype in (None, "text", "agent_message", "assistant_message", "message"):
                text = _coerce_text(
                    inner.get("text")
                    or inner.get("delta")
                    or inner.get("message")
                    or inner.get("content")
                )
                if text:
                    events.append(AdapterEvent.token(text))
            elif itype in ("command_execution", "tool_call", "tool_use", "mcp_tool_call"):
                events.append(
                    AdapterEvent.tool_use(
                        _coerce_text(inner.get("command") or inner.get("name") or "tool")[:80]
                    )
                )

        # Tool / command activity at the envelope level.
        elif kind in ("tool_call", "tool_use", "command", "exec_command", "command_execution",
                      "mcp_tool_call"):
            events.append(
                AdapterEvent.tool_use(
                    _coerce_text(inner.get("command") or inner.get("name") or "tool")[:80]
                )
            )

        if sid:
            events.append(AdapterEvent.meta(session_ref=sid))
        return events


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
        return _coerce_text(value.get("text") or value.get("content") or value.get("message"))
    return str(value)
