"""Daily morning report: run TradingAgents over Nifty 50 + SENSEX tickers.

This package contains the building blocks for the daily report feature:
- `nifty_sensex.py` — fetch Nifty 50 + SENSEX constituents from free sources
- `runner.py` — run the backtest loop over a list of tickers for a single date
- `schedule.bat` — Windows batch script to be registered with Task Scheduler
"""
