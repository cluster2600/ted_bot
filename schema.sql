-- ted_bot SQLite 3 schema
-- Run once:  python3 ted_scanner.py --init-db   (or)   sqlite3 ted.db < schema.sql

PRAGMA journal_mode = WAL;   -- concurrent read while the daily scan writes

-- Curated universe of publicly-traded European small-caps to watch.
-- company_name_cleaned holds the normalised legal name used for fuzzy matching
-- against TED winner text (lower-cased, suffixes/punctuation stripped).
CREATE TABLE IF NOT EXISTS small_caps_whitelist (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name_cleaned TEXT    NOT NULL,
    ticker               TEXT,                 -- yfinance symbol, e.g. "EXA.PA"
    isin                 TEXT    UNIQUE,        -- optional exact key
    exchange             TEXT,
    annual_revenue_eur   REAL,                 -- denominator for materiality
    market_cap           REAL,                 -- EUR; gate is 100M..2B
    updated_at           TEXT    DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_whitelist_name ON small_caps_whitelist(company_name_cleaned);

-- Every TED notice we have fully examined. Guarantees idempotent daily runs:
-- a notice already here is skipped, so no duplicate Telegram alerts.
CREATE TABLE IF NOT EXISTS processed_notices (
    notice_id    TEXT PRIMARY KEY,
    processed_at TEXT DEFAULT (datetime('now'))
);

-- Alerts successfully delivered to Telegram. Each row is evaluated after
-- 30 calendar days using adjusted market closes. The observed return is a
-- monitoring signal, not a causal attribution to the procurement award.
CREATE TABLE IF NOT EXISTS alerts (
    notice_id          TEXT PRIMARY KEY,
    company_name       TEXT NOT NULL,
    ticker             TEXT,
    notice_title       TEXT,
    notice_url         TEXT,
    contract_value_eur REAL NOT NULL,
    market_cap_eur     REAL,
    alerted_at         TEXT NOT NULL,
    due_at             TEXT NOT NULL,
    start_price        REAL,
    start_price_date   TEXT,
    end_price          REAL,
    end_price_date     TEXT,
    return_pct         REAL,
    evaluated_at       TEXT,
    evaluation_status  TEXT NOT NULL DEFAULT 'pending',
    reported_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_alerts_due ON alerts(evaluation_status, due_at);
