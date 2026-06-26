"""Codex adapter (``codex exec`` CLI).

- plan / critique: ``codex exec --json --sandbox read-only <prompt>``  (no edits)
- execute:         ``codex exec --json --sandbox workspace-write <prompt>`` (writes in the worktree)

Codex emits a clean JSONL event stream:
- ``{"type":"thread.started","thread_id":"<uuid>"}``        → the session/resume handle
- ``{"type":"item.completed","item":{"type":"agent_message","text":...}}`` → the agent's text
- ``{"type":"item.completed","item":{"type":"command_execution",...}}``    → a shell tool call
- ``{"type":"turn.completed","usage":{...}}``               → token usage (terminal)

Each verb runs a fresh ``codex exec`` (its process ``cwd`` is the worktree, set by the
base) with the sandbox set explicitly — ``exec resume`` can't set a sandbox, and the
deliberation prompt already carries the full panel context, so we don't chain sessions.
The captured ``thread_id`` is still returned so the user can open Codex's native session.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional

from ..adapter import AdapterEvent, CliAdapter, HealthStatus, RunContext


class CodexAdapter(CliAdapter):
    kind = "codex"
    default_binary = "codex"
    version_flag = ["--version"]

    async def health(self) -> HealthStatus:
        path = self.resolved_binary()
        if not path:
            return HealthStatus(name=self.name, kind=self.kind, installed=False,
                                detail="`codex` not found on PATH")
        version = await self._probe_version()
        return HealthStatus(name=self.name, kind=self.kind, installed=True, binary=path,
                            version=version, authed=None,  # confirmed at the FTU handshake
                            detail="ready (auth via `codex login`)")

    def open_command(self, session_ref: Optional[str], workdir: Path) -> Optional[str]:
        if not session_ref:
            return None
        return f"(cd {workdir} && {self.binary} resume {session_ref})"

    def _args(self, mode: str, prompt: str, ctx: RunContext) -> List[str]:
        sandbox = "workspace-write" if mode == "execute" else "read-only"
        args = ["exec", "--json", "--skip-git-repo-check", "--color", "never",
                "--sandbox", sandbox]
        model = ctx.model or self.model
        if model:
            args += ["--model", model]
        args += list(self.config.extra_args)
        args += [prompt]  # final positional: the instructions
        return args

    def _parse_line(self, obj: Any) -> List[AdapterEvent]:
        if not isinstance(obj, dict):
            return []
        kind = obj.get("type")
        if kind == "thread.started":
            sid = obj.get("thread_id")
            return [AdapterEvent.meta(session_ref=sid)] if sid else []
        if kind == "item.completed":
            return self._item(obj.get("item") or {})
        if kind == "turn.completed":
            usage = obj.get("usage") or {}
            tokens = {"input": usage.get("input_tokens"),
                      "output": usage.get("output_tokens")} if usage else None
            return [AdapterEvent.done("", None, tokens=tokens)]
        if kind in ("turn.failed", "error", "thread.error"):
            msg = obj.get("message") or _err_text(obj.get("error")) or "codex error"
            return [AdapterEvent.error(_coerce_text(msg))]
        return []

    def _item(self, item: dict) -> List[AdapterEvent]:
        it = item.get("type")
        if it == "agent_message":
            text = _coerce_text(item.get("text") or item.get("content"))
            return [AdapterEvent.token(text)] if text else []
        if it in ("command_execution", "command", "shell"):
            cmd = _coerce_text(item.get("command") or item.get("text"))
            return [AdapterEvent.tool_use("shell", cmd[:120])]
        if it in ("file_change", "patch", "apply_patch"):
            files = item.get("changes") or item.get("files") or item.get("path") or ""
            return [AdapterEvent.tool_use("edit", _coerce_text(files)[:120])]
        # reasoning / other items: not surfaced as panel text.
        return []


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ", ".join(_coerce_text(v) for v in value)
    if isinstance(value, dict):
        return _coerce_text(value.get("text") or value.get("content")
                            or value.get("path") or value.get("name"))
    return str(value)


def _err_text(value: Any) -> str:
    if isinstance(value, dict):
        return _coerce_text(value.get("message") or value.get("detail"))
    return _coerce_text(value)
