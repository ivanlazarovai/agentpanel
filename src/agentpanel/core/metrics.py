"""Append-only, timestamped metrics — performance + cost per agent over time.

Every meaningful event (a panelist finishing a plan/critique, a proceed/stand-down
decision, an observation, a judge clustering, an execution + diff, an outcome) is appended
as **one JSON object per line** to ``<repo>/.agentpanel/metrics.jsonl`` — which is
git-ignored (non-shareable by default) and never rewritten.

The format is deliberately minimal and flexible: each line is ``{ts, session, event, ...}``
where the trailing fields are just that event's data. New metrics need no schema change —
add a field to an event and it shows up in the log. That keeps it trivial to trend/chart
later (cost per agent, win-rate when elected, observer vs worker spend, judge cost, …).

The recorder is a synchronous tap on the :class:`~agentpanel.core.events.EventBus` (see
``add_listener``), so it captures the whole lifecycle without its own task, and a logging
error can never break a run.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from .events import Event, EventKind

# Events worth keeping for trends. Token streams are intentionally excluded (too noisy);
# everything here is a discrete, chartable fact about performance, cost, or outcome.
RECORDED_KINDS = {
    EventKind.RED_TEAM,
    EventKind.PANELIST_DONE,
    EventKind.PANELIST_ERROR,
    EventKind.PANELIST_TIMEOUT,
    EventKind.DECISION,
    EventKind.OBSERVATION,
    EventKind.CONSENSUS_COMPUTED,
    EventKind.CONVERGED,
    EventKind.ESCALATED,
    EventKind.EXECUTION_DONE,
    EventKind.DIFF_READY,
    EventKind.JUDGE,
    EventKind.PERMISSION_REQUEST,
    EventKind.PERMISSION_DECISION,
}

# Free-text fields trimmed so the log stays a metrics log, not a transcript.
_TRIM_FIELDS = ("plan", "text", "summary", "reason", "message", "diffstat")
_TRIM_LEN = 200


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class MetricsSink:
    """Appends one JSON record per line. Never truncates or rewrites."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, record: Dict[str, Any]) -> None:
        line = json.dumps(record, separators=(",", ":"), default=str)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def read(self) -> Iterable[Dict[str, Any]]:
        """Yield records back (for trending/charting / tests)."""
        if not self.path.exists():
            return
        for raw in self.path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if raw:
                yield json.loads(raw)


class MetricsRecorder:
    """Maps events to metric records and appends them via a :class:`MetricsSink`."""

    def __init__(self, sink: MetricsSink, session_id: str, clock=_now_iso) -> None:
        self.sink = sink
        self.session_id = session_id
        self.clock = clock

    def register(self, bus) -> None:
        bus.add_listener(self.handle)

    def handle(self, event: Event) -> None:
        if event.kind not in RECORDED_KINDS:
            return
        record = {
            "ts": self.clock(),
            "session": self.session_id,
            "event": event.kind.value,
            "seq": event.seq,
        }
        for key, value in event.data.items():
            if isinstance(value, str) and key in _TRIM_FIELDS and len(value) > _TRIM_LEN:
                value = value[:_TRIM_LEN] + "…"
            record[key] = value
        self.sink.write(record)


def repo_metrics_path(repo: Optional[Path]) -> Path:
    """Where a session's metrics live: per-repo if we have one (git-ignored under
    ``.agentpanel/``), else a global fallback under the AgentPanel home."""
    if repo is not None:
        return Path(repo) / ".agentpanel" / "metrics.jsonl"
    from .config import GLOBAL_DIR

    return GLOBAL_DIR / "metrics.jsonl"
