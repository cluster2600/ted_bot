#!/usr/bin/env python3
"""ted_bot — daily EU TED Contract-Award scanner for small-cap materiality alerts.

Flow: TED v3 search API -> CPV/date/notice-type filter -> resolve each winner to a
listed company (whitelist fuzzy-match, else LLM consolidates subsidiary -> listed
parent + ticker, cached) -> yfinance financials -> materiality & market-cap gate ->
Telegram push. Idempotent: every examined notice is recorded and every winner
resolution cached, so re-runs never double-alert or re-query the LLM.

Usage:
    python3 ted_scanner.py --init-db          # create tables from schema.sql
    python3 ted_scanner.py                     # daily scan (cron target)
    python3 ted_scanner.py --dry-run           # scan, log decisions, send nothing, record nothing
    python3 ted_scanner.py --dump 3            # print raw JSON of 3 notices (field discovery)
    python3 ted_scanner.py --evaluate-alerts   # run only due J+30 evaluations
    python3 ted_scanner.py --evaluation-report # rebuild the local table + graph
    python3 ted_scanner.py --selftest          # offline logic self-check

Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  (required to send)
     NVIDIA_API_KEY (optional — enables the Nemotron adapt-in-the-loop layer)
     TED_HEARTBEAT_URL (optional — dead-man's-switch ping after each run)
     TED_DB (default ./ted.db), TED_REPORT_DIR (default ./reports)
     TED_LOOKBACK_DAYS (default 3)
"""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import html
import json
import logging
import os
import re
import sqlite3
import sys
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter

from alert_evaluation import (
    ensure_schema as ensure_alert_schema,
    evaluate_due_alerts,
    latest_close,
    load_alerts,
    load_history,
    mark_reported,
    record_alert,
    render_reports,
    selftest as evaluation_selftest,
    telegram_caption,
    unreported_complete,
)

try:  # urllib3 ships with requests; import defensively across versions
    from urllib3.util.retry import Retry
except Exception:  # pragma: no cover
    Retry = None

try:
    import yfinance as yf
except ImportError:  # yfinance is optional at runtime; scan degrades gracefully
    yf = None

# LLM adapter uses the NVIDIA endpoint (OpenAI-compatible) over plain requests.

# --------------------------------------------------------------------------- #
# Configuration (env-overridable; the field list is the main calibration knob) #
# --------------------------------------------------------------------------- #
HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("TED_DB", os.path.join(HERE, "ted.db"))
SCHEMA_PATH = os.path.join(HERE, "schema.sql")
REPORT_DIR = os.environ.get("TED_REPORT_DIR", os.path.join(HERE, "reports"))

TED_SEARCH_URL = "https://api.ted.europa.eu/v3/notices/search"
# Notice permalink. "/en/notice/{pub}" is a 404 — the working forms are
# "/en/notice/-/detail/{pub}" (JS single-page app: 202 + empty body to any
# non-browser client) and "/en/notice/{pub}/html", which serves the rendered
# notice directly with a 200. Prefer the latter, and prefer the link the API
# hands us over any URL we build ourselves.
TED_DETAIL_URL = "https://ted.europa.eu/en/notice/{pub}/html"

# High-alpha CPV divisions (matched by prefix on the notice CPV codes).
CPV_CODES = ["72000000", "35000000", "33000000", "09330000"]
NOTICE_TYPES = ["can-standard", "can-social"]           # Contract Award Notices
LOOKBACK_DAYS = int(os.environ.get("TED_LOOKBACK_DAYS", "3"))  # >1 covers weekend gaps; dedup makes overlap free

# Validated against the live TED v3 vocabulary (1830 eForms business terms).
# `fields` is REQUIRED and non-empty; on drift we retry with _MINIMAL_FIELDS.
# Run --dump to re-inspect the live shape if TED renames anything.
REQUEST_FIELDS = [
    "publication-number", "notice-title", "notice-type", "publication-date",
    "classification-cpv", "winner-name", "winner-country",
    "result-value-notice", "result-value-cur-notice",
    "result-value-lot", "result-value-cur-lot",
    "tender-value", "tender-value-cur",
]
_MINIMAL_FIELDS = ["publication-number", "notice-type", "winner-name",
                   "result-value-notice", "result-value-cur-notice"]

MATERIALITY_THRESHOLD = 0.15           # contract EUR / annual revenue EUR (top-line impact)
CAP_MATERIALITY_THRESHOLD = 0.10       # contract EUR / market cap (valuation impact)
MARKET_CAP_MIN = 100_000_000           # EUR
MARKET_CAP_MAX = 2_000_000_000         # EUR
FUZZY_THRESHOLD = 0.87                 # difflib ratio to auto-accept a whitelist match
FUZZY_SOFT = 0.70                      # in [SOFT, THRESHOLD): ask the LLM to confirm
NVIDIA_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
LLM_MODEL = os.environ.get("TED_LLM_MODEL", "nvidia/nemotron-3-ultra-550b-a55b")
PAGE_LIMIT = 100
MAX_PAGES = 50                         # hard stop; 5000 notices/run is plenty

log = logging.getLogger("ted_bot")


# --------------------------------------------------------------------------- #
# Name normalisation & fuzzy matching                                          #
# --------------------------------------------------------------------------- #
_LEGAL_SUFFIXES = (
    "spa", "srl", "sarl", "sas", "sa", "ag", "gmbh", "plc", "ltd", "limited",
    "bv", "nv", "oyj", "oy", "ab", "as", "aps", "kft", "sp", "zoo", "sl",
    "se", "inc", "llc", "co", "corp", "group", "holding", "holdings",
    "uab", "sia", "ou", "doo", "dd", "ead", "ood", "sro",   # Baltic / CEE forms
)


def clean_name(raw: str) -> str:
    """Lower-case, strip punctuation and common legal suffixes for matching."""
    if not raw:
        return ""
    s = raw.lower().replace(".", "")            # "s.a." -> "sa" so the suffix collapses
    s = re.sub(r"[^a-z0-9 ]+", " ", s)          # remaining punctuation/diacritics -> space
    tokens = [t for t in s.split() if t and t not in _LEGAL_SUFFIXES]
    return " ".join(tokens).strip()


def top_candidate(winner_clean: str, whitelist: list[dict]) -> tuple[dict | None, float]:
    """Return (closest whitelist_row, difflib score). Threshold decision is the caller's."""
    best, best_score = None, 0.0
    for row in whitelist:
        score = difflib.SequenceMatcher(None, winner_clean, row["company_name_cleaned"]).ratio()
        if score > best_score:
            best, best_score = row, score
    return best, best_score


# --------------------------------------------------------------------------- #
# LLM adapter (NVIDIA Nemotron) — recovers drifted fields & confirms matches   #
# Optional: no NVIDIA_API_KEY -> every call returns None, behaviour identical    #
# is identical to pure heuristics. ponytail: one small model, no thinking, cheap#
# --------------------------------------------------------------------------- #
def _parse_json_blob(text: str | None) -> dict | None:
    """Pull the first {...} object out of an LLM reply, tolerating surrounding prose."""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


class LLMAdapterError(RuntimeError):
    """The LLM adapter was configured but every call failed — a config error, not a quiet day."""


# Call tally for the run. A misconfigured adapter (wrong model id, dead key) fails
# every single call while the scan still exits 0 with zero alerts — indistinguishable
# from "quiet day" unless we count and report it. See llm_health_report().
_LLM_CALLS = 0
_LLM_FAILURES = 0


def _llm_json(prompt: str) -> dict | None:
    global _LLM_CALLS, _LLM_FAILURES
    key = os.environ.get("NVIDIA_API_KEY")
    if not key:
        return None
    _LLM_CALLS += 1
    try:
        r = requests.post(
            NVIDIA_URL, timeout=60,
            headers={"Authorization": f"Bearer {key}"},
            json={"model": LLM_MODEL, "max_tokens": 512, "temperature": 0.2,
                  "chat_template_kwargs": {"enable_thinking": False},
                  "messages": [{"role": "user", "content": prompt}]})
        if r.status_code != 200:
            _LLM_FAILURES += 1
            log.error("NVIDIA LLM HTTP %d for model %r: %s",
                      r.status_code, LLM_MODEL, r.text[:200])
            return None
        text = r.json()["choices"][0]["message"]["content"]
    except (requests.RequestException, KeyError, ValueError, IndexError) as e:
        _LLM_FAILURES += 1
        log.error("NVIDIA LLM call failed (model %r): %s", LLM_MODEL, e)
        return None
    return _parse_json_blob(text)


def check_llm_config() -> None:
    """Warn at startup when the adapter is configured with a non-NVIDIA model id.

    Every id served by integrate.api.nvidia.com is "vendor/model". A bare name —
    e.g. the "claude-haiku-4-5" left behind by the Anthropic->Nemotron switch —
    is rejected by the endpoint, so all recovery and match-confirmation silently
    stops and the scan reports zero alerts.
    """
    if not os.environ.get("NVIDIA_API_KEY"):
        return
    if "/" not in LLM_MODEL:
        log.error("TED_LLM_MODEL=%r is not an NVIDIA model id (expected vendor/model, "
                  "e.g. nvidia/nemotron-3-ultra-550b-a55b) — every LLM call will fail",
                  LLM_MODEL)


def llm_health_report() -> bool:
    """Log the adapter's tally. False when it was configured but wholly unusable."""
    if not _LLM_CALLS:
        return True
    if _LLM_FAILURES == _LLM_CALLS:
        log.error("LLM adapter unusable: all %d call(s) failed with model %r — winner "
                  "recovery and fuzzy-match confirmation were skipped this run, so "
                  "alerts may be missing", _LLM_CALLS, LLM_MODEL)
        return False
    if _LLM_FAILURES:
        log.warning("LLM adapter degraded: %d/%d call(s) failed",
                    _LLM_FAILURES, _LLM_CALLS)
    return True


def llm_extract(notice: dict) -> dict | None:
    """Ask the LLM to read winner/value/currency straight from a raw notice."""
    prompt = (
        "Parse this EU TED procurement Contract Award Notice (JSON). Extract the company "
        "that WON the contract and the total awarded amount. Reply with ONLY minified JSON: "
        '{"winner": string|null, "value": number|null, "currency": "3-letter code"|null}. '
        "Use null for anything not present.\n\n"
        + json.dumps(notice, ensure_ascii=False)[:6000]  # cap tokens on huge notices
    )
    data = _llm_json(prompt)
    if not data:
        return None
    return {"winner": data.get("winner") or None,
            "value": _num(data.get("value")),
            "currency": (data.get("currency") or "EUR")}


def llm_same_entity(winner: str, company: str) -> bool:
    """Confirm a borderline name match (translations, abbreviations, holding names)."""
    prompt = (
        'Are these the same company, ignoring legal suffixes, translation, and abbreviation? '
        'Reply with ONLY JSON: {"same": true|false}.\n'
        f"A (TED award winner): {winner}\nB (watchlist company): {company}"
    )
    data = _llm_json(prompt)
    return bool(data and data.get("same") is True)


def _parent_from_blob(data: dict | None) -> dict | None:
    """Turn an llm_resolve_parent reply into a whitelist-shaped row, or None.

    Requires listed==true AND a ticker; otherwise the winner is treated as
    private/unlisted (caller caches the negative so we never re-ask).
    """
    if not data or data.get("listed") is not True:
        return None
    ticker = (data.get("ticker") or "").strip()
    if not ticker:
        return None
    return {"parent_name": (data.get("parent_name") or "").strip() or None,
            "ticker": ticker,
            "exchange": (data.get("exchange") or None)}


def llm_resolve_parent(winner: str) -> dict | None:
    """Resolve a procurement winner to its publicly-listed parent (or itself).

    This is the consolidation step: a TED/simap winner is usually the *local
    legal entity* ('Siemens Healthcare GmbH'), not the listed group. The LLM
    maps it to the ultimate listed parent + Yahoo-Finance ticker
    ('Siemens Healthineers', SHL.DE). Returns a whitelist-shaped dict
    {parent_name, ticker, exchange} when listed, else None.
    """
    prompt = (
        f'A company named "{winner}" won a European public-procurement contract. '
        "Determine whether it is publicly listed on a stock exchange, OR is a "
        "subsidiary whose ULTIMATE parent is publicly listed. Reply with ONLY "
        "minified JSON: "
        '{"listed": true|false, "parent_name": string|null, '
        '"ticker": string|null, "exchange": string|null}. '
        '"ticker" MUST be the Yahoo Finance symbol of the listed (parent) entity '
        'including its market suffix (e.g. "SHL.DE", "ROG.SW", "EXA.PA"), or null '
        "if not listed. Do not guess; use null when unsure."
    )
    return _parent_from_blob(_llm_json(prompt))


# --------------------------------------------------------------------------- #
# Defensive extraction from a TED notice dict                                  #
# --------------------------------------------------------------------------- #
def _leaves(obj, path=""):
    """Yield (key_path, scalar_value) for every leaf in nested dicts/lists."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _leaves(v, f"{path}.{k}".lstrip("."))
    elif isinstance(obj, list):
        for item in obj:
            yield from _leaves(item, path)
    else:
        yield path, obj


def _num(x):
    """Best-effort numeric parse; tolerates '1.234.567,89', '1,234,567.89', None."""
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip()
    s = re.sub(r"[^\d.,-]", "", s)
    if not s:
        return None
    # If both separators present, the last one is the decimal separator.
    if "," in s and "." in s:
        dec = max(s.rfind(","), s.rfind("."))
        s = re.sub(r"[.,]", "", s[:dec]) + "." + re.sub(r"[.,]", "", s[dec + 1:])
    else:
        s = s.replace(",", ".") if s.count(",") == 1 and s.rfind(",") >= len(s) - 3 else s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def _flatten_strings(v):
    """Yield string leaves from a str / list / language-keyed dict TED value."""
    if isinstance(v, str):
        if v:
            yield v
    elif isinstance(v, list):
        for x in v:
            yield from _flatten_strings(x)
    elif isinstance(v, dict):
        for x in v.values():
            yield from _flatten_strings(x)


def _first_number(v):
    """Largest positive number anywhere inside a str/list/dict value, else None."""
    nums = [n for n in (_num(s) for s in _flatten_strings(v)) if n and n > 0]
    return max(nums) if nums else None


def _pick_lang(v, prefer="eng"):
    """Collapse a {lang: text} field (or plain string) to one display string."""
    if isinstance(v, str):
        return v
    if isinstance(v, dict) and v:
        return v.get(prefer) or next(iter(v.values()))
    return ""


def _deep_extract(notice: dict) -> tuple[list[str], float | None, str | None]:
    """Heuristic fallback used only when the explicit fields come up empty.

    ponytail: key-name search over the flattened notice, so a future TED rename
    still yields *something*. The schema-aware path in extract_notice is primary.
    """
    winners, values, currencies = [], [], []
    for path, val in _leaves(notice):
        if val in (None, ""):
            continue
        key = path.lower()
        if ("winner-name" in key or "name-tenderer" in key) and isinstance(val, str):
            winners.append(val)
        elif ("value" in key or "amount" in key) and "cur" not in key:
            n = _num(val)
            if n and n > 0:
                values.append(n)
        elif "cur" in key and isinstance(val, str) and re.fullmatch(r"[A-Za-z]{3}", val):
            currencies.append(val.upper())
    return (list(dict.fromkeys(winners)),
            max(values) if values else None,
            currencies[0] if currencies else None)


def extract_notice(notice: dict) -> dict:
    """Pull id, winners, awarded value and currency from a TED CAN notice.

    Schema-aware for the requested eForms fields; falls back to _deep_extract
    (and, upstream in scan(), the LLM) when the schema drifts.
    """
    pub = notice.get("publication-number") or notice.get("ND")
    winners = list(dict.fromkeys(
        list(_flatten_strings(notice.get("winner-name")))
        or list(_flatten_strings(notice.get("organisation-name-tenderer")))))
    # ponytail: notice-level total preferred; on a multi-lot notice this attributes
    # the whole notice value to a single winner (over-alerts, never under). Per-lot
    # winner->value linkage would need the lot-result graph — upgrade if too noisy.
    value = (_first_number(notice.get("result-value-notice"))
             or _first_number(notice.get("result-value-lot"))
             or _first_number(notice.get("tender-value")))
    currency = next(iter(
        list(_flatten_strings(notice.get("result-value-cur-notice")))
        or list(_flatten_strings(notice.get("result-value-cur-lot")))
        or list(_flatten_strings(notice.get("tender-value-cur")))), None)

    if not winners or value is None:            # explicit fields empty -> heuristic
        w2, v2, c2 = _deep_extract(notice)
        winners = winners or w2
        if value is None:
            value, currency = v2, (currency or c2)

    lots = notice.get("result-value-lot")
    n_lots = len(lots) if isinstance(lots, list) else (1 if value else 0)

    return {
        "notice_id": str(pub) if pub else json.dumps(notice, sort_keys=True)[:64],
        "winners": winners,
        "value": value,
        "currency": (currency or "EUR").upper(),
        "lots": n_lots,
        "title": _pick_lang(notice.get("notice-title")),
        "url": _notice_url(notice, pub),
    }


def _notice_url(notice: dict, pub) -> str | None:
    """Permalink for a notice, preferring the one TED publishes in `links`.

    `links` carries xml/pdf/html/htmlDirect, each a language->URL dict. htmlDirect
    is the server-rendered page; `html` is the SPA route. Falling back to a URL we
    build ourselves keeps alerts working if `links` ever disappears.
    """
    links = notice.get("links")
    if isinstance(links, dict):
        for kind in ("htmlDirect", "html"):
            langs = links.get(kind)
            if isinstance(langs, dict) and langs:
                url = langs.get("ENG") or langs.get("MUL") or next(iter(langs.values()))
                if isinstance(url, str) and url.startswith("http"):
                    return url
    return TED_DETAIL_URL.format(pub=pub) if pub else None


# --------------------------------------------------------------------------- #
# HTTP: TED search + Telegram + FX                                             #
# --------------------------------------------------------------------------- #
def http_session() -> requests.Session:
    s = requests.Session()
    if Retry is not None:
        retry = Retry(total=4, backoff_factor=1.5,
                      status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=frozenset(("GET", "POST")))
        s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update({"Accept": "application/json", "User-Agent": "ted_bot/1.0"})
    return s


def build_query() -> str:
    since = (dt.date.today() - dt.timedelta(days=LOOKBACK_DAYS)).strftime("%Y%m%d")
    cpv = " ".join(CPV_CODES)
    ntype = " ".join(NOTICE_TYPES)
    return (f"classification-cpv IN ({cpv}) "
            f"AND notice-type IN ({ntype}) "
            f"AND publication-date >= {since}")


def fetch_notices(session: requests.Session) -> list[dict]:
    """Fetch all CAN notices for the lookback window, paginating with fallbacks."""
    query = build_query()
    notices: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        body = {"query": query, "page": page, "limit": PAGE_LIMIT,
                "scope": "ALL", "paginationMode": "PAGE_NUMBER",
                "fields": REQUEST_FIELDS}
        data = _post_with_fallback(session, body)
        if data is None:
            break
        batch = data.get("notices") or data.get("results") or []
        notices.extend(batch)
        total = data.get("totalNoticeCount") or data.get("total") or 0
        log.info("page %d: %d notices (running total %d / %s)",
                 page, len(batch), len(notices), total or "?")
        if len(batch) < PAGE_LIMIT:
            break
    return notices


def _post_with_fallback(session, body):
    """POST the search; on 4xx retry with a minimal known-good field set.

    TED requires a non-empty `fields` from a controlled vocabulary, so the
    fallback swaps in fewer fields rather than dropping the parameter.
    """
    for attempt_body in (body, {**body, "fields": _MINIMAL_FIELDS}):
        try:
            r = session.post(TED_SEARCH_URL, json=attempt_body, timeout=60)
        except requests.RequestException as e:
            log.error("TED request failed: %s", e)
            return None
        if r.status_code == 200:
            try:
                return r.json()
            except ValueError:
                log.error("TED returned non-JSON body (%d bytes)", len(r.content))
                return None
        if 400 <= r.status_code < 500 and attempt_body is body:
            log.warning("TED %d with full fields (%s); retrying minimal",
                        r.status_code, r.text[:200])
            continue
        log.error("TED HTTP %d: %s", r.status_code, r.text[:300])
        return None
    return None


_FX_CACHE: dict[str, float] = {"EUR": 1.0}


def to_eur(amount: float, currency: str) -> float | None:
    """Convert amount to EUR. yfinance FX with 1.0 fallback for EUR only.

    ponytail: naive spot FX via yfinance, cached per run. Swap for a real FX
    feed if multi-currency accuracy ever matters.
    """
    if amount is None:
        return None
    cur = (currency or "EUR").upper()
    if cur in _FX_CACHE:
        return amount * _FX_CACHE[cur]
    rate = None
    if yf is not None:
        try:
            hist = yf.Ticker(f"{cur}EUR=X").history(period="1d")
            if not hist.empty:
                rate = float(hist["Close"].iloc[-1])
        except Exception as e:
            log.warning("FX lookup %s->EUR failed: %s", cur, e)
    if rate is None:
        log.warning("no FX rate for %s; treating value as unconvertible", cur)
        return None
    _FX_CACHE[cur] = rate
    return amount * rate


# Quotes can be denominated in minor units (AVON.L reports "GBp"), but marketCap
# and totalRevenue are always returned in major units — so only the code needs
# normalising, never the magnitude.
_MINOR_UNIT_CCY = {"GBX": "GBP", "ZAC": "ZAR", "ILA": "ILS"}


def _norm_ccy(code: str | None) -> str:
    """Normalise a yfinance currency code to ISO-4217 (GBp/GBX -> GBP)."""
    cur = (code or "EUR").upper()
    return _MINOR_UNIT_CCY.get(cur, cur)


def refresh_financials(row: dict, session: requests.Session) -> dict:
    """Top up missing market_cap / annual_revenue_eur from yfinance, in place.

    Both gates are denominated in EUR, so each figure is converted from its own
    source currency: the quote currency for market cap, and the reporting currency
    of the statements for revenue. They genuinely differ — AVON.L quotes in GBp but
    reports revenue in USD. Storing raw yfinance numbers (as this did before) made
    every non-EUR company fail the €100M–€2B band by roughly its FX rate: a DKK or
    SEK cap reads ~7-11x too large and is silently rejected as "too big".
    """
    if yf is None or not row.get("ticker"):
        return row
    if row.get("market_cap") and row.get("annual_revenue_eur"):
        return row
    try:
        info = yf.Ticker(row["ticker"]).info or {}
    except Exception as e:
        log.warning("yfinance refresh failed for %s: %s", row["ticker"], e)
        return row
    cap = info.get("marketCap")
    rev = info.get("totalRevenue")
    cap_ccy = _norm_ccy(info.get("currency"))
    rev_ccy = _norm_ccy(info.get("financialCurrency") or info.get("currency"))
    if cap and not row.get("market_cap"):
        # Leave NULL rather than persist an unconverted figure: the row is cached,
        # so a wrong value would poison every future run, not just this one.
        cap_eur = to_eur(float(cap), cap_ccy)
        if cap_eur is None:
            log.warning("market cap for %s left unset: no %s->EUR rate",
                        row["ticker"], cap_ccy)
        else:
            if cap_ccy != "EUR":
                log.debug("%s market cap %.0f %s -> %.0f EUR",
                          row["ticker"], float(cap), cap_ccy, cap_eur)
            row["market_cap"] = cap_eur
            row["_dirty"] = True
    if rev and not row.get("annual_revenue_eur"):
        rev_eur = to_eur(float(rev), rev_ccy)
        if rev_eur is None:
            log.warning("revenue for %s left unset: no %s->EUR rate",
                        row["ticker"], rev_ccy)
        else:
            if rev_ccy != "EUR":
                log.debug("%s revenue %.0f %s -> %.0f EUR",
                          row["ticker"], float(rev), rev_ccy, rev_eur)
            row["annual_revenue_eur"] = rev_eur
            row["_dirty"] = True
    return row


def send_heartbeat(session: requests.Session, notices_seen: int, alerts: int) -> None:
    """Ping a dead-man's-switch URL (e.g. healthchecks.io) so a silent cron
    failure is noticed. No-op unless TED_HEARTBEAT_URL is set."""
    url = os.environ.get("TED_HEARTBEAT_URL")
    if not url:
        return
    try:
        session.get(url, params={"notices": notices_seen, "alerts": alerts}, timeout=15)
    except requests.RequestException as e:
        log.warning("heartbeat ping failed: %s", e)


def send_telegram_alert(text: str, session: requests.Session) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set; cannot send")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": False}
    try:
        r = session.post(url, json=payload, timeout=30)
        if r.status_code == 200:
            return True
        log.error("Telegram HTTP %d: %s", r.status_code, r.text[:200])
    except requests.RequestException as e:
        log.error("Telegram send failed: %s", e)
    return False


def send_telegram_photo(path: str, caption: str, session: requests.Session) -> bool:
    """Send the J+30 graph with a compact table in its Telegram caption."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set; cannot send report")
        return False
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    try:
        with open(path, "rb") as photo:
            response = session.post(
                url,
                data={"chat_id": chat, "caption": caption, "parse_mode": "HTML"},
                files={"photo": (os.path.basename(path), photo, "image/png")},
                timeout=60,
            )
        if response.status_code == 200:
            return True
        log.error("Telegram report HTTP %d: %s", response.status_code, response.text[:200])
    except (OSError, requests.RequestException) as error:
        log.error("Telegram report send failed: %s", error)
    return False


# --------------------------------------------------------------------------- #
# Decision + alert formatting                                                  #
# --------------------------------------------------------------------------- #
def evaluate(row: dict, value_eur: float) -> tuple[bool, dict]:
    """The core equation: will this contract move a listed small-cap's valuation?

    Fires when the company is a small-cap (cap in band) AND the contract is
    material either against revenue (top-line, >=15%) OR against market cap
    (valuation, >=10%). The cap ratio also lets a contract fire when yfinance
    returns market cap but no revenue. Returns (fire, metrics).
    """
    rev = row.get("annual_revenue_eur")
    cap = row.get("market_cap")
    materiality = (value_eur / rev) if (rev and rev > 0) else None
    cap_ratio = (value_eur / cap) if (cap and cap > 0) else None
    metrics = {"materiality": materiality, "cap_ratio": cap_ratio, "market_cap": cap,
               "annual_revenue_eur": rev, "value_eur": value_eur}
    in_band = cap is not None and MARKET_CAP_MIN <= cap <= MARKET_CAP_MAX
    impactful = ((materiality is not None and materiality >= MATERIALITY_THRESHOLD)
                 or (cap_ratio is not None and cap_ratio >= CAP_MATERIALITY_THRESHOLD))
    return (in_band and impactful), metrics


def format_alert(row, notice, metrics) -> str:
    # HTML parse_mode: TED titles/company names contain _ * [ ` freely, which
    # break Telegram's Markdown parser (400, lost alert). HTML needs only & < >
    # escaped, so html.escape() on every dynamic field is the robust choice.
    esc = html.escape
    cap_m = metrics["market_cap"] / 1e6 if metrics["market_cap"] else 0
    val_m = metrics["value_eur"] / 1e6
    mat = f"{metrics['materiality'] * 100:.1f}% of revenue" if metrics.get("materiality") else "revenue n/a"
    capr = f"{metrics['cap_ratio'] * 100:.1f}% of market cap" if metrics.get("cap_ratio") else "—"
    lots = notice.get("lots") or 1
    lot_txt = f"{lots} lots" if lots != 1 else "1 lot"
    link = notice.get("url") or TED_DETAIL_URL.format(pub=notice["notice_id"])
    title = esc(notice["title"] or "Contract Award Notice")
    company = esc(row["company_name_cleaned"].title())
    ticker = esc(row.get("ticker") or "n/a")
    return (
        f"\U0001F6A8 <b>TED Small-Cap Award Alert</b>\n\n"
        f"<b>Company:</b> {company} (<code>{ticker}</code>)\n"
        f"<b>Contract:</b> {title}\n"
        f"<b>Value:</b> €{val_m:,.1f}M ({esc(notice['currency'])}, {esc(lot_txt)})\n"
        f"<b>Impact:</b> <b>{esc(mat)}</b> · {esc(capr)}\n"
        f"<b>Market Cap:</b> €{cap_m:,.0f}M\n\n"
        f'<a href="{esc(link)}">View notice on TED</a>'
    )


# --------------------------------------------------------------------------- #
# Database                                                                     #
# --------------------------------------------------------------------------- #
def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        sql = f.read()
    with connect() as con:
        con.executescript(sql)
    log.info("initialised database at %s", DB_PATH)


def reset_financials() -> None:
    """Clear cached market_cap / annual_revenue_eur so the next run refetches them.

    Rows cached before the FX fix hold raw yfinance figures in the stock's own
    currency. refresh_financials() skips any row that already has both fields, so
    those stale values would survive the fix forever — run this once after
    upgrading, for rows that only yfinance filled (a ticker is required).
    """
    with connect() as con:
        cur = con.execute(
            "UPDATE small_caps_whitelist SET market_cap = NULL, annual_revenue_eur = NULL "
            "WHERE ticker IS NOT NULL AND ticker != ''")
        con.commit()
        log.info("cleared cached financials for %d row(s); the next scan refetches "
                 "them in EUR", cur.rowcount)


def load_whitelist(con) -> list[dict]:
    rows = con.execute(
        "SELECT id, company_name_cleaned, ticker, isin, exchange, "
        "annual_revenue_eur, market_cap FROM small_caps_whitelist"
    ).fetchall()
    return [dict(r) for r in rows]


def persist_row(con, row) -> None:
    con.execute(
        "UPDATE small_caps_whitelist SET annual_revenue_eur=?, market_cap=?, "
        "updated_at=datetime('now') WHERE id=?",
        (row.get("annual_revenue_eur"), row.get("market_cap"), row["id"]),
    )


def already_processed(con, notice_id) -> bool:
    return con.execute(
        "SELECT 1 FROM processed_notices WHERE notice_id=?", (notice_id,)
    ).fetchone() is not None


def mark_processed(con, notice_id) -> None:
    con.execute("INSERT OR IGNORE INTO processed_notices(notice_id) VALUES (?)",
                (notice_id,))


def build_evaluation_report() -> tuple[str, str, list[dict]]:
    """Regenerate the local HTML table and PNG graph without market/API calls."""
    with connect() as con:
        ensure_alert_schema(con)
        rows = load_alerts(con)
        con.commit()
    report, chart = render_reports(rows, Path(REPORT_DIR))
    log.info("J+30 report: %s (%d alert(s))", report, len(rows))
    return str(report), str(chart), rows


def run_alert_evaluations(*, send_report: bool = True) -> tuple[int, int]:
    """Evaluate due alerts, refresh the dashboard and send new results once."""
    session = http_session()
    with connect() as con:
        ensure_alert_schema(con)
        completed = evaluate_due_alerts(
            con,
            lambda ticker, start, end: load_history(yf, ticker, start, end),
        )
        con.commit()
        rows = load_alerts(con)
        report, chart = render_reports(rows, Path(REPORT_DIR))
        pending_report = unreported_complete(con)
        sent = 0
        if send_report and pending_report:
            if send_telegram_photo(str(chart), telegram_caption(pending_report), session):
                mark_reported(con, [row["notice_id"] for row in pending_report])
                con.commit()
                sent = len(pending_report)
        log.info(
            "J+30 evaluation: %d newly complete, %d reported; dashboard=%s",
            len(completed),
            sent,
            report,
        )
        return len(completed), sent


# Resolution cache: winner_clean -> listed?/ticker. Caches BOTH hits and misses
# so the LLM is asked about any given winner (mostly private distributors) once,
# ever. ponytail: created lazily so existing DBs need no migration.
def ensure_resolution_cache(con) -> None:
    con.execute(
        "CREATE TABLE IF NOT EXISTS resolved_entities ("
        " winner_clean TEXT PRIMARY KEY, listed INTEGER NOT NULL,"
        " parent_name TEXT, ticker TEXT, exchange TEXT,"
        " resolved_at TEXT DEFAULT (datetime('now')))")


def resolution_get(con, winner_clean):
    """Return cached row dict, or None if this winner was never resolved."""
    r = con.execute("SELECT listed, parent_name, ticker, exchange "
                    "FROM resolved_entities WHERE winner_clean=?",
                    (winner_clean,)).fetchone()
    return dict(r) if r else None


def resolution_put(con, winner_clean, parent) -> None:
    """Cache a resolution. parent=None marks a confirmed non-listed winner."""
    con.execute(
        "INSERT OR REPLACE INTO resolved_entities"
        "(winner_clean, listed, parent_name, ticker, exchange, resolved_at)"
        " VALUES (?,?,?,?,?, datetime('now'))",
        (winner_clean, 1 if parent else 0,
         parent["parent_name"] if parent else None,
         parent["ticker"] if parent else None,
         parent["exchange"] if parent else None))


# --------------------------------------------------------------------------- #
# Main scan                                                                    #
# --------------------------------------------------------------------------- #
def resolve_winner(con, winner: str, whitelist: list[dict],
                   allow_cache_write: bool) -> dict | None:
    """Map a procurement winner to a *listed* company row, or None.

    1. Fuzzy-match the local whitelist (fast, free allowlist).
    2. Else consult the persistent resolution cache.
    3. Else ask the LLM to consolidate subsidiary -> listed parent, and cache it
       (hit AND miss, so any given winner costs at most one LLM call ever).

    Returns a whitelist-shaped dict; whitelist rows carry an id, LLM-resolved
    parents carry id=None (never written back to the whitelist).
    """
    wc = clean_name(winner)
    if not wc:
        return None
    row, score = top_candidate(wc, whitelist)
    if row:
        accepted = score >= FUZZY_THRESHOLD
        if not accepted and score >= FUZZY_SOFT \
                and llm_same_entity(winner, row["company_name_cleaned"]):
            log.info("LLM confirmed borderline match (%.2f)", score)
            accepted = True
        if accepted:
            log.info("whitelist match: '%s' ~ '%s' (%.2f)", winner,
                     row["company_name_cleaned"], score)
            return row
    # Off-whitelist: consolidate the winner to its listed parent (subsidiary -> group).
    cached = resolution_get(con, wc)
    if cached is not None:
        if not cached["listed"]:
            return None
        parent = {"parent_name": cached["parent_name"], "ticker": cached["ticker"],
                  "exchange": cached["exchange"]}
    else:
        parent = llm_resolve_parent(winner)
        if allow_cache_write:
            resolution_put(con, wc, parent)
        if not parent:
            return None
        log.info("LLM resolved '%s' -> listed parent %s (%s)", winner,
                 parent["parent_name"], parent["ticker"])
    return {"id": None,
            "company_name_cleaned": clean_name(parent["parent_name"] or winner) or wc,
            "ticker": parent["ticker"], "exchange": parent["exchange"],
            "market_cap": None, "annual_revenue_eur": None}


def scan(dry_run: bool = False) -> int:
    session = http_session()
    check_llm_config()
    with connect() as con:
        ensure_resolution_cache(con)
        ensure_alert_schema(con)
        whitelist = load_whitelist(con)
        if not whitelist:
            log.warning("whitelist is empty — LLM resolution still catches listed winners")
        notices = fetch_notices(session)
        log.info("fetched %d notices; whitelist has %d companies",
                 len(notices), len(whitelist))

        alerts = 0
        for raw in notices:
            info = extract_notice(raw)
            nid = info["notice_id"]
            if already_processed(con, nid):
                continue
            # Adapt to schema drift: if the heuristics found no winner or no value,
            # let the LLM read them straight from the raw notice.
            if not info["winners"] or info["value"] is None:
                filled = llm_extract(raw)
                if filled:
                    if not info["winners"] and filled["winner"]:
                        info["winners"] = [filled["winner"]]
                        log.info("LLM recovered winner for %s: %s", nid, filled["winner"])
                    if info["value"] is None and filled["value"]:
                        info["value"], info["currency"] = filled["value"], filled["currency"]
                        log.info("LLM recovered value for %s: %s %s", nid,
                                 filled["value"], filled["currency"])
            for winner in info["winners"]:
                row = resolve_winner(con, winner, whitelist,
                                     allow_cache_write=not dry_run)
                if not row:
                    continue
                row = refresh_financials(row, session)
                if row.get("id") and row.pop("_dirty", False) and not dry_run:
                    persist_row(con, row)
                value_eur = to_eur(info["value"], info["currency"])
                if value_eur is None:
                    log.info("skip %s: no usable contract value", nid)
                    continue
                fire, metrics = evaluate(row, value_eur)
                log.info("%s: materiality=%s cap=%s -> %s", nid,
                         f"{metrics['materiality']:.2%}" if metrics["materiality"] else "n/a",
                         metrics["market_cap"], "FIRE" if fire else "hold")
                if fire:
                    msg = format_alert(row, info, metrics)
                    if dry_run:
                        log.info("[dry-run] would send:\n%s", msg)
                        alerts += 1
                    elif send_telegram_alert(msg, session):
                        alerts += 1
                        record_alert(
                            con,
                            row,
                            info,
                            metrics,
                            price_fetcher=(
                                (lambda ticker: latest_close(yf, ticker)) if yf is not None else None
                            ),
                        )
                        # Persist the successful delivery and its evaluation row together.
                        # A crash later in the scan must not cause a duplicate alert.
                        mark_processed(con, nid)
                        con.commit()
                break  # one winner match per notice is enough
            if not dry_run:
                mark_processed(con, nid)
        if not dry_run:
            con.commit()
        notices_seen = len(notices)
    healthy = llm_health_report()
    # A run whose LLM layer was wholly dead is not a healthy run: withhold the ping
    # so the dead-man's switch pages instead of reporting a green, alert-less scan.
    if not dry_run and healthy:
        send_heartbeat(session, notices_seen, alerts)
    log.info("scan complete: %d alert(s) fired", alerts)
    if not healthy:
        raise LLMAdapterError(
            f"LLM adapter failed on all {_LLM_CALLS} call(s) with model {LLM_MODEL!r}")
    return alerts


def dump_notices(n: int) -> None:
    session = http_session()
    notices = fetch_notices(session)[:n]
    print(json.dumps(notices, indent=2, ensure_ascii=False, default=str))


# --------------------------------------------------------------------------- #
# Offline self-check (ponytail: smallest thing that fails if logic breaks)     #
# --------------------------------------------------------------------------- #
def selftest() -> None:
    assert clean_name("Exail Technologies S.A.") == "exail technologies"
    wl = [{"company_name_cleaned": "exail technologies", "annual_revenue_eur": 200e6,
           "market_cap": 500e6, "ticker": "EXA.PA"}]
    row, score = top_candidate(clean_name("EXAIL TECHNOLOGIES SA"), wl)
    assert row is not None and score >= FUZZY_THRESHOLD, score
    assert top_candidate(clean_name("Totally Different Corp"), wl)[1] < FUZZY_THRESHOLD
    assert _parse_json_blob('noise {"same": true} tail') == {"same": True}
    assert _parse_json_blob("no json here") is None

    # Currency codes from yfinance: quotes may be in minor units, financials are not.
    # A raw DKK/SEK cap left unconverted reads ~7-11x too large and silently fails
    # the €100M-€2B band — that is what suppressed every non-EUR alert.
    # Notice permalink: prefer TED's own htmlDirect link; never emit /en/notice/{pub},
    # which 404s and shipped in every alert until 2026-08-07.
    assert _notice_url({"links": {"htmlDirect": {"ENG": "https://ted.europa.eu/en/notice/1-2026/html"},
                                  "html": {"ENG": "https://ted.europa.eu/en/notice/-/detail/1-2026"}}},
                       "1-2026") == "https://ted.europa.eu/en/notice/1-2026/html"
    assert _notice_url({}, "1-2026") == "https://ted.europa.eu/en/notice/1-2026/html"
    assert _notice_url({"links": {}}, None) is None

    assert _norm_ccy("GBp") == "GBP" and _norm_ccy("GBX") == "GBP"
    assert _norm_ccy(None) == "EUR" and _norm_ccy("dkk") == "DKK"
    _FX_CACHE["DKK"] = 0.134
    assert abs(to_eur(14_705_406_976, "DKK") - 1.970e9) < 1e7   # in band once converted
    assert not evaluate({"annual_revenue_eur": 1.144e9, "market_cap": 14.705e9},
                        value_eur=2.007e9)[0]                   # unconverted cap -> hold
    assert evaluate({"annual_revenue_eur": 1.144e9, "market_cap": 1.965e9},
                    value_eur=2.007e9)[0]                       # converted -> fires
    del _FX_CACHE["DKK"]

    # subsidiary -> listed-parent consolidation: needs listed==true AND a ticker
    assert _parent_from_blob({"listed": True, "parent_name": "Siemens Healthineers",
                              "ticker": "SHL.DE", "exchange": "Xetra"})["ticker"] == "SHL.DE"
    assert _parent_from_blob({"listed": False, "ticker": None}) is None
    assert _parent_from_blob({"listed": True, "ticker": ""}) is None      # listed, no ticker
    assert _parent_from_blob(None) is None

    fire, m = evaluate(wl[0], value_eur=40e6)          # 40/200 = 20% >= 15%, cap in band
    assert fire and abs(m["materiality"] - 0.20) < 1e-9
    assert not evaluate(wl[0], value_eur=10e6)[0]       # 5% materiality -> hold
    assert not evaluate({"annual_revenue_eur": 200e6, "market_cap": 5e9},
                        value_eur=100e6)[0]             # cap too big -> hold
    # valuation-materiality path: fires on contract/market-cap even with no revenue
    assert evaluate({"market_cap": 500e6}, value_eur=100e6)[0]      # 20% of cap, in band
    assert not evaluate({"market_cap": 500e6}, value_eur=10e6)[0]   # 2% of cap -> hold

    # real live TED shape: multilingual winner-name/title, string-number values
    real = extract_notice({
        "publication-number": "449218-2026",
        "winner-name": {"dan": ["Microsoft Danmark aps"]},
        "notice-title": {"eng": "Denmark – IT services", "dan": "Danmark"},
        "result-value-notice": "2976561.85", "result-value-cur-notice": "DKK",
        "tender-value": ["100"], "tender-value-cur": ["DKK"]})
    assert real["notice_id"] == "449218-2026", real
    assert real["winners"] == ["Microsoft Danmark aps"], real["winners"]
    assert real["value"] == 2976561.85 and real["currency"] == "DKK", real
    assert real["title"] == "Denmark – IT services", real["title"]
    assert list(_flatten_strings({"dan": ["A", "B"]})) == ["A", "B"]
    assert _pick_lang({"fra": "Bonjour"}) == "Bonjour"
    # deep-extract fallback still parses an unknown schema
    fb = extract_notice({"publication-number": "1-2026",
                         "winner-name": "Foo SA", "awarded-value": "40.000.000,00"})
    assert fb["value"] == 40_000_000.0 and fb["winners"] == ["Foo SA"], fb
    assert _num("1,234,567.89") == 1234567.89 and _num("1.234.567,89") == 1234567.89

    # format_alert must HTML-escape hostile TED text (& < >) so Telegram won't 400
    msg = format_alert(
        {"company_name_cleaned": "a & b soft", "ticker": None},
        {"notice_id": "9-2026", "title": "IT <svc> _consult_ & more", "currency": "EUR"},
        {"materiality": 0.20, "market_cap": 3e8, "value_eur": 2e6})
    assert "&amp;" in msg and "&lt;svc&gt;" in msg and "<b>" in msg, msg
    assert "<svc>" not in msg, msg
    evaluation_selftest()
    print("selftest: OK")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="TED small-cap award scanner")
    p.add_argument("--init-db", action="store_true", help="create tables and exit")
    p.add_argument("--dry-run", action="store_true", help="scan without sending/recording")
    p.add_argument("--dump", type=int, metavar="N", help="print N raw notices and exit")
    p.add_argument("--test-telegram", action="store_true",
                   help="send a canned message to verify Telegram wiring")
    p.add_argument("--evaluate-alerts", action="store_true",
                   help="evaluate alerts due at J+30, rebuild the dashboard and exit")
    p.add_argument("--evaluation-report", action="store_true",
                   help="rebuild the local J+30 HTML table and graph without API calls")
    p.add_argument("--reset-financials", action="store_true",
                   help="clear cached market cap/revenue so they refetch in EUR "
                        "(run once after the FX fix)")
    p.add_argument("--selftest", action="store_true", help="run offline logic check")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)

    if args.selftest:
        selftest(); return 0
    if args.init_db:
        init_db(); return 0
    if args.reset_financials:
        reset_financials(); return 0
    if args.dump:
        dump_notices(args.dump); return 0
    if args.test_telegram:
        ok = send_telegram_alert(
            "\U0001F6E0 <b>ted_bot</b> — Telegram wiring OK. You'll get award "
            "alerts here at 07:45 UTC.", http_session())
        print("telegram: sent ✔" if ok else "telegram: FAILED (check token/chat_id, see log)")
        return 0 if ok else 1
    if args.evaluation_report:
        build_evaluation_report()
        return 0
    if args.evaluate_alerts:
        run_alert_evaluations()
        return 0
    try:
        if not args.dry_run:
            try:
                run_alert_evaluations()
            except Exception:
                # A market-data/report problem must not suppress the core TED scan.
                log.exception("J+30 evaluation failed; continuing with the daily scan")
        scan(dry_run=args.dry_run)
        return 0
    except LLMAdapterError as e:
        # Already reported in full by llm_health_report(); no traceback needed.
        log.error("%s — fix TED_LLM_MODEL / NVIDIA_API_KEY in .env", e)
        return 1
    except Exception:
        log.exception("scan aborted with an unhandled error")
        return 1


if __name__ == "__main__":
    sys.exit(main())
