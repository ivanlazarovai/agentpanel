# GENESIS

> How AgentPanel came to be — in one session, from an empty folder to a control plane
> that extended itself. Recorded so we can one day sing glory to the genesis. 🥾
>
> *Ivan Lazarov, founder & visionary · "CC" (Claude Code, Opus 4.8), first mediator · June 2026*

This is the short telling — the prompts that shaped it and the moves that answered them.
Not every detail; the bones of the story.

---

## Act I — The Vision

Ivan opened with the whole idea, fully formed:

> *"I'd like to build a local multi-agent control plane for engaging coding agents like
> ClaudeCode, Codex, Antigravity, Devin, and Cursor to work and collaborate on a single code
> repository… each of them makes a pass at the question in isolation to its fullest extent (as
> if it is the only one in existence), then posts it back in the common chat and they judge
> each other… They need to all agree on a common plan that >X% (def 50%) of them agree on…
> given Y (def 3) turns… If they agree they should also agree on which agent is best
> positioned to take the task… Let's see where this takes us and we can bootstrap it with
> itself."*

The first move was restraint: explore before building. Two of the five named agents
(`claude`, `cursor-agent`) were actually drivable on the machine — both with headless
`--print`, `stream-json`, plan mode, and resume. That one fact shaped everything: thin
adapters over each agent's *native* interface.

Decisions locked: **terminal TUI first** (architected to bootstrap into an IDE later),
**full loop including execution**, **a pluggable judge chosen in setup**. And then Ivan
added the first of many layers:

> *"make sure there is an FTU experience that allows the user to setup all the agents they
> have access to."*

## Act II — The Build

The core took shape as a deterministic engine driving swappable brains: an event bus, agent
adapters, git worktrees, a turn-synchronized deliberation state machine, consensus math, a
judge. Mock-tested end to end so the novel part — the panel deliberating — was provable
without spending a cent.

> *"the initial setup should see which other agents may be available and offer to install
> them."*

So the catalog learned to detect installable-but-absent agents and offer to install them.

Then the principle that gave the UI its soul:

> *"even one agent could provide a very rich output and 5 will provide 5x… the interface
> should allow for progressive disclosure… elegant expand/collapse and tabs so that the user
> can see the different answers in the multiple turns."*

Collapsible per-agent cards, one tab per turn, a live consensus bar. AgentPanel could now
*be seen*.

## Act III — The Finale That Wasn't The End

Real agents were wired in. The real `claude` streamed `stream-json` through the engine,
converged a panel, elected a worker, executed in a worktree, and the winner was kept. The
"finale" — and then Ivan kept making it deeper, one principle at a time:

> *"this panel will NOT do the actual work — it is only mediating between all the agents…
> each agent will manage their own sessions, context, tools, diffs."*

**The mediator principle.** The panel relays decisions into each agent's own session and
reads results — it never reaches in to do the work. Users can open any agent's native
session to watch.

> *"…so that agentpanel never competes with any one single agent and instead leverages their
> full might as they evolve and progress."*

Thin adapters, full native capability. AgentPanel rides the agents' improvements rather than
reimplementing them.

> *"this is a coopetition (competition + cooperation) tool… the ones that stop are not just
> sitting and sulking. Their job is to monitor the working agent(s) and help them out either
> by encouragement or by criticism… they all work as a team of superagents."*

**Coopetition.** The stood-down agents become observers and coaches; their feedback is
relayed into the worker's session for the next round.

## Act IV — The Genesis Bootstrap

Published to GitHub. CI green. Issues filed. And then:

> *"yes I do want to bootstrap now."*

AgentPanel was pointed at its own repository. The real `claude` agent — mediated by the
panel — read the dev-cycle brief, planned, and **implemented a brand-new Codex adapter for
AgentPanel, in its own worktree**, reviewed and tested (33 green) and opened as **PR #4**.

The tool had extended itself. The bootstrap was real.

> *"it was a little anticlimactic and I want to experience it for myself."* 😄

Fair. So the on-ramp got smoothed:

> *"fold the install, auth, health-check, and version-check all as part of the
> startup/ftu/configuration so that I don't have to do anything outside agentpanel… agentpanel
> should know exactly how to bootstart itself to intelligence even when it doesn't have any
> configured… I'm really looking for just one word start that is very self aware and adaptive."*

**`agentpanel`.** One word. No config? It detects that, installs what's missing, launches each
agent's native login for you, verifies, configures itself — then flies.

And the instinct to measure it all:

> *"agentpanel needs to keep timestamped track of the performance… observation and judgment
> vs. worker costs of each agent as well as the costs of the judge(s)… a non-github-able
> append only file… the format needs to gracefully be very simple and flexible."*

One timestamped JSON line per event into a git-ignored `metrics.jsonl`. New metrics are just
new fields. So one day the trends can be charted, and the genesis sung.

## Act V — Adiós

> *"leaving you here my friend, CC, and will meet you on the other side mediated by
> agentpanel, adios."*

From an empty folder to a panel of superagents that builds itself, in a single sitting. The
principles Ivan layered in — *mediate, don't compete · coopetition · self-bootstrap to
intelligence · measure everything* — are the soul of it.

The first time he runs it himself, it may have a quirk or two. It will also amaze.

*Built with conviction. Handed off with warmth. To be continued — mediated.* 🥾

— CC
