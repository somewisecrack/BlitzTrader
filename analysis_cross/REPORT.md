# Cross-day gamma-blast survey — do blasts occur off expiry?

Question: across all recorded option data (not just the two expiry days already
studied), do ×3+ gamma blasts appear on non-expiry days, and what predicts them?

## Data available

Per-strike ATM option ladders (OHLCV + depth) are exported only from
**09-Jun-2026 onward** — earlier date folders (Apr, most of May/early-Jun)
hold only aggregate feed/indicator files, no per-strike ladders. The usable
universe is therefore the seven index-sessions of 09–15 Jun:

Detection used OHLCV only (`scan_day.py`): max rolling 5/15/30-min LTP
multiple per strike, with index-settlement "leak" prints filtered. Depth was
not needed to answer the detection question.

Data hygiene note: the bulk download via sub-agents produced repeated
cross-folder contamination (agents grabbing already-expired 09/11-Jun
contracts into later-date folders) and one corrupt decode. Every day was
byte-verified; contaminated folders were purged and re-pulled from exact
folder IDs. The table below is post-verification.

## Result — blasts are a 0-DTE phenomenon, full stop

| Date | Index | Expiry | DTE | strikes | underlying range | max 15-min mult | strikes ≥3× |
|---|---|---|---|---|---|---|---|
| 09-Jun | NIFTY | 09-Jun | **0** | 14 | 23106–23278 | **×10.4** (P23250) | 3 |
| 11-Jun | SENSEX | 11-Jun | **0** | 24 | 73538–74393 | **×6.1** (P73900) | 5 |
| 15-Jun | NIFTY | 16-Jun | 1 | 12 | 23820–24007 | ×1.72 (P23800) | 0 |
| 15-Jun | SENSEX | 18-Jun | 3 | 14 | 76148–76813 | ×1.27 (P76400) | 0 |
| 12-Jun | NIFTY | 16-Jun | 4 | 5* | 23316–23642 | ×1.91 (C23450) | 0 |
| 11-Jun | NIFTY | 16-Jun | 5 | 18 | 23074–23327 | ×1.33 (P23000) | 0 |
| 10-Jun | NIFTY | 16-Jun | 6 | 14 | 23201–23423 | ×1.47 (C23300) | 0 |
| 12-Jun | SENSEX | 18-Jun | 6 | 18 | 74454–75604 | ×1.66 (C74900) | 0 |

*12-Jun NIFTY: partial ladder (5 near-ATM strikes); does not change the verdict.

There is a **cliff between DTE 0 and DTE 1**, not a gentle fade. Every
non-expiry session — including the day *before* expiry (15-Jun NIFTY, DTE 1) —
caps out at ×1.3–1.9 across the entire ATM ladder. Only the two 0-DTE
sessions blast, and they blast hard (3–5 strikes each at ×3–10).

## The decisive control: 12-Jun SENSEX

12-Jun SENSEX had a **large underlying move — +1150 points (~1.5%), 74454→75604
intraday** — yet not a single strike blasted (max ×1.66). Compare 11-Jun
SENSEX (0-DTE), where a similar-magnitude index move produced ×6–9. This
isolates the cause: **a blast is not "a big move in the underlying," it is a
big move when gamma is exploding.** Six days out the same move barely dents
the multiple, because the option already carries large extrinsic value
(low gamma, high base premium — C74900 went 398→661, a big rupee move but only
×1.66).

## Mechanism

ATM gamma scales ~1/√T. As an expiry-day option's life collapses from a day
to the final ~90 minutes, its percentage sensitivity to the same index move
explodes. Both studied blasts fired in the **14:25–15:05 window** — the last
30–70 minutes — which is exactly where T→0. So the phenomenon is sharper than
"expiry day": it is the **final ~90 minutes of expiry day**.

## Implication for the predictive rules

The coiled-state conditions derived earlier (volume burst + crushed premium +
OI at session high, then a directional break with OI unwind) are **necessary
but not sufficient**. They describe *which strike* and *which moment*, but
they can only pay off when the gamma is there to amplify the move. This adds a
hard top-level gate, and it also explains the earlier "false-positive" worry:
off-expiry a strike can still compress premium and build OI (look "coiled")
yet have no possibility of detonating, because gamma is absent.

**Stage 0 (gate): only hunt blasts at 0 DTE, and arm only in the final ~2
hours.** On any non-expiry day, skip — the structural fuel (terminal gamma)
does not exist, regardless of how coiled a strike looks or how far the index
travels.

Stages 1–2 (shortlist the coiled cluster, enter on the directional break with
the OI-unwind accelerant) apply *within* that gate, as before.

## Status / limits

- 8 index-sessions: 2 at 0-DTE (both blast), 6 off-expiry across DTE 1/3/4/5/6
  (none blast). Clean and monotone, but still only two expiry events, both
  V-shaped reversal/trend days.
- Detection is OHLCV-only. Confirming that off-expiry days show the *coiled
  setup without detonation* (premium compression + OI build but no blast)
  would need the depth/OI series; the OHLCV already carries OI and is the
  natural next step.
- Earlier per-expiry detail: `analysis_20260609/`, `analysis_20260611/`,
  `analysis_common/RULES.md`.
