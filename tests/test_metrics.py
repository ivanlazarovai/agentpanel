"""Metrics: append-only JSONL capture of performance + cost over a session."""

from __future__ import annotations

import pytest

from agentpanel.core.metrics import MetricsRecorder, MetricsSink, repo_metrics_path
from tests.conftest import make_config, mock_agent


def test_sink_is_append_only_jsonl(tmp_path):
    sink = MetricsSink(tmp_path / "m.jsonl")
    sink.write({"ts": "t1", "event": "a", "cost_usd": 0.01})
    sink.write({"ts": "t2", "event": "b"})
    rows = list(sink.read())
    assert [r["event"] for r in rows] == ["a", "b"]
    # second open appends, doesn't truncate
    sink.write({"ts": "t3", "event": "c"})
    assert len(list(sink.read())) == 3


def test_recorder_trims_and_stamps(tmp_path):
    from agentpanel.core.events import EventBus, EventKind

    sink = MetricsSink(tmp_path / "m.jsonl")
    bus = EventBus()
    MetricsRecorder(sink, "s001", clock=lambda: "TS").register(bus)
    bus.publish(EventKind.PANELIST_TOKEN, agent="claude", text="x" * 999)  # noisy -> skipped
    bus.publish(EventKind.PANELIST_DONE, agent="claude", mode="plan", turn=0,
                cost_usd=0.05, duration_ms=1200, summary="y" * 999)
    rows = list(sink.read())
    assert len(rows) == 1  # token event not recorded
    r = rows[0]
    assert r["ts"] == "TS" and r["session"] == "s001" and r["event"] == "panelist_done"
    assert r["cost_usd"] == 0.05 and r["duration_ms"] == 1200
    assert r["summary"].endswith("…")  # long free-text trimmed


@pytest.mark.asyncio
async def test_session_writes_metrics_for_a_mock_run(tmp_path):
    from agentpanel.core.session import SessionManager

    # use a repo dir so metrics land under <repo>/.agentpanel/metrics.jsonl
    cfg = make_config([mock_agent("a", plan="A"), mock_agent("b", plan="A")], threshold=0.5)
    mgr = SessionManager(cfg)
    session = mgr.create("q", repo=tmp_path, use_worktrees=False)
    await session.run()

    sink = MetricsSink(repo_metrics_path(tmp_path))
    events = {r["event"] for r in sink.read()}
    assert "panelist_done" in events
    assert "judge" in events
    assert "converged" in events
    # durations are captured for each panelist
    done = [r for r in sink.read() if r["event"] == "panelist_done"]
    assert all("duration_ms" in r for r in done)
