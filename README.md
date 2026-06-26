# AgentPanel

[![CI](https://github.com/ivanlazarovai/agentpanel/actions/workflows/ci.yml/badge.svg)](https://github.com/ivanlazarovai/agentpanel/actions/workflows/ci.yml)

A **local multi-agent control plane**. Convene a *panel* of coding agents (Claude Code,
Cursor, Codex, …) on one shared repo. Ask them all the same thing; each plans it **in
isolation**, then they **deliberate in synchronized turns** — judging where they agree and
disagree — until a configurable share (**X%**) agree, or they run out of turns (**Y**) and
**escalate to you** with the top options. On convergence they elect the **best-positioned**
agent to execute; each agent works in **its own git worktree**, so you diff the results and
keep the winner. Multiple panel sessions run in parallel.

## Core principle: a mediator, not a worker

AgentPanel **never does the work and never competes with any single agent.** It brokers the
conversation — broadcasts the request, relays every plan to every other agent, computes
consensus, conveys the user's choices/overrides, and tells each agent's session to **proceed**
(selected) or **stand down**. Each agent keeps its own session, context, tools, memory, diffs,
and commits, and runs at its **full native capability** — so the panel automatically benefits
as the agents improve. You can **open any agent's native session** to watch the real work
(`AGENT_SESSION` events surface a `claude --resume <id>` / `cursor-agent --resume <id>`
command); inside it you'll see the panel's mediated input and the proceed/stand-down decision.

**Coopetition (competition + cooperation).** When agent(s) are selected to implement, the
ones that stood down don't go idle — they **observe and coach** the worker(s), feeding back
encouragement and criticism that the panel relays into the worker's own session for the next
round (`agentpanel ask --execute --review N`). One team of superagents, held to a high bar.

## Status

The UI-agnostic **core engine** is built and tested end to end:

| Area | State |
| --- | --- |
| Event bus (UI-agnostic contract) | ✅ |
| Config + roster persistence (TOML) | ✅ |
| Adapters: Claude Code, Cursor, mock | ✅ (health/plan/critique/execute wired) |
| Agent detection + install catalog | ✅ (`agentpanel doctor`) |
| Git worktree manager (create/diff/commit/keep/cleanup) | ✅ tested |
| Deliberation state machine (isolated → critique turns → consensus/escalate → elect) | ✅ tested |
| Consensus math + deterministic judge | ✅ tested |
| Headless runner + terminal renderer (`agentpanel ask`) | ✅ |
| Textual TUI (progressive disclosure: collapsible cards + per-turn tabs + parallel session tabs) | ✅ (`docs/tui-demo.svg`) |
| FTU wizard (detect → install absent → configure → judge/X/Y → write brief → verify) | ✅ (`docs/ftu-demo.svg`) |
| Model-backed judge (neutral Anthropic-API + designated-chair-agent, deterministic fallback) | ✅ |
| Real agents end-to-end (Claude streamed live through the engine) + execution → diff → keep | ✅ |

Validated against the real `claude` CLI: it plans in its worktree, streams `stream-json`
through the engine, the panel converges/escalates, the elected agent executes, and
`keep` merges the winner. (`cursor-agent` is detected but needs `cursor-agent login` to
join a panel — exactly what the FTU verification handshake checks for.)

## Try it (no agents, no cost)

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/agentpanel                        # one word: self-bootstraps on first run, then launches
.venv/bin/agentpanel bootstrap              # explicit cold start: install + log in + verify, then configure
.venv/bin/agentpanel doctor                 # which agents are installed / installable
.venv/bin/agentpanel --mock                 # launch the TUI with a built-in mock panel
.venv/bin/agentpanel ask --mock "Design the session persistence layer"   # headless
.venv/bin/agentpanel ask --mock --execute --keep claude "..."            # full loop incl. execution
.venv/bin/pytest -q                          # 31 tests, deterministic
```

Both `--mock` modes run a built-in panel that starts split and converges over turns —
a faithful dry run of the real flow. The TUI (`docs/tui-demo.svg`) shows collapsible
per-agent cards (collapsed = one-line summary; expand for the full plan) with one tab per
deliberation turn, a live consensus bar, and a tab per parallel session.

## Architecture

```
src/agentpanel/
  core/            # UI-agnostic engine — emits a typed event stream
    events.py        # pub/sub bus (the contract every client subscribes to)
    config.py        # roster + settings (X%, Y, judge) persisted as TOML
    adapter.py       # AgentAdapter ABC + CLI subprocess/stream-json base
    adapters/        # claude_code, cursor_agent, mock (+ install catalog)
    worktree.py      # per-(session, agent) git worktrees
    consensus.py     # agreement %, clustering result, best-positioned election
    judge.py         # Judge ABC + deterministic judge (model judges = next)
    deliberation.py  # the turn-synchronized state machine
    session.py       # Session + SessionManager (parallel sessions)
    ftu.py           # first-run ops: detect/install/write-brief/verify/assemble-config
  shared/dev_cycle.md  # the brief every agent reads before joining a panel
  tui/
    app.py           # AgentPanelApp — session tabs + progressive-disclosure panels
    session_view.py  # collapsible per-agent cards with per-turn tabs
    ftu_screen.py    # the setup wizard
  render.py          # headless terminal client
  cli.py             # agentpanel doctor | setup | ask
```

The TUI (and a later IDE/web frontend) are just additional subscribers to the same event
bus — the engine never imports a UI. A **metrics recorder** is another such subscriber: it
appends one timestamped JSON line per event (panelist done, judge, decision, observation,
execution, outcome) to `<repo>/.agentpanel/metrics.jsonl` — git-ignored, append-only, with
per-agent `duration_ms`/`cost_usd`/role so you can trend performance and cost over time. The
format is deliberately minimal and flexible: new metrics are just new fields.

See `/Users/ivolazy/.claude/plans/wise-purring-shore.md` for the full plan.
