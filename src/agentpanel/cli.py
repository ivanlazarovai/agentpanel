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
from typing import List, Optional

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
    boot = sub.add_parser("bootstrap",
                          help="cold start: install + log in + verify agents, then configure")
    boot.add_argument("--dir", default=".", help="base directory to operate in (default: cwd)")
    boot.add_argument("--no-install", action="store_true", help="don't install missing agents")
    boot.add_argument("--no-login", action="store_true", help="don't launch agent logins")
    add = sub.add_parser("add", help="quickly add agent(s): install + log in + verify, then keep")
    add.add_argument("names", nargs="*",
                     help="agent names to add (e.g. cursor codex); empty = any available")
    add.add_argument("--dir", default=".", help="base directory (default: cwd)")
    sess = sub.add_parser("sessions", help="list previously-saved panel sessions (resumable)")
    sess.add_argument("--repo", default=".", help="repository (default: cwd)")
    acc = sub.add_parser("account",
                         help="switch an agent's account, or clone it to run multiple accounts")
    acc.add_argument("action",
                     choices=["list", "set", "clear", "clone",
                              "login", "logout", "relogin", "status"])
    acc.add_argument("agent", nargs="?", help="roster agent name")
    acc.add_argument("rest", nargs="*",
                     help="credentials as KEY=VALUE or a bare API key; for `clone`, the first "
                          "value is the new agent name, then credentials")
    acc.add_argument("--label", help="human label for the account")
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
    if args.command == "bootstrap":
        return asyncio.run(_bootstrap(args.dir, not args.no_install, not args.no_login))
    if args.command == "add":
        return asyncio.run(_add(args.names, args.dir))
    if args.command == "sessions":
        return _sessions(args.repo)
    if args.command == "account":
        if args.action in ("login", "logout", "relogin", "status"):
            return asyncio.run(_account_auth(args.action, args.agent))
        return _account(args.action, args.agent, args.rest, args.label)
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


# Default credential env var per agent kind (so `account set cursor <key>` just works).
_KIND_KEYVAR = {
    "claude_code": "ANTHROPIC_API_KEY",
    "cursor_agent": "CURSOR_API_KEY",
    "codex": "OPENAI_API_KEY",
}


def _parse_creds(items: List[str], kind: str) -> dict:
    """Each item is KEY=VALUE, or a bare value mapped to the kind's default credential var."""
    env: dict = {}
    default_var = _KIND_KEYVAR.get(kind)
    for item in items:
        if "=" in item:
            key, _, value = item.partition("=")
            env[key.strip()] = value
        elif default_var:
            env[default_var] = item
    return env


async def _account_auth(action: str, agent: Optional[str]) -> int:
    """Run an agent's native auth command (login / logout / relogin / status)."""
    from .core import ftu

    if not agent:
        print(f"which agent? e.g. `agentpanel account {action} cursor`")
        return 1
    detected = next((a for a in await ftu.detect() if a.name == agent), None)
    if detected is None or not detected.installed:
        print(f"agent '{agent}' is not installed/known. Try `agentpanel doctor`.")
        return 1
    op = {"login": ftu.login, "logout": ftu.logout, "relogin": ftu.relogin, "status": ftu.status}
    res = await op[action](detected)
    print(f"{action} {agent}: {'ok' if res.ok else res.output}")
    if action in ("login", "relogin"):
        print("(sign in with the account you want; AgentPanel will use it next run)")
    return 0 if res.ok else 1


def _account(action: str, agent: Optional[str], rest: List[str], label: Optional[str]) -> int:
    config = cfg.load()
    if action == "list":
        if not config.roster:
            print("No agents configured. Run `agentpanel bootstrap` or `agentpanel add`.")
            return 0
        for a in config.roster:
            creds = ", ".join(sorted(a.env.keys())) if a.env else "(default login)"
            print(f"  {a.name:12} [{a.kind:12}]  account: {a.account or '—':10}  creds: {creds}")
        print("\nSwitch:  agentpanel account set <agent> <API_KEY>"
              "\nRun two: agentpanel account clone <agent> <newname> <API_KEY>")
        return 0

    if not agent:
        print("which agent? e.g. `agentpanel account set cursor <key>`")
        return 1

    if action == "clone":
        if not rest:
            print("usage: agentpanel account clone <agent> <newname> <API_KEY...>")
            return 1
        src = config.get(agent)
        if src is None:
            print(f"no agent '{agent}' in the roster (`agentpanel account list`).")
            return 1
        newname, creds = rest[0], rest[1:]
        clone = AgentConfig(name=newname, kind=src.kind, model=src.model, binary=src.binary,
                            enabled=True, verified=src.verified, account=label or newname,
                            env=_parse_creds(creds, src.kind))
        config.upsert(clone)
        cfg.save(config)
        print(f"Cloned {agent} → {newname} ({src.kind}). Both can now run in the panel.")
        return 0

    target = config.get(agent)
    if target is None:
        print(f"no agent '{agent}' in the roster (`agentpanel account list`).")
        return 1
    if action == "clear":
        target.env = {}
        target.account = None
        cfg.save(config)
        print(f"Cleared {agent}'s account — back to its default login.")
        return 0
    # set
    creds = _parse_creds(rest, target.kind)
    if not creds:
        print(f"provide a key: `agentpanel account set {agent} <API_KEY>`")
        return 1
    target.env.update(creds)
    if label:
        target.account = label
    cfg.save(config)
    print(f"Switched {agent} to account '{target.account or 'set'}' ({', '.join(creds)}).")
    return 0


def _sessions(repo: str) -> int:
    from .core.store import SessionStore

    records = SessionStore(Path(repo).resolve()).load_all()
    if not records:
        print("No saved sessions for this repo. (They're written under .agentpanel/sessions/.)")
        return 0
    print(f"Saved sessions ({len(records)}) — launch `agentpanel` to resume them:\n")
    for r in records:
        panel = ", ".join(p.name for p in r.panelists) or "(none)"
        print(f"  {r.id}  [{r.status}]  {r.updated[:16]}")
        print(f"      {r.question[:70]}")
        print(f"      panel: {panel}   ·   elected: {r.elected or '—'}")
    return 0


async def _add(names: List[str], directory: str) -> int:
    from .core import ftu

    repo = Path(directory).resolve()
    config = cfg.load() if cfg.config_exists() else None
    label = ", ".join(names) if names else "any available agents"
    print(f"Adding {label} …\n")
    config = await ftu.auto_bootstrap(repo, existing=config, only=(names or None),
                                      emit=lambda m: print(f"  {m}"))
    cfg.save(config)
    panel = [a.name for a in config.panel()]
    print(f"\nPanel now: {', '.join(panel) or '(none ready)'}.")
    return 0


async def _bootstrap(directory: str, do_install: bool, do_login: bool) -> int:
    from .core import ftu

    repo = Path(directory).resolve()
    print(f"AgentPanel cold start in {repo}\n")
    config = await ftu.auto_bootstrap(repo, do_install=do_install, do_login=do_login,
                                      emit=lambda m: print(f"  {m}"))
    cfg.save(config)
    panel = [a.name for a in config.panel()]
    print(f"\nSaved {cfg.GLOBAL_CONFIG}.")
    if len(panel) >= 2:
        print(f"Panel ready: {', '.join(panel)} — launch with `agentpanel`.")
    elif len(panel) == 1:
        print(f"One agent ready ({panel[0]}). Add a second for real deliberation "
              "(re-run bootstrap after installing/logging in another).")
    else:
        print("No agents verified yet. Re-run `agentpanel bootstrap` after resolving the notes above.")
    return 0


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
    # Self-aware + adaptive: bootstrap when there's no config OR the panel is thin
    # (< 2 ready agents). A real panel needs at least two agents to deliberate.
    config = cfg.load() if cfg.config_exists() else None
    # Readiness matches the TUI: an enabled agent counts even if it hasn't (re)passed the
    # verification handshake. Requiring `verified` here made the launcher re-bootstrap and
    # print "no agents" on every launch whenever agents were configured but not re-verified.
    ready = [a.name for a in (config.panel() or config.enabled_agents())] if config else []
    if len(ready) < 2:
        from .core import ftu

        if not config:
            print("No configuration found — bootstrapping AgentPanel…\n")
        elif ready:
            print(f"Panel is thin (only {', '.join(ready)}) — looking for another agent…\n")
        else:
            print("No agents configured yet — looking for agents to bring in…\n")
        config = asyncio.run(
            ftu.auto_bootstrap(Path.cwd(), existing=config, emit=lambda m: print(f"  {m}"))
        )
        cfg.save(config)
        panel = [a.name for a in (config.panel() or config.enabled_agents())]
        if not panel:
            print("\nNo agents are ready yet — resolve the notes above and run `agentpanel` again "
                  "(or `agentpanel --mock` for a demo).")
            return 1
        if len(panel) < 2:
            print(f"\nOnly {panel[0]} is ready. Add a second agent (e.g. log in to Cursor) for "
                  "real deliberation — re-run `agentpanel` to bring it in. Launching with one for now…\n")
        else:
            print(f"\nReady: {', '.join(panel)}. Launching…\n")
    run_tui(config=config)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
