# Gamma-Blast Study — NIFTY weekly options, expiry day 2026-06-09

Data: BlitzTrader per-minute OHLCV + best-5 depth snapshots for the tracked
ATM ladder (strikes 23050–23350 CE/PE, 9-JUN-2026 expiry), pulled from the
Drive export (`Gamma/NIFTY`). 356 snapshots per strike, 09:15–15:15 IST.

## Session narrative (underlying)

Open ~23232 → morning slide to ~23050–23100 by 11:23 → midday chop
23180–23220 → dip to 23180 around 13:55 → **strong rally 14:20–14:55 to
23274** → **sharp 33-pt reversal 14:55–15:00** → close ~23245.

## Gamma-blast events identified

| Strike | Window (IST) | Move | Multiple | Driver |
|---|---|---|---|---|
| **C23250** (ATM) | 14:27 → 14:41, peak ~14:52 | 4.95 → 23.20 → ~53 | ×4.7 in 15m, ×10+ to peak | Rally through 23250 |
| **C23200** | 14:26 → 14:40, peak ~14:55 | 21.95 → 62.35 → 82.80 | ×2.8 in 15m | Same rally (ITM-ward) |
| **C23300** | 14:20 → 14:54 | 0.85 → 5.90 | ×6.9 | Rally tail toward 23300 |
| **P23250** | 15:09 → 15:15 | 0.75 → 7.80 | ×10.4 in 6m | Closing reversal squeeze |
| **P23300** | 14:57 → 15:02 | 26.30 → 59.95 | ×2.3 in 5m | Same reversal |

(The 11:34–11:48 call bounce off the morning low was ×1.5–2.2 in 15m —
sharp but delta-driven, not a true blast. The 13:43–13:58 put pop, ×2.3–2.6,
was the same character.)

## Predictive signals in the 12:00+ data (before the 14:26 blast)

Ranked by how clearly they showed up:

### 1. Volume acceleration with flat/falling premium (lead: ~20–30 min)
C23250 5-min traded volume ran 20–30M contracts through 12:00–13:30
(baseline). From 14:05 it stepped up to 40–66M **while the premium made new
day lows (5–8)** — buyers absorbing without price impact. C23300 showed the
same (20M → 39–53M from 14:10). A volume rate >2× the midday baseline on a
near-ATM strike whose premium is NOT rising is accumulation, not hedging.

### 2. Record OI build into a premium low = fuel for the squeeze (lead: hours, trigger ~15 min)
C23250 OI rose almost monotonically 32.2M (12:00) → 49.9M (14:30) as the
premium decayed 20 → 6: writers pressing shorts all afternoon. During the
blast 14:40 bar, **OI unwound −15.9M in 5 minutes** — forced short covering
is what turned a delta move into a blast. The predictive form: on expiry
afternoon, a near-ATM strike at its premium low with OI still making highs
is a crowded short-gamma position; any push of the underlying through the
strike detonates it.

### 3. Underlying making higher lows toward the strike while the premium fails to respond (lead: ~30 min)
From 13:55 → 14:25 the index stepped 23183 → 23186 → 23190 → 23213, closing
to within 37 pts of 23250, yet C23250 traded at 7.65 — versus ~20 at 12:00
when the index was *further* (65 pts) away. Premium-to-distance compression
of that order (same moneyness, ~60% cheaper, 50 min to expiry) is the coiled
spring: IV/theta had been crushed and gamma was about to take over.

### 4. Persistent best-5 bid imbalance (worked for OTM C23300, weak for ATM)
C23300 depth showed bid/ask quantity imbalance of +0.3 to +0.56 from 14:00
onward, 35+ minutes before its move, with order-count imbalance matching.
On the ATM C23250 the imbalance stayed mildly positive (+0.1/+0.2) all day,
so it discriminated poorly there. Useful as a confirmation on OTM strikes,
not standalone.

### 5. The put-side mirror predicted the closing blast
While the rally ran (14:35–14:55), P23250 OI **quadrupled 12M → 53M** as its
premium collapsed 40 → 2.7 — writers selling the top aggressively. At 15:00
a 33-pt reversal forced −19.6M OI unwind and the put went ×10 in 6 minutes.
Same rule as (2), opposite side: late-day OI explosion into a near-zero
premium is a loaded spring for any snapback.

### Non-signals
Bid-ask spreads stayed pinned at ~0.05 on OTM strikes until *during* the
blasts (C23250 widened 0.05 → 0.11 only as it ran) — no advance warning.

## Suggested composite trigger (to validate on more days)

On expiry day after 13:30, flag a strike when ALL of:
1. 5-min volume ≥ 2× its 12:00–13:30 baseline for 2+ consecutive buckets,
2. premium within ~25% of its post-noon low (i.e., move not yet priced),
3. OI at/near session high (short fuel present),
4. underlying has made 2+ higher lows (calls) / lower highs (puts) toward
   the strike in the last 30 min.
Exit/invalidate if OI starts unwinding without price follow-through.

**Caveat:** this is one session and one regime (expiry-day V-shape). The
thresholds above are descriptive of 2026-06-09, not yet predictive — they
need testing against more expiry days, including days with no blast, to
measure false-positive rates.

## Reproducing

- `find_blasts.py` — scans `raw/*_ohlcv.jsonl` for max rolling 5/15/30-min
  LTP multiples per strike.
- `signals.py NIFTY09JUN26C23250 ...` — merged OHLCV+depth 5-min signal
  table (volume delta, OI delta, best-5 imbalance, spread, underlying).
- `raw/` (not committed) — copy the 28 jsonl files from
  Drive `Gamma/NIFTY/` for 2026-06-09.
