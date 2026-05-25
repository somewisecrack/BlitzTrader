"""
pairs/ — PairsTrader subsystem integrated into BlitzTrader.

Capital pool: ₹10,00,000 (separate from futures capital).
Scan: 08:30 IST via yfinance (15m/30m/1h, NIFTY 50 universe).
Entry: 09:15 IST market-open via Shoonya NSE equities.
Exit: 15:15 IST EOD forced close + independent trailing stops per leg.
"""
