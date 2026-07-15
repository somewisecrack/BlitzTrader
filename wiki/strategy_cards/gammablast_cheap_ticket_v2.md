# GammaBlast Cheap-Ticket Rules — v2

**Status:** validated against recorded ladder data, ready to promote to the live box
**Scopes:** `NIFTY` (Tuesday expiry), `SENSEX` (Thursday expiry)
**Tooling:** `tools/gamma_replay.py`, tests in `tests/test_gamma_replay.py`
**Promoted rules:** `wiki/promoted_filters/gammablast_cheap_ticket_v2_{NIFTY,SENSEX}.json`

## Goal

Enter at the lowest possible premium (≤ Rs 2, tiered up to Rs 6 for the first OTM
strike when the underlying is accelerating) and ride a gamma blast for the
biggest multiple, with tightly defined risk.

## Why (three losing sessions)

| Session | Actual result | Root cause |
|---|---|---|
| Jun 23 NIFTY | 0 trades | Data feed froze 14:03; a 0.90-conf ARMED candidate died on stale ticks |
| Jul 9 SENSEX | **−3,392** (12/13 HARD_STOP) | Bought calls all day into a falling tape; every entry came *after* the 10:42 CE peak |
| Jul 14 NIFTY | **−321** (2/2 HARD_STOP) | "Terminal anticipation" fired with `direction_move_5m_ok=false`; bought 24000 PE while spot held 24050 |

Common failure modes: **counter-trend entries with no direction confirmation**,
**mid-premium entries that bleed on the hard stop**, and **late entries that
always came after the move**.

## The rules

1. **Direction gate (mandatory).** Only enter when the underlying's 5-min move
   points *toward* the strike (rising for a CE, falling for a PE). This removes
   blind "terminal anticipation" — the single change that eliminates every losing
   entry in both replayed sessions.
2. **Velocity gate.** The underlying must have moved ≥ `MIN_UNDERLYING_MOVE_5M`
   points toward the strike (12 for NIFTY, 38 for SENSEX — scaled to the index).
3. **Cheap-entry cap.** Entry premium ≤ Rs 2, with a tiered allowance to Rs 6 for
   the first OTM strike when the underlying is accelerating (≥1.5× the velocity
   threshold) toward it.
4. **Proximity / OTM.** Strike must be on the correct OTM side and within
   `PROXIMITY_PCT` (0.6%) of spot — no dead, far-gone strikes.
5. **Scale + trail exit.** Scale out 50% at 3×, then trail the remainder at 25%
   of the running gain (armed at 2×). Cheap tickets are never hard-stopped on a
   fraction of a 1-rupee premium — the ticket *is* the stop.
6. **Time controls.** No entries after 15:00 (late prints are fake liquidity);
   flatten everything at 15:12.
7. **Re-entry cooldown.** No re-entry on the same strike+side within 15 min of an
   exit (kills the revenge-entry seen on Jul 14 trade #2).
8. **Widen the ladder to ±8.** The legacy ±2 ladder never tracks the deep-OTM
   strikes where a ≤Rs-2 ticket can blast 4-6×, so no cheap entry ever exists.
   `LADDER_OFFSETS = 8`.

## Validation (replay on real recorded ladders)

Run: `python3 tools/gamma_replay.py --ladder-dir <day> --config {nifty|sensex}`

| Session | Actual GammaBlast | v2 rules (replay) | Δ |
|---|---|---|---|
| Jul 14 NIFTY | −321.25 (2 HARD_STOP) | **−40.00** (1 controlled loss) | +281 |
| Jul 9 SENSEX | −3,392 (12 HARD_STOP) | **0.00** (no trade) | +3,392 |

**Honest read:** these rules are capital-preserving, not winner-manufacturing.
On both disaster days there was no clean sub-Rs-2 bang to catch:

- Jul 14's biggest move (24050 CE, 5.50 → 20.80, 3.78×) required buying at the
  exact bottom tick while the tape was *still falling* — the knife-catch the
  direction gate is built to refuse. Once direction confirmed, the premium was
  already Rs 14+ (above the cap).
- Jul 9 SENSEX fell all day: no CE ever had a valid "rising toward strike"
  signal, and the only ≤Rs-2 puts appeared after the 15:00 cutoff and then
  decayed into the close (P76500: 1.95 → 0.95). Refusing to trade was correct.

The edge shows up on **trend days** — a hard early directional move with the
widened ladder tracking a cheap OTM strike in the move's path. The synthetic
`test_winner_pnl_is_positive_and_scaled` exercises exactly that path.

## Deploying to the live box

The live GammaBlast loads scoped promoted rules at startup (its log shows
`Promoted rule: KEY = value (scope=NIFTY)`). Deployment is:

1. Copy `wiki/promoted_filters/gammablast_cheap_ticket_v2_{NIFTY,SENSEX}.json`
   to the box's promoted-rules directory.
2. Restart the GammaBlast session; it applies the scoped thresholds on the next
   expiry day. No engine source edit and no LLM involvement.

**Recommend paper-running (already virtual) for ~4 expiry sessions before
trusting live** — two sessions is not enough to prove the trend-day edge.
