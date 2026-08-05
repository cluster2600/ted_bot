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
