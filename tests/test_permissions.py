"""Tests for the consent + risk-graded permission policy."""

from __future__ import annotations

import pytest

from agentpanel.core.permissions import (
    DELETE_PATH,
    GIT_PUSH,
    READ_PATH,
    RUN_SHELL,
    WRITE_PATH,
    Decision,
    PermissionPolicy,
    PermissionRequest,
    RiskLevel,
    classify,
)


def req(action, target="", agent="claude"):
    return PermissionRequest(agent=agent, action=action, target=target)


def test_base_dir_consent(tmp_path):
    pol = PermissionPolicy()
    assert not pol.is_granted(tmp_path)
    pol.grant(tmp_path)
    assert pol.is_granted(tmp_path)
    assert pol.is_granted(tmp_path / "sub" / "file.py")  # children covered
    pol.revoke(tmp_path)
    assert not pol.is_granted(tmp_path)


def test_risk_classification(tmp_path):
    granted = [tmp_path]
    assert classify(req(WRITE_PATH, str(tmp_path / "a.py")), granted)[0] == RiskLevel.LOW
    # writing outside the granted dir is high risk
    assert classify(req(WRITE_PATH, "/etc/hosts"), granted)[0] == RiskLevel.HIGH
    assert classify(req(GIT_PUSH), granted)[0] == RiskLevel.HIGH
    assert classify(req(RUN_SHELL, "ls -la"), granted)[0] == RiskLevel.MEDIUM
    # destructive shell + delete outside are critical
    assert classify(req(RUN_SHELL, "sudo rm -rf /"), granted)[0] == RiskLevel.CRITICAL
    assert classify(req(DELETE_PATH, "/var/data"), granted)[0] == RiskLevel.CRITICAL


def test_decide_defaults_to_ask(tmp_path):
    pol = PermissionPolicy(granted_dirs=[str(tmp_path)])
    d = pol.decide(req(RUN_SHELL, "pytest -q"))
    assert d.outcome == "ask"
    assert d.risk == RiskLevel.MEDIUM
    # high-risk asks are flagged for extra care
    assert pol.decide(req(GIT_PUSH)).requires_confirmation is True


def test_remembered_allow_rule(tmp_path):
    pol = PermissionPolicy(granted_dirs=[str(tmp_path)])
    pol.remember(RUN_SHELL, outcome="allow", max_risk=RiskLevel.MEDIUM)
    assert pol.decide(req(RUN_SHELL, "pytest -q")).outcome == "allow"
    # a different category (git_push) is not covered by the run_shell rule -> still ask
    assert pol.decide(req(GIT_PUSH)).outcome == "ask"


def test_critical_never_auto_allowed(tmp_path):
    pol = PermissionPolicy(granted_dirs=[str(tmp_path)])
    # Even a broad allow rule cannot auto-approve a critical action.
    pol.remember(RUN_SHELL, outcome="allow", max_risk=RiskLevel.HIGH)
    d = pol.decide(req(RUN_SHELL, "sudo rm -rf /"))
    assert d.risk == RiskLevel.CRITICAL
    assert d.outcome == "ask"
    # And you cannot even create a critical auto-allow rule.
    with pytest.raises(ValueError):
        pol.remember(RUN_SHELL, outcome="allow", max_risk=RiskLevel.CRITICAL)


def test_deny_rule_wins(tmp_path):
    pol = PermissionPolicy(granted_dirs=[str(tmp_path)])
    pol.remember(GIT_PUSH, outcome="deny", max_risk=RiskLevel.HIGH)
    assert pol.decide(req(GIT_PUSH)).outcome == "deny"


def test_broadening_danger_flag():
    assert PermissionPolicy.broadening_is_dangerous(RiskLevel.HIGH) is True
    assert PermissionPolicy.broadening_is_dangerous(RiskLevel.LOW) is False


def test_policy_roundtrips_through_config(tmp_path):
    pol = PermissionPolicy(granted_dirs=[str(tmp_path)])
    pol.remember(READ_PATH, outcome="allow", max_risk=RiskLevel.LOW)
    data = pol.to_dict()
    pol2 = PermissionPolicy.from_dict(data)
    assert pol2.is_granted(tmp_path)
    assert pol2.decide(req(READ_PATH, str(tmp_path / "x"))).outcome == "allow"
    assert isinstance(pol2.decide(req(GIT_PUSH)), Decision)
