"""AgentPanel — a local multi-agent control plane.

A panel of coding agents (Claude Code, Cursor, Codex, ...) each plan a request in
isolation, then deliberate in synchronized turns over a shared repo until they reach
consensus (or escalate to the user). The winner executes in its own git worktree.

The ``core`` package is UI-agnostic: the engine emits a typed event stream that any
client (the bundled Textual TUI today, an IDE/web frontend later) can subscribe to.
"""

__version__ = "0.1.0"
