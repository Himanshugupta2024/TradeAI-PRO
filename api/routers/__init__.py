"""
TradeAI PRO — Domain Routers Package.
====================================
Modular routers deconstructed from api/routes.py:
  - health: Health, readiness, system telemetry
  - forex: Forex scanner, pairs, sessions, technical analysis
  - crypto: Crypto search, proxy, meme radar, signals
  - research: Next-day picks, edge scoring, macro heatmap, risk gauge
  - swing: Swing setups, intraday scanner, breakouts, morning scan
  - portfolio: Paper trading, portfolio, trade execution, performance
  - admin: Market control, autonomous operations, model sync, retrain, admin console
  - market: Market intelligence, quotes, screener, search, websocket, landing
"""

from api.routers import (
    health,
    forex,
    crypto,
    research,
    swing,
    portfolio,
    admin,
    market,
)

__all__ = [
    "health",
    "forex",
    "crypto",
    "research",
    "swing",
    "portfolio",
    "admin",
    "market",
]
