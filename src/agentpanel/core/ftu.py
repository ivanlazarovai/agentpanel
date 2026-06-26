"""First-time-user setup — the non-UI operations.

The wizard UI (``tui/ftu_screen.py``) is thin; the real work lives here so it's testable
headlessly:

- :func:`detect` — which agents are installed vs installable (with install hints)
- :func:`install` — run an agent's install command (only on explicit user confirmation)
- :func:`write_brief` — drop the shared dev-cycle brief into a repo (AGENTS.md + CLAUDE.md)
  so every agent reads it natively
- :func:`verify` — the handshake: ask an agent to confirm it understands the repo + brief
- :func:`assemble_config` — build the persisted :class:`Config` from the user's choices
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

try:  # packaged data access
    from importlib.resources import files as _res_files
except ImportError:  # pragma: no cover - <3.9 fallback (we target 3.9+)
    _res_files = None  # type: ignore

from .adapter import HealthStatus, RunContext
from .adapters import KNOWN_AGENTS, build
from .config import AgentConfig, Config, JudgeConfig, Settings

# Markers so we can update our brief in an existing file without clobbering the user's.
BRIEF_BEGIN = "<!-- AGENTPANEL:BEGIN (managed — edits between markers are overwritten) -->"
BRIEF_END = "<!-- AGENTPANEL:END -->"


@dataclass
class DetectedAgent:
    """One catalog agent after probing the machine."""

    name: str
    kind: str
    label: str
    installed: bool
    drivable: bool  # we have a working adapter for it
    binary: Optional[str] = None
    version: str = ""
    install_cmd: str = ""
    docs: str = ""
    health: Optional[HealthStatus] = None

    @property
    def installable(self) -> bool:
        return (not self.installed) and bool(self.install_cmd)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


async def detect() -> List[DetectedAgent]:
    """Probe every catalog agent. Drivable agents get a real health check; the rest are
    detected by presence on PATH (or an env var, for cloud agents)."""
    out: List[DetectedAgent] = []
    for entry in KNOWN_AGENTS:
        name, kind, label = str(entry["name"]), str(entry["kind"]), str(entry["label"])
        drivable = bool(entry.get("adapter"))
        probe = str(entry.get("probe") or "")
        install_cmd = str(entry.get("install") or "")
        docs = str(entry.get("docs") or "")
        if drivable:
            health = await build(AgentConfig(name=name, kind=kind)).health()
            out.append(
                DetectedAgent(
                    name=name, kind=kind, label=label, installed=health.installed,
                    drivable=True, binary=health.binary, version=health.version,
                    install_cmd=install_cmd, docs=docs, health=health,
                )
            )
        else:
            path = shutil.which(probe) if probe else None
            out.append(
                DetectedAgent(
                    name=name, kind=kind, label=label, installed=bool(path), drivable=False,
                    binary=path, install_cmd=install_cmd, docs=docs,
                )
            )
    return out


# ---------------------------------------------------------------------------
# Install (side-effecting — caller must confirm)
# ---------------------------------------------------------------------------


@dataclass
class InstallResult:
    ok: bool
    command: str
    output: str = ""


async def install(agent: DetectedAgent, timeout: float = 600.0) -> InstallResult:
    """Run the agent's install command in a shell. Caller is responsible for getting
    explicit user confirmation first — this actually mutates the system."""
    cmd = agent.install_cmd
    if not cmd:
        return InstallResult(ok=False, command="", output="no install command for this agent")
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        out = out_b.decode(errors="replace")
        return InstallResult(ok=proc.returncode == 0, command=cmd, output=out[-4000:])
    except asyncio.TimeoutError:
        return InstallResult(ok=False, command=cmd, output="install timed out")
    except Exception as exc:  # pragma: no cover - shell failure
        return InstallResult(ok=False, command=cmd, output=str(exc))


# ---------------------------------------------------------------------------
# Shared dev-cycle brief
# ---------------------------------------------------------------------------


def _brief_text() -> str:
    """The packaged dev-cycle brief."""
    if _res_files is not None:
        return (_res_files("agentpanel") / "shared" / "dev_cycle.md").read_text(encoding="utf-8")
    # Fallback: locate relative to this file.
    return (Path(__file__).resolve().parent.parent / "shared" / "dev_cycle.md").read_text()


def write_brief(repo: Path, filenames: Optional[List[str]] = None) -> List[Path]:
    """Write/refresh the brief into a repo so agents read it natively.

    Defaults to ``AGENTS.md`` (Cursor/Codex/others) and ``CLAUDE.md`` (Claude Code). If a
    file already exists, the brief is inserted/updated *between markers* so the user's own
    content is preserved.
    """
    filenames = filenames or ["AGENTS.md", "CLAUDE.md"]
    body = _brief_text().strip()
    block = f"{BRIEF_BEGIN}\n{body}\n{BRIEF_END}\n"
    written: List[Path] = []
    for fname in filenames:
        path = Path(repo) / fname
        if path.exists():
            existing = path.read_text(encoding="utf-8")
            if BRIEF_BEGIN in existing and BRIEF_END in existing:
                pre = existing.split(BRIEF_BEGIN)[0].rstrip()
                post = existing.split(BRIEF_END, 1)[1].lstrip()
                new = f"{pre}\n\n{block}\n{post}".strip() + "\n"
            else:
                new = existing.rstrip() + "\n\n" + block
        else:
            new = block
        path.write_text(new, encoding="utf-8")
        written.append(path)
    return written


# ---------------------------------------------------------------------------
# Verification handshake
# ---------------------------------------------------------------------------

_HANDSHAKE_PROMPT = (
    "This is an AgentPanel setup handshake. Read this repository's AGENTS.md / CLAUDE.md "
    "dev-cycle brief. In ONE short line, reply starting with the word READY, then name one "
    "rule you must follow (e.g. begin plans with APPROACH:, work in your own worktree). "
    "Do not edit any files."
)


@dataclass
class VerifyResult:
    agent: str
    ok: bool
    detail: str = ""
    transcript: str = ""


async def verify(agent_config: AgentConfig, repo: Path, timeout: float = 90.0) -> VerifyResult:
    """Run the handshake against one agent: does it read the brief and respond sensibly?

    Success = a non-error, non-empty response. We also note whether it acknowledged a rule
    (mentions READY/APPROACH/worktree) as a stronger signal.
    """
    adapter = build(agent_config)
    ctx = RunContext(workdir=Path(repo), timeout_s=timeout)
    collected: List[str] = []
    failed = False
    detail = ""
    try:
        gen = adapter.plan(_HANDSHAKE_PROMPT, ctx)
        agen = _with_timeout(gen, timeout)
        async for ev in agen:
            if ev.type == "token":
                collected.append(ev.text)
            elif ev.type == "error":
                failed, detail = True, ev.detail
            elif ev.type == "done" and ev.full_text:
                collected = [ev.full_text]
    except asyncio.TimeoutError:
        return VerifyResult(agent=agent_config.name, ok=False, detail="handshake timed out")
    except Exception as exc:  # pragma: no cover
        return VerifyResult(agent=agent_config.name, ok=False, detail=str(exc))

    text = "".join(collected).strip()
    if failed or not text:
        return VerifyResult(agent=agent_config.name, ok=False,
                            detail=detail or "no response", transcript=text)
    low = text.lower()
    acknowledged = any(k in low for k in ("ready", "approach", "worktree", "fit"))
    return VerifyResult(
        agent=agent_config.name, ok=True,
        detail="acknowledged the brief" if acknowledged else "responded",
        transcript=text[:500],
    )


async def _with_timeout(agen, timeout: float):
    """Wrap an async generator with an overall timeout."""
    loop_deadline = asyncio.get_event_loop().time() + timeout
    it = agen.__aiter__()
    while True:
        remaining = loop_deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError
        try:
            item = await asyncio.wait_for(it.__anext__(), timeout=remaining)
        except StopAsyncIteration:
            return
        yield item


# ---------------------------------------------------------------------------
# Config assembly
# ---------------------------------------------------------------------------


@dataclass
class AgentChoice:
    """The user's decisions for one agent in the wizard."""

    name: str
    kind: str
    enabled: bool = True
    model: Optional[str] = None
    binary: Optional[str] = None
    verified: bool = False


def assemble_config(
    choices: List[AgentChoice],
    judge: JudgeConfig,
    settings: Settings,
    repo: Optional[Path] = None,
) -> Config:
    """Build the persisted Config from wizard choices."""
    roster = [
        AgentConfig(
            name=c.name, kind=c.kind, enabled=c.enabled, model=c.model,
            binary=c.binary, verified=c.verified,
        )
        for c in choices
    ]
    return Config(
        roster=roster,
        judge=judge,
        settings=settings,
        repo=str(repo) if repo else None,
    )
