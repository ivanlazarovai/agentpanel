"""Model-backed judges (loaded lazily by :func:`agentpanel.core.judge.build_judge`).

Two neutral arbiters that cluster the panel's plans semantically rather than by exact
label:

- :class:`NeutralModelJudge` — a dedicated model via the Anthropic API. Independent of the
  panelists. Uses ``output_config.format`` (JSON schema) so the clustering comes back as
  validated JSON. Requires ``ANTHROPIC_API_KEY`` (or an ``ant`` profile) in the
  environment; if the SDK isn't installed or no credentials are present, ``build_judge``
  falls back to the deterministic judge.
- :class:`DesignatedAgentJudge` — one roster agent acts as neutral chair, judging via its
  own CLI (so it works with a subscription login, no API key needed).

Both return ``List[Cluster]``; the deterministic math in :mod:`consensus` takes it from there.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from .consensus import Cluster, PlanRecord

# Default judge model. Per Anthropic guidance, default to the latest Opus; the user can
# downgrade (e.g. to Haiku) in FTU/config for cost.
DEFAULT_JUDGE_MODEL = "claude-opus-4-8"

# JSON schema the judge must emit: a clustering of the panel's plans.
_CLUSTER_SCHEMA = {
    "type": "object",
    "properties": {
        "clusters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "members": {"type": "array", "items": {"type": "string"}},
                    "representative": {"type": "string"},
                },
                "required": ["label", "members", "representative"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["clusters"],
    "additionalProperties": False,
}


def _judge_prompt(question: str, plans: List[PlanRecord]) -> str:
    blocks = []
    for p in plans:
        if p.responded:
            blocks.append(f"### {p.agent}\n{p.text.strip()}")
    panel = "\n\n".join(blocks)
    return (
        "You are the neutral chair of a panel of coding agents that each proposed a plan "
        "for the same request. Group the plans into clusters by their underlying APPROACH "
        "(agents whose plans would lead to substantially the same implementation belong in "
        "one cluster, even if worded differently). For each cluster give a short label, the "
        "list of member agent names, and the single member whose plan best represents it. "
        "Judge on substance, not wording or who wrote it.\n\n"
        f"REQUEST:\n{question}\n\n"
        f"PLANS:\n{panel}\n\n"
        "Return ONLY the clustering."
    )


def _to_clusters(data: Dict[str, Any], plans: List[PlanRecord]) -> List[Cluster]:
    """Map the judge's JSON into Cluster objects, keeping only real responding agents."""
    valid = {p.agent for p in plans if p.responded}
    out: List[Cluster] = []
    for i, c in enumerate(data.get("clusters", [])):
        members = [m for m in c.get("members", []) if m in valid]
        if not members:
            continue
        rep = c.get("representative")
        if rep not in members:
            rep = members[0]
        out.append(Cluster(key=f"j{i}", label=c.get("label", f"cluster {i}"),
                           members=members, representative=rep))
    # Any responding agent the judge omitted becomes its own singleton cluster.
    placed = {m for c in out for m in c.members}
    for p in plans:
        if p.responded and p.agent not in placed:
            out.append(Cluster(key=f"solo-{p.agent}", label=p.agent, members=[p.agent],
                               representative=p.agent))
    return out


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Best-effort: parse the first balanced JSON object out of a text blob."""
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


class NeutralModelJudge:
    """Cluster plans with a dedicated Anthropic model (independent of the panel)."""

    def __init__(self, model: Optional[str] = None) -> None:
        # Import here so the dependency is optional; raises if anthropic isn't installed,
        # which build_judge catches to fall back to the deterministic judge.
        from anthropic import AsyncAnthropic  # noqa: F401

        self._client_cls = AsyncAnthropic
        self.model = model or DEFAULT_JUDGE_MODEL

    async def cluster(self, question: str, plans: List[PlanRecord]) -> List[Cluster]:
        responding = [p for p in plans if p.responded]
        if len(responding) <= 1:
            return [Cluster(key="solo", label=p.agent, members=[p.agent], representative=p.agent)
                    for p in responding]
        client = self._client_cls()
        resp = await client.messages.create(
            model=self.model,
            max_tokens=2000,
            output_config={"format": {"type": "json_schema", "schema": _CLUSTER_SCHEMA}},
            messages=[{"role": "user", "content": _judge_prompt(question, plans)}],
        )
        text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), "")
        data = _extract_json(text) or {"clusters": []}
        return _to_clusters(data, plans)


class DesignatedAgentJudge:
    """One roster agent acts as neutral chair, clustering via its own CLI (plan mode)."""

    def __init__(self, adapter) -> None:  # adapter: AgentAdapter
        self.adapter = adapter

    async def cluster(self, question: str, plans: List[PlanRecord]) -> List[Cluster]:
        from pathlib import Path

        from .adapter import RunContext

        responding = [p for p in plans if p.responded]
        if len(responding) <= 1:
            return [Cluster(key="solo", label=p.agent, members=[p.agent], representative=p.agent)
                    for p in responding]
        prompt = _judge_prompt(question, plans) + (
            "\n\nRespond with ONLY a JSON object of the form "
            '{"clusters":[{"label":"...","members":["..."],"representative":"..."}]}'
        )
        ctx = RunContext(workdir=Path.cwd())
        collected: List[str] = []
        async for ev in self.adapter.plan(prompt, ctx):
            if ev.type == "token":
                collected.append(ev.text)
            elif ev.type == "done" and ev.full_text:
                collected = [ev.full_text]
        data = _extract_json("".join(collected)) or {"clusters": []}
        return _to_clusters(data, plans)
