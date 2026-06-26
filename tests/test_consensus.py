"""Unit tests for the consensus math (judge-agnostic)."""

from __future__ import annotations

import asyncio

from agentpanel.core.consensus import (
    PlanRecord,
    evaluate,
    extract_fit,
    extract_label,
    top_options,
)
from agentpanel.core.judge import DeterministicJudge


def _plans(*specs):
    """specs: (agent, label, fit, weight, failed)"""
    out = []
    for agent, label, fit, weight, failed in specs:
        text = f"APPROACH: {label}\nbody\nFIT: {fit}\n" if not failed else ""
        out.append(PlanRecord(agent=agent, text=text, turn=0, fit=fit, weight=weight, failed=failed))
    return out


def _cluster_and_eval(plans, threshold):
    clusters = asyncio.run(DeterministicJudge().cluster("q", plans))
    return evaluate(plans, clusters, threshold, turn=0)


def test_extract_helpers():
    assert extract_fit("FIT: 0.9") == 0.9
    assert extract_fit("no fit here", default=0.4) == 0.4
    assert extract_fit("FIT: 9.0") == 1.0  # clamped
    assert extract_label("APPROACH: Use Redis\nrest") == "Use Redis"
    assert extract_label("no label") is None


def test_unanimous_converges():
    plans = _plans(("a", "A", 0.5, 1, False), ("b", "A", 0.7, 1, False), ("c", "A", 0.6, 1, False))
    r = _cluster_and_eval(plans, threshold=0.5)
    assert r.converged
    assert r.agreement == 1.0
    assert r.elected == "b"  # highest fit
    assert r.dissenters == []


def test_split_below_threshold_does_not_converge():
    plans = _plans(("a", "A", 0.5, 1, False), ("b", "B", 0.5, 1, False))
    r = _cluster_and_eval(plans, threshold=0.75)
    assert not r.converged
    assert r.agreement == 0.5
    assert len(r.clusters) == 2


def test_weighted_majority():
    # b has weight 3 -> its cluster dominates even though outnumbered by count.
    plans = _plans(("a", "A", 0.5, 1, False), ("b", "B", 0.9, 3, False), ("c", "A", 0.5, 1, False))
    r = _cluster_and_eval(plans, threshold=0.5)
    assert r.converged
    assert r.leading.label == "B"
    assert r.elected == "b"


def test_failed_agents_excluded_from_total():
    # c failed -> denominator is only a+b; they agree -> converged at 100%.
    plans = _plans(("a", "A", 0.5, 1, False), ("b", "A", 0.5, 1, False), ("c", "B", 0.5, 1, True))
    r = _cluster_and_eval(plans, threshold=0.9)
    assert r.converged
    assert r.agreement == 1.0
    assert "c" in r.silent
    assert r.responders == 2


def test_top_options_orders_by_weight():
    plans = _plans(
        ("a", "A", 0.5, 1, False), ("b", "A", 0.6, 1, False),
        ("c", "B", 0.9, 1, False), ("d", "C", 0.4, 1, False),
    )
    clusters = asyncio.run(DeterministicJudge().cluster("q", plans))
    r = evaluate(plans, clusters, 0.9, 0)
    options = top_options(plans, r.clusters, n=3)
    assert options[0]["label"] == "A"  # weight 2, first
    assert {o["label"] for o in options} == {"A", "B", "C"}
    assert options[0]["backers"] == ["a", "b"]
