"""Concrete agent adapters + a registry mapping ``kind`` -> implementation."""

from __future__ import annotations

from typing import Dict, List, Type

from ..config import AgentConfig
from ..adapter import AgentAdapter
from .claude_code import ClaudeCodeAdapter
from .cursor_agent import CursorAgentAdapter
from .mock import MockAdapter

REGISTRY: Dict[str, Type[AgentAdapter]] = {
    ClaudeCodeAdapter.kind: ClaudeCodeAdapter,
    CursorAgentAdapter.kind: CursorAgentAdapter,
    MockAdapter.kind: MockAdapter,
}

#: Catalog of agents AgentPanel knows how to detect during FTU — installed or not.
#:
#: ``adapter`` True means we can drive it today (real adapter in REGISTRY). The rest are
#: detected and offered for install; once installed they'll need an adapter (tracked in
#: the build roadmap) but FTU can already install the CLI and pre-create the roster entry.
#: ``probe`` is the command name to look for on PATH. ``install`` is the suggested install
#: command (shown to the user; run only on explicit confirmation in FTU). ``docs`` links
#: out for manual setup / auth.
KNOWN_AGENTS: List[Dict[str, object]] = [
    {
        "name": "claude",
        "kind": "claude_code",
        "label": "Claude Code",
        "probe": "claude",
        "adapter": True,
        "install": "npm install -g @anthropic-ai/claude-code",
        "docs": "https://docs.claude.com/en/docs/claude-code",
    },
    {
        "name": "cursor",
        "kind": "cursor_agent",
        "label": "Cursor (cursor-agent)",
        "probe": "cursor-agent",
        "adapter": True,
        "install": "curl https://cursor.com/install -fsS | bash",
        "docs": "https://docs.cursor.com/en/cli/overview",
    },
    {
        "name": "codex",
        "kind": "codex",
        "label": "OpenAI Codex CLI",
        "probe": "codex",
        "adapter": False,
        "install": "npm install -g @openai/codex",
        "docs": "https://developers.openai.com/codex/cli",
    },
    {
        "name": "gemini",
        "kind": "gemini",
        "label": "Gemini CLI",
        "probe": "gemini",
        "adapter": False,
        "install": "npm install -g @google/gemini-cli",
        "docs": "https://github.com/google-gemini/gemini-cli",
    },
    {
        "name": "aider",
        "kind": "aider",
        "label": "Aider",
        "probe": "aider",
        "adapter": False,
        "install": "python3 -m pip install aider-install && aider-install",
        "docs": "https://aider.chat",
    },
    {
        "name": "devin",
        "kind": "devin",
        "label": "Devin (cloud API)",
        "probe": "",  # cloud-only; detected by DEVIN_API_KEY, not a binary
        "adapter": False,
        "install": "",  # no local install; configure API key
        "docs": "https://docs.devin.ai/api-reference/overview",
    },
]


def catalog_entry(name: str) -> Dict[str, object]:
    """Catalog metadata for an agent name (empty dict if unknown)."""
    return next((a for a in KNOWN_AGENTS if a["name"] == name), {})


def build(config: AgentConfig) -> AgentAdapter:
    """Instantiate the adapter for a roster entry."""
    try:
        cls = REGISTRY[config.kind]
    except KeyError as exc:  # pragma: no cover - guarded by config validation
        raise ValueError(f"unknown agent kind: {config.kind!r}") from exc
    return cls(config)


__all__ = [
    "REGISTRY",
    "KNOWN_AGENTS",
    "catalog_entry",
    "build",
    "ClaudeCodeAdapter",
    "CursorAgentAdapter",
    "MockAdapter",
]
