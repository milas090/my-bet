import json
import math 
import os
from datetime import datetime, timezone

import redis
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse

REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD")
VS_CURRENCY = os.environ.get("VS_CURRENCY", "usd")

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

client = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    password=REDIS_PASSWORD,
    decode_responses=True,
)

@app.get("/healthz")
def healthz():
    return {"status": "ok"}

@app.get("/readyz")
def readyz(response: Response):
    try:
        client.ping()
        return {"status": "ready"}
    except redis.RedisError as e:
        response.status_code = 503
        return {"status": "not-ready", "reason": str(e)}

@app.get("/api/prices")
def prices():
    try:
        raw = client.hgetall("prices:latest")
        last_run = client.get("meta:last_run")
    except redis.RedisError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {
        "currency": VS_CURRENCY,
        "last_run": last_run,
        "served_at": datetime.now(timezone.utc).isoformat(),
        "prices": {k: json.loads(v) for k, v in raw.items()},
    }

@app.get("/api/history/{symbol}")
def history(symbol: str, limit: int = 60):
    limit = max(1, min(limit, 288))
    try:
        rows = client.lrange(f"history:{symbol}", 0, limit - 1)
    except redis.RedisError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {
        "symbol": symbol,
        "currency": VS_CURRENCY,
        "points": [json.loads(r) for r in reversed(rows)],
    }

@app.get("/api/volatility/{symbol}")
def volatility(symbol: str):
    try:
        rows = client.lrange(f"history:{symbol}", 0, 287)
    except redis.RedisError as e:
        raise HTTPException(status_code=503, detail=str(e))

    if len(rows) < 2:
        raise HTTPException(status_code=404, detail="not enough data")

    prices = [json.loads(r)["p"] for r in reversed(rows)]

    log_returns = [
        math.log(prices[i] / prices[i - 1])
        for i in range(1, len(prices))
    ]

    n = len(log_returns)
    mean_return = sum(log_returns) / n
    variance = sum((r - mean_return) ** 2 for r in log_returns) / n
    std_dev = math.sqrt(variance)
    mean_price = sum(prices) / len(prices)
    annualized = std_dev * math.sqrt(105120) * 100

    return {
        "symbol": symbol,
        "currency": VS_CURRENCY,
        "data_points": len(prices),
        "annualized_volatility_percent": round(annualized, 2),
        "mean_price": round(mean_price, 2),
    }



@app.get("/", response_class=HTMLResponse)
def dashboard():
    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>crypto ticker</title>
<style>
  body { background:#0d1117; color:#e6edf3; font:15px/1.5 monospace; margin:0; padding:2rem; }
  h1 { font-size:1rem; color:#7d8590; text-transform:uppercase; letter-spacing:.1em; }
  table { border-collapse:collapse; width:100%; max-width:600px; }
  th { text-align:left; color:#7d8590; font-size:.8rem; padding:.5rem 1rem .5rem 0; border-bottom:1px solid #21262d; }
  td { padding:.6rem 1rem .6rem 0; border-bottom:1px solid #161b22; }
  .up { color:#3fb950; } .down { color:#f85149; }
  footer { margin-top:1rem; color:#484f58; font-size:.8rem; }
</style>
</head>
<body>
<h1>crypto ticker</h1>
<table>
  <thead><tr><th>asset</th><th>price</th><th>24h</th></tr></thead>
  <tbody id="rows"><tr><td colspan="3">loading...</td></tr></tbody>
</table>
<footer id="meta"></footer>
<script>
async function load() {
  const r = await fetch('/api/prices', {cache:'no-store'});
  const d = await r.json();
  const rows = Object.entries(d.prices || {});
  document.getElementById('rows').innerHTML = rows.map(([sym, v]) => {
    const ch = v.change_24h;
    const cls = ch == null ? '' : ch >= 0 ? 'up' : 'down';
    const sign = ch == null ? '-' : (ch >= 0 ? '+' : '') + ch.toFixed(2) + '%';
    const price = new Intl.NumberFormat(undefined, {style:'currency',currency:d.currency.toUpperCase()}).format(v.price);
    return `<tr><td>${sym}</td><td>${price}</td><td class="${cls}">${sign}</td></tr>`;
  }).join('');
  document.getElementById('meta').textContent = 'last fetch: ' + (d.last_run || 'never');
}
load();
setInterval(load, 15000);
</script>
</body>
</html>"""
