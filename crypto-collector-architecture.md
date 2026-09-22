# crypto-ticker — Infrastructure Architecture (One-Pager)

**Repo:** `my-bet` · **Namespace:** `crypto-ticker` · **Hosting:** self-hosted bare-metal (no cloud)
**Cluster:** Kubernetes v1.36.3, kubeadm, 3 nodes · **Updated:** <date>

---

## 1. High-Level Architecture

Two independent data cadences feed two stores; one read-only API serves clients.

```
                    Internet
        ┌───────────────┴───────────────┐
   Binance WebSocket               CoinGecko REST (every 5 min)
   wss ...@ticker + @kline_1m      GET /simple/price
        │                               │
        ▼                               ▼
  ┌──────────────┐               ┌──────────────┐
  │  collector   │               │   fetcher    │
  │ Deployment ×1│               │ CronJob */5  │
  │ long-running │               │ one-shot pod │
  └───┬──────┬───┘               └──────┬───────┘
 live tick│  │ closed 1m candle   latest+history │
      ▼        ▼                        ▼
  ┌────────┐ ┌────────────────┐  ┌──────────────────────────┐
  │ Redis  │ │   ClickHouse   │  │          Redis           │
  │ prices:│ │ crypto.prices  │  │ prices:latest (merged)   │
  │ latest │ │ (1m OHLCV rows,│  │ history:{sym} (288 pts)  │
  │ (tick) │ │  MergeTree)    │  │ meta:last_run            │
  └────────┘ └────────────────┘  └───────────┬──────────────┘
                                             │ reads
                                             ▼
                              ┌──────────────────────────┐
                              │  crypto-api (FastAPI ×2) │
                              │  /api/prices             │
                              │  /api/history/{symbol}   │
                              │  /api/volatility/{symbol}│
                              │  /  (HTML dashboard)     │
                              └───────────┬──────────────┘
                                          │ nginx Ingress (172.16.0.4:80)
                                          ▼
                                    Browser / clients
```

**Components (all in namespace `crypto-ticker`):**

| Workload | Kind | Image | Purpose |
|----------|------|-------|---------|
| `collector` | Deployment ×1 | `crypto-collector:v1` (local) | Streams Binance WS (`btcusdt`): live tick → Redis, closed 1m candle → ClickHouse |
| `fetcher` | CronJob `*/5 * * * *` | `crypto-fetcher:v1` (local) | Polls CoinGecko (bitcoin,ethereum,solana) → Redis latest + 288-point rolling history |
| `crypto-api` | Deployment ×2 | `crypto-api:v1` (local) | FastAPI read-only API + dashboard; reads Redis |
| `redis` | Deployment ×1 | `valkey/valkey:7.2-alpine` | Hot cache / live state (AOF persistence) |
| `clickhouse` | StatefulSet ×1 | `clickhouse/clickhouse-server:24.3-alpine` | Analytics warehouse for 1m OHLCV candles |

---

## 2. Low-Level Kubernetes Architecture

### Cluster (self-hosted, bare-metal)
| Node | Role | IP |
|------|------|----|
| `cplane-01` | control-plane | 172.16.0.2 |
| `node-01` | worker | 172.16.0.3 |
| `node-02` | worker | 172.16.0.4 |

- **OS:** Ubuntu 24.04 · **Runtime:** containerd 2.2.6
- **CNI:** Calico (tigera-operator) with **BPF dataplane**, kube-proxy replacement, `VXLANCrossSubnet`, pod CIDR `192.168.0.0/16`
- **Ingress:** NGINX ingress controller (namespace `ingress-nginx`)
- **Storage:** `local-path` (Rancher local-path provisioner, default SC, `WaitForFirstConsumer`)

### Workloads & Networking
| Item | Detail |
|------|--------|
| Services | `redis:6379`, `clickhouse:8123/9000`, `crypto-api:80` — all `ClusterIP` (internal only) |
| Ingress | `crypto-ticker` → nginx, host `*`, listens `172.16.0.4:80` → `crypto-api` |
| Images | Built locally on nodes; `imagePullPolicy: Never` (no registry) |
| Secrets | `redis-secret`, `clickhouse-secret` (Opaque, `password` key) |
| ConfigMap | `fetcher-config` (SYMBOLS, VS_CURRENCY, REDIS_HOST/PORT, HISTORY_MAX_POINTS) |

### Storage (PVCs, `local-path`)
| PVC | Size | Mounted by |
|-----|------|-----------|
| `redis-pvc` | 1Gi | redis (`/data`, AOF) |
| `clickhouse-pvc` | 10Gi | clickhouse (`/var/lib/clickhouse`) |

### NetworkPolicies (default-deny model)
| Policy | Applies to | Allows |
|--------|-----------|--------|
| `default-deny-all` | all pods | — (blocks all ingress+egress) |
| `allow-dns` | all pods | egress DNS 53 (UDP/TCP) |
| `redis-policy` | redis | ingress 6379 from `crypto-api` + `fetcher` |
| `fetcher-policy` | fetcher | egress 6379→redis, egress 443→internet |
| `api-policy` | crypto-api | ingress 8000, egress 6379→redis |

### Security hardening (applied everywhere)
- Namespace PSA: `restricted` (enforce/audit/warn)
- Non-root (`runAsUser 10001`, redis `999`), `allowPrivilegeEscalation: false`
- `readOnlyRootFilesystem: true` (collector/api), `capabilities.drop: ALL`, `seccompProfile: RuntimeDefault`
- `automountServiceAccountToken: false` on all pods
- API liveness `/healthz`, readiness `/readyz` (Redis ping)

### Resource budgets
| Workload | Requests (cpu/mem) | Limits (cpu/mem) |
|----------|-------------------|------------------|
| collector / fetcher | 50m / 64Mi | 100m / 128Mi |
| crypto-api | 100m / 128Mi | 200m / 256Mi |
| redis | 100m / 128Mi | 200m / 256Mi |
| clickhouse | 200m / 512Mi | 500m / 1Gi |

---

## 3. Data Paths (detail)

1. **1-minute path (real-time):** Binance WS → `collector` → Redis `prices:latest` (tick) **and** ClickHouse `crypto.prices` (closed 1m OHLCV, permanent).
2. **5-minute path (fallback + history):** CoinGecko REST → `fetcher` → Redis `prices:latest` (merged view) + `history:{sym}` (LRANGE/LTRIM ring, 288 pts) + `meta:last_run`.
3. **Read path:** Browser/client → nginx Ingress → `crypto-api` (×2) → Redis → JSON/HTML response.

---

## 4. Known Gaps / Notes
- ⚠️ **`collector` has no NetworkPolicy.** `default-deny-all` blocks its egress to Binance `:443`, Redis `:6379`, and ClickHouse `:8123` — only DNS is allowed. Add a `collector-policy` (egress 443 + 6379 + 8123) or the collector will keep crash-looping. (Observed: collector pod in `Error`, 71 restarts.)
- ClickHouse table must be created once (DB `crypto`, table `prices`) — collector inserts but does not create the table.
- Local images (`imagePullPolicy: Never`) must exist on whichever node schedules the pod.
- Redis is single-replica (Deployment, not StatefulSet); AOF on PVC gives persistence but no HA/failover.
- Ingress has no TLS (HTTP only, `:80`).
