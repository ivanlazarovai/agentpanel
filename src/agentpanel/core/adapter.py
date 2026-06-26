"""The uniform agent interface.

Every coding agent — Claude Code, Cursor, a mock, later Codex/Devin — is wrapped in an
:class:`AgentAdapter` so the deliberation engine never special-cases a vendor. Adapters
expose four verbs as **async generators** that yield normalized :class:`AdapterEvent`s:

- ``health()``   — is it installed / authed? (drives ``doctor`` + FTU detection)
- ``plan()``     — produce an isolated plan in the agent's *plan mode*
- ``critique()`` — given everyone's plans, emit a revised plan + a structured ballot,
                   *resuming the agent's own session* so it keeps its own understanding
- ``execute()``  — carry out a plan in the agent's *execute mode*, in its worktree

The final ``done`` event of a stream carries the full text and a ``session_ref`` (the
agent's own resume handle) so the next turn can continue that agent's context.

CLI-backed agents share :class:`CliAdapter`, which runs the binary and streams its
``--output-format stream-json`` output; subclasses only implement argument building and
line parsing (each vendor's JSON schema differs).
"""

from __future__ import annotations

import asyncio
import json
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, List, Optional

from .config import AgentConfig


# ---------------------------------------------------------------------------
# Data types crossing the adapter boundary
# ---------------------------------------------------------------------------


@dataclass
class HealthStatus:
    """Result of probing an agent binary. ``installed`` gates panel membership."""

    name: str
    kind: str
    installed: bool
    binary: Optional[str] = None
    version: str = ""
    authed: Optional[bool] = None  # None = could not determine cheaply/offline
    models: List[str] = field(default_factory=list)
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.installed and self.authed is not False


@dataclass
class RunContext:
    """Everything an adapter needs to run one verb for one panelist."""

    workdir: Path  # the agent's own git worktree
    session_ref: Optional[str] = None  # resume handle from a prior turn (None = fresh)
    model: Optional[str] = None
    effort: Optional[str] = None  # low|medium|high|xhigh|max — bound exploration cost/latency
    timeout_s: Optional[float] = None
    turn: int = 0  # 0 = isolated planning; 1..Y = critique turns


@dataclass
class AdapterEvent:
    """A normalized streaming event from any agent.

    ``type`` is one of ``token`` | ``tool`` | ``done`` | ``error``. The terminal
    ``done`` event carries ``full_text`` (the agent's complete output) and
    ``session_ref`` (its resume handle).
    """

    type: str
    text: str = ""
    tool: str = ""
    detail: str = ""
    full_text: str = ""
    session_ref: Optional[str] = None
    is_error: bool = False
    cost_usd: Optional[float] = None  # reported by the agent on a done event, if available
    tokens: Optional[dict] = None  # input/output token usage, if available

    @classmethod
    def token(cls, text: str) -> "AdapterEvent":
        return cls(type="token", text=text)

    @classmethod
    def tool_use(cls, tool: str, detail: str = "") -> "AdapterEvent":
        return cls(type="tool", tool=tool, detail=detail)

    @classmethod
    def done(cls, full_text: str, session_ref: Optional[str] = None,
             cost_usd: Optional[float] = None, tokens: Optional[dict] = None) -> "AdapterEvent":
        return cls(type="done", full_text=full_text, session_ref=session_ref,
                   cost_usd=cost_usd, tokens=tokens)

    @classmethod
    def meta(cls, session_ref: Optional[str] = None) -> "AdapterEvent":
        """Side-channel event: carries state (e.g. session id) but renders nothing."""
        return cls(type="meta", session_ref=session_ref)

    @classmethod
    def error(cls, detail: str) -> "AdapterEvent":
        return cls(type="error", detail=detail, is_error=True)


# ---------------------------------------------------------------------------
# Base adapter
# ---------------------------------------------------------------------------


class AgentAdapter(ABC):
    """Abstract base. ``kind`` matches :attr:`AgentConfig.kind`."""

    kind: str = "abstract"
    default_binary: str = ""

    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self.name = config.name
        self.binary = config.binary or self.default_binary
        self.model = config.model

    # -- introspection -----------------------------------------------------

    @abstractmethod
    async def health(self) -> HealthStatus:
        ...

    def open_command(self, session_ref: Optional[str], workdir: Path) -> Optional[str]:
        """Shell command the user can run to open this agent's *native* session and watch
        the real work — AgentPanel only mediates; the agent owns the session. None if the
        agent has no resumable native session yet."""
        return None

    # -- the three deliberation/execution verbs ----------------------------

    @abstractmethod
    def plan(self, prompt: str, ctx: RunContext) -> AsyncIterator[AdapterEvent]:
        """Isolated planning pass (plan mode). Async generator."""

    @abstractmethod
    def critique(
        self, prompt: str, peers: str, ctx: RunContext
    ) -> AsyncIterator[AdapterEvent]:
        """Revise own plan + judge peers. ``peers`` is the rendered panel so far."""

    @abstractmethod
    def execute(self, plan: str, ctx: RunContext) -> AsyncIterator[AdapterEvent]:
        """Carry out ``plan`` in execute mode, committing in the worktree."""


# ---------------------------------------------------------------------------
# CLI subprocess base
# ---------------------------------------------------------------------------


class CliAdapter(AgentAdapter):
    """Shared machinery for agents driven through a headless CLI.

    Subclasses implement:
    - :meth:`_args` — build argv for a given mode/prompt/context
    - :meth:`_parse_line` — turn one stream-json line (dict) into AdapterEvents
    - :meth:`_version_args` / :meth:`health` specifics as needed
    """

    #: argv to print version, used by the default health() probe
    version_flag: List[str] = ["--version"]

    def resolved_binary(self) -> Optional[str]:
        """Absolute path to the binary, or None if not on PATH / configured path missing."""
        if self.binary and Path(self.binary).is_file():
            return self.binary
        return shutil.which(self.binary) if self.binary else None

    async def _probe_version(self) -> str:
        path = self.resolved_binary()
        if not path:
            return ""
        try:
            proc = await asyncio.create_subprocess_exec(
                path,
                *self.version_flag,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            return out.decode(errors="replace").strip().splitlines()[0] if out else ""
        except Exception:
            return ""

    # -- subclass hooks ----------------------------------------------------

    @abstractmethod
    def _args(self, mode: str, prompt: str, ctx: RunContext) -> List[str]:
        """argv (excluding the binary) for ``mode`` in {plan, critique, execute}."""

    @abstractmethod
    def _parse_line(self, obj: Any) -> List[AdapterEvent]:
        """Map one decoded stream-json object to zero or more AdapterEvents.

        Should also stash any session/chat id so the terminal ``done`` can carry it;
        see how subclasses accumulate ``self`` state per run is avoided by reading the
        id out of the object and returning it on the ``done`` event instead.
        """

    # -- the verbs (shared streaming impl) ---------------------------------

    def plan(self, prompt: str, ctx: RunContext) -> AsyncIterator[AdapterEvent]:
        return self._run("plan", prompt, ctx)

    def critique(self, prompt: str, peers: str, ctx: RunContext) -> AsyncIterator[AdapterEvent]:
        full = _critique_prompt(prompt, peers)
        return self._run("critique", full, ctx)

    def execute(self, plan: str, ctx: RunContext) -> AsyncIterator[AdapterEvent]:
        return self._run("execute", plan, ctx)

    async def _run(self, mode: str, prompt: str, ctx: RunContext) -> AsyncIterator[AdapterEvent]:
        """Run the CLI and stream normalized events. Tolerant of partial JSON lines."""
        path = self.resolved_binary()
        if not path:
            yield AdapterEvent.error(f"{self.name}: binary '{self.binary}' not found")
            return

        args = self._args(mode, prompt, ctx)
        try:
            proc = await asyncio.create_subprocess_exec(
                path,
                *args,
                cwd=str(ctx.workdir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as exc:  # pragma: no cover - exec failure
            yield AdapterEvent.error(f"{self.name}: failed to launch: {exc}")
            return

        collected: List[str] = []
        session_ref: Optional[str] = None
        assert proc.stdout is not None
        try:
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    # Non-JSON (text output-format or a stray log line): treat as token.
                    collected.append(line)
                    yield AdapterEvent.token(line + "\n")
                    continue
                for ev in self._parse_line(obj):
                    if ev.session_ref:
                        session_ref = ev.session_ref
                    if ev.type == "meta":
                        # Side-channel (e.g. session id); capture but don't render.
                        continue
                    if ev.type == "token":
                        collected.append(ev.text)
                    if ev.type == "done":
                        # Subclass already assembled full_text; honor it (+ any cost/usage).
                        yield AdapterEvent.done(
                            ev.full_text or "".join(collected),
                            ev.session_ref or session_ref,
                            cost_usd=ev.cost_usd,
                            tokens=ev.tokens,
                        )
                        await _drain(proc)
                        return
                    yield ev
        except Exception as exc:  # pragma: no cover - stream failure
            yield AdapterEvent.error(f"{self.name}: stream error: {exc}")

        # Stream ended without an explicit done line; synthesize one.
        rc = await proc.wait()
        if rc != 0:
            err = (await proc.stderr.read()).decode(errors="replace") if proc.stderr else ""
            yield AdapterEvent.error(f"{self.name}: exit {rc}: {err.strip()[:500]}")
            return
        yield AdapterEvent.done("".join(collected), session_ref)


async def _drain(proc: asyncio.subprocess.Process) -> None:
    """Best-effort cleanup so the child doesn't linger after we got our result."""
    try:
        if proc.returncode is None:
            await asyncio.wait_for(proc.wait(), timeout=5)
    except Exception:
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def _critique_prompt(question: str, peers: str) -> str:
    """The shared instruction wrapped around the panel for a critique turn.

    Deliberation here is adversarial by design: convergence only counts if it survives
    scrutiny. If a RED-TEAM ASSIGNMENT appears below, carry it out rigorously — your job
    there is to find what's wrong, not to be agreeable."""
    return (
        "You are one panelist among several coding agents answering the SAME request "
        "on a shared repository. Below are all panelists' current plans (including your "
        "own), and possibly a red-team assignment for you. Then:\n"
        "1. If you were given a RED-TEAM ASSIGNMENT, refute that peer's plan hard — "
        "assumptions, edge cases, failure modes, cost. Don't soften it to be agreeable.\n"
        "2. Address any critique aimed at YOUR plan: defend it or concede and revise.\n"
        "3. State concisely where you AGREE and DISAGREE with the others.\n"
        "4. Give your revised plan (begin with 'APPROACH:' and end with 'FIT:').\n"
        "5. Name which single plan you would back now, and which agent is best positioned "
        "to execute.\n\n"
        f"=== THE REQUEST ===\n{question}\n\n"
        f"=== THE PANEL SO FAR ===\n{peers}\n"
    )
