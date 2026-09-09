# Crypto Market-Data Platform — Architecture

> **Audience:** developers joining the project.
> **Purpose:** explain what we are building, how the moving parts fit together, the exact data
> paths (1-minute and 5-minute), the storage layout, and what we work on next.
> **Status:** reflects the live system in the `crypto-ticker` Kubernetes namespace as of 2026-09-09.

---

## Table of contents

1. [What we are building](#1-what-we-are-building)
2. [System at a glance](#2-system-at-a-glance)
3. [The two data cadences](#3-the-two-data-cadences)
   - [The 1-minute path (Binance WebSocket)](#31-the-1-minute-path-binance-websocket)
   - [The 5-minute path (CoinGecko poll)](#32-the-5-minute-path-coingecko-poll)
4. [Components](#4-components)
5. [Storage design](#5-storage-design)
6. [API surface](#6-api-surface)
7. [Kubernetes topology](#7-kubernetes-topology)
8. [Current status](#8-current-status)
9. [What we do next (roadmap)](#9-what-we-do-next-roadmap)
10. [Configuration reference](#10-configuration-reference)

---

## 1. What we are building

We are building a **crypto market-data platform**: a small set of Python services that collect
crypto price data from two sources, store it in two complementary stores, and expose it through a
REST API and a web dashboard.

The core idea is **two time scales that serve two different jobs**:

| Time scale | Source | Primary destination | Job |
|---|---|---|---|
| **Real-time / 1-minute** | Binance WebSocket | Redis (live tick) + ClickHouse (1m OHLCV candles) | Live dashboard prices + permanent analytics history |
| **5-minute** | CoinGecko REST poll | Redis rolling history | Lightweight always-on fallback + volatility indicator |

In plain terms:

- **Redis** is the *hot cache / live state*: the price you see on the dashboard right now, plus a
  rolling 24-hour history per symbol.
- **ClickHouse** is the *analytics warehouse*: every closed 1-minute candle is stored forever and
  can be aggregated (5m, 15m, 1h, …) later.
- The **API** reads live state from Redis and (soon) analytics from ClickHouse.

This is the foundation. Later work will add derived analytics (volatility, momentum), aggregation,
and richer endpoints/dashboards on top of the stored candles.

**Important naming note:** the delivery repository on GitHub is named `my-bet`; the internal project
codename is `crypto-ticker` (namespace and folder names). They are the same project.

---

## 2. System at a glance

```
                          ┌────────────────────┐
                          │      Internet      │
                          └──┬─────────────┬───┘
              Binance WebSocket│             │CoinGecko REST (every 5 min)
              wss ...@ticker    │             │GET /simple/price
              wss ...@kline_1m  │             │
                               ▼             ▼
                    ┌────────────────┐  ┌──────────────────┐
                    │   collector    │  │     fetcher      │
                    │  (long-running │  │ (K8s CronJob,    │
                    │   process)     │  │  */5 * * * * )   │
                    └───┬────────┬───┘  └────────┬─────────┘
              live tick │        │ closed 1m     │ latest snapshot + 5-min sample
              (~1s)     │        │ candle        │ for each symbol
                        ▼        ▼               ▼
                 ┌──────────┐  ┌────────────────────┐   ┌───────────────────────────────┐
                 │  Redis   │  │    ClickHouse      │   │            Redis               │
                 │ prices:  │  │  crypto.prices     │   │ prices:latest  (merged view)  │
                 │ latest   │  │  (1m OHLCV rows,   │   │ history:{sym}  (ring, 288 pts)│
                 │ (tick)   │  │   MergeTree)       │   │ meta:last_run                 │
                 └──────────┘  └────────────────────┘   └───────────────┬───────────────┘
                                                                        │ reads
                                                                        ▼
                                                          ┌──────────────────────────┐
                                                          │  crypto-api (FastAPI ×2) │
                                                          │  /api/prices             │
                                                          │  /api/history/{symbol}   │
                                                          │  /api/volatility/{symbol}│
                                                          │  /  (dashboard)          │
                                                          └───────────┬──────────────┘
                                                                      │ Ingress (nginx)
                                                                      ▼
                                                                 Browser / clients
```

All components live in the Kubernetes namespace **`crypto-ticker`** (see
[§7 Kubernetes topology](#7-kubernetes-topology)).

---

## 3. The two data cadences

### 3.1 The 1-minute path (Binance WebSocket)

**Producer:** the **collector** (`app/collector/collector.py`). It opens one WebSocket to Binance:

```
wss://stream.binance.com:9443/stream?streams=btcusdt@ticker/btcusdt@kline_1m
```

Two streams arrive on the same socket and are handled differently:

**a) `btcusdt@ticker` — live price → Redis**
Binance pushes the 24h rolling ticker roughly once per second. On every message the collector
writes into the Redis hash **`prices:latest`** (key `bitcoin`):

```json
{ "price": 78175.0, "change_24h": -0.46, "updated_at": "2026-09-09T21:25:02+00:00" }
```

This is what keeps the dashboard price fresh between 5-minute fetches.

**b) `btcusdt@kline_1m` — closed 1-minute candle → ClickHouse**
Binance sends the in-progress 1-minute candle on every trade/tick; the payload has a flag `x`
("is the candle closed?"). The collector **ignores every non-final update** and only inserts when
`x == true`, i.e. exactly one row **per completed minute**:

| Binance field | Column in `crypto.prices` |
|---|---|
| `t` (open time, ms) | `timestamp` (UTC) |
| fixed | `symbol` (`bitcoin`) |
| `o` / `h` / `l` / `c` | `open` / `high` / `low` / `close` |
| `v` | `volume` (base asset) |
| `q` | `quote_volume` (quote asset) |
| `n` | `trades` (count) |
| `V` | `taker_buy_volume` |
| `Q` | `taker_buy_quote` |
| fixed | `source` = `binance` |

If the socket drops, the collector reconnects after 5 seconds and resumes — but note that candles
that *closed while disconnected are not backfilled* (see roadmap, gap-filling).

> **Current limitation:** the collector tracks a **single symbol** (`SYMBOL=bitcoin`,
> `BINANCE_SYMBOL=btcusdt`). Extending to several streams is on the roadmap.

### 3.2 The 5-minute path (CoinGecko poll)

**Producer:** the **fetcher** (`app/fetcher/fetch_prices.py`), a Kubernetes **CronJob** scheduled
every 5 minutes (`*/5 * * * *`, `concurrencyPolicy: Forbid`). Each run:

1. Calls CoinGecko once for all configured symbols:

   ```
   GET https://api.coingecko.com/api/v3/simple/price
       ?ids=bitcoin,ethereum,solana&vs_currencies=usd&include_24hr_change=true
   ```

2. Writes three things to Redis in one pipeline (atomic-ish, single round trip):
   - **`prices:latest`** hash — latest snapshot + 24h change for *all* symbols (so the dashboard
     works even when the collector is not running).
   - **`history:{symbol}`** list — pushes one 5-minute sample `{"t": <epoch>, "p": <price>}` and
     trims to the newest **288** entries (24 hours at a 5-minute cadence).
   - **`meta:last_run`** string — ISO timestamp of the successful run (data freshness signal).

**Design rationale:** the 5-minute fetcher is deliberately simple and robust (REST poll every 5 min,
short-lived pod, no state). It guarantees we always have recent prices and a rolling 24h series even
if the real-time WebSocket path is down — the two paths *overwrite the same* `prices:latest` keys,
so whichever last wrote wins. The `meta:last_run` value tells us when the last snapshot landed.

---

## 4. Components

All services are Python 3.12, run as non-root, and are defined in `app/`.

### 4.1 `collector` — Binance real-time feed (1-minute)
- **Location:** `app/collector/collector.py` (+ `requirements.txt`)
- **Runtime:** long-running process (WebSocket kept open, reconnect loop with 5s backoff, ping every 30s).
- **Outputs:** Redis `prices:latest` (live tick) and ClickHouse `crypto.prices` (closed 1m candle).
- **Libraries:** `websocket-client`, `clickhouse-connect`, `redis`.
- **Status:** code complete and tested locally against the cluster (via port-forward); **not yet
  containerized / deployed as a pod** — see roadmap P0.

### 4.2 `fetcher` — CoinGecko poll (5-minute)
- **Location:** `app/fetcher/fetch_prices.py`, `Dockerfile`, `requirements.txt`
- **Runtime:** K8s CronJob every 5 min; fails fast (exit code), `backoffLimit: 2`,
  `activeDeadlineSeconds: 240`, `concurrencyPolicy: Forbid`.
- **Outputs:** Redis `prices:latest`, `history:{symbol}` (ring of 288), `meta:last_run`.
- **Logs:** one-line JSON to stdout (`ts`, `level`, `msg`).
- **Status:** **deployed and running** (image `crypto-fetcher:v1`).

### 4.3 `crypto-api` — FastAPI service + dashboard
- **Location:** `app/api/main.py`, `Dockerfile`, `requirements.txt`
- **Runtime:** K8s Deployment, 2 replicas, uvicorn on :8000, liveness `/healthz`, readiness `/readyz`.
- **Outputs:** REST JSON + a self-contained HTML dashboard (no frontend build step).
- **Status:** **deployed and running** (image `crypto-api:v1`). Endpoints in [§6](#6-api-surface).

### 4.4 Redis (Valkey) — hot cache / live state
- Image `valkey/valkey:7.2-alpine`, single replica, AOF persistence on a 1 Gi PVC, password from
  secret `redis-secret`. Only reachable inside the namespace (NetworkPolicies).

### 4.5 ClickHouse — analytics warehouse
- Image `clickhouse/clickhouse-server:24.3-alpine`, StatefulSet, 10 Gi PVC. DB `crypto` with two
  tables today (exact live DDL in [§5](#5-storage-design)). HTTP :8123 and native :9000, ClusterIP only.

---

## 5. Storage design

### 5.1 Redis keys

| Key | Type | Written by | Contents / semantics |
|---|---|---|---|
| `prices:latest` | hash | fetcher + collector | `symbol → JSON {price, change_24h, updated_at}`. Same keys overwritten by both writers — last write wins (collector = ~1s fresh while running, fetcher = 5-min fallback). |
| `history:{symbol}` | list | fetcher | newest-first 5-min samples `{"t": epoch_s, "p": price}`, **LPUSH + LTRIM 0..287 → max 288 entries ≈ 24 h**. |
| `meta:last_run` | string | fetcher | ISO-8601 UTC time of last successful CoinGecko fetch. |

No expiry is set on these keys by design (ring-buffer trim bounds `history:*`; `prices:latest`
always holds the latest snapshot).

### 5.2 ClickHouse — live schema (as deployed)

Database `crypto`. Table `crypto.prices` (raw 1-minute candles, appended by the collector):

```sql
CREATE TABLE crypto.prices
(
    `timestamp` DateTime64(3, 'UTC'),
    `symbol` String,
    `open` Float64,
    `high` Float64,
    `low` Float64,
    `close` Float64,
    `volume` Float64,
    `quote_volume` Float64,
    `trades` Int64,
    `taker_buy_volume` Float64,
    `taker_buy_quote` Float64,
    `source` String
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(timestamp)
ORDER BY (symbol, timestamp)
```

Query pattern: `SELECT * FROM crypto.prices WHERE symbol='bitcoin' AND timestamp >= now()-INTERVAL 1 DAY
ORDER BY timestamp`.

Table `crypto.metrics` (derived analytics — **created, empty, no writer yet**):

```sql
CREATE TABLE crypto.metrics
(
    `timestamp` DateTime64(3, 'UTC'),
    `symbol` String,
    `metric_name` String,
    `value` Float64,
    `period` String
)
ENGINE = ReplacingMergeTree(timestamp)
ORDER BY (symbol, metric_name, period, timestamp)
```

`ReplacingMergeTree(timestamp)` gives us **upsert semantics**: re-writing the same
`(symbol, metric_name, period, timestamp)` replaces the previous row, which is ideal for scheduled
metric recomputation (e.g. realized volatility, momentum per symbol/period).

> **Caveat:** the DDL above was applied manually via `clickhouse-client` and is **not yet in the
> repo**. Making schema-as-code (with an init container or migration step) is roadmap item P0-2.

---

## 6. API surface

All JSON, served by `crypto-api`. CORS-free, no auth yet (internal only behind ingress).

| Endpoint | Reads from | Returns |
|---|---|---|
| `GET /healthz` | – | `{"status":"ok"}` (liveness) |
| `GET /readyz` | Redis ping | `{"status":"ready"}` / 503 (readiness) |
| `GET /api/prices` | `prices:latest` + `meta:last_run` | currency, last run, served-at, and per-symbol `{price, change_24h, updated_at}` |
| `GET /api/history/{symbol}?limit=N` | `history:{symbol}` (≤ 288) | chronological 5-min points `{t, p}` |
| `GET /api/volatility/{symbol}` | `history:{symbol}` (288 pts) | annualized volatility from **log returns**, mean price, data points |
| `GET /` | JS fetch of `/api/prices` | self-contained dashboard (dark table of price + 24h %, auto-refresh 15 s) |

**Volatility formula** (see `api/main.py`): standard deviation of per-sample log returns,
annualized with `× √105120` because the 5-minute samples mean **288 × 365 = 105,120 bars/year**.
This is a *proxy* indicator computed from the Redis ring buffer — later it will come from
ClickHouse-aggregated candles (and be persisted into `crypto.metrics`).

---

## 7. Kubernetes topology

Namespace **`crypto-ticker`** (Pod Security `restricted`, Calico with **BPF dataplane** —
see `custom-resources-bpf.yaml` at repo root for the Calico installation CRs).

### Workloads

| Workload | Kind | Image | Replicas | Notes |
|---|---|---|---|---|
| `redis` | Deployment | `valkey/valkey:7.2-alpine` | 1 | AOF on, PVC 1 Gi, secret `redis-secret` |
| `clickhouse` | StatefulSet | `clickhouse/clickhouse-server:24.3-alpine` | 1 | PVC 10 Gi (`clickhouse-pvc`), secret `clickhouse-secret`, user `default` |
| `crypto-api` | Deployment | `crypto-api:v1` (local) | 2 | probes on :8000, read-only root fs |
| `fetcher` | CronJob | `crypto-fetcher:v1` (local) | – | `*/5 * * * *`, Forbid, 240 s deadline |
| collector | (not deployed yet) | – | – | runs locally today; **P0: containerize + deploy** |

Services: `redis:6379`, `clickhouse:8123/9000`, `crypto-api:80→8000` (all ClusterIP) + nginx
Ingress `crypto-ticker` (path `/` → `crypto-api`, rewrite).

### Network policies (`09-networkpolicy.yaml`) — deny-by-default

| Policy | Effect |
|---|---|
| `default-deny-all` | no ingress/egress unless allowed below |
| `allow-dns` | egress :53 TCP/UDP for everyone |
| `redis-policy` | ingress :6379 **only** from `crypto-api` and `fetcher` pods |
| `fetcher-policy` | egress to redis :6379 **and** :443 (CoinGecko over HTTPS) |
| `api-policy` | ingress :8000; egress only to redis :6379 |

> When the collector is deployed it needs a matching policy: egress to `:443` (Binance WSS) and
> ingress from nothing (no inbound), plus Redis :6379 and ClickHouse :8123 egress.

### Secrets & images

- Secrets `redis-secret` and `clickhouse-secret` (each a single `password` key) exist in the
  namespace but were created manually — their manifests are **not in the repo yet**.
- Images `crypto-fetcher:v1` / `crypto-api:v1` use `imagePullPolicy: Never` (built and loaded onto
  the node out-of-band; tarballs under `app/`). They predate a registry/CI pipeline (see roadmap).

---

## 8. Current status

What is **live right now** (verified in the cluster):

- ✅ Redis, ClickHouse, `crypto-api` (×2) running; fetcher CronJob fires every 5 min.
- ✅ `prices:latest` holds fresh snapshots for bitcoin/ethereum/solana; `history:*` ring buffers
  are full (288 points ≈ 24 h at 5 min); `meta:last_run` updates each cycle.
- ✅ `crypto.prices` has real 1-minute Binance candles (from earlier collector runs); the
  collector→ClickHouse insert path is proven.
- ⚠️ **Collector is not running 24/7 in the cluster** — it was executed locally against the
  cluster via port-forward for development. ClickHouse currently holds only those test minutes.
- ⚠️ `crypto.metrics` table exists but nothing writes to it yet.
- ⚠️ ClickHouse DDL, secrets manifests, and the collector Dockerfile/Deployment are missing from
  the repo (they exist only in the live cluster / shell history).

Known rough edges: single-symbol collector; no backfill of candles missed while disconnected; no
TTL/retention policy on ClickHouse; no auth on the API; images are local-only (no registry).

---

## 9. What we do next (roadmap)

Prioritized so each step leaves the system more complete and shippable.

### P0 — Close the loop (make the 1-minute path run in the cluster)
1. **Containerize + deploy the collector.** Add `app/collector/Dockerfile`, build
   `crypto-collector:v1`, add a Deployment (+ NetworkPolicy for Binance WSS egress and
   Redis/ClickHouse egress), wire secrets via env, add a readiness/health signal (e.g. "candle
   written in last 2 min"). This makes the platform genuinely real-time 24/7.
2. **Schema-as-code.** Commit the `crypto.prices` / `crypto.metrics` DDL (SQL file + init container
   or a tiny migration step) so a fresh cluster reproduces the exact schema.
3. **Commit all manifests & secrets templates** (with placeholder values + apply docs) and the
   collector image build into the repo so the cluster is reproducible.

### P1 — Real analytics value
4. **Expose ClickHouse data via the API.** Add candle endpoints
   (`/api/candles/{symbol}?interval=1m|5m|15m|1h&range=…`) backed by ClickHouse aggregation so
   charts/analytics stop depending on the 24 h Redis ring.
5. **Aggregations.** Materialized views (or an aggregator job) rolling 1m candles up to
   5m/15m/1h/4h/1d for fast reads and long-term charting.
6. **Metrics writer.** A scheduled job computing derived metrics (realized volatility, momentum,
   volume profile, spreads) and upserting them into `crypto.metrics` per `(symbol, period)` — this
   is where the volatility indicator graduates from a Redis-ring proxy to a real persisted series.
7. **Multi-symbol collector** (config-driven list of Binance pairs + their kline streams) so the
   1-minute history covers all watched assets, not just BTC.

### P2 — Production hardening
8. **Gap-filling / backfill:** on reconnect, fetch missed 1m klines via Binance REST
   (`/api/v3/klines`) and insert anything the socket missed.
9. **Retention policy:** ClickHouse TTL for raw 1m data (keep raw ~1y, keep aggregates forever),
   Redis eviction/expiry hygiene, backup strategy (ClickHouse + Redis AOF) off-cluster.
10. **Delivery pipeline:** container registry + CI/CD, tag images instead of `:v1`/`Never`,
    lint manifests, add tests (insert path, API contract).
11. **Secrets & security:** move secrets to External Secrets/Vault or at least commit templated
    manifests; add auth (token/API key) in front of the API.
12. **Observability:** Prometheus metrics on all three services, alerting on "no candle written for
    N minutes" and "fetcher failing", dashboard/alert wiring.
13. **Dashboard v2:** real charts from `/api/candles` and `/api/volatility` (this becomes the
    product surface for the betting/prediction features that follow).

---

## 10. Configuration reference

Everything is environment-driven (no config files); each component has safe defaults for local runs.

| Env var | Default | Used by | Meaning |
|---|---|---|---|
| `SYMBOLS` | `bitcoin,ethereum,solana` | fetcher, api (shared ConfigMap `fetcher-config`) | CoinGecko ids |
| `VS_CURRENCY` | `usd` | fetcher, api | quote currency |
| `SYMBOL` | `bitcoin` | collector | internal symbol name (ClickHouse/Redis key) |
| `BINANCE_SYMBOL` | `btcusdt` | collector | Binance pair id (streams: `@ticker`, `@kline_1m`) |
| `REDIS_HOST` / `REDIS_PORT` | `redis` / `6379` | all | Redis location |
| `REDIS_PASSWORD` | – | all | from secret `redis-secret/password` |
| `CLICKHOUSE_HOST` / `CLICKHOUSE_PORT` | `clickhouse` / `8123` | collector | ClickHouse HTTP endpoint |
| `CLICKHOUSE_USER` / `CLICKHOUSE_PASSWORD` | `default` / secret | collector | from secret `clickhouse-secret/password` |
| `CLICKHOUSE_DB` | `crypto` | collector | database name |
| `HISTORY_MAX_POINTS` | `288` | fetcher | ring-buffer cap (24 h at 5 min) |

Useful local-run pattern (what was used in dev): port-forward both stores and set
`REDIS_HOST=localhost CLICKHOUSE_HOST=localhost` plus passwords from the two secrets.

---

*Generated from the live cluster + source tree. Update this document whenever the architecture
changes — it is the onboarding contract for new engineers.*
