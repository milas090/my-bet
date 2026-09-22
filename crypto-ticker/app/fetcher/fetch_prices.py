import json
import os
import sys
import time
from datetime import datetime, timezone

import redis
import requests

SYMBOLS = os.environ.get("SYMBOLS", "bitcoin,ethereum,solana")
VS_CURRENCY = os.environ.get("VS_CURRENCY", "usd")
REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD")

def log(level, msg):
    print(f'{{"ts":"{datetime.now(timezone.utc).isoformat()}","level":"{level}","msg":"{msg}"}}', flush=True)

def fetch_prices():
    url = "https://api.coingecko.com/api/v3/simple/price"
    params = {
        "ids": SYMBOLS,
        "vs_currencies": VS_CURRENCY,
        "include_24hr_change": "true",
    }
    headers = {"Accept": "application/json"}
    resp = requests.get(url, params=params, headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()

def write_to_redis(client, payload):
    now = datetime.now(timezone.utc).isoformat()
    epoch = int(time.time())
    pipe = client.pipeline()
    written = 0
    for coin, data in payload.items():
        price = data.get(VS_CURRENCY)
        if price is None:
            continue
        record = {
            "price": price,
            "change_24h": data.get(f"{VS_CURRENCY}_24h_change"),
            "updated_at": now,
        }
        pipe.hset("prices:latest", coin, json.dumps(record))
        pipe.lpush(f"history:{coin}", json.dumps({"t": epoch, "p": price}))
        pipe.ltrim(f"history:{coin}", 0, 287)
        written += 1
    pipe.set("meta:last_run", now)
    pipe.execute()
    return written

def main():
    log("info", f"starting fetch for {SYMBOLS}")
    client = redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        password=REDIS_PASSWORD,
        decode_responses=True,
    )
    try:
        client.ping()
    except redis.RedisError as e:
        log("error", f"redis connection failed: {e}")
        return 1
    try:
        payload = fetch_prices()
    except Exception as e:
        log("error", f"fetch failed: {e}")
        return 1
    count = write_to_redis(client, payload)
    log("info", f"wrote {count} symbols to redis")
    return 0

if __name__ == "__main__":
    sys.exit(main())
