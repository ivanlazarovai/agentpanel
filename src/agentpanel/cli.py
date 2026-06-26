"""AgentPanel command-line entry point.

    agentpanel              launch the TUI (or the FTU wizard on first run)
    agentpanel doctor       probe every known agent: installed? installable? authed?
    agentpanel setup        run the first-time-user / reconfiguration wizard
    agentpanel ask "<q>"    headless: run one panel session to convergence/escalation
    agentpanel --version

``doctor`` is also the detection engine the FTU wizard reuses, so it understands both
installed agents and installable-but-absent ones (with install hints).
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path
from typing import List

from . import __version__
from .core import config as cfg
from .core.adapters import KNOWN_AGENTS, build
from .core.adapter import HealthStatus
from .core.config import AgentConfig


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentpanel", description="Local multi-agent control plane")
    parser.add_argument("--version", action="version", version=f"agentpanel {__version__}")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("doctor", help="probe installed/installable agents")
    sub.add_parser("setup", help="run the first-time-user / reconfiguration wizard")
    parser.add_argument("--mock", action="store_true",
                        help="launch the TUI with a built-in mock panel (no real agents)")
    ask = sub.add_parser("ask", help="run one headless panel session")
    ask.add_argument("question", help="the request to put to the panel")
    ask.add_argument("--repo", default=".", help="repository to work in (default: cwd)")
    ask.add_argument("--mock", action="store_true",
                     help="run a built-in mock panel (no real agents, no cost) — a demo of the flow")
    ask.add_argument("--no-worktrees", action="store_true",
                     help="skip git worktrees (deliberation only, no execution)")
    ask.add_argument("--execute", action="store_true",
                     help="after convergence, the elected agent executes in its worktree "
                          "(on escalation, the top option's agent executes)")
    ask.add_argument("--keep", metavar="AGENT",
                     help="after executing, merge AGENT's branch into the working branch")
    ask.add_argument("--review", type=int, default=0, metavar="N",
                     help="coopetition: N rounds where stood-down agents observe + coach the "
                          "worker(s) between execution rounds (default 0)")

    args = parser.parse_args(argv)

    if args.command == "doctor":
        return asyncio.run(_doctor())
    if args.command == "setup":
        return _setup()
    if args.command == "ask":
        return asyncio.run(_ask(args.question, args.repo, args.mock, args.no_worktrees,
                                args.execute, args.keep, args.review))

    # No subcommand: launch TUI, or nudge to setup on first run.
    return _launch(args.mock)


# ---------------------------------------------------------------------------
# doctor — agent detection (shared with FTU)
# ---------------------------------------------------------------------------


async def detect_agents() -> List[dict]:
    """Probe each catalog agent. Returns rows with health + install metadata.

    A row is::

        {entry: <catalog dict>, status: HealthStatus|None, installable: bool}

    ``status`` is None for catalog agents we can't yet drive (no adapter); for those we
    still report whether the binary is present and how to install it.
    """
    rows: List[dict] = []
    for entry in KNOWN_AGENTS:
        has_adapter = bool(entry.get("adapter"))
        probe = str(entry.get("probe") or "")
        if has_adapter:
            adapter = build(AgentConfig(name=str(entry["name"]), kind=str(entry["kind"])))
            status = await adapter.health()
        else:
            present = bool(probe and shutil.which(probe))
            status = HealthStatus(
                name=str(entry["name"]),
                kind=str(entry["kind"]),
                installed=present,
                binary=shutil.which(probe) if probe else None,
                detail="installed (adapter coming soon)" if present else "not installed",
            )
        rows.append(
            {
                "entry": entry,
                "status": status,
                "installable": bool(entry.get("install")) and not status.installed,
            }
        )
    return rows


async def _doctor() -> int:
    rows = await detect_agents()
    print("AgentPanel — agent detection\n")
    for row in rows:
        entry, st = row["entry"], row["status"]
        mark = "✓" if st.installed else "·"
        adapter = "drivable" if entry.get("adapter") else "detect-only"
        line = f"  {mark} {str(entry['label']):24} [{adapter:11}]"
        if st.installed:
            ver = f" {st.version}" if st.version else ""
            auth = {True: "authed", False: "NOT authed", None: "auth?"}[st.authed]
            line += f" {st.binary or ''}{ver}  ({auth})"
        else:
            line += "  not installed"
            if entry.get("install"):
                line += f"\n      install: {entry['install']}"
            if entry.get("docs"):
                line += f"\n      docs:    {entry['docs']}"
        print(line)

    installed = [r for r in rows if r["status"].installed]
    print(f"\n{len(installed)}/{len(rows)} agents installed.")
    if not cfg.config_exists():
        print("\nNo config yet — run `agentpanel setup` to configure your panel.")
    return 0


# ---------------------------------------------------------------------------
# Stubs for commands implemented in later build steps
# ---------------------------------------------------------------------------


def _setup() -> int:
    from .tui.ftu_screen import run as run_ftu

    result = run_ftu()
    if result is None:
        print("Setup cancelled — no changes saved.")
        return 1
    panel = result.panel() or result.enabled_agents()
    print(f"Saved {cfg.GLOBAL_CONFIG}. Panel: {', '.join(a.name for a in panel) or '(none)'}.")
    print("Launch the panel with `agentpanel`.")
    return 0


async def _ask(question: str, repo: str, mock: bool, no_worktrees: bool,
               execute: bool = False, keep: str | None = None, review: int = 0) -> int:
    from .core.session import SessionManager
    from .render import render

    if mock or not cfg.config_exists():
        if not mock:
            print("(no config found — running a built-in mock panel; `agentpanel setup` to use real agents)\n")
        config = _demo_config()
        use_worktrees = execute  # mock execution needs worktrees too
    else:
        config = cfg.load()
        use_worktrees = not no_worktrees

    mgr = SessionManager(config)
    session = mgr.create(question, repo=Path(repo).resolve(), use_worktrees=use_worktrees)

    async def _after() -> None:
        outcome = session.outcome
        if not (execute and outcome and use_worktrees):
            return
        if outcome.status == "converged" and outcome.elected:
            agents = [outcome.elected]
        elif outcome.status == "escalated" and outcome.options:
            agents = [outcome.options[0]["representative"]]
        else:
            agents = []
        if not agents:
            return
        await session.execute(agents, review_rounds=review)
        if keep and keep in agents:
            branch = await session.keep(keep)
            print(f"\n✓ Kept {keep} → merged into {branch}")

    await render(session, after=_after if execute else None)
    return 0 if session.status in ("converged", "escalated") else 1


def _demo_config():
    """A built-in mock panel that *deliberates*: c starts dissenting, then converges."""
    from .core.config import AgentConfig, Config, JudgeConfig, Settings

    return Config(
        roster=[
            AgentConfig(name="claude", kind="mock", extra_args=["plan=use-a-state-machine", "fit=0.8"]),
            AgentConfig(name="cursor", kind="mock", extra_args=["plan=use-a-state-machine", "fit=0.6"]),
            AgentConfig(name="codex", kind="mock",
                        extra_args=["plan=use-event-sourcing", "switch_to=use-a-state-machine",
                                    "switch_turn=2", "fit=0.5"]),
        ],
        judge=JudgeConfig(backend="deterministic"),
        settings=Settings(consensus_threshold=0.75, max_turns=3),
    )


def _launch(mock: bool) -> int:
    from .tui.app import run as run_tui

    if mock:
        run_tui(config=_demo_config(), demo_question="Design the session persistence layer")
        return 0
    if not cfg.config_exists():
        # First run: route through the FTU wizard, then launch the panel with the result.
        from .tui.ftu_screen import run as run_ftu

        print("First run — launching setup…")
        config = run_ftu()
        if config is None:
            print("Setup cancelled. Run `agentpanel setup` when ready, "
                  "or `agentpanel --mock` to try a demo panel.")
            return 1
    else:
        config = cfg.load()
    run_tui(config=config)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
