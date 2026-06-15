"""
GammaBlast/tools/gamma_candidate_engine.py — Core signal detection engine for GammaBlast.

Detects "Gamma Blast" candidates: options that can move ×3–10 in 5–15 minutes on expiry day,
exclusively in the final ~90 minutes of trading (post-13:00 IST).

Two-stage detection pipeline:
  Stage 1 COILED  — premium near session low, OI near session high, elevated burst volume
                    (fuel is loaded; the coil is wound)
  Stage 2 RELEASE — underlying breaks toward the strike, OI flips from building to unwinding,
                    buyers overwhelm the depth (the coil fires)

Status FSM per candidate:
  WATCHING → COILED → ARMED → RELEASED
                    → INVALIDATED  (from any active state)

No BlitzTrader imports. Standalone module.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time
from statistics import mean
from typing import Optional

logger = logging.getLogger("GammaBlast.CandidateEngine")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VOLUME_BURST_RATIO: float = 1.5          # vol_delta >= ratio * baseline to count as burst
VOLUME_BURST_MIN_IN_WINDOW: int = 2      # min burst buckets in last-6 window for COILED
PREMIUM_MAX_RATIO: float = 1.25          # ltp <= ratio * session_low_ltp  (near-low check)
OI_HIGH_RATIO: float = 0.90             # oi >= ratio * session_high_oi   (near-high check)
BID_IMBALANCE_MIN: float = 0.3          # buyer-side imbalance threshold for both CE and PE

# Clocks (IST naive times — compared against datetime.time())
_GATE_OPEN: time = time(13, 0)          # no candidates before this
_BASELINE_START: time = time(12, 0)     # volume baseline window start
_BASELINE_END: time = time(13, 25)      # volume baseline window end
_SESSION_LOW_START: time = time(12, 0)  # track session low ltp from noon onwards
_CUTOFF: time = time(15, 12)            # invalidate all remaining after this

# Direction look-back: 30 min = 6 buckets of 5 min each
_DIRECTION_LOOKBACK_BUCKETS: int = 6

# OI unwind: check declining vs previous N buckets
_OI_UNWIND_LOOKBACK: int = 2

# Invalidation: underlying moves away by more than N strike steps
_AWAY_STEP_THRESHOLD: int = 2


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class CandidateRecord:
    candidate_id: str
    symbol: str
    expiry: str
    strike: int
    option_type: str                    # "CE" or "PE"
    tsym: str
    confidence_score: float = 0.0
    rank: int = 0
    status: str = "WATCHING"           # WATCHING | COILED | ARMED | RELEASED | INVALIDATED
    rule_version: str = "v1"
    observed_features: dict = field(default_factory=dict)
    reason: str = ""
    invalidation_reason: str = ""
    first_seen_at: Optional[datetime] = None
    coiled_at: Optional[datetime] = None
    released_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class GammaCandidateEngine:
    """
    Per-symbol, per-expiry engine that processes 5-min bucket snapshots for a set
    of option strikes and maintains a board of COILED / ARMED / RELEASED candidates.

    Typical call sequence per 5-min tick:
        engine.push_bucket(strike, option_type, bucket_dict)
        active = engine.evaluate_all(current_ist_dt, underlying_ltp)

    All timestamps are IST-naive datetime objects unless otherwise noted.
    """

    def __init__(
        self,
        symbol: str,
        expiry_date: date,
        strike_step: int,
        atm_offsets: list = None,
    ) -> None:
        if atm_offsets is None:
            atm_offsets = [-2, -1, 0, 1, 2]

        self.symbol = symbol.upper()
        self.expiry_date = expiry_date
        self.expiry = expiry_date.strftime("%d-%b-%Y").upper()
        self.strike_step = strike_step
        self.atm_offsets = atm_offsets

        # (strike, option_type) → list[BucketData dict]
        self._history: dict[tuple[int, str], list[dict]] = {}

        # (strike, option_type) → CandidateRecord
        self._board: dict[tuple[int, str], CandidateRecord] = {}

        # Session high OI per (strike, ot)  — tracked from first bucket
        self._session_high_oi: dict[tuple[int, str], int] = {}

        # Session low LTP per (strike, ot) — tracked from 12:00 onwards
        self._session_low_ltp: dict[tuple[int, str], float] = {}

        # Volume baseline (mean vol_delta of buckets in 12:00–13:25 window)
        self._volume_baseline: dict[tuple[int, str], float] = {}

        logger.info(
            "GammaCandidateEngine init: symbol=%s expiry=%s strike_step=%s offsets=%s",
            self.symbol, self.expiry, self.strike_step, self.atm_offsets,
        )

    # ------------------------------------------------------------------
    # Public: ingest
    # ------------------------------------------------------------------

    def push_bucket(self, strike: int, option_type: str, bucket: dict) -> None:
        """
        Ingest one 5-min bucket snapshot for a given strike/option_type.
        Updates session statistics and recomputes the candidate stage.

        Args:
            strike:       Integer strike price.
            option_type:  "CE" or "PE".
            bucket:       Dict matching BucketData schema (ts, ltp, ltp_max,
                          vol_delta, oi, und_ltp, bid_imbalance).
        """
        ot = option_type.upper()
        key = (strike, ot)

        if key not in self._history:
            self._history[key] = []
            logger.debug("New strike tracked: %s %s %s", self.symbol, strike, ot)

        self._history[key].append(bucket)

        ts: datetime = bucket["ts"]
        bucket_time: time = ts.time() if isinstance(ts, datetime) else ts

        ltp: float = float(bucket["ltp"])
        oi: int = int(bucket["oi"])
        vol_delta: int = int(bucket["vol_delta"])

        # --- Session high OI (all buckets) ---
        prev_high_oi = self._session_high_oi.get(key, 0)
        if oi > prev_high_oi:
            self._session_high_oi[key] = oi

        # --- Session low LTP (post-noon only) ---
        if bucket_time >= _SESSION_LOW_START:
            prev_low = self._session_low_ltp.get(key, float("inf"))
            if ltp < prev_low:
                self._session_low_ltp[key] = ltp

        # --- Volume baseline (12:00–13:25) ---
        if _BASELINE_START <= bucket_time <= _BASELINE_END and vol_delta > 0:
            self._volume_baseline[key] = self._recompute_baseline(key)

        # --- Ensure a CandidateRecord exists ---
        if key not in self._board:
            tsym = self._make_tsym(strike, ot)
            cid = self._make_candidate_id(strike, ot, ts)
            self._board[key] = CandidateRecord(
                candidate_id=cid,
                symbol=self.symbol,
                expiry=self.expiry,
                strike=strike,
                option_type=ot,
                tsym=tsym,
                first_seen_at=ts,
            )

    # ------------------------------------------------------------------
    # Public: evaluate
    # ------------------------------------------------------------------

    def evaluate_all(
        self, current_ist_time: datetime, underlying_ltp: float
    ) -> list[CandidateRecord]:
        """
        Evaluate every tracked strike and advance their FSM states.

        Returns the full list of active CandidateRecords (all statuses).
        Callers can filter by status / score as needed.

        Args:
            current_ist_time:  Current IST datetime (naive).
            underlying_ltp:    Current underlying index LTP.
        """
        now_time: time = current_ist_time.time()

        if now_time >= _CUTOFF:
            self._invalidate_all_active("Clock past 15:12 cutoff")
            return list(self._board.values())

        if now_time < _GATE_OPEN:
            # No candidates before 13:00 — return board as-is (all WATCHING)
            return list(self._board.values())

        all_records = list(self._board.values())
        cluster_keys = self._detect_clusters(all_records)

        for key, record in self._board.items():
            if record.status == "INVALIDATED":
                continue

            strike, ot = key
            history = self._history.get(key, [])
            if not history:
                continue

            # --- Invalidation checks (any active state) ---
            inv_reason = self._check_invalidation(
                key, record, history, underlying_ltp, now_time
            )
            if inv_reason:
                self._do_invalidate(record, inv_reason, current_ist_time)
                continue

            # --- Feature extraction ---
            features = self._extract_features(key, history, underlying_ltp)
            in_cluster = key in cluster_keys

            coiled = self._is_coiled(key, features)
            oi_unwinding = self._is_oi_unwinding(history)
            direction_ok = self._direction_confirmed(ot, history, underlying_ltp)
            imbalance_ok = features.get("last_bid_imbalance", 0.0) >= BID_IMBALANCE_MIN

            # --- Confidence score ---
            score = self._compute_score(
                coiled=coiled,
                oi_unwinding=oi_unwinding,
                direction_ok=direction_ok,
                imbalance_ok=imbalance_ok,
                in_cluster=in_cluster,
            )
            record.confidence_score = score
            record.observed_features = features

            # --- FSM transitions ---
            if record.status == "WATCHING":
                if coiled:
                    record.status = "COILED"
                    record.coiled_at = current_ist_time
                    record.reason = "Stage1: premium near low, OI near high, burst volume"
                    logger.info(
                        "COILED: %s %s %s score=%.2f",
                        self.symbol, strike, ot, score,
                    )

            elif record.status == "COILED":
                if not coiled:
                    # Conditions no longer met — revert to WATCHING
                    record.status = "WATCHING"
                    record.reason = "COILED conditions lapsed"
                    record.coiled_at = None
                elif oi_unwinding and direction_ok:
                    record.status = "ARMED"
                    record.reason = (
                        "Stage2: OI unwinding + direction confirmed"
                    )
                    logger.info(
                        "ARMED: %s %s %s score=%.2f",
                        self.symbol, strike, ot, score,
                    )

            elif record.status == "ARMED":
                if not coiled:
                    # Lost coil conditions — step back
                    record.status = "WATCHING"
                    record.reason = "ARMED: COILED conditions lost"
                    record.coiled_at = None
                elif imbalance_ok and oi_unwinding and direction_ok:
                    record.status = "RELEASED"
                    record.released_at = current_ist_time
                    record.reason = (
                        "Stage2 complete: bid imbalance + OI unwind + direction"
                    )
                    logger.info(
                        "RELEASED: %s %s %s score=%.2f",
                        self.symbol, strike, ot, score,
                    )

            elif record.status == "RELEASED":
                # RELEASED is terminal-positive; just update score and features
                pass

        # Rank active candidates by score
        self._rerank()

        return list(self._board.values())

    # ------------------------------------------------------------------
    # Public: query
    # ------------------------------------------------------------------

    def get_armed_candidates(self, min_score: float = 0.5) -> list[CandidateRecord]:
        """
        Return candidates with status ARMED or RELEASED and score >= min_score,
        sorted by confidence_score descending.
        """
        active_statuses = {"ARMED", "RELEASED"}
        result = [
            r for r in self._board.values()
            if r.status in active_statuses and r.confidence_score >= min_score
        ]
        result.sort(key=lambda r: r.confidence_score, reverse=True)
        return result

    # ------------------------------------------------------------------
    # Cluster detection
    # ------------------------------------------------------------------

    def _detect_clusters(self, candidates: list[CandidateRecord]) -> set[tuple[int, str]]:
        """
        Return set of (strike, option_type) pairs that belong to a cluster.

        A cluster is ≥3 same-type strikes where each strike is within 2*strike_step
        of at least one other member in the cluster (i.e. the set of strikes is
        contiguous within that adjacency radius).

        Only COILED-or-better candidates count toward cluster membership.
        """
        active_statuses = {"COILED", "ARMED", "RELEASED"}
        adjacency = 2 * self.strike_step
        cluster_members: set[tuple[int, str]] = set()

        for ot in ("CE", "PE"):
            strikes_for_ot = sorted(
                r.strike
                for r in candidates
                if r.option_type == ot and r.status in active_statuses
            )
            if len(strikes_for_ot) < 3:
                continue

            # Group into connected components by adjacency
            components: list[list[int]] = []
            for s in strikes_for_ot:
                placed = False
                for comp in components:
                    if any(abs(s - c) <= adjacency for c in comp):
                        comp.append(s)
                        placed = True
                        break
                if not placed:
                    components.append([s])

            for comp in components:
                if len(comp) >= 3:
                    for s in comp:
                        cluster_members.add((s, ot))

        return cluster_members

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _recompute_baseline(self, key: tuple[int, str]) -> float:
        """Mean vol_delta of baseline-window buckets for this key."""
        history = self._history.get(key, [])
        baseline_vols = [
            int(b["vol_delta"])
            for b in history
            if _BASELINE_START
            <= (b["ts"].time() if isinstance(b["ts"], datetime) else b["ts"])
            <= _BASELINE_END
            and int(b["vol_delta"]) > 0
        ]
        return mean(baseline_vols) if baseline_vols else 0.0

    def _extract_features(
        self,
        key: tuple[int, str],
        history: list[dict],
        underlying_ltp: float,
    ) -> dict:
        """Compute observable features for the most recent state of this strike."""
        last = history[-1]
        recent_6 = history[-6:] if len(history) >= 6 else history

        baseline = self._volume_baseline.get(key, 0.0)
        session_low = self._session_low_ltp.get(key, float("inf"))
        session_high_oi = self._session_high_oi.get(key, 0)

        burst_count = (
            sum(
                1 for b in recent_6
                if baseline > 0 and int(b["vol_delta"]) >= VOLUME_BURST_RATIO * baseline
            )
            if baseline > 0
            else 0
        )

        last_ltp = float(last["ltp"])
        last_oi = int(last["oi"])
        last_imbalance = float(last.get("bid_imbalance", 0.0))

        ltp_vs_low = (last_ltp / session_low) if session_low not in (0.0, float("inf")) else float("inf")
        oi_vs_high = (last_oi / session_high_oi) if session_high_oi > 0 else 0.0

        # OI trend over last 2 buckets
        oi_declining = False
        if len(history) >= _OI_UNWIND_LOOKBACK + 1:
            recent_ois = [int(b["oi"]) for b in history[-((_OI_UNWIND_LOOKBACK + 1)):]]
            oi_declining = all(
                recent_ois[i] < recent_ois[i - 1]
                for i in range(1, len(recent_ois))
            )

        # Underlying 30-min ago
        und_30m_ago = None
        if len(history) >= _DIRECTION_LOOKBACK_BUCKETS:
            und_30m_ago = float(history[-_DIRECTION_LOOKBACK_BUCKETS]["und_ltp"])

        return {
            "last_ltp": last_ltp,
            "last_oi": last_oi,
            "last_bid_imbalance": last_imbalance,
            "session_low_ltp": session_low,
            "session_high_oi": session_high_oi,
            "volume_baseline": baseline,
            "burst_count_6": burst_count,
            "ltp_vs_low_ratio": ltp_vs_low,
            "oi_vs_high_ratio": oi_vs_high,
            "oi_declining": oi_declining,
            "underlying_ltp": underlying_ltp,
            "underlying_ltp_30m_ago": und_30m_ago,
        }

    def _is_coiled(self, key: tuple[int, str], features: dict) -> bool:
        """Return True iff Stage 1 COILED conditions are met."""
        baseline = features["volume_baseline"]
        if baseline <= 0:
            return False

        burst_ok = features["burst_count_6"] >= VOLUME_BURST_MIN_IN_WINDOW

        session_low = features["session_low_ltp"]
        premium_ok = (
            session_low not in (0.0, float("inf"))
            and features["last_ltp"] <= PREMIUM_MAX_RATIO * session_low
        )

        oi_ok = features["oi_vs_high_ratio"] >= OI_HIGH_RATIO

        return burst_ok and premium_ok and oi_ok

    def _is_oi_unwinding(self, history: list[dict]) -> bool:
        """Return True if OI is strictly declining over the last 2 buckets."""
        if len(history) < _OI_UNWIND_LOOKBACK + 1:
            return False
        recent_ois = [int(b["oi"]) for b in history[-((_OI_UNWIND_LOOKBACK + 1)):]]
        return all(recent_ois[i] < recent_ois[i - 1] for i in range(1, len(recent_ois)))

    def _direction_confirmed(
        self,
        option_type: str,
        history: list[dict],
        current_und: float,
    ) -> bool:
        """
        Return True if the underlying has moved toward the strike (i.e. in the
        direction that benefits buyers of this option type) over the last 30 min.

        CE blast: underlying is higher now than 30 min ago (rally toward calls).
        PE blast: underlying is lower now than 30 min ago (sell-off toward puts).
        """
        if len(history) < _DIRECTION_LOOKBACK_BUCKETS:
            return False
        und_30m_ago = float(history[-_DIRECTION_LOOKBACK_BUCKETS]["und_ltp"])
        if option_type == "CE":
            return current_und > und_30m_ago
        else:  # PE
            return current_und < und_30m_ago

    def _compute_score(
        self,
        *,
        coiled: bool,
        oi_unwinding: bool,
        direction_ok: bool,
        imbalance_ok: bool,
        in_cluster: bool,
    ) -> float:
        """
        Compute confidence score in [0.0, 1.0].

        Breakdown:
          0.30  base for being COILED
          +0.20 OI unwinding (writers covering)
          +0.20 direction confirmed
          +0.10 bid imbalance >= threshold
          +0.20 part of a cluster (≥3 adjacent coiled strikes)
        """
        if not coiled:
            return 0.0
        score = 0.30
        if oi_unwinding:
            score += 0.20
        if direction_ok:
            score += 0.20
        if imbalance_ok:
            score += 0.10
        if in_cluster:
            score += 0.20
        return min(score, 1.0)

    def _check_invalidation(
        self,
        key: tuple[int, str],
        record: CandidateRecord,
        history: list[dict],
        underlying_ltp: float,
        now_time: time,
    ) -> Optional[str]:
        """
        Return an invalidation reason string if the candidate should be INVALIDATED,
        or None if it is still valid.

        Invalidation triggers:
          1. Clock past 15:12.
          2. Underlying moved away from strike by > 2 strike steps.
          3. OI is rebuilding after having unwound (only relevant for ARMED/RELEASED).
        """
        # Clock gate already checked in evaluate_all, but defensive check here too
        if now_time >= _CUTOFF:
            return "Clock past 15:12 cutoff"

        # Only invalidate active (non-WATCHING) candidates on direction / OI grounds
        if record.status == "WATCHING":
            return None

        strike, ot = key

        # --- Underlying moved away by > 2 steps ---
        away_threshold = _AWAY_STEP_THRESHOLD * self.strike_step
        if ot == "CE":
            # CE is harmed when underlying moves well below the strike
            distance = strike - underlying_ltp
        else:
            # PE is harmed when underlying moves well above the strike
            distance = underlying_ltp - strike

        if distance > away_threshold:
            return (
                f"Underlying moved away from strike by "
                f"{distance:.0f} pts (threshold {away_threshold})"
            )

        # --- OI rebuilt after unwind (for ARMED/RELEASED only) ---
        if record.status in ("ARMED", "RELEASED"):
            if self._oi_rebuilding_after_unwind(history):
                return "OI rebuilt after prior unwind — blast window closed"

        return None

    def _oi_rebuilding_after_unwind(self, history: list[dict]) -> bool:
        """
        Return True if OI had been declining and is now rising again.

        Pattern: OI went down (writers covering) and then back up (new writers
        entering) — the gamma event did not fire or has reset.

        Requires at least 5 buckets; checks for a valley followed by a rise.
        """
        if len(history) < 5:
            return False
        ois = [int(b["oi"]) for b in history[-5:]]
        # Look for: decline in first half, then rise in last 2
        valley_idx = ois.index(min(ois))
        if valley_idx == 0 or valley_idx >= len(ois) - 1:
            return False
        # There was a decline to the valley, then a rise after it
        declined = ois[0] > ois[valley_idx]
        risen = ois[-1] > ois[valley_idx]
        return declined and risen

    def _do_invalidate(
        self, record: CandidateRecord, reason: str, ts: datetime
    ) -> None:
        """Transition a candidate to INVALIDATED and log."""
        record.status = "INVALIDATED"
        record.invalidation_reason = reason
        logger.info(
            "INVALIDATED: %s %s %s — %s",
            record.symbol, record.strike, record.option_type, reason,
        )

    def _invalidate_all_active(self, reason: str) -> None:
        """Invalidate every non-terminal candidate (called at cutoff time)."""
        for record in self._board.values():
            if record.status not in ("INVALIDATED", "RELEASED"):
                record.status = "INVALIDATED"
                record.invalidation_reason = reason

    def _rerank(self) -> None:
        """Assign rank (1 = best) to all non-INVALIDATED candidates by score."""
        active = [
            r for r in self._board.values() if r.status != "INVALIDATED"
        ]
        active.sort(key=lambda r: r.confidence_score, reverse=True)
        for i, record in enumerate(active, start=1):
            record.rank = i

    def _make_tsym(self, strike: int, option_type: str) -> str:
        """Build a Shoonya-style tsym for reference (best-effort, no API call)."""
        exp_code = self.expiry_date.strftime("%d%b%y").upper()
        cp = "C" if option_type == "CE" else "P"
        return f"{self.symbol}{exp_code}{cp}{strike}"

    def _make_candidate_id(self, strike: int, option_type: str, ts: datetime) -> str:
        """Generate a unique candidate ID."""
        ts_epoch = int(ts.timestamp()) if isinstance(ts, datetime) else 0
        return f"{self.symbol}_{strike}_{option_type}_{ts_epoch}"
