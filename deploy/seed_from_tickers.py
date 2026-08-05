#!/usr/bin/env python3
"""Build the small_caps_whitelist from a list of tickers via yfinance.

Turns the manual SQL chore into one command. Reuses the scanner's clean_name so
the stored name matches exactly what the matcher normalises TED winners to.

    python3 deploy/seed_from_tickers.py EXA.PA THEON.AS SWP.PA        # print SQL
    python3 deploy/seed_from_tickers.py --file tickers.txt           # one ticker/line
    python3 deploy/seed_from_tickers.py --write ted.db EXA.PA ...     # insert directly
    python3 deploy/seed_from_tickers.py --selftest                    # offline check

Note: yfinance returns revenue/market cap in the stock's *reporting currency*.
Materiality assumes EUR — fine for EUR-listed names; convert others yourself.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from ted_scanner import clean_name  # noqa: E402  (reuse the exact normaliser)

COLS = ["company_name_cleaned", "ticker", "isin", "exchange",
        "annual_revenue_eur", "market_cap"]


def _row_from_info(ticker: str, info: dict, isin: str | None = None) -> dict:
    name = info.get("longName") or info.get("shortName") or ticker
    return {
        "company_name_cleaned": clean_name(name),
        "ticker": ticker,
        "isin": isin or info.get("isin") or None,
        "exchange": info.get("fullExchangeName") or info.get("exchange"),
        "annual_revenue_eur": info.get("totalRevenue"),
        "market_cap": info.get("marketCap"),
    }


def fetch_row(ticker: str) -> dict:
    import yfinance as yf  # imported lazily so --selftest needs no network/deps
    t = yf.Ticker(ticker)
    info = t.info or {}
    isin = None
    try:
        isin = t.isin
    except Exception:
        pass
    return _row_from_info(ticker, info, isin)


def _sql_literal(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def to_sql(rows: list[dict]) -> str:
    body = ",\n".join(
        "  (" + ", ".join(_sql_literal(r[c]) for c in COLS) + ")" for r in rows)
    return (f"INSERT OR IGNORE INTO small_caps_whitelist\n"
            f"  ({', '.join(COLS)})\nVALUES\n{body};")


def write_db(path: str, rows: list[dict]) -> int:
    con = sqlite3.connect(path)
    con.executemany(
        f"INSERT OR IGNORE INTO small_caps_whitelist ({', '.join(COLS)}) "
        f"VALUES ({', '.join('?' * len(COLS))})",
        [tuple(r[c] for c in COLS) for r in rows])
    con.commit()
    n = con.total_changes
    con.close()
    return n


def selftest() -> None:
    r = _row_from_info("EXA.PA", {
        "longName": "Exail Technologies S.A.", "totalRevenue": 184000000,
        "marketCap": 520000000, "fullExchangeName": "Paris"}, isin="FR0012160365")
    assert r["company_name_cleaned"] == "exail technologies", r
    assert r["isin"] == "FR0012160365" and r["annual_revenue_eur"] == 184000000, r
    assert _sql_literal(None) == "NULL"
    assert _sql_literal("O'Neil") == "'O''Neil'"
    sql = to_sql([r])
    assert "exail technologies" in sql and "184000000" in sql and "NULL" not in sql, sql
    # a missing field renders as NULL, not a crash
    bare = _row_from_info("XYZ", {})
    assert "NULL" in to_sql([bare])
    print("selftest: OK")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Seed the whitelist from tickers")
    p.add_argument("tickers", nargs="*", help="yfinance symbols, e.g. EXA.PA")
    p.add_argument("--file", help="read tickers from a file (one per line)")
    p.add_argument("--write", metavar="DB", help="insert into this SQLite db")
    p.add_argument("--selftest", action="store_true")
    args = p.parse_args(argv)

    if args.selftest:
        selftest(); return 0

    tickers = list(args.tickers)
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            tickers += [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    if not tickers:
        p.error("give at least one ticker (or --file)")

    rows = []
    for tk in tickers:
        try:
            row = fetch_row(tk)
        except Exception as e:
            print(f"# {tk}: fetch failed ({e})", file=sys.stderr)
            continue
        if not row["annual_revenue_eur"] or not row["market_cap"]:
            print(f"# {tk}: missing revenue/market_cap — fill in manually", file=sys.stderr)
        rows.append(row)

    if not rows:
        return 1
    if args.write:
        print(f"inserted/updated {write_db(args.write, rows)} row(s) in {args.write}")
    else:
        print(to_sql(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
