"""Judges: cluster the panel's plans into agreement groups.

The judge is the neutral arbiter of "who agrees with whom" — deliberately separate from
the panelists so consensus can't be won by sycophancy. It is pluggable and chosen in FTU
(see :class:`agentpanel.core.config.JudgeConfig`):

- :class:`DeterministicJudge` — clusters by the explicit ``APPROACH:`` label. Zero-cost,
  fully reproducible; powers the engine's unit tests and the mock panel.
- :class:`NeutralModelJudge` — a dedicated model groups plans semantically (step 5).
- :class:`DesignatedAgentJudge` — one roster agent acts as neutral chair (step 5).

All judges return ``List[Cluster]``; :func:`agentpanel.core.consensus.evaluate` turns
clusters into the verdict.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from typing import Dict, List

from .consensus import Cluster, PlanRecord, extract_label


class Judge(ABC):
    """Groups plans into agreement clusters."""

    @abstractmethod
    async def cluster(self, question: str, plans: List[PlanRecord]) -> List[Cluster]:
        ...


class DeterministicJudge(Judge):
    """Cluster by explicit ``APPROACH:`` label (exact, case-insensitive).

    Plans without a label fall back to a hash of their normalized text, so two identical
    plans still cluster together. This mirrors — deterministically — what the semantic
    judge does fuzzily, which is exactly what the engine tests need.
    """

    async def cluster(self, question: str, plans: List[PlanRecord]) -> List[Cluster]:
        buckets: Dict[str, Cluster] = {}
        for p in plans:
            if not p.responded:
                continue
            label = extract_label(p.text)
            if label:
                key = f"label:{label.lower()}"
                display = label
            else:
                norm = " ".join(p.text.lower().split())
                key = "hash:" + hashlib.sha1(norm.encode()).hexdigest()[:10]
                display = (p.text.strip().splitlines() or ["(empty)"])[0][:60]
            cluster = buckets.get(key)
            if cluster is None:
                cluster = Cluster(key=key, label=display, representative=p.agent)
                buckets[key] = cluster
            cluster.members.append(p.agent)
            # Representative = highest-fitness member (best plan to show for the cluster).
            if p.fit > _fit_of(plans, cluster.representative):
                cluster.representative = p.agent
        return list(buckets.values())


def _fit_of(plans: List[PlanRecord], agent: str) -> float:
    return next((p.fit for p in plans if p.agent == agent), 0.0)


def build_judge(judge_config, roster_adapters: Dict[str, object] | None = None) -> Judge:
    """Construct the configured judge.

    Falls back to the deterministic judge when the configured backend isn't available yet
    (e.g. model judges arrive in step 5), so the engine always has a working judge.
    """
    backend = getattr(judge_config, "backend", "neutral_model")
    try:
        if backend == "neutral_model":
            from .judges_model import NeutralModelJudge  # lazy: optional anthropic dep

            return NeutralModelJudge(model=getattr(judge_config, "model", None))
        if backend == "designated_agent":
            from .judges_model import DesignatedAgentJudge

            agent_name = getattr(judge_config, "agent", None)
            if roster_adapters and agent_name in roster_adapters:
                return DesignatedAgentJudge(roster_adapters[agent_name])
    except Exception:
        pass
    return DeterministicJudge()
