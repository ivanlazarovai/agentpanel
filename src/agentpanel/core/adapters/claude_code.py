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

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, AsyncIterator, List, Optional

from ..adapter import AdapterEvent, CliAdapter, HealthStatus, RunContext


class _PersistentClaude:
    """One long-running ``claude`` process fed messages over stream-json stdin, so context
    accumulates in-memory across deliberation turns (no per-call startup / session replay)."""

    def __init__(self, binary: str, args: List[str], cwd: Path, env: Optional[dict] = None) -> None:
        self.binary = binary
        self.args = args
        self.cwd = cwd
        self.env = env
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.session_ref: Optional[str] = None

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            self.binary, *self.args, cwd=str(self.cwd), env=self.env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def send(self, prompt: str, parse_line) -> AsyncIterator[AdapterEvent]:
        """Send one user message; stream this turn's events until its result."""
        assert self.proc is not None and self.proc.stdin is not None and self.proc.stdout is not None
        msg = {"type": "user", "message": {"role": "user", "content": prompt}}
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        await self.proc.stdin.drain()

        collected: List[str] = []
        async for raw in self.proc.stdout:
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            for ev in parse_line(obj):
                if ev.session_ref:
                    self.session_ref = ev.session_ref
                if ev.type == "meta":
                    continue
                if ev.type == "token":
                    collected.append(ev.text)
                if ev.type == "done":
                    yield AdapterEvent.done(ev.full_text or "".join(collected),
                                            ev.session_ref or self.session_ref,
                                            cost_usd=ev.cost_usd, tokens=ev.tokens)
                    return
                yield ev
        # Stream ended (process died) — synthesize a terminal event.
        yield AdapterEvent.done("".join(collected), self.session_ref)

    async def close(self) -> None:
        if self.proc is None:
            return
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
            await asyncio.wait_for(self.proc.wait(), timeout=10)
        except Exception:
            try:
                self.proc.kill()
            except ProcessLookupError:
                pass


class ClaudeCodeAdapter(CliAdapter):
    kind = "claude_code"
    default_binary = "claude"
    version_flag = ["--version"]

    def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
        super().__init__(config)
        self._live: Optional[_PersistentClaude] = None  # warm deliberation process

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

    # -- persistent deliberation (plan + critique share one warm process) --

    async def plan(self, prompt: str, ctx: RunContext) -> AsyncIterator[AdapterEvent]:
        async for ev in self._deliberate(prompt, ctx):
            yield ev

    async def critique(self, prompt: str, peers: str, ctx: RunContext) -> AsyncIterator[AdapterEvent]:
        from ..adapter import _critique_prompt

        async for ev in self._deliberate(_critique_prompt(prompt, peers), ctx):
            yield ev

    async def _deliberate(self, prompt: str, ctx: RunContext) -> AsyncIterator[AdapterEvent]:
        path = self.resolved_binary()
        if not path:
            yield AdapterEvent.error(f"{self.name}: binary '{self.binary}' not found")
            return
        if self._live is None:
            self._live = _PersistentClaude(path, self._persistent_args(ctx), ctx.workdir,
                                           env=self.subprocess_env())
            try:
                await self._live.start()
            except Exception as exc:  # pragma: no cover - exec failure
                self._live = None
                yield AdapterEvent.error(f"{self.name}: failed to start session: {exc}")
                return
        try:
            async for ev in self._live.send(prompt, self._parse_line):
                yield ev
        except Exception as exc:  # pragma: no cover - stream failure
            yield AdapterEvent.error(f"{self.name}: session stream error: {exc}")

    def _persistent_args(self, ctx: RunContext) -> List[str]:
        # No -p prompt (it streams over stdin); plan mode = read-only deliberation.
        args = ["-p", "--input-format", "stream-json", "--output-format", "stream-json",
                "--verbose", "--permission-mode", "plan"]
        model = ctx.model or self.model
        if model:
            args += ["--model", model]
        if ctx.effort:
            args += ["--effort", ctx.effort]
        if ctx.budget_usd:
            args += ["--max-budget-usd", str(ctx.budget_usd)]
        # A restored session arrives with a prior session_ref → resume the agent's own
        # native session so its accumulated context comes back (fresh otherwise).
        if ctx.session_ref:
            args += ["--resume", ctx.session_ref]
        args += ["--add-dir", str(ctx.workdir)]
        args += list(self.config.extra_args)
        return args

    async def aclose(self) -> None:
        if self._live is not None:
            await self._live.close()
            self._live = None

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
