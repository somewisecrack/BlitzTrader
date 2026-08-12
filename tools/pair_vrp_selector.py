"""Local, source-only IVP/VRP metrics and pair structure selection.

This module deliberately has no execution, ledger, Telegram, Yahoo, browser, or
OmniSpread dependency.  It ports the daily ATM-IV reconstruction convention used
by OmniSpread: historical NSE option data only, then-current monthly expiries,
both ATM CE and PE required, and Black-Scholes IV solved with Brent's method.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable


UNAVAILABLE = None


@dataclass(frozen=True)
class PairVolatilityMetrics:
    ticker: str
    current_iv: float | None
    ivp: float | None
    realized_vol: float | None
    vrp: float | None
    source: str = "nselib/NSE"


@dataclass(frozen=True)
class StructureDecision:
    structure_type: str
    reason: str
    enabled: bool
    x: PairVolatilityMetrics | None = None
    y: PairVolatilityMetrics | None = None

    def audit(self) -> dict[str, Any]:
        return {
            "structure_type": self.structure_type,
            "reason": self.reason,
            "enabled": self.enabled,
            "x": asdict(self.x) if self.x else None,
            "y": asdict(self.y) if self.y else None,
        }


def select_pair_structure(
    x: PairVolatilityMetrics | None,
    y: PairVolatilityMetrics | None,
    *,
    enabled: bool,
    default_structure: str,
    sell_threshold: float,
    buy_threshold: float,
    ivp_guard_enabled: bool,
    ivp_sell_floor: float,
) -> StructureDecision:
    """Pure VRP gate.  It neither reads market data nor changes trading state."""
    if not enabled:
        return StructureDecision(default_structure, "feature disabled", False, x, y)
    if not x or not y or x.vrp is None or y.vrp is None:
        return StructureDecision(default_structure, "metrics unavailable", True, x, y)
    if min(x.vrp, y.vrp) >= sell_threshold:
        if ivp_guard_enabled:
            if x.ivp is None or y.ivp is None:
                return StructureDecision(default_structure, "IVP guard metrics unavailable", True, x, y)
            if ((x.ivp + y.ivp) / 2.0) < ivp_sell_floor:
                return StructureDecision(default_structure, "IVP guard rejected low historical IV", True, x, y)
        return StructureDecision("CREDIT_SPREAD", "both legs meet VRP sell threshold", True, x, y)
    if max(x.vrp, y.vrp) <= buy_threshold:
        return StructureDecision("FUTURES_PLUS_OPTIONS", "both legs meet VRP buy threshold", True, x, y)
    return StructureDecision(default_structure, "no strong vol signal", True, x, y)


class NsePairVolatilityProvider:
    """Reconstruct daily ATM IV and RV from NSE/nselib only.

    The on-disk cache contains only previously reconstructed NSE observations;
    it is a performance cache, never a secondary market-data source.
    """

    def __init__(
        self,
        cache_dir: Path,
        *,
        ivp_lookback_days: int,
        min_valid_observations: int,
        fetch_calendar_days: int,
        min_dte_days: int,
        realized_window: int,
        risk_free_rate: float,
        fetch_option: Callable[..., pd.DataFrame] | None = None,
        fetch_underlying: Callable[..., pd.DataFrame] | None = None,
        now: Callable[[], datetime] = datetime.now,
    ):
        self.cache_dir = Path(cache_dir)
        self.ivp_lookback_days = ivp_lookback_days
        self.min_valid_observations = min_valid_observations
        self.fetch_calendar_days = fetch_calendar_days
        self.min_dte_days = min_dte_days
        self.realized_window = realized_window
        self.risk_free_rate = risk_free_rate
        self._fetch_option = fetch_option
        self._fetch_underlying = fetch_underlying
        self._now = now

    @staticmethod
    def _symbol(ticker: str) -> str:
        return str(ticker).upper().removesuffix(".NS").removesuffix(".BO")

    @staticmethod
    def _column(frame: pd.DataFrame, *names: str) -> str:
        lookup = {str(col).upper(): col for col in frame.columns}
        for name in names:
            if name.upper() in lookup:
                return lookup[name.upper()]
        raise ValueError(f"NSE response missing required column: {names[0]}")

    def _option_fetcher(self) -> Callable[..., pd.DataFrame]:
        if self._fetch_option:
            return self._fetch_option
        from nselib import derivatives
        return derivatives.option_price_volume_data

    def _underlying_fetcher(self) -> Callable[..., pd.DataFrame]:
        if self._fetch_underlying:
            return self._fetch_underlying
        from nselib import capital_market
        return capital_market.price_volume_data

    @staticmethod
    def _bs_price(option_type: str, spot: float, strike: float, years: float, rate: float, sigma: float) -> float:
        root_time = math.sqrt(years)
        d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * years) / (sigma * root_time)
        d2 = d1 - sigma * root_time
        cdf = lambda value: 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))
        if option_type == "CE":
            return spot * cdf(d1) - strike * math.exp(-rate * years) * cdf(d2)
        return strike * math.exp(-rate * years) * cdf(-d2) - spot * cdf(-d1)

    def _implied_vol(self, option_type: str, spot: float, strike: float, years: float, price: float) -> float | None:
        if min(spot, strike, years, price) <= 0:
            return None
        intrinsic = max(spot - strike, 0.0) if option_type == "CE" else max(strike - spot, 0.0)
        ceiling = spot if option_type == "CE" else strike * math.exp(-self.risk_free_rate * years)
        if price <= intrinsic or price >= ceiling:
            return None
        def objective(sigma: float) -> float:
            return self._bs_price(option_type, spot, strike, years, self.risk_free_rate, sigma) - price
        try:
            from scipy.optimize import brentq
            if objective(0.0001) > 0 or objective(5.0) < 0:
                return None
            value = float(brentq(objective, 0.0001, 5.0, xtol=1e-6, maxiter=100))
        except (RuntimeError, ValueError):
            return None
        return value if 0.01 <= value <= 3.0 else None

    def _normalise_option(self, raw: pd.DataFrame) -> pd.DataFrame:
        import pandas as pd
        frame = raw.copy()
        date_col = self._column(frame, "date", "TIMESTAMP")
        expiry_col = self._column(frame, "expiry", "EXPIRY_DT")
        strike_col = self._column(frame, "STRIKE_PRICE")
        option_col = self._column(frame, "OPTION_TYPE")
        close_col = self._column(frame, "CLOSING_PRICE")
        underlying_col = self._column(frame, "UNDERLYING_VALUE")
        volume_col = self._column(frame, "TOT_TRADED_QTY")
        result = pd.DataFrame({
            "date": pd.to_datetime(frame[date_col], errors="coerce"),
            "expiry": pd.to_datetime(frame[expiry_col], errors="coerce"),
            "strike": pd.to_numeric(frame[strike_col], errors="coerce"),
            "type": frame[option_col].astype(str).str.upper(),
            "close": pd.to_numeric(frame[close_col], errors="coerce"),
            "underlying": pd.to_numeric(frame[underlying_col], errors="coerce"),
            "volume": pd.to_numeric(frame[volume_col], errors="coerce"),
        })
        return result.dropna(subset=["date", "expiry", "strike", "close", "underlying", "volume"])

    def _series(self, ticker: str) -> list[dict[str, float | str]]:
        import pandas as pd
        now = self._now()
        raw = self._option_fetcher()(
            symbol=self._symbol(ticker), instrument="OPTSTK", option_type=None,
            from_date=(now - timedelta(days=self.fetch_calendar_days)).strftime("%d-%m-%Y"),
            to_date=now.strftime("%d-%m-%Y"),
        )
        frame = self._normalise_option(raw)
        if frame.empty:
            return []
        latest_by_month: dict[tuple[int, int], Any] = {}
        for expiry in sorted(frame["expiry"].drop_duplicates()):
            key = (expiry.year, expiry.month)
            latest_by_month[key] = expiry
        monthly = sorted(latest_by_month.values())
        observations: list[dict[str, float | str]] = []
        for day, rows in frame.groupby(frame["date"].dt.normalize()):
            eligible = [expiry for expiry in monthly if (expiry.normalize() - day).days >= self.min_dte_days]
            if not eligible:
                continue
            expiry = eligible[0]
            chain = rows[rows["expiry"] == expiry]
            if chain.empty:
                continue
            spot = float(chain["underlying"].iloc[0])
            atm = min(chain["strike"].unique(), key=lambda strike: abs(float(strike) - spot))
            years = max((expiry.normalize() - day).days, 1) / 365.0
            values: list[float] = []
            valid = True
            for option_type in ("CE", "PE"):
                leg = chain[(chain["strike"] == atm) & (chain["type"] == option_type)]
                if leg.empty or float(leg.iloc[0]["volume"]) <= 0:
                    valid = False
                    break
                iv = self._implied_vol(option_type, spot, float(atm), years, float(leg.iloc[0]["close"]))
                if iv is None:
                    valid = False
                    break
                values.append(iv)
            if valid:
                observations.append({"date": day.strftime("%Y-%m-%d"), "atm_iv": sum(values) / 2.0})
        return observations

    def _cache_path(self, ticker: str) -> Path:
        return self.cache_dir / f"{self._symbol(ticker)}_{self._now().strftime('%Y-%m-%d')}.json"

    def _load_or_build_series(self, ticker: str) -> list[dict[str, float | str]]:
        path = self._cache_path(ticker)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            observations = payload.get("observations")
            if isinstance(observations, list):
                return observations
        except (OSError, ValueError, AttributeError):
            pass
        observations = self._series(ticker)
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"source": "nselib/NSE", "observations": observations}), encoding="utf-8")
        except OSError:
            pass
        return observations

    def _realized_vol(self, ticker: str) -> float | None:
        import pandas as pd
        now = self._now()
        raw = self._underlying_fetcher()(
            symbol=self._symbol(ticker),
            from_date=(now - timedelta(days=self.fetch_calendar_days)).strftime("%d-%m-%Y"),
            to_date=now.strftime("%d-%m-%Y"),
        )
        frame = raw.copy()
        date_col = self._column(frame, "date", "TIMESTAMP", "CH_TIMESTAMP")
        close_col = self._column(frame, "CLOSING_PRICE", "CH_CLOSING_PRICE")
        values = pd.DataFrame({"date": pd.to_datetime(frame[date_col], errors="coerce"), "close": pd.to_numeric(frame[close_col], errors="coerce")}).dropna()
        values = values.sort_values("date").drop_duplicates("date", keep="last")
        # log returns retain the reference convention and a full window is required.
        returns = pd.Series([math.log(values["close"].iloc[i] / values["close"].iloc[i - 1]) for i in range(1, len(values)) if values["close"].iloc[i - 1] > 0])
        if len(returns) < self.realized_window:
            return None
        value = float(returns.tail(self.realized_window).std(ddof=1) * math.sqrt(252))
        return value if math.isfinite(value) and value > 0 else None

    def metrics(self, ticker: str) -> PairVolatilityMetrics:
        try:
            series = self._load_or_build_series(ticker)
            current_iv = float(series[-1]["atm_iv"]) if series else None
            prior = series[-(self.ivp_lookback_days + 1):-1]
            ivp = None
            if current_iv is not None and len(prior) >= self.min_valid_observations:
                ivp = 100.0 * sum(float(row["atm_iv"]) < current_iv for row in prior) / len(prior)
            realized = self._realized_vol(ticker)
            vrp = (current_iv - realized) if current_iv is not None and realized is not None else None
            return PairVolatilityMetrics(ticker, current_iv, ivp, realized, vrp)
        except Exception:
            return PairVolatilityMetrics(ticker, None, None, None, None)
