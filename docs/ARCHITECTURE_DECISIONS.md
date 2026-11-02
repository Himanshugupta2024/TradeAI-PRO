# TradeAI-PRO — Architecture Decision Records (ADR)

## ADR-001: Autonomous Execution Engine Policy
- **Decision**: Zero-click autonomous paper trading for research & paper evaluation; STRICTLY NO unattended real-money broker execution.
- **Reason**: Allows statistical validation across hundreds of market sessions without capital risk or regulatory violations.
- **Alternatives Rejected**: Real-broker live API execution (rejected due to SEBI compliance, slippage risks, and capital safety).
- **Date**: 2026-11-02
- **Files Affected**: `agents/execution/paper_trading.py`, `api/service.py`, `api/routes.py`.

## ADR-002: Multi-Asset Concurrency Bounded to 5 Positions
- **Decision**: Max 5 concurrent open positions across NSE Equities and Liquid Crypto majors (BTC/ETH).
- **Reason**: Prevents portfolio heat explosion, capital over-commitment, and correlated market drawdowns.
- **Alternatives Rejected**: Unlimited positions (causes high drawdown), NSE-only (ignores 24/7 crypto learning data).
- **Date**: 2026-11-02
- **Files Affected**: `agents/execution/paper_trading.py`, `config/settings.py`.

## ADR-003: Smart Trailing & 15:15 IST Equity Auto-Square-off
- **Decision**: Move SL to breakeven at 1R; take 50% profit at 1.5R; close all equity intraday trades at 15:15 IST.
- **Reason**: Protects earned profits and eliminates overnight gap-down risks for intraday setups. Crypto continues 24/7 with ATR trailing.
- **Alternatives Rejected**: Overnight holding for all intraday setups (high gap risk), fixed SL/TP with no trailing (leaves money on the table).
- **Date**: 2026-11-02
- **Files Affected**: `agents/execution/paper_trading.py`, `api/service.py`.

## ADR-004: Hybrid Self-Learning Loop (Every 10 Trades + Daily 15:45 IST Review)
- **Decision**: Trigger Bayesian weight updates every 10 closed trades and run a master review at 15:45 IST post-market.
- **Reason**: Responsive enough to adjust to changing market regimes while maintaining statistical robustness via Bayesian shrinkage.
- **Alternatives Rejected**: Immediate retrain per 1 trade (overfitting to noise), waiting for 50 trades (too slow to adapt to weekly regime shifts).
- **Date**: 2026-11-02
- **Files Affected**: `agents/analytics/feedback_loop.py`, `agents/signal_engine/signal_generator.py`.

## ADR-005: Offload Heavy Work to `asyncio.to_thread`
- **Decision**: Wrap all indicator calculations, ML inferences, and database writes with `asyncio.to_thread`.
- **Reason**: Keeps FastAPI event loop sub-50ms responsive; prevents WebSocket price streaming drops.
- **Date**: 2026-11-02
- **Files Affected**: `api/service.py`, `api/routes.py`.
