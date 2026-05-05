from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from itertools import combinations

import numpy as np
import pandas as pd
import scipy.stats as st
from statsmodels.regression.linear_model import OLS
from statsmodels.tools.tools import add_constant
from statsmodels.tsa.stattools import adfuller
from statsmodels.tsa.vector_ar.vecm import coint_johansen
import yfinance as yf

from config import (
    ADF_PVALUE_LIMIT,
    BATCH_SIZE,
    BLOCK_LEN_FACTOR,
    ENSEMBLE_M,
    HURST_LIMIT,
    INTERVAL_PERIODS,
    MAX_TOTAL_SIMS,
    MIN_BARS,
    NIFTY50_SYMBOLS,
    PAIR_INTERVALS,
    RNG_SEED,
    SIMS_PER_DRAW,
    USE_BOOTSTRAP_RESID,
    Z_SCORE_LIMIT,
)

logger = logging.getLogger("BlitzTrader.PairsScanner")


@dataclass
class PairCandidate:
    x_symbol: str
    y_symbol: str
    timeframe: str
    method: str
    beta: float
    z_score: float
    hurst: float
    half_life: int
    entry_reference_x: float
    entry_reference_y: float
    price_corr: float
    return_corr: float
    prob_profit: float
    prob_profit_low: float
    prob_profit_high: float
    matched_timeframes: list[str] = field(default_factory=list)

    @property
    def pair_key(self) -> tuple[str, str]:
        return tuple(sorted((self.x_symbol, self.y_symbol)))

    @property
    def direction(self) -> str:
        return "SHORT_SPREAD" if self.z_score > 0 else "LONG_SPREAD"


@dataclass
class ScreenedPair:
    x_symbol: str
    y_symbol: str
    timeframe: str
    method: str
    beta_ts: pd.Series
    spread: pd.Series
    half_life: int
    price_corr: float
    return_corr: float
    entry_reference_x: float
    entry_reference_y: float


class PairScanner:
    def fetch_universe(self) -> list[str]:
        symbols = [f"{symbol}.NS" for symbol in NIFTY50_SYMBOLS]
        logger.info("Loaded %s NIFTY 50 symbols", len(symbols))
        return symbols

    def fetch_interval_data(self, tickers: list[str], interval: str) -> pd.DataFrame:
        period = INTERVAL_PERIODS[interval]
        batches = [tickers[i:i + BATCH_SIZE] for i in range(0, len(tickers), BATCH_SIZE)]
        frames: list[pd.DataFrame] = []
        for batch in batches:
            try:
                raw = yf.download(
                    batch,
                    period=period,
                    interval=interval,
                    auto_adjust=False,
                    progress=False,
                    threads=False,
                )
            except Exception as exc:
                logger.warning("yfinance batch failed for %s %s: %s", interval, batch[:3], exc)
                continue
            if raw.empty:
                continue
            if isinstance(raw.columns, pd.MultiIndex):
                close = raw["Adj Close"] if "Adj Close" in raw.columns.get_level_values(0) else raw["Close"]
            else:
                close = raw[["Adj Close"]] if "Adj Close" in raw.columns else raw[["Close"]]
                close.columns = [batch[0]]
            if isinstance(close, pd.Series):
                close = close.to_frame(name=batch[0])
            frames.append(close.dropna(axis=1, how="all"))
        if not frames:
            return pd.DataFrame()
        combined = pd.concat(frames, axis=1)
        combined = combined.loc[:, ~combined.columns.duplicated()].sort_index()
        combined = combined.ffill().bfill()
        combined = combined.loc[~combined.index.duplicated(keep="last")]
        combined = combined.dropna(axis=1, thresh=int(len(combined) * 0.9))
        return combined

    @staticmethod
    def hurst(ts: np.ndarray) -> float:
        if len(ts) < 20:
            return np.nan
        lags = range(2, min(100, len(ts) - 1) + 1)
        tau = [np.std(ts[lag:] - ts[:-lag]) for lag in lags]
        if any(t == 0 for t in tau):
            return np.nan
        return np.polyfit(np.log(list(lags)), np.log(tau), 1)[0] * 2.0

    @staticmethod
    def kalman_beta_series(x: pd.Series, y: pd.Series, beta0: float, r_noise: float = 1e-5) -> pd.Series:
        x_arr, y_arr = x.values, y.values
        beta, p = beta0, 1.0
        q = np.var(y_arr - beta0 * x_arr) or 1.0
        betas = np.zeros(len(x_arr))
        for idx in range(len(x_arr)):
            p += r_noise
            h = x_arr[idx]
            resid = y_arr[idx] - h * beta
            s = h * p * h + q
            k = p * h / s if s else 0.0
            beta += k * resid
            p *= (1 - k * h)
            betas[idx] = beta
        return pd.Series(betas, index=x.index)

    @staticmethod
    def _block_bootstrap_errors(resid: np.ndarray, horizon: int, rng: np.random.Generator, block_len: int) -> np.ndarray:
        n = len(resid)
        if n == 0:
            return rng.normal(0, 1, size=horizon)
        block_len = max(1, int(block_len))
        samples: list[float] = []
        while len(samples) < horizon:
            start = int(rng.integers(0, n))
            end = start + block_len
            if end <= n:
                block = resid[start:end]
            else:
                block = np.concatenate([resid[start:n], resid[0:(end % n)]])
            samples.extend(block.tolist())
        return np.array(samples[:horizon])

    @staticmethod
    def _simulate_path_ar1(a: float, phi: float, r0: float, eps_seq: np.ndarray) -> np.ndarray:
        horizon = len(eps_seq)
        path = np.empty(horizon + 1)
        path[0] = r0
        for idx in range(1, horizon + 1):
            path[idx] = a + phi * path[idx - 1] + eps_seq[idx - 1]
        return path

    @staticmethod
    def _safe_float(val: float, default: float = 0.0) -> float:
        num = float(val)
        return num if math.isfinite(num) else default

    def screen_pair(self, x_sym: str, y_sym: str, prices: pd.DataFrame, interval: str) -> ScreenedPair | None:
        pair_prices = prices[[x_sym, y_sym]].dropna()
        if len(pair_prices) < max(50, MIN_BARS):
            return None

        pair_returns = pair_prices.pct_change().dropna()
        if len(pair_returns) < 50:
            return None

        price_corr = round(pair_prices.corr().iloc[0, 1], 2)
        return_corr = round(pair_returns.corr().iloc[0, 1], 2)

        beta_ts = None
        spread = None
        cadf_pass = False
        johansen_pass = False
        beta0_j = None

        try:
            ols = OLS(pair_prices[y_sym], add_constant(pair_prices[x_sym])).fit()
            beta0 = float(ols.params.iloc[1])
            beta_ts = self.kalman_beta_series(pair_prices[x_sym], pair_prices[y_sym], beta0)
            spread = pair_prices[y_sym] - beta_ts * pair_prices[x_sym]
            try:
                pval = adfuller(spread.dropna(), autolag="AIC")[1]
            except Exception:
                pval = 1.0
            if pval < ADF_PVALUE_LIMIT:
                cadf_pass = True
        except Exception:
            cadf_pass = False

        try:
            jr = coint_johansen(pair_prices, det_order=0, k_ar_diff=1)
            trace, ct = jr.lr1, jr.cvt[:, 1]
            maxe, cm = jr.lr2, jr.cvm[:, 1]
            if any(trace[idx] > ct[idx] and maxe[idx] > cm[idx] for idx in range(2)):
                johansen_pass = True
                eig_idx = int(np.argmax(jr.eig))
                v1, v2 = jr.evec[:, eig_idx]
                beta0_j = -v1 / v2
        except Exception:
            johansen_pass = False
            beta0_j = None

        if not (cadf_pass or johansen_pass):
            return None

        if cadf_pass and beta_ts is not None and spread is not None:
            method = "CADF" if not johansen_pass else "Both"
        else:
            try:
                beta_ts = self.kalman_beta_series(pair_prices[x_sym], pair_prices[y_sym], float(beta0_j))
                spread = pair_prices[y_sym] - beta_ts * pair_prices[x_sym]
                method = "Johansen"
            except Exception:
                return None

        lag = spread.shift(1).bfill()
        ret = spread - lag
        b = np.polyfit(lag, ret, 1)[0] if np.std(lag) > 0 else 0
        half_life = max(1, int(round(-np.log(2) / b))) if b != 0 else 1
        half_life = min(half_life, max(1, len(spread) // 3))

        rolling_mean = spread.rolling(window=half_life, min_periods=max(1, half_life // 2)).mean()
        rolling_std = spread.rolling(window=half_life, min_periods=max(1, half_life // 2)).std()
        current_std = rolling_std.iloc[-1]
        if not current_std or np.isnan(current_std):
            return None
        z_score = float((spread.iloc[-1] - rolling_mean.iloc[-1]) / current_std)
        hurst_val = float(self.hurst(spread.values))

        if not math.isfinite(z_score) or abs(z_score) <= Z_SCORE_LIMIT:
            return None
        if not math.isfinite(hurst_val) or hurst_val >= HURST_LIMIT:
            return None

        return ScreenedPair(
            x_symbol=x_sym,
            y_symbol=y_sym,
            timeframe=interval,
            method=method,
            beta_ts=beta_ts,
            spread=spread,
            half_life=half_life,
            price_corr=price_corr,
            return_corr=return_corr,
            entry_reference_x=round(float(pair_prices[x_sym].iloc[-1]), 2),
            entry_reference_y=round(float(pair_prices[y_sym].iloc[-1]), 2),
        )

    def run_ensemble_mc(self, item: ScreenedPair) -> PairCandidate:
        spread = item.spread
        beta_ts = item.beta_ts
        half_life = min(item.half_life, max(1, len(spread) // 3))
        block_len = max(1, int(round(max(1, half_life * BLOCK_LEN_FACTOR))))

        yvals = spread.values[1:]
        xvals = spread.values[:-1]
        x_design = np.column_stack([np.ones(len(xvals)), xvals])
        model = np.linalg.lstsq(x_design, yvals, rcond=None)[0]
        a_hat, phi_hat = float(model[0]), float(model[1])
        fitted = x_design.dot(model)
        resid = yvals - fitted
        sigma_hat = float(np.std(resid, ddof=1))

        n_obs = len(yvals)
        sse = np.sum((yvals - fitted) ** 2)
        mse = sse / max(1, n_obs - 2)
        try:
            xtx_inv = np.linalg.inv(x_design.T.dot(x_design))
            se = np.sqrt(np.diag(xtx_inv) * mse)
            cov_params = np.diag(se ** 2)
        except Exception:
            se = np.array([1.0, 1.0])
            cov_params = np.eye(2)

        rng_main = np.random.default_rng(RNG_SEED)
        total_sims = int(ENSEMBLE_M) * int(SIMS_PER_DRAW)
        sims_per_local = max(1, int(MAX_TOTAL_SIMS // max(1, int(ENSEMBLE_M)))) if total_sims > MAX_TOTAL_SIMS else SIMS_PER_DRAW

        r0 = float(spread.iloc[-1])
        rolling_mean = spread.rolling(window=half_life, min_periods=1).mean()
        rolling_std = spread.rolling(window=half_life, min_periods=1).std()
        mean_last = float(rolling_mean.iloc[-1])
        std_last = float(rolling_std.iloc[-1])
        z_score = (r0 - mean_last) / (std_last if std_last != 0 else 1e-12)
        trade_sign = -1 if z_score > 0 else 1

        p_draws: list[float] = []
        for _ in range(int(ENSEMBLE_M)):
            try:
                params_sample = rng_main.multivariate_normal(mean=[a_hat, phi_hat], cov=cov_params)
                a_s, phi_s = float(params_sample[0]), float(params_sample[1])
            except Exception:
                a_s = float(rng_main.normal(a_hat, se[0]))
                phi_s = float(rng_main.normal(phi_hat, se[1]))

            phi_s = max(min(phi_s, 0.999), -0.999)
            df_chi = max(1, n_obs - 2)
            chi2_draw = st.chi2.rvs(df_chi, random_state=rng_main)
            sigma_s = sigma_hat * np.sqrt(df_chi / chi2_draw) if chi2_draw > 0 else sigma_hat

            wins = 0
            for _ in range(int(sims_per_local)):
                if USE_BOOTSTRAP_RESID and len(resid) > 0:
                    eps_seq = self._block_bootstrap_errors(resid, half_life, rng_main, block_len)
                else:
                    eps_seq = rng_main.normal(0, sigma_s, size=half_life)
                path = self._simulate_path_ar1(a_s, phi_s, r0, eps_seq)
                for idx in range(1, half_life + 1):
                    delta = path[idx] - r0
                    pnl_currency = trade_sign * delta
                    if pnl_currency > 0:
                        wins += 1
                        break
            p_draws.append(wins / sims_per_local if sims_per_local > 0 else 0.0)

        p_draws_clean = np.array([p for p in p_draws if not np.isnan(p)])
        if p_draws_clean.size == 0:
            p_median_pct, p_low, p_high = 0.0, 0.0, 0.0
        else:
            p_median_pct = round(float(np.median(p_draws_clean)) * 100.0, 1)
            p_low = round(float(np.percentile(p_draws_clean, 5)) * 100.0, 1)
            p_high = round(float(np.percentile(p_draws_clean, 95)) * 100.0, 1)

        return PairCandidate(
            x_symbol=item.x_symbol.replace(".NS", ""),
            y_symbol=item.y_symbol.replace(".NS", ""),
            timeframe=item.timeframe,
            method=item.method,
            beta=abs(float(beta_ts.iloc[-1])),
            z_score=round(self._safe_float(z_score), 3),
            hurst=round(self._safe_float(self.hurst(spread.values), 0.5), 3),
            half_life=half_life,
            entry_reference_x=item.entry_reference_x,
            entry_reference_y=item.entry_reference_y,
            price_corr=item.price_corr,
            return_corr=item.return_corr,
            prob_profit=self._safe_float(p_median_pct),
            prob_profit_low=self._safe_float(p_low),
            prob_profit_high=self._safe_float(p_high),
            matched_timeframes=[item.timeframe],
        )

    def run_scan(self) -> list[PairCandidate]:
        tickers = self.fetch_universe()
        merged: dict[tuple[str, str], PairCandidate] = {}
        for interval in PAIR_INTERVALS:
            prices = self.fetch_interval_data(tickers, interval)
            if prices.empty:
                logger.warning("No prices returned for %s", interval)
                continue
            active = [ticker for ticker in tickers if ticker in prices.columns]
            logger.info("Scanning %s interval with %s active symbols", interval, len(active))
            for x_sym, y_sym in combinations(active, 2):
                screened = self.screen_pair(x_sym, y_sym, prices, interval)
                if not screened:
                    continue
                candidate = self.run_ensemble_mc(screened)
                key = candidate.pair_key
                existing = merged.get(key)
                if not existing:
                    merged[key] = candidate
                    continue
                if interval not in existing.matched_timeframes:
                    existing.matched_timeframes.append(interval)
                better = candidate.prob_profit > existing.prob_profit
                tie = math.isclose(candidate.prob_profit, existing.prob_profit)
                if better or (tie and abs(candidate.z_score) > abs(existing.z_score)):
                    candidate.matched_timeframes = sorted(set(existing.matched_timeframes + candidate.matched_timeframes))
                    merged[key] = candidate

        return sorted(
            merged.values(),
            key=lambda c: (c.prob_profit, abs(c.z_score), -c.half_life),
            reverse=True,
        )
