"""
Tests for tools/gamma_replay.py — the GammaBlast cheap-ticket rule engine.

These use small, deterministic synthetic ladders (no external data) so they run
anywhere. Each ladder is a list of (hhmmss, ltp, underlying) rows for one strike.
"""
import json
from pathlib import Path

import pytest

from tools import gamma_replay as gr
from tools.gamma_replay import RuleConfig, CONFIGS


def _write_ladder(tmp_path: Path, strike: int, opt_type: str, rows: list) -> None:
    """Write one strike's JSONL file. rows = list of (hhmmss, ltp, underlying)."""
    fname = tmp_path / f"NIFTY14JUL26{'C' if opt_type == 'CE' else 'P'}{strike}.jsonl"
    with open(fname, "w") as fh:
        for hhmmss, ltp, u in rows:
            rec = {
                "timestamp_ist": f"2026-07-14T{hhmmss}.000000",
                "symbol": "NIFTY", "expiry": "14-JUL-2026",
                "strike": strike, "option_type": opt_type,
                "underlying_ltp": u, "ltp": ltp,
            }
            fh.write(json.dumps(rec) + "\n")


# ── Config / promoted-rules ─────────────────────────────────────────────────────


def test_promoted_rules_roundtrip():
    cfg = CONFIGS["tiered6"]
    rules = cfg.promoted_rules()
    assert rules["ENTRY_MAX_PREMIUM"] == 2.0
    assert rules["ENTRY_MAX_PREMIUM_TIER"] == 6.0
    assert rules["ENTRY_CUTOFF"] == "15:25"
    assert rules["TIME_STOP"] == "15:30"
    assert rules["RECORDER_END"] == "15:30"
    assert rules["LOT_SIZE"] == 25


def test_sensex_velocity_is_scaled_up():
    assert CONFIGS["sensex"].min_underlying_move_5m > CONFIGS["nifty"].min_underlying_move_5m


# ── Direction gate ──────────────────────────────────────────────────────────────


def test_direction_gate_rejects_counter_trend_call(tmp_path):
    """A CE while the underlying is FALLING must not trigger an entry (knife-catch)."""
    # 24100 CE, spot falling 24080 -> 24020, premium cheap. Direction toward CE = up.
    rows = [
        ("14:40:00", 1.5, 24080),
        ("14:41:00", 1.4, 24070),
        ("14:42:00", 1.3, 24060),
        ("14:43:00", 1.2, 24050),
        ("14:44:00", 1.1, 24040),
        ("14:45:00", 1.0, 24030),
        ("14:46:00", 0.9, 24020),
    ]
    _write_ladder(tmp_path, 24100, "CE", rows)
    strikes, _ = gr.load_ladder_dir(tmp_path)
    trades = gr.replay(strikes, CONFIGS["nifty"])
    assert trades == []


def test_direction_gate_allows_toward_trend_call(tmp_path):
    """A cheap CE while the underlying rips UP toward it should trigger an entry."""
    # 24100 CE, spot ripping 24010 -> 24095, premium starts cheap then blasts.
    rows = [
        ("14:40:00", 1.0, 24010),
        ("14:41:00", 1.2, 24030),
        ("14:42:00", 1.6, 24050),
        ("14:43:00", 2.0, 24065),   # +55 over 3 ticks toward strike -> direction ok
        ("14:44:00", 4.0, 24080),
        ("14:45:00", 8.0, 24092),   # blasts
        ("14:46:00", 6.0, 24085),
        ("14:47:00", 3.0, 24070),
    ]
    _write_ladder(tmp_path, 24100, "CE", rows)
    strikes, _ = gr.load_ladder_dir(tmp_path)
    trades = gr.replay(strikes, CONFIGS["nifty"])
    assert len(trades) == 1
    assert trades[0].opt_type == "CE"
    assert trades[0].entry_price <= 2.0  # entered cheap


# ── Premium cap ─────────────────────────────────────────────────────────────────


def test_premium_cap_blocks_expensive_entry(tmp_path):
    """A strike moving in the right direction but priced above the cap is skipped."""
    rows = [
        ("14:40:00", 8.0, 24010),
        ("14:41:00", 9.0, 24030),
        ("14:42:00", 10.0, 24050),
        ("14:43:00", 12.0, 24065),
        ("14:44:00", 15.0, 24080),
    ]
    _write_ladder(tmp_path, 24100, "CE", rows)
    strikes, _ = gr.load_ladder_dir(tmp_path)
    # strict2 = hard Rs 2 cap, no tier
    trades = gr.replay(strikes, CONFIGS["strict2"])
    assert trades == []


# ── Exit management ─────────────────────────────────────────────────────────────


def test_time_stop_flattens_position(tmp_path):
    """An open position is flattened at the 15:30 close even if still rising."""
    rows = [
        ("15:20:00", 1.0, 24010),
        ("15:21:00", 1.5, 24040),   # direction ok, cheap, before 15:25 cutoff -> entry
        ("15:29:00", 5.0, 24095),
        ("15:30:10", 6.0, 24099),   # >= 15:30 time stop -> exit here
        ("15:31:00", 9.0, 24101),   # never reached (after the close)
    ]
    _write_ladder(tmp_path, 24100, "CE", rows)
    strikes, _ = gr.load_ladder_dir(tmp_path)
    trades = gr.replay(strikes, CONFIGS["nifty"])
    assert len(trades) == 1
    assert trades[0].exit_reason == "TIME_STOP"
    assert trades[0].exit_ts <= "15:30:10"


def test_winner_pnl_is_positive_and_scaled(tmp_path):
    """A clean blast produces a positive P&L sized by the 25-lot multiplier."""
    rows = [
        ("14:40:00", 1.0, 24010),
        ("14:41:00", 1.5, 24040),   # entry ~1.5
        ("14:42:00", 3.5, 24070),   # >3x -> scale out
        ("14:43:00", 6.0, 24090),   # peak
        ("14:44:00", 5.0, 24085),   # trail retrace -> exit remainder
        ("14:45:00", 2.0, 24060),
    ]
    _write_ladder(tmp_path, 24100, "CE", rows)
    strikes, _ = gr.load_ladder_dir(tmp_path)
    trades = gr.replay(strikes, CONFIGS["nifty"])
    assert len(trades) == 1
    t = trades[0]
    assert t.pnl > 0
    # pnl == (exit - entry) * 25
    assert t.pnl == pytest.approx((t.exit_price - t.entry_price) * 25, abs=0.01)


# ── Cooldown ────────────────────────────────────────────────────────────────────


def test_cooldown_blocks_immediate_reentry(tmp_path):
    """After an exit, the same strike+side cannot re-enter within cooldown_min."""
    # Two blast-and-fade cycles back to back on the same strike.
    rows = [
        ("14:20:00", 1.0, 24010),
        ("14:21:00", 1.5, 24040),   # entry #1
        ("14:22:00", 4.0, 24080),   # scale
        ("14:23:00", 2.0, 24050),   # trail exit ~14:23
        ("14:24:00", 1.0, 24010),   # cheap again, direction will re-point up
        ("14:25:00", 1.5, 24040),   # would re-enter but within 15-min cooldown
        ("14:26:00", 4.0, 24080),
        ("14:27:00", 2.0, 24050),
    ]
    _write_ladder(tmp_path, 24100, "CE", rows)
    strikes, _ = gr.load_ladder_dir(tmp_path)
    trades = gr.replay(strikes, CONFIGS["nifty"])
    # Only the first cycle should trade; the second is inside cooldown.
    assert len(trades) == 1
    assert trades[0].entry_ts == "14:21:00"


# ── Entry cutoff ────────────────────────────────────────────────────────────────


def test_no_entry_after_cutoff(tmp_path):
    """Cheap directional setups after the 15:25 entry cutoff are ignored."""
    rows = [
        ("15:26:00", 1.0, 24010),
        ("15:27:00", 1.5, 24040),   # direction ok + cheap, but after 15:25 cutoff
        ("15:28:00", 4.0, 24080),
    ]
    _write_ladder(tmp_path, 24100, "CE", rows)
    strikes, _ = gr.load_ladder_dir(tmp_path)
    trades = gr.replay(strikes, CONFIGS["nifty"])
    assert trades == []


def test_entry_allowed_in_late_window(tmp_path):
    """A cheap directional setup at 15:20 (after old 15:00 cutoff) now trades."""
    rows = [
        ("15:18:00", 1.0, 24010),
        ("15:19:00", 1.2, 24030),
        ("15:20:00", 1.6, 24050),   # direction ok + cheap, within new 15:25 window
        ("15:21:00", 4.0, 24080),
        ("15:22:00", 6.0, 24092),
        ("15:23:00", 3.0, 24070),
    ]
    _write_ladder(tmp_path, 24100, "CE", rows)
    strikes, _ = gr.load_ladder_dir(tmp_path)
    trades = gr.replay(strikes, CONFIGS["nifty"])
    assert len(trades) == 1
    assert trades[0].entry_ts <= "15:20:00"


# ── Loader robustness ───────────────────────────────────────────────────────────


def test_loader_skips_bad_and_zero_ticks(tmp_path):
    fname = tmp_path / "NIFTY14JUL26C24100.jsonl"
    with open(fname, "w") as fh:
        fh.write("not json\n")
        fh.write(json.dumps({"timestamp_ist": "2026-07-14T14:40:00.0",
                             "symbol": "NIFTY", "strike": 24100, "option_type": "CE",
                             "underlying_ltp": 24010, "ltp": 0}) + "\n")  # zero ltp
        fh.write(json.dumps({"timestamp_ist": "2026-07-14T14:41:00.0",
                             "symbol": "NIFTY", "strike": 24100, "option_type": "CE",
                             "underlying_ltp": 24040, "ltp": 1.5}) + "\n")  # good
    strikes, sym = gr.load_ladder_dir(tmp_path)
    assert sym == "NIFTY"
    assert "C24100" in strikes
    assert len(strikes["C24100"].ticks) == 1  # only the valid, positive-ltp row
