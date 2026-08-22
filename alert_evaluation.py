#!/usr/bin/env python3
"""Persist TED alerts and evaluate their market performance after 30 days."""

from __future__ import annotations

import datetime as dt
import html
import math
import sqlite3
import tempfile
from pathlib import Path
from typing import Callable, Iterable, Optional


EVALUATION_DAYS = 30
STABLE_BAND_PCT = 2.0
CHART_FILENAME = "ted-alertes-j30.png"
REPORT_FILENAME = "ted-alertes-j30.html"

PricePoint = tuple[dt.date, float]
PriceFetcher = Callable[[str], Optional[PricePoint]]
HistoryLoader = Callable[[str, dt.date, dt.date], list[PricePoint]]


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_utc(value: dt.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_utc(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def ensure_schema(con: sqlite3.Connection) -> None:
    """Create the additive alert table for both new and already-running databases."""
    con.execute(
        "CREATE TABLE IF NOT EXISTS alerts ("
        " notice_id TEXT PRIMARY KEY,"
        " company_name TEXT NOT NULL,"
        " ticker TEXT,"
        " notice_title TEXT,"
        " notice_url TEXT,"
        " contract_value_eur REAL NOT NULL,"
        " market_cap_eur REAL,"
        " alerted_at TEXT NOT NULL,"
        " due_at TEXT NOT NULL,"
        " start_price REAL,"
        " start_price_date TEXT,"
        " end_price REAL,"
        " end_price_date TEXT,"
        " return_pct REAL,"
        " evaluated_at TEXT,"
        " evaluation_status TEXT NOT NULL DEFAULT 'pending',"
        " reported_at TEXT)"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_alerts_due "
        "ON alerts(evaluation_status, due_at)"
    )


def _finite_price(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def points_from_history(history: object) -> list[PricePoint]:
    """Convert a yfinance-like history frame to sorted date/close points."""
    if history is None or getattr(history, "empty", True):
        return []
    try:
        indices = history.index
        closes = history["Close"]
    except (AttributeError, KeyError, TypeError):
        return []
    points: list[PricePoint] = []
    for index, raw_close in zip(indices, closes):
        value = _finite_price(raw_close)
        if value is None:
            continue
        raw_date = index.date() if hasattr(index, "date") else index
        if isinstance(raw_date, dt.datetime):
            raw_date = raw_date.date()
        if isinstance(raw_date, dt.date):
            points.append((raw_date, value))
    return sorted(points, key=lambda item: item[0])


def latest_close(yf_module: object, ticker: str) -> PricePoint | None:
    if not yf_module or not ticker:
        return None
    history = yf_module.Ticker(ticker).history(period="7d", auto_adjust=True)
    points = points_from_history(history)
    return points[-1] if points else None


def load_history(
    yf_module: object,
    ticker: str,
    start: dt.date,
    end: dt.date,
) -> list[PricePoint]:
    if not yf_module or not ticker:
        return []
    history = yf_module.Ticker(ticker).history(
        start=start.isoformat(),
        end=end.isoformat(),
        auto_adjust=True,
    )
    return points_from_history(history)


def select_window_prices(
    points: Iterable[PricePoint],
    alert_date: dt.date,
    due_date: dt.date,
) -> tuple[PricePoint | None, PricePoint | None]:
    """Use the last close at/before alert and first close at/after J+30."""
    ordered = sorted(points, key=lambda item: item[0])
    starts = [point for point in ordered if point[0] <= alert_date]
    ends = [point for point in ordered if point[0] >= due_date]
    return (starts[-1] if starts else None, ends[0] if ends else None)


def record_alert(
    con: sqlite3.Connection,
    row: dict,
    notice: dict,
    metrics: dict,
    *,
    now: dt.datetime | None = None,
    price_fetcher: PriceFetcher | None = None,
) -> bool:
    """Record an alert once, after Telegram delivery has succeeded."""
    ensure_schema(con)
    alerted_at = now or utc_now()
    due_at = alerted_at + dt.timedelta(days=EVALUATION_DAYS)
    ticker = (row.get("ticker") or "").strip() or None
    snapshot = None
    if ticker and price_fetcher:
        try:
            snapshot = price_fetcher(ticker)
        except Exception:
            snapshot = None
    status = "pending" if ticker else "untrackable"
    start_date, start_price = (snapshot if snapshot else (None, None))
    cursor = con.execute(
        "INSERT OR IGNORE INTO alerts("
        " notice_id, company_name, ticker, notice_title, notice_url,"
        " contract_value_eur, market_cap_eur, alerted_at, due_at,"
        " start_price, start_price_date, evaluation_status"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            notice["notice_id"],
            row.get("company_name_cleaned") or ticker or "unknown",
            ticker,
            notice.get("title"),
            notice.get("url"),
            metrics["value_eur"],
            metrics.get("market_cap"),
            iso_utc(alerted_at),
            iso_utc(due_at),
            start_price,
            start_date.isoformat() if start_date else None,
            status,
        ),
    )
    return cursor.rowcount == 1


def evaluate_due_alerts(
    con: sqlite3.Connection,
    history_loader: HistoryLoader,
    *,
    now: dt.datetime | None = None,
) -> list[dict]:
    """Evaluate due pending alerts; market holidays remain pending until data exists."""
    ensure_schema(con)
    current = now or utc_now()
    rows = con.execute(
        "SELECT * FROM alerts WHERE evaluation_status='pending' AND due_at<=? "
        "ORDER BY due_at",
        (iso_utc(current),),
    ).fetchall()
    completed: list[dict] = []
    for raw in rows:
        alert = dict(raw)
        ticker = alert.get("ticker")
        if not ticker:
            con.execute(
                "UPDATE alerts SET evaluation_status='untrackable' WHERE notice_id=?",
                (alert["notice_id"],),
            )
            continue
        alert_date = parse_utc(alert["alerted_at"]).date()
        due_date = parse_utc(alert["due_at"]).date()
        try:
            points = history_loader(
                ticker,
                alert_date - dt.timedelta(days=7),
                due_date + dt.timedelta(days=8),
            )
        except Exception:
            continue
        historical_start, end = select_window_prices(points, alert_date, due_date)
        # Prefer a freshly loaded historical J0 close so both endpoints use the
        # same split/dividend adjustments. The alert-time snapshot is only a
        # fallback when the provider no longer returns that historical point.
        if historical_start:
            start = historical_start
        elif alert.get("start_price"):
            start = (
                dt.date.fromisoformat(alert["start_price_date"]),
                float(alert["start_price"]),
            )
        else:
            start = None
        if not start or not end:
            continue
        performance = end[1] / start[1] - 1.0
        con.execute(
            "UPDATE alerts SET start_price=?, start_price_date=?, end_price=?,"
            " end_price_date=?, return_pct=?, evaluated_at=?,"
            " evaluation_status='complete' WHERE notice_id=?",
            (
                start[1],
                start[0].isoformat(),
                end[1],
                end[0].isoformat(),
                performance,
                iso_utc(current),
                alert["notice_id"],
            ),
        )
        alert.update(
            {
                "start_price": start[1],
                "start_price_date": start[0].isoformat(),
                "end_price": end[1],
                "end_price_date": end[0].isoformat(),
                "return_pct": performance,
                "evaluated_at": iso_utc(current),
                "evaluation_status": "complete",
            }
        )
        completed.append(alert)
    return completed


def load_alerts(con: sqlite3.Connection) -> list[dict]:
    ensure_schema(con)
    return [
        dict(row)
        for row in con.execute(
            "SELECT * FROM alerts ORDER BY alerted_at DESC, notice_id DESC"
        ).fetchall()
    ]


def unreported_complete(con: sqlite3.Connection) -> list[dict]:
    ensure_schema(con)
    return [
        dict(row)
        for row in con.execute(
            "SELECT * FROM alerts WHERE evaluation_status='complete' "
            "AND reported_at IS NULL ORDER BY evaluated_at, notice_id"
        ).fetchall()
    ]


def mark_reported(con: sqlite3.Connection, notice_ids: Iterable[str], *, now=None) -> None:
    timestamp = iso_utc(now or utc_now())
    con.executemany(
        "UPDATE alerts SET reported_at=? WHERE notice_id=?",
        [(timestamp, notice_id) for notice_id in notice_ids],
    )


def direction_label(return_pct: float | None) -> str:
    if return_pct is None:
        return "En attente"
    value = return_pct * 100
    if value > STABLE_BAND_PCT:
        return "Hausse"
    if value < -STABLE_BAND_PCT:
        return "Baisse"
    return "Stable"


def render_chart(rows: list[dict], output: Path) -> None:
    """Render a colorblind-friendly diverging J+30 bar chart."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter

    complete = [row for row in rows if row.get("return_pct") is not None]
    complete.sort(key=lambda row: float(row["return_pct"]))
    height = max(4.0, 1.8 + 0.55 * max(1, len(complete)))
    fig, ax = plt.subplots(figsize=(10, height))
    if complete:
        values = [float(row["return_pct"]) * 100 for row in complete]
        labels = [
            f"{row.get('ticker') or 'n/a'} · {row['alerted_at'][:10]}"
            for row in complete
        ]
        colors = [
            "#0072B2" if value > STABLE_BAND_PCT
            else "#D55E00" if value < -STABLE_BAND_PCT
            else "#999999"
            for value in values
        ]
        bars = ax.barh(labels, values, color=colors)
        ax.axvline(0, color="#333333", linewidth=1)
        padding = max(0.5, max(abs(value) for value in values) * 0.02)
        for bar, value in zip(bars, values):
            inside = abs(value) >= 4.0
            if inside:
                x = value - padding if value >= 0 else value + padding
                align = "right" if value >= 0 else "left"
                color = "#FFFFFF"
            else:
                x = value + padding if value >= 0 else value - padding
                align = "left" if value >= 0 else "right"
                color = "#222222"
            ax.text(
                x,
                bar.get_y() + bar.get_height() / 2,
                f"{value:+.1f}%",
                va="center",
                ha=align,
                fontsize=9,
                color=color,
                fontweight="bold" if inside else "normal",
            )
        average = sum(values) / len(values)
        ax.set_title(
            f"Alertes TED : rendement moyen à J+30 {average:+.1f}%",
            fontsize=14,
            fontweight="bold",
        )
    else:
        ax.text(
            0.5,
            0.5,
            "Aucune alerte n’a encore atteint J+30",
            transform=ax.transAxes,
            ha="center",
            va="center",
            fontsize=13,
            color="#555555",
        )
        ax.set_title("Alertes TED : évaluation à J+30", fontsize=14, fontweight="bold")
        ax.set_yticks([])
    ax.set_xlabel("Rendement ajusté du titre entre l’alerte et J+30")
    ax.xaxis.set_major_formatter(FuncFormatter(lambda value, _position: f"{value:.0f}%"))
    ax.grid(axis="x", color="#DDDDDD", linewidth=0.8)
    ax.grid(axis="y", visible=False)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_visible(False)
    fig.text(
        0.01,
        0.01,
        "Observation de marché, sans attribution causale au contrat TED.",
        fontsize=8,
        color="#666666",
    )
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(output, dpi=150, bbox_inches="tight")
    plt.close(fig)


def _money(value: float | None) -> str:
    if value is None:
        return "—"
    return f"€{value / 1_000_000:,.1f} M"


def render_html(rows: list[dict], output: Path, chart_name: str) -> None:
    evaluated = sum(row.get("evaluation_status") == "complete" for row in rows)
    pending = sum(row.get("evaluation_status") == "pending" for row in rows)
    body_rows = []
    for row in rows:
        performance = row.get("return_pct")
        performance_text = "—" if performance is None else f"{performance * 100:+.1f}%"
        css_class = "pending"
        if performance is not None:
            css_class = "up" if performance * 100 > STABLE_BAND_PCT else (
                "down" if performance * 100 < -STABLE_BAND_PCT else "flat"
            )
        link = row.get("notice_url")
        notice = html.escape(row["notice_id"])
        notice_cell = f'<a href="{html.escape(link)}">{notice}</a>' if link else notice
        body_rows.append(
            "<tr>"
            f"<td>{html.escape(row['alerted_at'][:10])}</td>"
            f"<td><strong>{html.escape(row.get('ticker') or 'n/a')}</strong><br>"
            f"<span>{html.escape(row.get('company_name') or '')}</span></td>"
            f"<td>{notice_cell}</td>"
            f"<td>{_money(row.get('contract_value_eur'))}</td>"
            f"<td>{html.escape(row['due_at'][:10])}</td>"
            f"<td class=\"{css_class}\">{performance_text}</td>"
            f"<td>{direction_label(performance)}</td>"
            "</tr>"
        )
    if not body_rows:
        body_rows.append('<tr><td colspan="7" class="empty">Aucune alerte enregistrée.</td></tr>')
    document = f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>TED Bot — évaluation des alertes à J+30</title>
<style>
body{{font:15px system-ui,sans-serif;color:#17202a;background:#f5f7fa;margin:0;padding:32px}}
main{{max-width:1200px;margin:auto}} h1{{margin-bottom:6px}} .note{{color:#59636e}}
.cards{{display:flex;gap:16px;margin:24px 0}} .card{{background:#fff;border-radius:10px;padding:16px 22px;box-shadow:0 1px 4px #ccd3da}}
.chart{{display:block;width:100%;background:#fff;border-radius:10px;margin:20px 0}}
table{{width:100%;border-collapse:collapse;background:#fff;box-shadow:0 1px 4px #ccd3da}}
th,td{{padding:11px 12px;text-align:left;border-bottom:1px solid #e4e8ec;vertical-align:top}}
th{{background:#112a46;color:#fff}} td span{{color:#66717c}} a{{color:#0067a5}}
.up{{color:#0072b2;font-weight:700}} .down{{color:#d55e00;font-weight:700}} .flat{{color:#666;font-weight:700}}
.pending,.empty{{color:#777}} @media(max-width:800px){{body{{padding:14px}} table{{font-size:12px}} th,td{{padding:7px}}}}
</style></head><body><main>
<h1>Évaluation des alertes TED à J+30</h1>
<p class="note">Le rendement est une observation du titre, pas une preuve que le contrat a causé son évolution.</p>
<div class="cards"><div class="card"><strong>{len(rows)}</strong><br>alertes suivies</div>
<div class="card"><strong>{evaluated}</strong><br>évaluées</div><div class="card"><strong>{pending}</strong><br>en attente</div></div>
<img class="chart" src="{html.escape(chart_name)}" alt="Graphique des rendements à J+30">
<table><thead><tr><th>Alerte</th><th>Société</th><th>Avis TED</th><th>Contrat</th><th>Échéance</th><th>J+30</th><th>Lecture</th></tr></thead>
<tbody>{''.join(body_rows)}</tbody></table>
</main></body></html>"""
    output.write_text(document, encoding="utf-8")


def render_reports(rows: list[dict], directory: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    chart = directory / CHART_FILENAME
    report = directory / REPORT_FILENAME
    render_chart(rows, chart)
    render_html(rows, report, chart.name)
    return report, chart


def telegram_caption(rows: list[dict]) -> str:
    recent = sorted(rows, key=lambda row: row.get("evaluated_at") or "", reverse=True)[:8]
    lines = ["Ticker       J+30"]
    for row in recent:
        ticker = (row.get("ticker") or "n/a")[:10]
        value = float(row["return_pct"]) * 100
        lines.append(f"{ticker:<10} {value:+6.1f}%")
    table = html.escape("\n".join(lines))
    return (
        "📊 <b>TED · évaluation J+30</b>\n"
        f"<pre>{table}</pre>"
        "Observation de marché, sans attribution causale au contrat."
    )


def selftest() -> None:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    ensure_schema(con)
    alerted = dt.datetime(2026, 1, 2, 8, tzinfo=dt.timezone.utc)
    inserted = record_alert(
        con,
        {"company_name_cleaned": "example", "ticker": "EXM.PA"},
        {"notice_id": "1-2026", "title": "Example award", "url": "https://example.invalid"},
        {"value_eur": 40_000_000, "market_cap": 500_000_000},
        now=alerted,
        price_fetcher=lambda _ticker: (dt.date(2026, 1, 2), 100.0),
    )
    assert inserted and not record_alert(
        con,
        {"company_name_cleaned": "example", "ticker": "EXM.PA"},
        {"notice_id": "1-2026", "title": "Duplicate"},
        {"value_eur": 40_000_000, "market_cap": 500_000_000},
        now=alerted,
    )
    # J+30 lands on Sunday 2026-02-01, so the first real close is Monday 02-02.
    # The historical J0 value deliberately supersedes the alert-time snapshot,
    # proving both endpoints use one consistently adjusted series.
    points = [
        (dt.date(2026, 1, 2), 90.0),
        (dt.date(2026, 2, 2), 101.25),
    ]
    completed = evaluate_due_alerts(
        con,
        lambda _ticker, _start, _end: points,
        now=dt.datetime(2026, 2, 3, tzinfo=dt.timezone.utc),
    )
    assert len(completed) == 1 and abs(completed[0]["return_pct"] - 0.125) < 1e-9
    assert completed[0]["start_price"] == 90.0
    assert completed[0]["end_price_date"] == "2026-02-02"
    assert direction_label(0.125) == "Hausse"
    with tempfile.TemporaryDirectory() as raw_temp:
        report, chart = render_reports(load_alerts(con), Path(raw_temp))
        assert report.stat().st_size > 1_000 and chart.stat().st_size > 1_000
        page = report.read_text(encoding="utf-8")
        assert "EXM.PA" in page and "+12.5%" in page and "<table>" in page
    print("alert_evaluation selftest: OK")


if __name__ == "__main__":
    selftest()
