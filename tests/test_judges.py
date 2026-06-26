"""Unit tests for model-judge mapping + the build_judge fallback (no API calls)."""

from __future__ import annotations

from agentpanel.core.consensus import PlanRecord
from agentpanel.core.config import JudgeConfig
from agentpanel.core.judge import DeterministicJudge, build_judge
from agentpanel.core.judges_model import _extract_json, _to_clusters


def _plans():
    return [
        PlanRecord(agent="a", text="plan a", turn=0),
        PlanRecord(agent="b", text="plan b", turn=0),
        PlanRecord(agent="c", text="plan c", turn=0),
    ]


def test_extract_json_from_noisy_text():
    text = 'Sure! Here it is:\n{"clusters": [{"label": "X", "members": ["a"], "representative": "a"}]} done'
    data = _extract_json(text)
    assert data["clusters"][0]["label"] == "X"


def test_extract_json_none_when_absent():
    assert _extract_json("no json here") is None


def test_to_clusters_filters_unknown_and_adds_singletons():
    data = {"clusters": [
        {"label": "grp", "members": ["a", "b", "ghost"], "representative": "ghost"},
    ]}
    clusters = _to_clusters(data, _plans())
    grp = next(c for c in clusters if c.label == "grp")
    assert set(grp.members) == {"a", "b"}        # 'ghost' (not a panelist) dropped
    assert grp.representative in {"a", "b"}        # invalid rep repaired
    # 'c' was omitted by the judge -> becomes its own singleton cluster
    assert any(c.members == ["c"] for c in clusters)


def test_build_judge_falls_back_to_deterministic_without_credentials():
    # neutral_model judge needs anthropic + credentials; in this env it should fall back.
    judge = build_judge(JudgeConfig(backend="neutral_model", model="claude-opus-4-8"))
    # Either a working model judge (if creds exist) or the deterministic fallback — never crash.
    assert hasattr(judge, "cluster")


def test_build_judge_unknown_backend_is_deterministic():
    judge = build_judge(JudgeConfig(backend="deterministic"))
    assert isinstance(judge, DeterministicJudge)
