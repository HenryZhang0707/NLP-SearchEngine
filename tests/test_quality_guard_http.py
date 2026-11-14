from types import SimpleNamespace

import pytest

from evaluation.audit_quality_guard_http import FIXED, GUARD, request_pairs
from evaluation.benchmark_dureader_policies_http import capture_parity, policy_ce_budget, quality_guard_heavy


def test_quality_guard_uses_frozen_threshold_and_constraint_fallback():
    assert quality_guard_heavy(0.154, 0.153)
    assert not quality_guard_heavy(0.153, 0.153)
    assert not quality_guard_heavy(0.9, 0.153, explicit_constraints=True)


def test_quality_guard_budget_is_fixed_or_heavy():
    assert policy_ce_budget('quality-guard-40', 33, False) == 33
    assert policy_ce_budget('quality-guard-40', 33, True) == 40
    assert policy_ce_budget('fixed-pair-budget', 33, False) == 33
    assert policy_ce_budget('ce28', 33, False) == 28


def test_quality_guard_audit_requires_exact_fixed_control_and_unique_pairs():
    queries = {"q": "apple"}
    gate = SimpleNamespace(predict=lambda features: [features[0][0]])
    evidence = {
        "query_id": "q", "client_wall_ms": 1, "fallback": False,
        "explicit_constraints": False, "vector_attempt": True,
        "gate_features": [0.2, 0, 0, 5], "results": [str(i) for i in range(40)],
    }
    guard = {**evidence, "policy": GUARD, "gate_heavy": True, "ce_pairs": 40}
    fixed = {**evidence, "policy": FIXED, "gate_heavy": False, "ce_pairs": 33}
    assert request_pairs([fixed, guard], queries, gate, 0.153)[FIXED] == [fixed]
    for bad in ({**fixed, "ce_pairs": 40}, {**fixed, "fallback": True},
                {**fixed, "vector_attempt": None}, {**fixed, "query_id": "different"}, guard):
        with pytest.raises(ValueError):
            request_pairs([guard, bad], queries, gate, 0.153)

    historical = {key: value for key, value in fixed.items() if key not in ("fallback", "explicit_constraints")}
    with pytest.raises(ValueError):
        request_pairs([guard, historical], queries, gate, 0.153)
    assert request_pairs([guard, historical], queries, gate, 0.153, historical=True)[FIXED] == [historical]


def test_existing_benchmark_policies_retain_capture_parity():
    recorded = {"heavy": False, "actions": {"bm25": ["d"], "learned-router": ["d"]}}
    row = {"results": ["d"], "gate_heavy": False}
    assert capture_parity(row, "bm25", recorded) is True
    assert capture_parity({**row, "results": ["other"]}, "bm25", recorded) is False
    assert capture_parity(row, GUARD, recorded) is None
    with pytest.raises(ValueError, match="Router decision"):
        capture_parity({**row, "gate_heavy": True}, "learned-router", recorded)
