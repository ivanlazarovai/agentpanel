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
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

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
    auth_cmd: str = ""
    auth_logout_cmd: str = ""
    auth_status_cmd: str = ""
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
        auth_cmd = str(entry.get("auth") or "")
        logout_cmd = str(entry.get("auth_logout") or "")
        status_cmd = str(entry.get("auth_status") or "")
        docs = str(entry.get("docs") or "")
        common = dict(name=name, kind=kind, label=label, install_cmd=install_cmd,
                      auth_cmd=auth_cmd, auth_logout_cmd=logout_cmd, auth_status_cmd=status_cmd,
                      docs=docs)
        if drivable:
            health = await build(AgentConfig(name=name, kind=kind)).health()
            out.append(DetectedAgent(installed=health.installed, drivable=True,
                                     binary=health.binary, version=health.version,
                                     health=health, **common))
        else:
            path = shutil.which(probe) if probe else None
            out.append(DetectedAgent(installed=bool(path), drivable=False, binary=path, **common))
    return out


# Phrases in an agent's error output that mean "not logged in" (vs a real failure).
_AUTH_HINTS = ("authenticat", "log in", "login", "logged in", "api key", "api-key",
               "unauthorized", "not signed in", "sign in", "credential")


def needs_login(text: str) -> bool:
    low = (text or "").lower()
    return any(h in low for h in _AUTH_HINTS)


async def run_interactive(cmd: str) -> InstallResult:
    """Run an interactive auth command attached to the terminal (so browser/device-code
    flows and prompts work). In a TUI, call this inside ``app.suspend()``."""
    if not cmd:
        return InstallResult(ok=False, command="", output="no command for this agent")
    try:
        proc = await asyncio.create_subprocess_shell(cmd)  # inherits stdio
        rc = await proc.wait()
        return InstallResult(ok=rc == 0, command=cmd, output="ok" if rc == 0 else f"exited {rc}")
    except Exception as exc:  # pragma: no cover - shell failure
        return InstallResult(ok=False, command=cmd, output=str(exc))


async def login(agent: DetectedAgent) -> InstallResult:
    """Launch the agent's native login (sign in with the account you want)."""
    return await run_interactive(agent.auth_cmd)


async def logout(agent: DetectedAgent) -> InstallResult:
    """Sign the agent out, clearing its stored auth — so a re-login picks a new account."""
    return await run_interactive(agent.auth_logout_cmd)


async def relogin(agent: DetectedAgent) -> InstallResult:
    """Sign out then sign in — the clean way to switch a browser-login account."""
    await run_interactive(agent.auth_logout_cmd)
    return await run_interactive(agent.auth_cmd)


async def status(agent: DetectedAgent) -> InstallResult:
    """Show the agent's auth status (which account it's signed in as)."""
    return await run_interactive(agent.auth_status_cmd)


# Env var that carries an API-key account, per agent kind (for key-based auth like Gemini).
KIND_KEYVAR = {
    "claude_code": "ANTHROPIC_API_KEY",
    "cursor_agent": "CURSOR_API_KEY",
    "codex": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
}

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Normalize raw plan tokens (claude `subscriptionType`, cursor "Subscription Tier",
# codex `chatgpt_plan_type`) into display labels.
_PLAN_LABEL = {"max": "Max", "pro": "Pro", "pro+": "Pro+", "plus": "Plus",
               "ultra": "Ultra", "team": "Team", "free": "Free", "business": "Business"}

# Approximate public list prices (USD/mo). Plans & prices change constantly and tiers
# overlap (e.g. Claude "max" is sold at two price points) — this is only a hint; the live
# figure lives in each provider's dashboard.
PLAN_PRICE = {
    ("claude_code", "max"): "≈ $100–200/mo list",
    ("claude_code", "pro"): "≈ $20/mo list",
    ("cursor_agent", "pro+"): "≈ $60/mo list",
    ("cursor_agent", "pro"): "≈ $20/mo list",
    ("cursor_agent", "ultra"): "≈ $200/mo list",
    ("codex", "plus"): "≈ $20/mo list",
    ("codex", "pro"): "≈ $200/mo list",
    ("codex", "team"): "≈ $30/user/mo list",
}

# Live limits / remaining usage are NOT exposed headlessly by any of these CLIs — point the
# user at where the real figure lives instead of fabricating one.
USAGE_HINT = {
    "claude_code": "limits/usage not in CLI — run `claude` then `/usage`",
    "cursor_agent": "limits/usage not in CLI — see cursor.com/dashboard",
    "codex": "limits/usage not in CLI — see chatgpt.com settings",
    "gemini": "limits/usage not in CLI — see Google AI Studio quota",
}


@dataclass
class AccountStatus:
    """What we can learn about an agent's signed-in account from its CLI / stored creds."""

    account: str = ""   # email or account id
    plan: str = ""      # display label, e.g. "Max", "Pro+", "Plus"
    price: str = ""     # list-price hint for that plan (approximate)
    renews: str = ""    # e.g. "renews 2026-07-04"
    usage: str = ""     # where to find live limits/usage (CLIs don't report it)
    detail: str = ""    # raw status text, shown in the expanded view

    @property
    def line(self) -> str:
        """Compact one-liner for the 👤 row: account · plan."""
        return " · ".join(b for b in (self.account, self.plan) if b)

    def report(self, label: str) -> str:
        """Multi-line block for the Status view."""
        rows = [f"{label}",
                f"  account : {self.account or '—'}",
                f"  plan    : {self.plan or '—'}" + (f"   {self.price}" if self.price else "")]
        if self.renews:
            rows.append(f"  {self.renews}")
        rows.append(f"  usage   : {self.usage or 'not reported by this CLI'}")
        if self.detail:
            rows.append("  ── raw ──")
            rows.extend("  " + ln for ln in self.detail.splitlines()[:12])
        return "\n".join(rows)


def _jwt_claims(token: str) -> dict:
    """Decode a JWT payload (no signature check — we only read our own stored token)."""
    import base64
    import json

    try:
        p = token.split(".")[1]
        p += "=" * (-len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p))
    except Exception:
        return {}


async def _run_capture(cmd: str, timeout: float) -> str:
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return out_b.decode(errors="replace")
    except Exception:
        return ""


def _read_json(path: str) -> dict:
    import json

    try:
        with open(os.path.expanduser(path)) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _gemini_account() -> str:
    """Gemini stores its signed-in Google account locally (no status command)."""
    d = _read_json("~/.gemini/google_accounts.json")
    if d.get("active") or d.get("email"):
        return d.get("active") or d.get("email")
    claims = _jwt_claims(_read_json("~/.gemini/oauth_creds.json").get("id_token", ""))
    return claims.get("email", "")


async def account_status(agent: DetectedAgent, timeout: float = 20.0) -> AccountStatus:
    """Gather the signed-in account, plan, renewal and a usage pointer for one agent.

    Each provider exposes this differently — Claude in `auth status` JSON, Cursor in
    `about`, Codex inside its stored OAuth token, Gemini in a local creds file — so this
    is per-kind. Account ID and plan are obtainable for all signed-in agents; live
    limits/usage are not exposed by any CLI, so we point to the dashboard instead."""
    kind = agent.kind
    st = AccountStatus(usage=USAGE_HINT.get(kind, ""))
    plan_key = ""
    if kind == "claude_code":
        import json

        text = await _run_capture("claude auth status", timeout)
        st.detail = text.strip()
        try:
            d = json.loads(text)
            st.account = d.get("email") or d.get("orgName") or ""
            plan_key = str(d.get("subscriptionType") or "").lower()
        except Exception:
            m = _EMAIL_RE.search(text)
            st.account = m.group(0) if m else ""
    elif kind == "cursor_agent":
        text = await _run_capture("cursor-agent about", timeout)
        st.detail = text.strip()
        for ln in text.splitlines():
            low = ln.lower()
            if "user email" in low:
                m = _EMAIL_RE.search(ln)
                st.account = m.group(0) if m else st.account
            elif "subscription tier" in low:
                plan_key = ln.split()[-1].lower()  # "Subscription Tier   Pro+" -> "pro+"
        if not st.account:
            m = _EMAIL_RE.search(await _run_capture("cursor-agent status", timeout))
            st.account = m.group(0) if m else ""
    elif kind == "codex":
        creds = _read_json("~/.codex/auth.json")
        toks = creds.get("tokens") or {}
        claims = _jwt_claims(toks.get("id_token", ""))
        st.account = claims.get("email") or toks.get("account_id") or ""
        auth = claims.get("https://api.openai.com/auth") or {}
        plan_key = str(auth.get("chatgpt_plan_type") or "").lower()
        until = auth.get("chatgpt_subscription_active_until")
        if until:
            st.renews = f"renews {str(until)[:10]}"
        if creds.get("auth_mode"):
            st.detail = f"auth_mode: {creds['auth_mode']}"
    elif kind == "gemini":
        st.account = _gemini_account()
    if not st.account:
        var = KIND_KEYVAR.get(kind)
        if var and os.environ.get(var):
            st.account = f"API key ({var})"
    if plan_key:
        st.plan = _PLAN_LABEL.get(plan_key, plan_key[:1].upper() + plan_key[1:])
        st.price = PLAN_PRICE.get((kind, plan_key), "")
    return st


async def auth_account(agent: DetectedAgent, timeout: float = 20.0) -> str:
    """Compact 'account · plan' label for the setup row (see :func:`account_status`)."""
    return (await account_status(agent, timeout)).line


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
    needs_login: bool = False  # the failure looks like an auth/login problem


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
        msg = detail or text or "no response"
        return VerifyResult(agent=agent_config.name, ok=False, detail=msg,
                            transcript=text, needs_login=needs_login(msg))
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


def recommend_judge(verified_agents: List[str]) -> JudgeConfig:
    """Pick a sensible neutral judge from what's available, no API key required.

    A verified roster agent acts as neutral chair (works with a subscription login);
    deterministic when nothing is verified yet. (The user can switch to a dedicated
    Anthropic-API judge later if they have a key.)
    """
    if verified_agents:
        return JudgeConfig(backend="designated_agent", agent=verified_agents[0])
    return JudgeConfig(backend="deterministic")


# ---------------------------------------------------------------------------
# Cold-start orchestrator — bootstart from zero config to a working panel
# ---------------------------------------------------------------------------


async def auto_bootstrap(
    repo: Path,
    *,
    existing: Optional[Config] = None,
    only: Optional[List[str]] = None,
    do_install: bool = True,
    do_login: bool = True,
    emit=lambda _msg: None,
) -> Config:
    """Bring AgentPanel up to a working multi-agent state — from zero, topping up a thin
    panel, or adding specific agents (``only=[names]``).

    For each drivable agent in scope: reuse it untouched if already verified in
    ``existing`` (no cost), else install it if missing, run its native login if the
    verification handshake says it's not authed, and verify it. Agents out of scope are
    preserved from ``existing``. Then assemble a ready-to-save :class:`Config`, preserving
    ``existing`` judge/settings/permissions and granting the base directory.
    """
    repo = Path(repo).resolve()
    write_brief(repo, ["AGENTS.md"])

    existing_by_name = {a.name: a for a in (existing.roster if existing else [])}
    prior = {n: a for n, a in existing_by_name.items() if a.verified}
    result: Dict[str, AgentConfig] = dict(existing_by_name)  # preserve everything by default
    verified: List[str] = [n for n, a in prior.items() if a.enabled]

    for agent in [a for a in await detect() if a.drivable]:
        in_scope = (agent.name in only) if only is not None else True
        # Out of scope, or already verified (and not explicitly targeted) → leave as-is.
        if not in_scope or (agent.name in prior and not (only and agent.name in only)):
            if agent.name in prior and agent.name not in verified:
                verified.append(agent.name)
            if agent.name in prior:
                emit(f"{agent.label}: ✓ already configured")
            continue

        if not agent.installed and do_install and agent.installable:
            emit(f"installing {agent.label}…  ($ {agent.install_cmd})")
            res = await install(agent)
            emit(f"  {'installed' if res.ok else 'install failed: ' + res.output[-120:]}")
            agent = next((a for a in await detect() if a.name == agent.name), agent)
        if not agent.installed:
            emit(f"skipping {agent.label} (not installed)")
            continue

        vr = await verify(AgentConfig(name=agent.name, kind=agent.kind), repo)
        if not vr.ok and vr.needs_login and agent.auth_cmd and do_login:
            emit(f"{agent.label} needs login — launching `{agent.auth_cmd}`…")
            await login(agent)
            vr = await verify(AgentConfig(name=agent.name, kind=agent.kind), repo)

        result[agent.name] = AgentConfig(name=agent.name, kind=agent.kind,
                                         enabled=vr.ok, verified=vr.ok)
        if vr.ok and agent.name not in verified:
            verified.append(agent.name)
        emit(f"{agent.label}: {'✓ ready' if vr.ok else '· ' + vr.detail}")

    # Preserve the user's existing choices; only pick a judge if there isn't one yet.
    judge = existing.judge if existing else recommend_judge(verified)
    settings = existing.settings if existing else Settings()
    granted = list((existing.permissions if existing else {}).get("granted_dirs", []))
    if str(repo) not in granted:
        granted.append(str(repo))
    config = Config(
        roster=list(result.values()),
        judge=judge,
        settings=settings,
        repo=str(repo),
        permissions={"granted_dirs": granted, "rules":
                     (existing.permissions if existing else {}).get("rules", []),
                     "default": (existing.permissions if existing else {}).get("default", "ask")},
    )
    emit(f"panel: {', '.join(verified) or '(none verified yet)'}  ·  judge: {config.judge.backend}")
    return config
