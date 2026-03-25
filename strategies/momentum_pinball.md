---
description: Short-term price flip based on first-hour breakout.
---

# Momentum Pinball (Intraday/Swing)

**Momentum Pinball** uses a one-period Rate of Change (ROC) filtered by an RSI to identify 1-2 day flips in market direction.

### 📊 Audit Performance
- **Win Rate**: **50.61%**
- **Profit Factor**: **1.06**
- **Avg PnL**: **0.05%**
- **Performance by Price**: Best in **$5-$20** range (1.09 PF).
- **Verdict**: **APPROVED**

### 🛠️ Technical Rules

#### **Indicator Setup**
- **LBR/RSI**: 3-period RSI of a 1-period Rate of Change (today's close vs. yesterday's close).

#### **Buy Setup (Long)**
1.  **Setup Day**: LBR/RSI value is **less than 30**.
2.  **Entry Day (Day 2)**: Place a buy stop above the **High of the first hour's range**.
3.  **Initial Stop**: At the Low of the first hour's range.
4.  **Exit Day (Day 3)**: Exit on morning follow-through or by the close of the next day.

### 💡 Best Practices
- **Confirmation**: Entering on the first-hour breakout ensures that the market is actually moving in the direction of the RSI reversal.
- **Overnight Hold**: If the trade closes with a profit on Day 2, carry it overnight for a gap-up exit on Day 3.
- **Subjective Overlap**: High overlap with 80-20 setups; use both to confirm exhaustion.
