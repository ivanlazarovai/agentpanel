"""Codex adapter: argv construction + JSONL event parsing (no real CLI)."""

from __future__ import annotations

from pathlib import Path

from agentpanel.core.adapters.codex import CodexAdapter
from agentpanel.core.config import AgentConfig
from agentpanel.core.adapter import RunContext


def _adapter() -> CodexAdapter:
    return CodexAdapter(AgentConfig(name="codex", kind="codex"))


def test_args_sandbox_by_mode():
    a = _adapter()
    ctx = RunContext(workdir=Path("/tmp"), model="gpt-x")
    plan = a._args("plan", "do x", ctx)
    assert "exec" in plan and "--json" in plan
    assert plan[plan.index("--sandbox") + 1] == "read-only"
    assert plan[-1] == "do x" and plan[plan.index("--model") + 1] == "gpt-x"
    execute = a._args("execute", "ship it", ctx)
    assert execute[execute.index("--sandbox") + 1] == "workspace-write"


def test_parse_stream_extracts_text_session_and_usage():
    a = _adapter()
    # thread.started -> session ref (meta, not rendered)
    meta = a._parse_line({"type": "thread.started", "thread_id": "abc-123"})
    assert meta[0].type == "meta" and meta[0].session_ref == "abc-123"
    # agent_message -> token text
    tok = a._parse_line({"type": "item.completed",
                         "item": {"type": "agent_message", "text": "the plan"}})
    assert tok[0].type == "token" and tok[0].text == "the plan"
    # command_execution -> tool event
    tool = a._parse_line({"type": "item.completed",
                          "item": {"type": "command_execution", "command": "ls -la"}})
    assert tool[0].type == "tool" and tool[0].tool == "shell"
    # turn.completed -> done with token usage
    done = a._parse_line({"type": "turn.completed",
                          "usage": {"input_tokens": 10, "output_tokens": 3}})
    assert done[0].type == "done" and done[0].tokens == {"input": 10, "output": 3}


def test_parse_error_event():
    a = _adapter()
    ev = a._parse_line({"type": "turn.failed", "message": "model overloaded"})
    assert ev[0].type == "error" and "overloaded" in ev[0].detail


def test_codex_is_registered_and_buildable():
    from agentpanel.core.adapters import REGISTRY, build

    assert REGISTRY["codex"] is CodexAdapter
    assert isinstance(build(AgentConfig(name="codex", kind="codex")), CodexAdapter)
