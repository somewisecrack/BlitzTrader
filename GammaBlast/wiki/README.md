# GammaBlast Wiki

Self-improving knowledge base for the GammaBlast virtual options scanner.

## Structure

```
wiki/
├── hypotheses/          candidate rule changes (PROPOSED → BACKTESTED → PROMOTED)
├── backtest_results/    archive of backtest outputs
├── promoted_rules/      active deterministic overrides loaded at startup
└── daily_reviews/       per-day post-market reviews (YYYYMMDD.md)
```

## How the loop works

After each expiry-day session (15:30 IST timer):

1. **`evaluate_gamma_day.py`** reads ladder JSONL + candidate audit + journal
   and writes `wiki/daily_reviews/YYYYMMDD.md`.

2. **`propose_gamma_hypotheses.py`** reads the review and uses Gemini to propose
   specific parameter changes → `wiki/hypotheses/YYYYMMDD_<slug>.json`.

3. **`backtest_gamma_hypothesis.py`** replays the same day's ladder data with
   the proposed parameter override, computes precision/recall vs ground truth,
   and writes the result back into the hypothesis JSON.

4. **`promote_gamma_hypothesis.py`** promotes hypotheses whose backtest verdict
   is IMPROVE (no precision or recall regression) to
   `wiki/promoted_rules/<rule_id>.json`.

At next startup, `main.py` loads `wiki/promoted_rules/*.json` and overrides
the config defaults.

## Rule lifecycle

```
PROPOSED → BACKTESTED (verdict: IMPROVE | MIXED | REGRESS) → PROMOTED
```

Only IMPROVE verdicts with non-negative delta_precision and delta_recall are
promoted. Use `--force` on `promote_gamma_hypothesis.py` only after manual review.

## Invariants

- Promoted rules are deterministic Python conditions / numeric thresholds only.
- No LLM calls in the live scanning path (09:15–15:15 IST).
- Gemini is only called post-market by `propose_gamma_hypotheses.py`.

## Research background

GammaBlast is built on empirical research across 8 NSE/BSE index sessions
(9–15 Jun 2026) showing that ×3–10 option multiples occur exclusively at
0 DTE (expiry day), in the final ~90 minutes, driven by terminal gamma
(ATM gamma ~1/√T → ∞ as T→0).

Key rules (v1):
- **Stage 0 (gate):** 0 DTE only; arm only after 13:00 IST.
- **Stage 1 (COILED):** premium ≤1.25× session low + OI ≥90% session high
  + volume ≥1.5× baseline in ≥2 of last 6 buckets.
- **Stage 2 (RELEASE):** underlying breaks post-13:00 range edge toward
  cluster; OI flips building→unwinding; bid imbalance ≥+0.3.

See `GammaBlast/research/analysis_common/RULES.md` for full derivation, and
`GammaBlast/research/analysis_cross/REPORT.md` for the cross-day survey.
