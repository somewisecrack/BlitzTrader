# GammaBlast research

Empirical gamma-blast study that the GammaBlast app is built on. All
gamma-blast analysis lives here (not in the BlitzTrader repo).

## Contents

| Path | What |
|---|---|
| `analysis_20260609/` | NIFTY 09-Jun 0-DTE — call blasts ×10 (rally day) |
| `analysis_20260611/` | SENSEX 11-Jun 0-DTE — put blasts ×6 (reversal day) |
| `analysis_20260616/` | NIFTY 16-Jun 0-DTE — **no blast** (pin day, control case) |
| `analysis_20260618/` | SENSEX 18-Jun 0-DTE — call-cluster blast ×30; app missed it (recorder bug) |
| `analysis_common/` | `RULES.md` — cross-index rule synthesis; trigger/arm scripts |
| `analysis_cross/` | Cross-day survey — blasts are a 0-DTE-only phenomenon |

## Headline findings

- Gamma blasts (option ×3–10 in 5–15 min) occur **only at 0 DTE**, in the final
  ~90 minutes — a hard cliff at DTE 1.
- Two-stage trigger: **COILED** (premium at session low + OI at session high +
  volume burst) → **RELEASE** (underlying breaks range toward the cluster, or an
  opposite-side thrust fails; OI flips building→unwinding).
- Terminal gamma is **necessary but not sufficient**: 16-Jun NIFTY had the gamma
  but pinned (no break) → no blast. 18-Jun SENSEX had the gamma + an upside break
  from a failed put probe → ×30 call-cluster blast.

Start with `analysis_common/RULES.md` and `analysis_cross/REPORT.md`.
