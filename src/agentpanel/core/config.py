"""Roster + settings persistence.

Two scopes:
- **Global** (``~/.agentpanel/config.toml``): the agent roster, judge choice, and
  deliberation defaults (X%, Y turns, barrier timeout). Written by the FTU wizard.
- **Per-repo** (``<repo>/.agentpanel/``): worktrees, session state, and the shared
  dev-cycle brief — anything tied to one codebase.

Config is plain dataclasses round-tripped through TOML so it stays hand-editable.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

try:  # Python 3.11+
    import tomllib as _toml_read
except ModuleNotFoundError:  # pragma: no cover - exercised on 3.9/3.10
    import tomli as _toml_read  # type: ignore

import tomli_w

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

GLOBAL_DIR = Path(os.environ.get("AGENTPANEL_HOME", Path.home() / ".agentpanel"))
GLOBAL_CONFIG = GLOBAL_DIR / "config.toml"
REPO_DIR_NAME = ".agentpanel"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclass
class AgentConfig:
    """One panelist's configuration.

    ``kind`` selects the adapter implementation (``claude_code``, ``cursor_agent``,
    ``mock``, ...). ``binary``/``model`` are optional overrides; adapters fall back to
    their own defaults. ``enabled`` agents that pass FTU verification join the panel.
    """

    name: str  # display + worktree id, e.g. "claude" or "cursor"
    kind: str  # adapter key
    binary: Optional[str] = None  # path override; None -> adapter default
    model: Optional[str] = None
    enabled: bool = True
    verified: bool = False  # passed the FTU dev-cycle handshake
    weight: float = 1.0  # vote weight in consensus (default equal)
    extra_args: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AgentConfig":
        known = {f: d[f] for f in cls.__dataclass_fields__ if f in d}  # type: ignore[attr-defined]
        return cls(**known)


@dataclass
class JudgeConfig:
    """Who arbitrates consensus. Chosen in FTU; never hardcoded.

    - ``backend="neutral_model"``: a dedicated model (``model``) judges via the
      Anthropic API. Independent of the panelists.
    - ``backend="designated_agent"``: one roster agent (``agent``) acts as neutral chair.
    """

    backend: str = "neutral_model"  # "neutral_model" | "designated_agent"
    model: Optional[str] = None  # for neutral_model
    agent: Optional[str] = None  # for designated_agent

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "JudgeConfig":
        known = {f: d[f] for f in cls.__dataclass_fields__ if f in d}  # type: ignore[attr-defined]
        return cls(**known)


@dataclass
class Settings:
    """Deliberation defaults. ``X`` and ``Y`` from the spec live here."""

    consensus_threshold: float = 0.5  # X: fraction that must agree (0..1)
    max_turns: int = 3  # Y: critique turns before escalation
    barrier_timeout_s: float = 120.0  # per-turn deadline for slow/dead agents
    escalation_top_n: int = 3  # options shown to the user on non-convergence

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Settings":
        known = {f: d[f] for f in cls.__dataclass_fields__ if f in d}  # type: ignore[attr-defined]
        return cls(**known)


@dataclass
class Config:
    """Top-level global config: roster + judge + settings."""

    roster: List[AgentConfig] = field(default_factory=list)
    judge: JudgeConfig = field(default_factory=JudgeConfig)
    settings: Settings = field(default_factory=Settings)
    repo: Optional[str] = None  # last-bound repo path (convenience)

    # -- roster helpers ----------------------------------------------------

    def enabled_agents(self) -> List[AgentConfig]:
        return [a for a in self.roster if a.enabled]

    def panel(self) -> List[AgentConfig]:
        """Agents that actually deliberate: enabled and verified."""
        return [a for a in self.roster if a.enabled and a.verified]

    def get(self, name: str) -> Optional[AgentConfig]:
        return next((a for a in self.roster if a.name == name), None)

    def upsert(self, agent: AgentConfig) -> None:
        for i, a in enumerate(self.roster):
            if a.name == agent.name:
                self.roster[i] = agent
                return
        self.roster.append(agent)

    # -- serialization -----------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "roster": [a.to_dict() for a in self.roster],
            "judge": self.judge.to_dict(),
            "settings": self.settings.to_dict(),
            "repo": self.repo,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Config":
        return cls(
            roster=[AgentConfig.from_dict(a) for a in d.get("roster", [])],
            judge=JudgeConfig.from_dict(d.get("judge", {})),
            settings=Settings.from_dict(d.get("settings", {})),
            repo=d.get("repo"),
        )


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------


def config_exists() -> bool:
    """True if a global config has been written (i.e. FTU has run)."""
    return GLOBAL_CONFIG.exists()


def load(path: Optional[Path] = None) -> Config:
    """Load global config, or return defaults if none exists yet."""
    path = path or GLOBAL_CONFIG
    if not path.exists():
        return Config()
    with path.open("rb") as fh:
        return Config.from_dict(_toml_read.load(fh))


def save(config: Config, path: Optional[Path] = None) -> Path:
    """Persist global config, creating the directory tree as needed."""
    path = path or GLOBAL_CONFIG
    path.parent.mkdir(parents=True, exist_ok=True)
    # tomli_w rejects None; drop null keys recursively before dumping.
    payload = _strip_none(config.to_dict())
    with path.open("wb") as fh:
        tomli_w.dump(payload, fh)
    return path


def repo_dir(repo: Path) -> Path:
    """The per-repo AgentPanel directory (created on demand)."""
    d = Path(repo) / REPO_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _strip_none(obj: Any) -> Any:
    """Recursively remove keys whose value is None (TOML has no null)."""
    if isinstance(obj, dict):
        return {k: _strip_none(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_strip_none(v) for v in obj]
    return obj
