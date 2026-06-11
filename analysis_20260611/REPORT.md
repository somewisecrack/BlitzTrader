# Gamma-Blast Study — SENSEX weekly options, expiry day 2026-06-11

Data: BlitzTrader per-minute OHLCV + best-5 depth snapshots for the tracked
ATM ladder (strikes 73400–74500 CE/PE, 11-JUN-2026 expiry, BFO), pulled from
the Drive export. 155–349 snapshots per strike (the ladder grew as the index
rallied through new strikes), 09:15–15:15 IST. Companion study:
`analysis_20260609/` (NIFTY 9-Jun expiry).

## Session narrative (underlying)

Open ~73650 → **strong morning rally** through 74000 (09:53) to the midday
high **74366 at 12:30** (ATM journal walked 73600 → 74500 by 12:31) →
afternoon bleed 74366 → ~74000 by 14:05 → chop 74000–74130 → **failed spike
to 74135 at 14:24–14:28** → **violent reversal: −340 pts 14:30–15:05 to
73770** → bounce into the 73842 close (pinned between 73800/73900).

## Gamma-blast events identified

| Strike | Window (IST) | Move | Multiple | Driver |
|---|---|---|---|---|
| **P73900** | 14:35 → 14:47, ran to 15:05 | 11.90 → 72.75 → ~128 | ×6.1 in 12m, ×9.3 in 30m | Reversal through 73900 |
| **P74000** | 14:35 → 14:50, ran to 15:05 | 31.85 → 126.90 → ~220 | ×4.0 in 15m, ×6.7 in 30m | Same (ITM-ward) |
| **P73700** | 14:35 → 14:48 | 3.25 → 14.90 | ×4.6 | Reversal tail — then bled to 0.30 (finished OTM) |
| **P73800** | 14:35 → 14:48 | 6.60 → 28.70 | ×4.4 | Same |
| **P74100** | 14:33 → 14:47 | 62.75 → 205.50 | ×3.3 | Same |
| **C74100–C74500** | 14:24 → 14:28 | e.g. C74200 18.30 → 48.25, C74400 4.10 → 11.70 | ×2.1–2.9 in 4m | The failed up-spike — collapsed to ~0 within 40m |
| **C73800** | 15:13 → 15:15 | 8.70 → 19.55 | ×2.3 | Closing pin battle at 73842 |

(The 09:37–10:20 call gains — ×1.5–2.0 in 15m on 73400–74000 CE — were the
morning trend: delta-driven, premiums already large, not blasts.)

## Predictive signals in the 12:00+ data (before the 14:35 blast)

Same framework as the 9-Jun NIFTY study; signal numbering matches.

### 1. Volume acceleration with flat/falling premium (lead: ~20–30 min)
P73900 5-min volume ran 1.5–4.5M through 12:00–13:00 (baseline), 7–10M from
13:20, then **10–20M over 14:05–14:25 while the premium fell 45 → 15.85 — a
new post-noon low**. P74000 identical (3.5–7M baseline → 18–27M by 14:25,
premium 100 → 31). 3–5× baseline volume into falling premium = absorption.

### 2. Record OI build into a premium low = fuel (lead: ~1h, trigger ~10 min)
P73900 OI climbed 5.4M (12:00) → 8.62M (14:25, session high) as premium made
its low; P74000 8.1M → 11.8M (14:25) → 12.85M (14:30). During the blast the
shorts were carried out: P74000 unwound **−5.4M in the 14:45 bar alone**,
P73900 −1.7M/−2.6M/−1.7M across 14:45–15:05. Exactly the June-9 pattern:
premium low + OI high = crowded short gamma, detonated by the index move.

### 3. Underlying lower highs toward the strikes (lead: ~60 min)
74366 (12:30) → 74314 (13:00) → 74147 (13:30) → 74135 (14:25). The 14:24
spike was the third lower high; puts at 73900/74000 were 100–200 pts away
with premiums at day lows when it failed.

### 4. Best-5 bid imbalance (confirmation)
P73900 +0.37/+0.34/+0.31 over 14:20–14:35; P74000 +0.36/+0.38 at
14:15–14:20. As on June 9, useful confirmation but not standalone (it was
also positive in quiet stretches).

### 5. The call-side fired too — and shows why the exit rule matters
C74200 had the *same* composite at 14:20: OI at session high (8.76M),
premium at day low (18.30), elevated volume (14.8M), +0.40 imbalance. The
14:24 spike paid ×2.6 in 4 minutes — then the move failed: by 14:30 OI was
unwinding (−413k) with price fading, the documented invalidation condition.
Honoring it got you out near 29; holders rode it to 0.15. On a reversal day
the composite flags both sides ~10 min apart; the OI-unwind exit is what
separates the scalp from the round-trip.

### Non-signals / misses
- Spreads again gave no advance warning (pinned 0.05–0.13 on the blast puts
  until the move was underway).
- **Trigger miss:** P73700 did ×4.6 without qualifying — its OI was *not* at
  a session high (5.6–5.9M vs 6.7M earlier). The composite catches the
  crowded strikes, not every tail along for the ride; P73700 also expired
  worthless 27 minutes after its peak, the worst risk/reward of the set.

## Composite trigger — second-session validation

Applying the June-9 rule (after 13:30: ① 5-min vol ≥2× the 12:00–13:30
baseline for 2+ buckets, ② premium within ~25% of post-noon low, ③ OI
at/near session high, ④ 2+ lower highs toward the strike for puts):

- **P73900 / P74000: all four true by ~14:25–14:30** — about 10 minutes
  before liftoff and 35–40 before the peak. ×6–9 followed.
- **C74200 (and neighbors): flagged ~14:20**, paid ×2.6, then invalidated at
  14:30 — exit rule required.
- Both sessions' decisive move began ~14:25 IST; the rule's 13:30 arming
  time looks right for both NIFTY and SENSEX weeklies.
- Still descriptive, n=2, both V-shaped expiry days. Next: a no-blast
  control day to measure false-positive rate.

## Reproducing

- `find_blasts.py` — scans `raw/SENSEX2*_ohlcv.jsonl` for max rolling
  5/15/30-min LTP multiples per strike.
- `signals.py SENSEX2661173900PE ...` — merged OHLCV+depth 5-min signal
  table (volume delta, OI delta, best-5 imbalance, spread, underlying).
- `raw/` (not committed) — copy the 48 jsonl files + `SENSEX_ATM_STRIKES.jsonl`
  from the Drive folder for 2026-06-11.
