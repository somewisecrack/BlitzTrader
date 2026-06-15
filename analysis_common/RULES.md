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

---

## Stage 0 — the gate: only at 0 DTE (cross-day survey, 09–15 Jun)

A survey of every day with per-strike data (8 index-sessions, see
`analysis_cross/REPORT.md`) shows ×3+ blasts occur **only on expiry day
(0 DTE)**. There is a cliff, not a fade: the two 0-DTE sessions blast ×6–10
(3–5 strikes each); all six non-expiry sessions — including T-1 — cap at
×1.3–1.9 across the whole ATM ladder.

The control case is 12-Jun SENSEX: a +1150-point (~1.5%) intraday index move
with **no** blast (max ×1.66), because it was 6 DTE. The same-size move at
0 DTE (11-Jun SENSEX) gave ×6–9. So a blast needs terminal gamma, not just a
big underlying move; gamma ~1/√T explodes only in the final ~90 minutes, which
is exactly when both blasts fired (14:25–15:05).

**Gate: only hunt blasts at 0 DTE, arming in the final ~2 hours. On any
non-expiry day, skip — the fuel (terminal gamma) is absent no matter how
coiled a strike looks or how far the index travels.** Stages 1–2 below operate
only inside this gate.

## Operational playbook — two stages

All thresholds are self-normalized to each strike's own session (volume vs
that strike's 12:00–13:25 mean bucket; premium vs its post-12:00 low; OI vs
its session high). "Spacing" = 50 NIFTY / 100 SENSEX. Source: `arm_timeline.py`.

### Stage 1 — Shortlist the watchlist (build from 13:30, refresh every 5 min)

Universe: ATM ladder within ~1% of spot (re-center as spot moves; the ATM
journal does this).

Put a strike on the watchlist when ALL three hold on a 5-min bucket at/after
13:30:
- **Volume:** ≥2 of the last 6 buckets at ≥1.5× the strike's own
  12:00–13:25 mean bucket volume.
- **Premium:** last close ≤1.25× the strike's running post-12:00 low.
- **OI:** ≥90% of the strike's running session-high OI (crowded and still
  building, not yet unwinding).

This yields ~6–8 names and on both days contained every eventual blaster.
Rank within the list:
1. **Cluster first.** ≥3 adjacent same-type strikes arming together is the
   high-conviction wall (SENSEX 73700–74000P all armed 13:30). Isolated
   single arms are low quality.
2. **Near the money.** Keep strikes within 1–2 spacings of spot; the ×6–10
   moves came from there. Tails show big backtested multiples but miss more
   and expire worthless.
3. Both sides will appear — do NOT pick a side or enter here. This stage
   only defines what to watch.

### Stage 2 — Take position (arm the watchlist; act on the break, ~14:15–14:45)

Entry trigger (the DETONATE — required; arming alone is never an entry):
the underlying breaks the edge of its post-13:00 range, OR a thrust to one
side fails and reverses (SENSEX 14:24 spike to 74135 failed → puts). Enter
the watchlist strikes on the side the break favors.

- **Strike pick:** the 1–2 shortlisted strikes nearest spot on the firing
  side. Skip the deep tail even though its backtest multiple is biggest.
- **Confirm at entry (all must align, else skip):** best-5 bid imbalance
  ≥ +0.3 on the firing side; OI on the chosen strike still flat/up (unwind
  not yet started); clock before ~14:45.
- **Scale:** partial on the break, add as OI begins to unwind fast — that
  short-cover is the accelerant (P74000 −5.4M in one bar drove its ramp).
- **Abort before entry:** underlying stalls ≥10 min short of the strike, or
  ~14:45 passes with no break.
- **Exit / invalidate after entry:** OI starts *building* again (fresh
  writers) while premium fails to make new highs → exit (SENSEX C74200,
  14:30). Take profit when the OI unwind stalls and premium stops making new
  highs. Hard time stop at the close — never hold a far-OTM into expiry
  hoping (P73700: ×4.6 then to 0.30).

### Worked traces
- **NIFTY 09-Jun:** 13:45 shortlist = {23250C, 23300C} (clean, calls only).
  Rally never reversed → enter calls on the continuation break. ×4.7 / ×5.4.
- **SENSEX 11-Jun:** 13:30 shortlist = put wall 73700–74000 (cluster) +
  74200–74300C (contamination). Hold side. 14:24 up-spike fails → enter
  near-money puts 73900/74000P at 14:25–14:35 → ×6.1 / ×4.0, accelerated by
  the OI unwind.

---

## Condition-based (time-free) formulation

The clock anchors above (13:30 arm, 14:45 cutoff, the noon volume baseline)
were proxies for market *states*, not rules. Expressed as states, time drops
out — the conditions can only become true once theta+drift have compressed
premium and writers have loaded OI, which is *why* they fire late. Replace
the fixed noon baseline with each strike's trailing-median bucket volume.

### Stage 1 condition — the COILED strike (per strike, session-relative)
A strike is a candidate when all hold simultaneously:
- **Premium crushed:** current premium ≤ ~1.25× its session low, and that
  low is a large drop from its session high (it has actually been compressed,
  not merely quiet).
- **OI loaded and still building:** OI ≥ ~90% of its session high AND net OI
  change positive over the trailing ~30 min (writers still pressing).
- **Absorption volume:** trailing volume ≥ ~1.5× the strike's own trailing-
  median bucket volume (turnover elevated while premium does NOT recover).

One-line tell: **premium making session lows while OI makes session highs on
rising volume** — a price/positioning divergence. **Cluster gate:** focus
where ≥3 adjacent same-type strikes are coiled together; that wall is the
high-conviction zone. Both option sides can coil at once — Stage 1 does not
pick a side.

### Stage 2 condition — the RELEASE (no clock)
Enter when, on a coiled cluster:
- **Direction commits:** underlying breaks the edge of its established
  intraday range toward the cluster, OR a thrust to the opposite side fails
  and reverses.
- **OI regime flips:** the loaded OI on the firing-side strikes turns from
  building to **unwinding** (writers covering) — price-down/OI-up flips to
  premium-up/OI-down. This flip is the detonation and the cleanest single
  confirmation; it is also the accelerant.
- **Imbalance confirms:** best-5 bid imbalance ≥ +0.3 on the firing side.
- **Strike:** the coiled strikes nearest spot on the firing side.

No-go / exhaustion (replaces the 14:45 cutoff with a state): do not enter if
OI has already substantially unwound or premium has already re-expanded —
the fuel is spent. Exit when the OI unwind stalls and premium stops making
new highs, or if OI starts re-building (fresh writers) against the position.
