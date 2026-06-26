"""Consent + a risk-graded, remembered permission policy.

AgentPanel operates inside a **base directory** (default: the current directory, user-
reconfigurable). Before it reads or writes there — to stage agent inputs and read their
outputs — it needs the user's **explicit consent** for that directory. Beyond that, agents
ask to do things (run a command, touch a path outside the base, push, install). For now the
panel forwards those asks to the user; over time the user can **broaden** what's auto-accepted
by *category*, unlike Claude Code's per-call strictness.

That broadening is a dangerous lever, so every request carries a **risk judgement**
(:class:`RiskLevel`). Remembered "allow" rules only auto-apply up to the risk ceiling the user
set, **critical actions are never auto-allowed**, and broadening to high risk is flagged as
needing explicit confirmation.

This module is the policy core (pure, testable). Wiring it into live agent prompts is a
separate step; adapters/UI call :meth:`PermissionPolicy.decide` and surface the result.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional


class RiskLevel(enum.IntEnum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4

    @property
    def label(self) -> str:
        return self.name.lower()


# Action categories an agent (or the panel) can request.
READ_PATH = "read_path"
WRITE_PATH = "write_path"
RUN_SHELL = "run_shell"
NETWORK = "network"
GIT_PUSH = "git_push"
DELETE_PATH = "delete_path"
INSTALL = "install"

_BASE_RISK: Dict[str, RiskLevel] = {
    READ_PATH: RiskLevel.LOW,
    WRITE_PATH: RiskLevel.LOW,
    RUN_SHELL: RiskLevel.MEDIUM,
    NETWORK: RiskLevel.MEDIUM,
    DELETE_PATH: RiskLevel.MEDIUM,
    GIT_PUSH: RiskLevel.HIGH,
    INSTALL: RiskLevel.HIGH,
}

# Shell shapes that are critical no matter where they run.
_DANGEROUS = [
    re.compile(r"\brm\s+-[a-z]*r[a-z]*f", re.I),
    re.compile(r"\bsudo\b", re.I),
    re.compile(r"\bmkfs\b", re.I),
    re.compile(r"\bdd\s+if=", re.I),
    re.compile(r":\(\)\s*\{", re.I),            # fork bomb
    re.compile(r"\bchmod\s+-R\s+777", re.I),
    re.compile(r"curl[^\n|]*\|\s*(sh|bash)", re.I),
    re.compile(r"wget[^\n|]*\|\s*(sh|bash)", re.I),
    re.compile(r">\s*/dev/sd", re.I),
]


@dataclass
class PermissionRequest:
    """Something an agent (or the panel) wants to do."""

    agent: str
    action: str  # one of the category constants above
    target: str = ""  # path / command / url
    detail: str = ""


@dataclass
class Decision:
    outcome: str  # "allow" | "ask" | "deny"
    risk: RiskLevel
    reason: str
    requires_confirmation: bool = False  # broadening this (high/critical) needs explicit OK
    rule_id: Optional[str] = None


@dataclass
class PermissionRule:
    """A remembered auto-decision for a category of request."""

    id: str
    action: str  # a category, or "*" for any
    outcome: str = "allow"  # "allow" | "deny"
    scope: str = "*"  # directory prefix the rule applies to, or "*"
    max_risk: int = int(RiskLevel.LOW)  # auto-applies only up to this risk

    def matches(self, req: PermissionRequest, risk: RiskLevel, base_dirs: List[Path]) -> bool:
        if self.action not in ("*", req.action):
            return False
        if int(risk) > self.max_risk:
            return False
        if self.scope != "*":
            tp = _as_path(req.target)
            if tp is None or not _within(tp, [Path(self.scope)]):
                return False
        return True

    def to_dict(self) -> Dict:
        return {"id": self.id, "action": self.action, "outcome": self.outcome,
                "scope": self.scope, "max_risk": self.max_risk}

    @classmethod
    def from_dict(cls, d: Dict) -> "PermissionRule":
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}  # type: ignore[attr-defined]
        return cls(**known)


def classify(req: PermissionRequest, granted_dirs: List[Path]) -> tuple:
    """Return ``(RiskLevel, reason)`` for a request — the always-on risk judgement."""
    risk = _BASE_RISK.get(req.action, RiskLevel.MEDIUM)
    reasons = [f"{req.action} is {risk.label} by default"]

    tp = _as_path(req.target)
    outside = tp is not None and not _within(tp, granted_dirs)
    if outside and req.action in (WRITE_PATH, DELETE_PATH, READ_PATH):
        risk = max(risk, RiskLevel.HIGH)
        reasons.append("target is outside the granted directory")

    if req.action == RUN_SHELL and any(p.search(req.target) for p in _DANGEROUS):
        risk = RiskLevel.CRITICAL
        reasons.append("command matches a destructive pattern")
    if req.action == DELETE_PATH and outside:
        risk = RiskLevel.CRITICAL
        reasons.append("deleting outside the granted directory")

    return RiskLevel(int(risk)), "; ".join(reasons)


class PermissionPolicy:
    """Decides allow/ask/deny for a request, given granted dirs + remembered rules."""

    def __init__(self, granted_dirs: Optional[List[str]] = None,
                 rules: Optional[List[PermissionRule]] = None,
                 default: str = "ask") -> None:
        self.granted_dirs: List[Path] = [Path(d).resolve() for d in (granted_dirs or [])]
        self.rules: List[PermissionRule] = list(rules or [])
        self.default = default
        self._seq = 0

    # -- base-directory consent -------------------------------------------

    def is_granted(self, path) -> bool:
        return _within(Path(path).resolve(), self.granted_dirs)

    def grant(self, path) -> Path:
        p = Path(path).resolve()
        if not _within(p, self.granted_dirs):
            self.granted_dirs.append(p)
        return p

    def revoke(self, path) -> None:
        p = Path(path).resolve()
        self.granted_dirs = [d for d in self.granted_dirs if d != p]

    # -- per-request decision ---------------------------------------------

    def decide(self, req: PermissionRequest) -> Decision:
        risk, reason = classify(req, self.granted_dirs)
        # Explicit deny rules win.
        for r in self.rules:
            if r.outcome == "deny" and r.matches(req, risk, self.granted_dirs):
                return Decision("deny", risk, f"denied by rule {r.id}", rule_id=r.id)
        # Allow rules auto-apply — but NEVER for critical, no matter the rule.
        if risk != RiskLevel.CRITICAL:
            for r in self.rules:
                if r.outcome == "allow" and r.matches(req, risk, self.granted_dirs):
                    return Decision("allow", risk, f"auto-allowed by rule {r.id}", rule_id=r.id)
        # Otherwise ask. High/critical asks are flagged for extra care.
        return Decision("ask", risk, reason, requires_confirmation=risk >= RiskLevel.HIGH)

    # -- broadening (the dangerous lever) ---------------------------------

    def remember(self, action: str, outcome: str = "allow", scope: str = "*",
                 max_risk: RiskLevel = RiskLevel.LOW) -> PermissionRule:
        """Add a remembered rule. Refuses to auto-allow critical; flags high broadening."""
        if outcome == "allow" and int(max_risk) >= int(RiskLevel.CRITICAL):
            raise ValueError("critical actions can never be auto-allowed")
        self._seq += 1
        rule = PermissionRule(id=f"r{self._seq}", action=action, outcome=outcome,
                              scope=scope, max_risk=int(max_risk))
        self.rules.append(rule)
        return rule

    @staticmethod
    def broadening_is_dangerous(max_risk: RiskLevel) -> bool:
        """The UI must get explicit confirmation before remembering an allow this broad."""
        return int(max_risk) >= int(RiskLevel.HIGH)

    # -- persistence ------------------------------------------------------

    def to_dict(self) -> Dict:
        return {
            "granted_dirs": [str(d) for d in self.granted_dirs],
            "rules": [r.to_dict() for r in self.rules],
            "default": self.default,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "PermissionPolicy":
        pol = cls(
            granted_dirs=d.get("granted_dirs", []),
            rules=[PermissionRule.from_dict(r) for r in d.get("rules", [])],
            default=d.get("default", "ask"),
        )
        pol._seq = len(pol.rules)
        return pol


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _as_path(target: str) -> Optional[Path]:
    """Interpret a request target as a filesystem path if it looks like one."""
    if not target:
        return None
    if target.startswith(("/", "./", "../", "~")) or (len(target) > 1 and target[1] == ":"):
        try:
            return Path(target).expanduser().resolve()
        except (OSError, RuntimeError):
            return None
    return None


def _within(path: Path, roots: List[Path]) -> bool:
    path = path.resolve()
    for root in roots:
        try:
            path.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False
