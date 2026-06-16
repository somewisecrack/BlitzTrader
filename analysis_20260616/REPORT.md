# NIFTY 16-Jun-2026 expiry — 0-DTE NO-BLAST control case

Today's expiry (NIFTY weekly, Tuesday 16-Jun, **0 DTE**) produced **no gamma
blast**. The entire ATM±2 ladder (23850–24050 CE/PE) topped out at **×2.27**
(C23950, 14:56) on a clean 15-min basis with a ≥5-rupee base floor. Nothing
approached the ×3 threshold, let alone the ×6–10 of a real blast.

## Data
ATM±2 ladder recorded once/min, 09:15–15:15 IST: strikes 23850, 23900, 23950,
24000, 24050 (CE+PE), OHLCV+OI. Source folder
`1A9uamJEZACkjLLfL5HQkxm5N710qeQbD`. Raw under `raw/NIFTY/`.

## Why no blast — the underlying pinned
NIFTY spot held a **101.9-point band all session (23897.7–23999.5, 0.43%)** and
closed at 23988.2, pinned between the 23950 and 24000 strikes. Opened 23916.3,
drifted up ~70 pts by early afternoon, chopped sideways into the close. No
directional break in either direction.

| Date | Index | DTE | spot range | max 15m mult | blasts ≥3× |
|---|---|---|---|---|---|
| 09-Jun | NIFTY | 0 | 172 pts | ×10.4 | 3 |
| 11-Jun | SENSEX | 0 | 855 pts | ×6.1 | 5 |
| **16-Jun** | **NIFTY** | **0** | **102 pts (0.43%)** | **×2.27** | **0** |

## The late pops were pin whipsaw, not a blast
The ×2-ish moves clustered in the terminal-gamma window (14:44–14:56) but were
**not** a blast:
- Both calls AND puts popped simultaneously (C23950, C24000, P23950, P24000 all
  ×2.0–2.3). A real blast is one-sided; both-sided means spot oscillating across
  the pin, not breaking out.
- Tiny in rupee terms: C24000 13→18→4→11→1.7; P23950 5→10→0.25. All round-tripped
  to near-zero by expiry. The "×2–3 multiple" is an artifact of a few-rupee base.
- Ignore the raw-scan "P23950 ×2.88": contaminated by a settlement glitch (its
  14:35 tick reads `ltp=23990.9`, the spot leaking into the premium field).
  Cleaned figure is ×2.25.

## What it confirms about the rules
First **0-DTE no-blast** day in the set — a clean control that separates the
necessary ingredients:

- **Stage 0 (terminal gamma, 0-DTE):** present.
- **Stage 1 (coiling / OI build):** present — C24000 OI climbed 60.5M→69.5M into
  the close (writers piling into the pin ceiling).
- **Stage 2 (RELEASE — directional range break + OI unwind):** never happened;
  spot pinned. → **no detonation.**

This is exactly the `analysis_common/RULES.md` thesis: terminal gamma is
**necessary but not sufficient** — a blast also needs the directional break.
Today had the gamma but not the move. GammaBlast would have taken no position:
Stage 1 might flag 24000C as coiled, but Stage 2 never triggers, so nothing
reaches RELEASED.

Clean per-strike 15-min multiples (base ≥5 rs):
```
C23950 x2.27 14:56  20.8->47.2     P23900 x1.62 10:10
P23950 x2.25 14:44   5.2->11.8     C23900 x1.59 10:22
C24000 x2.24 14:55   5.1->11.4     P24050 x1.42 14:45
P24000 x2.03 14:44  22.2->45.1     C23850 x1.41 10:24
P23850 x1.72 10:10  15.6->26.9     C24050 x1.39 13:37
```
