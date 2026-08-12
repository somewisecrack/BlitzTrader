from tools.pair_vrp_selector import PairVolatilityMetrics, select_pair_structure


def _metric(vrp, ivp=60.0):
    return PairVolatilityMetrics("AAA.NS", 0.25, ivp, 0.20, vrp)


def _select(x, y, **overrides):
    values = {
        "enabled": True,
        "default_structure": "CREDIT_SPREAD",
        "sell_threshold": 0.03,
        "buy_threshold": -0.03,
        "ivp_guard_enabled": False,
        "ivp_sell_floor": 50.0,
    }
    values.update(overrides)
    return select_pair_structure(x, y, **values)


def test_both_rich_selects_credit_at_threshold():
    decision = _select(_metric(0.03), _metric(0.06))
    assert decision.structure_type == "CREDIT_SPREAD"
    assert decision.reason == "both legs meet VRP sell threshold"


def test_both_cheap_selects_futures_plus_options_at_threshold():
    decision = _select(_metric(-0.04), _metric(-0.03))
    assert decision.structure_type == "FUTURES_PLUS_OPTIONS"
    assert decision.reason == "both legs meet VRP buy threshold"


def test_mixed_and_middling_use_default():
    assert _select(_metric(0.05), _metric(-0.05)).reason == "no strong vol signal"
    assert _select(_metric(0.01), _metric(0.02)).structure_type == "CREDIT_SPREAD"


def test_unavailable_never_fabricates_a_choice():
    unavailable = PairVolatilityMetrics("BBB.NS", None, None, None, None)
    decision = _select(_metric(0.08), unavailable, default_structure="FUTURES_PLUS_OPTIONS")
    assert decision.structure_type == "FUTURES_PLUS_OPTIONS"
    assert decision.reason == "metrics unavailable"


def test_feature_off_returns_existing_default_without_using_metrics():
    decision = _select(_metric(-0.20), _metric(-0.20), enabled=False)
    assert decision.structure_type == "CREDIT_SPREAD"
    assert decision.reason == "feature disabled"


def test_ivp_guard_can_block_otherwise_rich_credit_signal():
    decision = _select(_metric(0.08, 10), _metric(0.04, 20), ivp_guard_enabled=True)
    assert decision.structure_type == "CREDIT_SPREAD"
    assert decision.reason == "IVP guard rejected low historical IV"


def test_ivp_guard_requires_both_percentiles():
    missing_ivp = PairVolatilityMetrics("BBB.NS", 0.25, None, 0.20, 0.05)
    decision = _select(_metric(0.08), missing_ivp, ivp_guard_enabled=True)
    assert decision.reason == "IVP guard metrics unavailable"


def test_decision_is_pure_and_audit_is_serializable():
    x, y = _metric(0.08), _metric(0.05)
    decision = _select(x, y)
    assert x.vrp == 0.08 and y.vrp == 0.05
    assert decision.audit()["x"]["vrp"] == 0.08
