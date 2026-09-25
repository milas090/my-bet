CREATE DATABASE IF NOT EXISTS crypto;

-- 1-minute OHLCV candles from Binance.
-- ReplacingMergeTree: rows with the same (symbol, timestamp) collapse into one,
-- so duplicate writes (e.g. two collector pods during a rollout) are harmless.
-- Duplicates disappear on background merge; use FINAL in queries that need exact counts.
CREATE TABLE IF NOT EXISTS crypto.prices
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
ENGINE = ReplacingMergeTree
PARTITION BY toYYYYMM(timestamp)
ORDER BY (symbol, timestamp);
