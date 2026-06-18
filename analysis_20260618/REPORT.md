# SENSEX 18-Jun-2026 expiry — GammaBlast missed a clean call-cluster blast

Today's SENSEX 0-DTE (Thursday 18-Jun) produced a strong **late call-side
GammaBlast** (per Shoonya recovered 1-min history): 77400CE ×30, 77300CE ×22,
77200CE ×6.7, 77500CE ×6.1, 77100CE ×3.1, all peaking 15:09–15:10 from a
14:56 base; preceded by a put-side burst (77100PE ×3.34, 77000PE ×3.03) at
14:24–14:30. GammaBlast missed it (symbol-resolution bug).

## Part 1 — The recording is corrupted (independently confirmed + diagnosed)

GammaBlast's own Drive recording (folder `1c4b69j4U...`, strikes 76700–77300
CE/PE) does **not** contain the blast and cannot be trusted:

- **Wrong-expiry contracts.** Files are labelled `expiry=18-JUN-2026` but the
  prices are impossible for 0-DTE. At 15:10 recorded 77100CE = ₹262 with spot
  77168 (68 ITM) and ~5 min to expiry → ₹194 of time value. A 0-DTE option
  carries ≲₹15 of time value in the final 5 min even at 150% IV. These are a
  **later expiry's** premiums (weeks of time value), so the token resolved to
  the wrong series. The recorded series shows smooth theta-decay (77300CE
  ₹124–232, max 15-min ×1.36) and entirely misses the detonation.
- **Implausible order-flow fields.** Recorded volume 46–76 **million** against
  OI ~1M (46–76× turnover) — garbage. So recorded OI/volume are unusable.
- **Sampling gaps.** 77100PE / 77000PE have **zero records 14:20–14:35** —
  exactly the put-burst window.
- **Truncated ladder.** Highest strikes captured = 77300; the biggest movers
  (77400/77500 CE) were never recorded — the ATM±2 window didn't re-center up
  fast enough with the rally (or the bug dropped them).

Net: the only complete OI/volume recording is wrong, and the recovered Shoonya
history is price-only. **The micro-triggers cannot be measured for this event.**

## Part 2 — Are the conditions still valid? (vs. the recovered true event)

Everything observable is **consistent** — this is a 3rd confirming blast after
09-Jun NIFTY (×10) and 11-Jun SENSEX (×6), and the cleanest cluster example:

| Condition (RULES.md) | This event | Verdict |
|---|---|---|
| Stage 0 — 0-DTE, final ~90 min | fired 14:56→15:10 (final ~15–35 min) | ✅ confirmed, most terminal yet |
| Cluster gate (≥3 adjacent same-type) | 77100–77500 CE = 5-strike call wall | ✅ textbook |
| Tail = biggest multiple | 77400CE ×30 (₹1.55→46.6, tiny rupees) | ✅ |
| Near-money = biggest rupees | 77200CE ×6.7 (₹36→243) | ✅ |
| Opposite-thrust-fails detonator | put burst 14:24–14:30 ×3.3 → reversal → call blast 14:56 | ✅ same structure as 11-Jun's failed 14:24 spike |
| RELEASE = break toward cluster (up) | call-side blast ⇒ upside break; even truncated recorded spot rose 76979→77140 into the close | ✅ directionally (magnitude understated by the bad recording) |

**Cannot be verified for this event (data limitation, not a rule failure):**
- Stage 1 COILED micro-state (premium ≤1.25× session low + OI ≥90% session
  high + volume ≥1.5× baseline) — recorded OI/volume are corrupt.
- Stage 2 OI-unwind accelerant — same reason.
- Best-5 bid imbalance ≥ +0.3 — depth unavailable in recovered history.

**Conclusion:** the conditions are **not invalidated** — at the macro level
(timing, cluster, tail/near-money split, failed-probe trigger, upside
direction) this event matches the rules on every count. But it can't *prove*
the order-flow micro-triggers, because the complete recording is broken and the
clean history lacks order flow. Treat it as a consistent confirmation, not a
micro-level validation.

## Part 3 — Engineering follow-ups for GammaBlast

1. **Symbol resolution fix is necessary but not sufficient.** Add a recorder
   **sanity guard** that would have caught this: reject/flag a sample where
   premium > intrinsic + plausible-0-DTE-time-value cap, or where cumulative
   volume ≫ a few × OI. A 0-DTE contract priced like a later expiry should
   never be written silently.
2. **Re-center the ATM ladder faster / widen it on trend days** so a rally that
   runs 3–5 strikes (like today) keeps the firing strikes in-window
   (77400/77500 were never tracked).
3. **Close sampling gaps** — the 14:20–14:35 put gap sits right on a signal
   window; the loop should not silently drop ticks.
4. Weekday gating was **correct** (SENSEX Thursday) — this was a token miss,
   not a calendar miss.
