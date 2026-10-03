
-- ClickHouse logins and their rights.
-- Passwords are NOT stored here. ${GRAFANA_READER_PASSWORD} is filled in
-- from the Kubernetes Secret "clickhouse-grafana-reader" when applied.
-- Safe to run again: IF NOT EXISTS skips anything that already exists.

-- Read-only, and no query may run longer than 60 seconds.
CREATE SETTINGS PROFILE IF NOT EXISTS reader_profile SETTINGS
    readonly = 1,
    max_execution_time = 60 MAX 60 CHANGEABLE_IN_READONLY;

-- Used by Grafana to draw dashboards.
CREATE USER IF NOT EXISTS grafana_reader
    IDENTIFIED WITH sha256_password BY '${GRAFANA_READER_PASSWORD}'
    SETTINGS PROFILE 'reader_profile';

-- It may read the candles table, and nothing else.
GRANT SELECT ON crypto.prices TO grafana_reader;
