---
description: Identifying lack of buying interest during rallies.
---

# VPA No Demand Test (Intraday)

A **No Demand Test** is an intraday signal that identifies the lack of professional interest in higher prices. It typically occurs on a pullback or a weak rally attempt within a downtrend.

### 📊 Audit Performance
- **Primary Timeframe**: **5-Minute Intraday** (PF 1.34)
- **Win Rate**: **48.9% (5m)**
- **Verdict**: **APPROVED** (Scalping Reversal Signal)

### 🛠️ Technical Rules

#### **Setup (Short)**
1.  **Trend**: Bearish (below VWAP).
2.  **Action**: Price rallies briefly but with **Narrowing Spreads**.
3.  **Volume**: Significant drop in volume (**Low Vol** < 0.7x MA).
4.  **Confirmation**: Immediate bearish candle following the low-vol peak.

#### **Execution**
- **Entry**: Short at the close of the No Demand bar.
- **Stop Loss**: High of the rally attempt.
- **Exit**: 3-5 bars (Scalp).
