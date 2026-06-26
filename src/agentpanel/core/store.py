"""Session persistence — pick up where you left off after a restart.

AgentPanel doesn't need to store the whole transcript: each agent keeps its *own* native
session on disk (Claude/Cursor/Codex resume by id). So we persist a compact **session
record** per panel session — the question, outcome, and crucially each panelist's
``session_ref`` (its native resume handle) — to ``<repo>/.agentpanel/sessions/<id>.json``
(git-ignored). On restart we reload the records, and resuming a session reattaches each
agent to its own session via that handle. If an agent's native session has since been
purged, it simply starts fresh — but the panel state (plans, decisions) is still here.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class PanelistRecord:
    name: str
    kind: str
    session_ref: Optional[str] = None  # the agent's own native resume handle
    plan: str = ""
    fit: float = 0.5


@dataclass
class SessionRecord:
    id: str
    question: str
    repo: Optional[str]
    status: str = "pending"
    created: str = field(default_factory=_now)
    updated: str = field(default_factory=_now)
    elected: Optional[str] = None
    plan: str = ""
    options: List[Dict[str, Any]] = field(default_factory=list)
    panelists: List[PanelistRecord] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SessionRecord":
        panelists = [PanelistRecord(**p) for p in d.get("panelists", [])]
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d and k != "panelists"}  # type: ignore[attr-defined]
        return cls(panelists=panelists, **known)


class SessionStore:
    """Stores one JSON file per session under ``<repo>/.agentpanel/sessions/``."""

    def __init__(self, repo: Path) -> None:
        self.dir = Path(repo) / ".agentpanel" / "sessions"

    def _ensure(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)

    def save(self, record: SessionRecord) -> Path:
        self._ensure()
        record.updated = _now()
        path = self.dir / f"{record.id}.json"
        path.write_text(json.dumps(record.to_dict(), indent=2), encoding="utf-8")
        return path

    def load(self, sid: str) -> Optional[SessionRecord]:
        path = self.dir / f"{sid}.json"
        if not path.exists():
            return None
        return SessionRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def load_all(self) -> List[SessionRecord]:
        if not self.dir.exists():
            return []
        records = []
        for path in self.dir.glob("*.json"):
            try:
                records.append(SessionRecord.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except (json.JSONDecodeError, OSError, TypeError):
                continue
        return sorted(records, key=lambda r: r.updated, reverse=True)

    def delete(self, sid: str) -> None:
        path = self.dir / f"{sid}.json"
        if path.exists():
            path.unlink()
