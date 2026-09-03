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
    """Render J+30 returns, or contract values while every alert is pending."""
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
        ax.set_xlabel("Rendement ajusté du titre entre l’alerte et J+30")
        ax.xaxis.set_major_formatter(FuncFormatter(lambda value, _position: f"{value:.0f}%"))
    elif rows:
        pending = sorted(rows, key=lambda row: float(row["contract_value_eur"]))
        values = [float(row["contract_value_eur"]) / 1_000_000 for row in pending]
        labels = [
            f"{row.get('ticker') or 'n/a'} · {row['notice_id']}"
            for row in pending
        ]
        bars = ax.barh(labels, values, color="#176B87")
        padding = max(0.5, max(values) * 0.015)
        for bar, value in zip(bars, values):
            ax.text(
                value + padding,
                bar.get_y() + bar.get_height() / 2,
                f"€{_number(value)} M",
                va="center",
                ha="left",
                fontsize=9,
                color="#17324D",
                fontweight="bold",
            )
        total = sum(values)
        ax.set_xlim(0, max(values) * 1.24)
        ax.set_title(
            f"Contrats suivis · €{_number(total)} M cumulés",
            fontsize=14,
            fontweight="bold",
        )
        ax.set_xlabel("Valeur publiée du contrat (millions d’euros)")
        ax.xaxis.set_major_formatter(FuncFormatter(lambda value, _position: f"€{value:.0f} M"))
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


def _number(value: float, digits: int = 1) -> str:
    return f"{value:,.{digits}f}".replace(",", " ").replace(".", ",")


def _money(value: float | None) -> str:
    if value is None:
        return "—"
    if value >= 1_000_000_000:
        return f"€{_number(value / 1_000_000_000, 2)} Md"
    return f"€{_number(value / 1_000_000, 1)} M"


def _contract_cap_ratio(row: dict) -> float | None:
    value = row.get("contract_value_eur")
    cap = row.get("market_cap_eur")
    if value is None or cap is None or float(cap) <= 0:
        return None
    return float(value) / float(cap)


def _tracking(row: dict, today: dt.date) -> tuple[int, str]:
    if row.get("evaluation_status") == "complete":
        return 100, "Évaluée"
    if row.get("evaluation_status") == "untrackable":
        return 0, "Non suivable"
    alerted = parse_utc(row["alerted_at"]).date()
    due = parse_utc(row["due_at"]).date()
    span = max(1, (due - alerted).days)
    elapsed = min(span, max(0, (today - alerted).days))
    remaining = max(0, (due - today).days)
    label = "Évaluation disponible" if remaining == 0 else f"J+{elapsed} · encore {remaining} j"
    return round(elapsed / span * 100), label


def render_html(
    rows: list[dict],
    output: Path,
    chart_name: str,
    *,
    now: dt.datetime | None = None,
) -> None:
    current = now or utc_now()
    today = current.date()
    evaluated = sum(row.get("evaluation_status") == "complete" for row in rows)
    pending = sum(row.get("evaluation_status") == "pending" for row in rows)
    total_value = sum(float(row.get("contract_value_eur") or 0) for row in rows)
    largest = max(rows, key=lambda row: float(row.get("contract_value_eur") or 0), default=None)
    ratios = [ratio for row in rows if (ratio := _contract_cap_ratio(row)) is not None]
    max_ratio = max(ratios, default=None)
    due_dates = [
        parse_utc(row["due_at"]).date()
        for row in rows
        if row.get("evaluation_status") == "pending"
    ]
    next_due = min(due_dates, default=None)
    next_due_days = max(0, (next_due - today).days) if next_due else None
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
        ratio = _contract_cap_ratio(row)
        ratio_text = "—" if ratio is None else f"{_number(ratio * 100)}%"
        progress, tracking_label = _tracking(row, today)
        due_date = parse_utc(row["due_at"]).date()
        title = html.escape(row.get("notice_title") or "Avis d’attribution")
        body_rows.append(
            "<tr>"
            f"<td><span class=eyebrow>{html.escape(row['alerted_at'][:10])}</span>"
            f"<strong class=ticker>{html.escape(row.get('ticker') or 'n/a')}</strong>"
            f"<span>{html.escape(row.get('company_name') or '')}</span></td>"
            f"<td><strong>{notice_cell}</strong><span class=notice-title>{title}</span></td>"
            f"<td><strong>{_money(row.get('contract_value_eur'))}</strong>"
            f"<span>Contrat / cap. : {ratio_text}</span></td>"
            f"<td><strong>{due_date.isoformat()}</strong><span>{html.escape(tracking_label)}</span>"
            f"<div class=progress aria-label=\"Progression {progress}%\"><i style=\"width:{progress}%\"></i></div></td>"
            f"<td class=\"{css_class}\">{performance_text}</td>"
            f"<td><span class=badge>{direction_label(performance)}</span></td>"
            "</tr>"
        )
    if not body_rows:
        body_rows.append('<tr><td colspan="6" class="empty">Aucune alerte enregistrée.</td></tr>')
    next_due_value = "—" if next_due_days is None else f"{next_due_days} j"
    max_ratio_value = "—" if max_ratio is None else f"{_number(max_ratio * 100)}%"
    largest_text = "Aucun contrat suivi" if largest is None else (
        f"Plus gros signal : {html.escape(largest.get('ticker') or largest.get('company_name') or 'n/a')} "
        f"avec {_money(largest.get('contract_value_eur'))}."
    )
    chart_title = "Performance boursière à J+30" if evaluated else "Taille des contrats en suivi"
    document = f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<meta name="robots" content="noindex,nofollow">
<title>TED Market Signals — tableau de bord</title>
<style>
*{{box-sizing:border-box}} body{{font:15px Inter,ui-sans-serif,system-ui,sans-serif;color:#17324d;background:#eef3f5;margin:0}}
main{{max-width:1240px;margin:auto;padding:32px}} header{{background:linear-gradient(125deg,#102a43,#176b87);color:#fff;border-radius:18px;padding:30px;box-shadow:0 14px 35px #102a4326}}
.eyebrow{{display:block;text-transform:uppercase;letter-spacing:.1em;font-size:11px;font-weight:700;color:#6f8495;margin-bottom:6px}}
header .eyebrow{{color:#a9d6e5}} h1{{font-size:clamp(28px,4vw,44px);line-height:1.05;margin:0 0 10px}} .lede{{max-width:720px;color:#dceaf0;margin:0}}
.refresh{{display:inline-block;margin-top:18px;padding:7px 11px;border-radius:999px;background:#ffffff18;color:#dceaf0;font-size:12px}}
.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:20px 0}} .card{{background:#fff;border:1px solid #dce7eb;border-radius:14px;padding:18px 20px;box-shadow:0 8px 20px #23445a0c}}
.card strong{{display:block;font-size:27px;line-height:1.1;color:#102a43}} .card span{{display:block;color:#6f8495;margin-top:6px;font-size:12px}}
.panel{{background:#fff;border:1px solid #dce7eb;border-radius:16px;padding:22px;margin:18px 0;box-shadow:0 8px 20px #23445a0c}} .panel h2{{font-size:17px;margin:0 0 4px}} .panel-note{{color:#6f8495;margin:0 0 16px;font-size:13px}}
.insight{{display:flex;justify-content:space-between;gap:20px;align-items:center;background:#dff4ef;border-left:4px solid #16856b;border-radius:10px;padding:15px 17px;margin:18px 0;color:#24554a}}
.chart{{display:block;width:100%;border-radius:10px}}
.table-wrap{{overflow-x:auto}} table{{width:100%;border-collapse:collapse;min-width:900px}} th,td{{padding:14px 12px;text-align:left;border-bottom:1px solid #e7eef1;vertical-align:middle}}
th{{color:#6f8495;font-size:11px;text-transform:uppercase;letter-spacing:.08em}} tbody tr:hover{{background:#f7fafb}} td span{{display:block;color:#6f8495;font-size:12px;margin-top:4px}} a{{color:#176b87;text-decoration:none}} a:hover{{text-decoration:underline}}
.ticker{{display:block;font-size:16px;color:#102a43}} .notice-title{{max-width:330px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}} .badge{{display:inline-block!important;width:max-content;padding:5px 9px;border-radius:999px;background:#edf2f4;color:#4f6675!important;font-weight:700}}
.progress{{width:130px;height:5px;background:#dfe8ec;border-radius:99px;overflow:hidden;margin-top:8px}} .progress i{{display:block;height:100%;background:#f2a65a;border-radius:99px}}
.up{{color:#16856b;font-weight:800}} .down{{color:#c8563d;font-weight:800}} .flat{{color:#607582;font-weight:800}} .pending,.empty{{color:#83939d}}
footer{{display:flex;justify-content:space-between;gap:18px;color:#71838e;font-size:12px;padding:8px 2px 30px}}
@media(max-width:900px){{main{{padding:18px}} .cards{{grid-template-columns:repeat(2,1fr)}} .insight,footer{{align-items:flex-start;flex-direction:column}}}}
@media(max-width:520px){{main{{padding:10px}} header{{padding:23px 20px}} .cards{{grid-template-columns:1fr 1fr}} .card{{padding:15px}} .card strong{{font-size:22px}}}}
@media print{{body{{background:#fff}} main{{max-width:none}} header,.panel,.card{{box-shadow:none}}}}
</style></head><body><main>
<header><span class=eyebrow>Veille marchés publics européens</span><h1>TED Market Signals</h1>
<p class=lede>Les contrats attribués qui peuvent compter pour des small caps européennes, suivis de l’alerte jusqu’à la mesure boursière à J+30.</p>
<span class=refresh>Actualisé le {current.strftime('%d.%m.%Y à %H:%M')} UTC</span></header>
<section class=cards><div class=card><span class=eyebrow>Signaux</span><strong>{len(rows)}</strong><span>{pending} en cours · {evaluated} évalué(s)</span></div>
<div class=card><span class=eyebrow>Volume cumulé</span><strong>{_money(total_value)}</strong><span>Valeurs publiées par TED</span></div>
<div class=card><span class=eyebrow>Intensité maximale</span><strong>{max_ratio_value}</strong><span>Contrat / capitalisation à l’alerte</span></div>
<div class=card><span class=eyebrow>Prochaine mesure</span><strong>{next_due_value}</strong><span>{next_due.isoformat() if next_due else 'Aucune échéance'}</span></div></section>
<aside class=insight><strong>{largest_text}</strong><span>Les montants ne sont ni des revenus acquis ni une prévision de cours.</span></aside>
<section class=panel><h2>{chart_title}</h2><p class=panel-note>Avant J+30, le graphique compare les montants publiés. Dès qu’une mesure arrive à maturité, il affiche les rendements ajustés.</p>
<img class=chart src="{html.escape(chart_name)}" alt="Graphique de synthèse des alertes TED"></section>
<section class=panel><h2>Pipeline des alertes</h2><p class=panel-note>Progression calendaire jusqu’à J+30 et intensité du contrat par rapport à la capitalisation observée.</p>
<div class=table-wrap><table><thead><tr><th>Société</th><th>Avis TED</th><th>Contrat</th><th>Suivi J+30</th><th>Performance</th><th>Lecture</th></tr></thead>
<tbody>{''.join(body_rows)}</tbody></table></div></section>
<footer><span>Source contrats : TED. Données de marché : yfinance, cours ajustés.</span><span>Observation informative, sans attribution causale ni conseil d’investissement.</span></footer>
</main></body></html>"""
    output.write_text(document, encoding="utf-8")


def render_reports(
    rows: list[dict],
    directory: Path,
    *,
    now: dt.datetime | None = None,
) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    chart = directory / CHART_FILENAME
    report = directory / REPORT_FILENAME
    render_chart(rows, chart)
    render_html(rows, report, chart.name, now=now)
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
        assert "TED Market Signals" in page
        assert "EXM.PA" in page and "+12.5%" in page and "<table>" in page
        assert "€40,0 M" in page and "Contrat / cap.\u00a0: 8,0%" in page
        assert '<meta name="robots" content="noindex,nofollow">' in page

        pending_rows = [
            {
                "notice_id": "2-2026",
                "company_name": "pending example",
                "ticker": "PND.PA",
                "notice_title": "Pending award",
                "notice_url": "https://example.invalid/pending",
                "contract_value_eur": 40_000_000,
                "market_cap_eur": 200_000_000,
                "alerted_at": "2026-01-02T08:00:00+00:00",
                "due_at": "2026-02-01T08:00:00+00:00",
                "evaluation_status": "pending",
                "return_pct": None,
            }
        ]
        pending_report, pending_chart = render_reports(
            pending_rows,
            Path(raw_temp) / "pending",
            now=dt.datetime(2026, 1, 7, 8, tzinfo=dt.timezone.utc),
        )
        pending_page = pending_report.read_text(encoding="utf-8")
        assert pending_chart.stat().st_size > 1_000
        assert "Taille des contrats en suivi" in pending_page
        assert "J+5 · encore 25 j" in pending_page
        assert "Contrat / cap.\u00a0: 20,0%" in pending_page
    print("alert_evaluation selftest: OK")


if __name__ == "__main__":
    selftest()
