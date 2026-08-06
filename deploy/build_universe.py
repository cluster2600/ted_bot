#!/usr/bin/env python3
"""Génère une whitelist EXHAUSTIVE de small-caps cotées UE/EEE via un screener.

« Toutes les boîtes cotées UE, on garde les small-caps » — non hallucinable à la
main. Ce script interroge le screener Financial Modeling Prep (clé gratuite sur
financialmodelingprep.com), filtre 100 M–2 Md et les secteurs alignés CPV, et
écrit un whitelist.sql prêt à seeder. Réutilise clean_name pour normaliser.

    FMP_API_KEY=xxx python3 deploy/build_universe.py > deploy/whitelist.sql

ponytail: FMP par défaut ; si tu as EODHD/autre, remplace fetch(). Le screener
gratuit peut plafonner le nb de résultats — vérifie le compte en stderr.
"""
from __future__ import annotations
import os, sys, requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from ted_scanner import clean_name  # noqa: E402

FMP = "https://financialmodelingprep.com/api/v3/stock-screener"

# Places UE + EEE (Norvège incluse) — accès direct aux marchés publics UE.
EXCHANGES = ["Paris", "Amsterdam", "Brussels", "Lisbon", "XETRA", "Frankfurt",
             "Milan", "Madrid", "Stockholm", "Copenhagen", "Helsinki", "Oslo",
             "Warsaw", "Vienna", "Dublin", "Athens"]
# Secteurs plausibles pour les CPV tech/défense/biotech/énergie.
SECTORS = {"Technology", "Healthcare", "Industrials", "Energy", "Utilities",
           "Communication Services", "Basic Materials"}
CAP_MIN, CAP_MAX = 100_000_000, 2_000_000_000


def fetch() -> dict:
    key = os.environ.get("FMP_API_KEY")
    if not key:
        sys.exit("export FMP_API_KEY=... (clé gratuite financialmodelingprep.com)")
    rows: dict[str, tuple] = {}
    for ex in EXCHANGES:
        params = {"marketCapMoreThan": CAP_MIN, "marketCapLowerThan": CAP_MAX,
                  "exchange": ex, "isEtf": "false", "isFund": "false",
                  "isActivelyTrading": "true", "limit": 5000, "apikey": key}
        try:
            r = requests.get(FMP, params=params, timeout=60)
        except requests.RequestException as e:
            print(f"-- {ex}: {e}", file=sys.stderr); continue
        if r.status_code != 200:
            print(f"-- {ex}: HTTP {r.status_code} {r.text[:120]}", file=sys.stderr); continue
        for c in r.json():
            if c.get("sector") not in SECTORS:
                continue
            name = clean_name(c.get("companyName") or "")
            sym = c.get("symbol")
            if name and sym and name not in rows:
                rows[name] = (sym, c.get("exchangeShortName") or ex, c.get("marketCap"))
    return rows


def main() -> int:
    rows = fetch()
    items = sorted(rows.items())
    print("-- Whitelist auto (screener FMP, small-caps UE/EEE, secteurs CPV).")
    print("INSERT OR IGNORE INTO small_caps_whitelist "
          "(company_name_cleaned, ticker, exchange, market_cap) VALUES")
    for i, (name, (sym, ex, cap)) in enumerate(items):
        n = name.replace("'", "''")
        end = ";" if i == len(items) - 1 else ","
        print(f"('{n}','{sym}','{ex}',{int(cap) if cap else 'NULL'}){end}")
    print(f"-- {len(items)} sociétés", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
