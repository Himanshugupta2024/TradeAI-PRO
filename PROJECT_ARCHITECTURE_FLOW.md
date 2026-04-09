# TradeAI PRO - Architecture and Flow Diagrams

This document provides visual architecture and flow diagrams for the project.

## 1) System Architecture (High Level)

```mermaid
graph TD
    U[User / Trader] --> D[Dashboard UI<br/>dashboard/index.html + app.js]
    D -->|REST API| S[FastAPI Backend<br/>server.py]
    D -->|WebSocket| W[/ws/prices]

    S --> C1[_market_data cache]
    S --> C2[_signal_cache]
    S --> C3[_regime_cache]

    S --> DB[(SQLite<br/>trading.db)]
    S --> PT[(paper_trades.json)]
    S --> H[(health.json)]

    S --> MI[agents/market_intelligence]
    S --> FE[agents/feature_engine]
    S --> SG[agents/signal_engine]
    S --> AI[agents/ai_engine]
    S --> EX[agents/execution]
    S --> BK[agents/backtester]

    MI --> YF[yFinance / NSE feeds]
    MI --> BN[Binance / market feeds]

    AR[autonomous_runner.py] --> DI[agents/data_ingestion]
    AR --> FE
    AR --> AI
    AR --> SG
    AR --> EX
    AR --> AL[agents/alerts<br/>Telegram]
    AR --> DB
    AR --> H

    MP[main.py pipeline] --> DI
    MP --> FE
    MP --> DB
```

## 2) Core Runtime Component Map

```mermaid
graph LR
    subgraph Ingestion
      B1[binance_fetcher]
      N1[nse_fetcher]
      WS1[binance_ws]
      DM1[data_manager]
    end

    subgraph FeatureEngine
      I1[indicators]
      SR1[support_resistance]
      T1[trend]
      R1[regime_detector]
      M1[mtf_scorer]
    end

    subgraph Decisioning
      A1[fallback_predictor]
      S1[signal_generator]
    end

    subgraph Execution
      P1[paper_trading]
      TS1[trailing_sl]
      PS1[position_sizing]
    end

    subgraph API_and_UI
      API1[server.py FastAPI]
      UI1[dashboard/]
    end

    B1 --> I1
    N1 --> I1
    WS1 --> DM1
    DM1 --> I1

    I1 --> SR1 --> T1 --> R1 --> M1
    M1 --> A1 --> S1 --> P1 --> TS1

    P1 --> API1 --> UI1
    S1 --> API1
```

## 3) Data Pipeline Flow (main.py)

```mermaid
flowchart TD
    A[Start run_pipeline] --> B[init_db]
    B --> C[Loop crypto symbols x timeframes]
    C --> D[Fetch historical OHLCV]
    D --> E[calculate_indicators]
    E --> F[calculate_support_resistance]
    F --> G[Store in data_store]

    G --> H[Loop NSE symbols x timeframes]
    H --> I[Fetch historical OHLCV]
    I --> J[calculate_indicators]
    J --> K[calculate_support_resistance]
    K --> L[Store in data_store]

    L --> M[Classify HTF trend]
    M --> N[Propagate HTF trend to lower TF]
    N --> O[Summarize rows/features]
    O --> P[Pipeline complete]
```

## 4) FastAPI Background Refresh Flow (server.py)

```mermaid
flowchart TD
    A[FastAPI startup lifespan] --> B[init_db]
    B --> C[Spawn _initial_load task]
    B --> D[Spawn _background_refresh task]

    C --> E[Phase 1 load priority symbols]
    E --> F[Feature engineering per symbol]
    F --> G[Update caches: market/signal/regime]

    G --> H[Phase 2 load remaining symbols in batches]
    H --> I[Phase 3 load daily data cache]
    I --> J[Mark loading ready]

    D --> K[Every ~60s refresh loop]
    K --> L[Fetch fresh data]
    L --> M[Recompute indicators + regime + signals]
    M --> N[Update caches]
    N --> O[Broadcast prices/signals to ws clients]
    O --> K
```

## 5) Autonomous Trading Loop (autonomous_runner.py)

```mermaid
flowchart TD
    A[Start autonomous runner] --> B[init_db + setup engine]
    B --> C{WebSocket enabled?}
    C -->|Yes| D[Use BinanceWebSocket + DataManager]
    C -->|No| E[Polling fetchers]

    D --> F[run_symbol for each symbol]
    E --> F

    F --> G[Get LTF and HTF data]
    G --> H[Indicators + support/resistance + trend]
    H --> I[detect_regime]
    I --> J[predict_batch AI confidence]
    J --> K[generate_signals]

    K --> L{Signal LONG/SHORT?}
    L -->|Yes| M[paper_trading execute]
    L -->|No| N[Manage open trades]

    M --> O[Send Telegram signal alert]
    N --> P[Update trailing SL + close checks]
    O --> Q[Write health heartbeat]
    P --> Q

    Q --> R{Next cycle}
    R --> F
```

## 6) Request Flow: Dashboard to Decision

```mermaid
sequenceDiagram
    participant User
    participant UI as Dashboard UI
    participant API as FastAPI server.py
    participant FE as Feature Engine
    participant SG as Signal Generator
    participant EX as Paper Trading
    participant DB as SQLite / JSON state

    User->>UI: Open dashboard / select symbol
    UI->>API: GET /api/market/{symbol}
    API->>FE: calculate indicators + trend + regime
    FE-->>API: Enriched dataframe
    API->>SG: generate_signals
    SG-->>API: Signal + confidence + SL/TP
    API-->>UI: JSON response

    User->>UI: Monitor live prices
    UI->>API: Connect /ws/prices
    API-->>UI: Streaming prices and updates

    API->>EX: execute/update paper trades (when applicable)
    EX->>DB: Persist trade state
    API->>DB: Persist/read candles, signals, trades
```

## Notes

- Main backend API and dashboard runtime: server.py + dashboard/
- Autonomous trading runtime: autonomous_runner.py
- Batch pipeline runtime: main.py
- Core domain logic lives under agents/
- Context state files: context_cache.json, news_cache.json, daily_api_usage.json
  (cache-first ingestion for 24hr + 7-day context layer)
