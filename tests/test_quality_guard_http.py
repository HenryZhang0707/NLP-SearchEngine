from evaluation.benchmark_dureader_policies_http import policy_ce_budget, quality_guard_heavy


def test_quality_guard_uses_frozen_threshold_and_constraint_fallback():
    assert quality_guard_heavy(0.154, 0.153)
    assert not quality_guard_heavy(0.153, 0.153)
    assert not quality_guard_heavy(0.9, 0.153, explicit_constraints=True)


def test_quality_guard_budget_is_fixed_or_heavy():
    assert policy_ce_budget('quality-guard-40', 33, False) == 33
    assert policy_ce_budget('quality-guard-40', 33, True) == 40
    assert policy_ce_budget('fixed-pair-budget', 33, False) == 33
    assert policy_ce_budget('ce28', 33, False) == 28
