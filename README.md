# my-bet — crypto market-data platform

Real-time crypto price collection, storage, and analytics.

- **Live prices & 1-minute OHLCV candles** streamed from **Binance** (WebSocket) into **Redis**
  (hot cache) and **ClickHouse** (analytics warehouse).
- **5-minute spot snapshots** polled from **CoinGecko** into a rolling 24-hour Redis history
  (always-on fallback + volatility indicator).
- **FastAPI service + web dashboard** on top, deployed in Kubernetes (`crypto-ticker` namespace).

> ⚠️ Repository note: this repo is the delivery home for the project internally codenamed
> `crypto-ticker`. Architecture and onboarding documentation lives in
> **[ARCHITECTURE.md](ARCHITECTURE.md)** — start there.
