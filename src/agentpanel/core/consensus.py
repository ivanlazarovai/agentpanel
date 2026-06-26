"""Consensus math: turn clusters of plans into a verdict.

The *judge* (see :mod:`agentpanel.core.judge`) groups the panel's plans into agreement
**clusters**. This module is the deterministic part: given those clusters and per-agent
weights, it computes the agreement fraction, decides whether the threshold X is met, and
elects the best-positioned agent to execute. Kept judge-agnostic so the same logic backs
the deterministic test judge and the real semantic judge.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class PlanRecord:
    """One agent's current plan (latest turn)."""

    agent: str
    text: str
    turn: int
    session_ref: Optional[str] = None
    fit: float = 0.5  # self-reported fitness to execute this plan (0..1)
    weight: float = 1.0  # vote weight in consensus
    failed: bool = False  # agent errored or timed out this turn

    @property
    def responded(self) -> bool:
        return not self.failed and bool(self.text.strip())


@dataclass
class Cluster:
    """A set of agents whose plans the judge deemed equivalent."""

    key: str
    label: str
    members: List[str] = field(default_factory=list)
    representative: str = ""  # agent whose plan best represents the cluster
    weight: float = 0.0  # sum of member weights


@dataclass
class ConsensusResult:
    """The verdict for one turn."""

    turn: int
    clusters: List[Cluster]
    agreement: float  # leading cluster weight / total responding weight
    converged: bool
    leading: Optional[Cluster]
    elected: Optional[str]  # best-positioned agent in the leading cluster
    ranking: List[Tuple[str, float]]  # all responders by fitness, desc
    dissenters: List[str]  # responders outside the leading cluster
    responders: int
    silent: List[str]  # agents that failed/timed out this turn


# ---------------------------------------------------------------------------
# Parsing helpers (shared with the mock + real adapters' plan text)
# ---------------------------------------------------------------------------

_FIT_RE = re.compile(r"^\s*FIT:\s*([0-9]*\.?[0-9]+)", re.IGNORECASE | re.MULTILINE)
_APPROACH_RE = re.compile(r"^\s*APPROACH:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


def extract_fit(text: str, default: float = 0.5) -> float:
    m = _FIT_RE.search(text or "")
    if not m:
        return default
    try:
        return max(0.0, min(1.0, float(m.group(1))))
    except ValueError:
        return default


def extract_label(text: str) -> Optional[str]:
    """The explicit ``APPROACH:`` label if present (used by the deterministic judge)."""
    m = _APPROACH_RE.search(text or "")
    return m.group(1).strip() if m else None


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


def evaluate(
    plans: List[PlanRecord],
    clusters: List[Cluster],
    threshold: float,
    turn: int,
) -> ConsensusResult:
    """Compute the verdict from clustered plans.

    Agreement = (weight of the largest cluster) / (weight of all *responding* agents).
    Converged when agreement >= threshold and at least one agent responded.
    Election picks the highest-fitness agent within the leading cluster.
    """
    by_agent: Dict[str, PlanRecord] = {p.agent: p for p in plans}
    responders = [p for p in plans if p.responded]
    silent = [p.agent for p in plans if not p.responded]
    total_weight = sum(p.weight for p in responders)

    # Recompute cluster weights from responding members only.
    for c in clusters:
        members = [a for a in c.members if a in by_agent and by_agent[a].responded]
        c.members = members
        c.weight = sum(by_agent[a].weight for a in members)

    live_clusters = [c for c in clusters if c.members]
    leading = max(live_clusters, key=lambda c: (c.weight, len(c.members)), default=None)
    agreement = (leading.weight / total_weight) if (leading and total_weight) else 0.0
    converged = bool(leading) and total_weight > 0 and agreement >= threshold

    elected = None
    if leading:
        elected = _elect(leading.members, by_agent)

    ranking = sorted(
        ((p.agent, p.fit) for p in responders), key=lambda t: (-t[1], t[0])
    )
    dissenters = (
        [a for a in (p.agent for p in responders) if a not in (leading.members if leading else [])]
        if leading
        else [p.agent for p in responders]
    )

    return ConsensusResult(
        turn=turn,
        clusters=clusters,
        agreement=agreement,
        converged=converged,
        leading=leading,
        elected=elected,
        ranking=ranking,
        dissenters=dissenters,
        responders=len(responders),
        silent=silent,
    )


def _elect(members: List[str], by_agent: Dict[str, PlanRecord]) -> Optional[str]:
    """Best-positioned agent in a cluster: highest fitness, tie-broken by weight then name."""
    if not members:
        return None
    # min over negated numerics => highest fit, then highest weight, then name A->Z.
    return min(members, key=lambda a: (-by_agent[a].fit, -by_agent[a].weight, a))


def top_options(plans: List[PlanRecord], clusters: List[Cluster], n: int) -> List[Dict]:
    """Build the top-N options for user escalation, largest cluster first.

    Each option = one cluster, represented by its strongest plan, with the agents backing
    it. Used when the panel fails to reach X% within Y turns.
    """
    by_agent = {p.agent: p for p in plans}
    ranked = sorted(clusters, key=lambda c: (c.weight, len(c.members)), reverse=True)
    options: List[Dict] = []
    for c in ranked[:n]:
        rep = c.representative or (c.members[0] if c.members else "")
        plan = by_agent.get(rep)
        options.append(
            {
                "label": c.label,
                "backers": list(c.members),
                "representative": rep,
                "plan": plan.text if plan else "",
                "weight": c.weight,
            }
        )
    return options
