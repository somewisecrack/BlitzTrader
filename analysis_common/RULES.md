# Gamma-Blast Rules — cross-index synthesis (NIFTY 2026-06-09, SENSEX 2026-06-11)

Sources: `analysis_20260609/` (NIFTY weekly expiry, rally day → call blasts
14:27–15:09) and `analysis_20260611/` (SENSEX weekly expiry, reversal day →
put blasts 14:33–15:05). `trigger_scan.py` mechanically replays the trigger
over every strike of both sessions.

## Does the NIFTY rule set transfer to SENSEX?

**The ingredients transfer; the formulation didn't.**

The same four phenomena preceded every blast on both indices, on opposite
sides (calls on the rally day, puts on the reversal day):

1. volume bursts ≥1.5–2× the strike's own midday baseline,
2. premium pinned within ~25% of its post-noon low,
3. OI at/near its session high (crowded short gamma = fuel),
4. underlying staircasing toward the strike,
   and in every blast the move was amplified by a forced **OI unwind**
   (−15.9M NIFTY C23250; −5.4M in one bar on SENSEX P74000).

But the June-9 report stated the rule as *all four true in the same 5-min
bucket with fixed 2× thresholds*. Replayed mechanically (`--strict`), that
version **misses all three NIFTY blasts it was derived from** and all the
SENSEX ones — the conditions co-occur within a ~30-minute window, never in
one bucket. A windowed restatement (≥2 volume bursts in 30 min, OI ≥90% of
session high, premium ≤1.25× post-noon low, underlying nearer the strike
than 30 min ago) scores, across both days:

| | flagged & blasted (≥×3) | flagged, no blast | blasts missed |
|---|---|---|---|
| NIFTY 09-Jun | 3 (C23250, C23300, P23250) | 3 | 0 |
| SENSEX 11-Jun | 5 (P73700–P74100) | 6 | 0 |

**Recall 8/8 across two indices; precision ~47%; lead time 40–65 min.**
Two qualifiers: (a) most "false positives" were near-misses that still
popped ×2–2.7 (the 13:30–13:55 SENSEX call flags paid ×2.5 at the 14:24
spike before dying); the genuinely bad flags were late call fires *during*
the put collapse — wrong side of an established directional move. (b) the
flag is early: entering on it alone bleeds theta for up to an hour.

## General rule set (v2 — arm / detonate / abort)

Everything is normalized to the strike's own session, so nothing
index-specific survives: volume as a multiple of that strike's 12:00–13:30
baseline, premium relative to its post-noon low, OI relative to its session
high, distance in strike-spacing units (50 NIFTY / 100 SENSEX ≈ 0.07–0.13%
of spot).

**ARM** a strike (from ~13:15, re-check each 5-min bucket): in the last
30 min, ≥2 buckets at ≥1.5× baseline volume, AND premium ≤1.25× its
post-noon low, AND OI ≥90% of its session high. This is "writers pressing a
crowded short into a cheap premium" — the fuel. On both days the eventual
blast strikes armed 40–65 min before detonating.

**DETONATE** (entry timing — required, arming alone is not an entry): the
underlying breaks the edge of its post-13:00 range *toward* the armed
strike, or a spike toward the opposite side fails (June-11's 14:24 high was
the put-side detonator). Prefer the armed strike nearest the underlying:
the ×6–10 moves were within 1–2 spacings of spot; tail strikes lag, pay
less, and round-trip to zero (P73700 expired worthless 27 min after its
×4.6 peak).

**ABORT / EXIT**: OI starts unwinding while premium fails to make new highs
(SENSEX C74200 at 14:30 — turned a ×2.6 scalp into a saved round-trip);
underlying stalls ≥10 min before reaching the strike; or ~14:45 passes
without detonation (after that, theta wins). Never take a fresh flag
against a collapse already underway on the other side.

**Confirmation only:** best-5 bid imbalance ≥ +0.3 leaned the right way
before every blast but also during quiet stretches; spreads gave no advance
warning on either index.

Shared regularity worth tracking: on both expiry days the decisive move
began ~14:25 IST.

**Status: n=2**, both V-shaped expiry sessions. The precision number (and
the 13:30/14:45 clock bounds) need a no-blast control day and a trend day
before any of this is tradeable.
