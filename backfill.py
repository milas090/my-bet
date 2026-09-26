import os 
import clickhouse_connect
import json 
from datetime import datetime, timedelta, timezone
import urllib.request
import clickhouse_connect

COINS = [
        ("bitcoin", "BTCUSDT"),
        ("litecoin", "LTCUSDT"),
        ("bitcoin-cash","BCHUSDT"),
        ]

START = datetime.now(timezone.utc) - timedelta(hours=24)
END = datetime.now(timezone.utc) - timedelta(minutes=2)

COLUMNS = [
"timestamp", "symbol", "open", "high", "low", "close",
    "volume", "quote_volume", "trades", "taker_buy_volume",
    "taker_buy_quote", "source",
        ]

client = clickhouse_connect.get_client(
    host=os.environ.get("CLICKHOUSE_HOST", "localhost"),
    port=int(os.environ.get("CLICKHOUSE_PORT", "8123")),
    username=os.environ.get("CLICKHOUSE_USER", "default"),
    password=os.environ.get("CLICKHOUSE_PASSWORD"),
    database=os.environ.get("CLICKHOUSE_DB", "crypto"),
)

 
def fetch (pair, start_ms, end_ms):
    url = (
            "https://api.binance.com/api/v3/klines"
            f"?symbol={pair}&interval=1m"
            f"&startTime={start_ms}&endTime={end_ms}&limit=1000"
            )
    with urllib.request.urlopen(url) as response:
           return json.load(response)


for coin, pair in COINS:
    cursor = START
    total = 0

    while cursor < END:
        candles = fetch(pair, int(cursor.timestamp() * 1000), int(END.timestamp() * 1000))
        if not candles:
            break

        rows = []
        for c in candles:
            opened = datetime.fromtimestamp(c[0] / 1000, tz=timezone.utc)
            if opened >= END:
                continue
            rows.append([
                opened, coin,
                float(c[1]), float(c[2]), float(c[3]), float(c[4]),
                float(c[5]), float(c[7]), int(c[8]),
                float(c[9]), float(c[10]), "binance",
            ])

        if rows:
            client.insert("prices", rows, column_names=COLUMNS)
            total += len(rows)

        last = datetime.fromtimestamp(candles[-1][0] / 1000, tz=timezone.utc)
        cursor = last + timedelta(minutes=1)

    print(f"{coin:<14} inserted {total} candles")
