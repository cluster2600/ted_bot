-- Sample small-cap watchlist. Copy to deploy/whitelist.sql, edit, and commit
-- to have cloud-init seed it automatically (see terraform/README.md).
--
-- company_name_cleaned MUST be normalised the way the scanner cleans TED text:
-- lower-case, no punctuation, legal suffix (SA/GmbH/SpA/…) stripped.
-- Leave annual_revenue_eur / market_cap NULL to have yfinance fill them in
-- (a ticker is required for that).

INSERT OR IGNORE INTO small_caps_whitelist
  (company_name_cleaned, ticker, isin, exchange, annual_revenue_eur, market_cap)
VALUES
  ('exail technologies',  'EXA.PA',   'FR0012160365', 'Euronext Paris',     184000000,  520000000),
  ('theon international',  'THEON.AS', 'GRW000000009', 'Euronext Amsterdam', 271000000, 1600000000),
  ('sword group',         'SWP.PA',   'FR0004180578', 'Euronext Paris',     318000000,  380000000);
