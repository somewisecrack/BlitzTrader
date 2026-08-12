import math
from datetime import datetime

import pytest

pd = pytest.importorskip("pandas")

from tools.pair_vrp_selector import NsePairVolatilityProvider


def _provider(option_frame, future_frame):
    return NsePairVolatilityProvider(
        cache_dir="/tmp/unused-pair-vrp-test-cache",
        ivp_lookback_days=250,
        min_valid_observations=2,
        fetch_calendar_days=400,
        min_dte_days=7,
        realized_window=2,
        risk_free_rate=0.065,
        fetch_option=lambda **kwargs: option_frame,
        fetch_underlying=lambda **kwargs: future_frame,
        now=lambda: datetime(2026, 7, 1),
    )


def _option_rows(provider, days, expiry, sigma=0.25):
    rows = []
    for day in days:
        years = (expiry - day).days / 365.0
        for option_type in ("CE", "PE"):
            rows.append({
                "TIMESTAMP": day.strftime("%d-%b-%Y"), "EXPIRY_DT": expiry.strftime("%d-%b-%Y"),
                "STRIKE_PRICE": 100.0, "OPTION_TYPE": option_type,
                "CLOSING_PRICE": provider._bs_price(option_type, 100.0, 100.0, years, 0.065, sigma),
                "UNDERLYING_VALUE": 100.0, "TOT_TRADED_QTY": 10,
            })
    return pd.DataFrame(rows)


def test_atm_iv_requires_both_traded_sides_and_solves_brent(tmp_path):
    seed = _provider(pd.DataFrame(), pd.DataFrame())
    days = [pd.Timestamp("2026-06-01"), pd.Timestamp("2026-06-02"), pd.Timestamp("2026-06-03")]
    chain = _option_rows(seed, days, pd.Timestamp("2026-06-25"))
    provider = _provider(chain, pd.DataFrame())
    provider.cache_dir = tmp_path
    series = provider._series("AAA.NS")
    assert len(series) == 3
    assert series[-1]["atm_iv"] == pytest.approx(0.25, abs=1e-5)
    chain.loc[chain["OPTION_TYPE"] == "PE", "TOT_TRADED_QTY"] = 0
    assert _provider(chain, pd.DataFrame())._series("AAA.NS") == []


def test_metrics_uses_strict_ivp_and_annualized_21_style_realized_vol(tmp_path):
    seed = _provider(pd.DataFrame(), pd.DataFrame())
    days = [pd.Timestamp("2026-06-01"), pd.Timestamp("2026-06-02"), pd.Timestamp("2026-06-03")]
    chain = _option_rows(seed, days, pd.Timestamp("2026-06-25"), sigma=0.25)
    futures = pd.DataFrame({
        "TIMESTAMP": [day.strftime("%d-%b-%Y") for day in days],
        "CLOSING_PRICE": [100.0, 101.0, 102.0],
    })
    provider = _provider(chain, futures)
    provider.cache_dir = tmp_path
    metrics = provider.metrics("AAA.NS")
    expected_rv = math.sqrt(252) * pd.Series([math.log(101 / 100), math.log(102 / 101)]).std(ddof=1)
    assert metrics.current_iv == pytest.approx(0.25, abs=1e-5)
    assert metrics.ivp == 0.0  # equal observations are not strictly below current.
    assert metrics.realized_vol == pytest.approx(expected_rv)
    assert metrics.vrp == pytest.approx(0.25 - expected_rv)
