"""Morning digest: condense the day's per-ticker reports into one action PDF.

Runs CHAINED right after the report sweep completes on each trading day
(Mon-Fri, minus declared NSE/BSE exchange holidays) - the cron gate fires
the sweep at DAILY_REPORT_HOUR and invokes this module immediately when the
sweep returns, so the digest always summarizes a just-completed day's
reports (never a half-written folder, and never a separately scheduled
timer that could miss or double-fire). It reads every ticker's folder
written under DAILY_REPORTS_DIR/<today>/, extracts the structured decision
data (rating, action, entry, stop, sizing, target, horizon, executive
summary), selects the stocks of interest for the day, and renders a single
expert-UI PDF:

  python -m scripts.daily_report.digest

Selection rule ("only stocks of interest"):
- Directional calls - Buy / Overweight / Underweight / Sell - are the
  actionable set. Pure Hold ratings are excluded unless they carry a
  fresh structured entry/stop (defensive trim).
- Tickers with no parseable decision (REVIEW, missing trader output, or a
  failed sweep cell) never make the PDF - a half-data row is worse than
  no row.
- If nothing is interesting, the PDF is a quiet one-pager: "Have a restful
  day" - explicitly, not a silent empty file.

Rendering: self-contained HTML -> PDF via WeasyPrint (the Docker image
ships pango/cairo/fontconfig + DejaVu fonts). A host without the native
libraries still gets the print-ready HTML next to it.

Output path: DAILY_DIGEST_PDF, default <reports_dir>/digest/<YYYY-MM-DD>.pdf.

Exit codes:
    0 - PDF written (including the quiet restful-day variant)
    1 - today's report folder is missing (sweep has not run / failed)
"""

from __future__ import annotations

import html
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from scripts.daily_report import config as daily_config
from tradingagents import default_config as config_module
from tradingagents.dataflows.date_window import get_current_date

# ---------------------------------------------------------------------------
# Decision extraction
# ---------------------------------------------------------------------------

# The rendered structured sections inside complete_report.md. Each is a
# `**Label**: value` line; a structured render always carries the label.
_DECISION_LABELS = (
    "Rating",
    "Action",
    "Entry Price",
    "Stop Loss",
    "Position Sizing",
    "Price Target",
    "Time Horizon",
    "Executive Summary",
)

_RATING_RE = re.compile(r"\b(Buy|Overweight|Hold|Underweight|Sell)\b")
_ACTION_RE = re.compile(r"\b(Buy|Hold|Sell)\b")

# Directional ratings are "of interest"; a bare Hold is not (unless it
# carries a fresh entry/stop — see _is_interesting).
_DIRECTIONAL = {"buy", "overweight", "underweight", "sell"}


@dataclass
class Decision:
    """The actionable core of one ticker's report — everything the digest
    card needs and nothing the reader has to dig for."""

    ticker: str
    rating: str = ""
    action: str = ""
    entry: str = ""
    stop: str = ""
    sizing: str = ""
    target: str = ""
    horizon: str = ""
    executive: str = ""

    @property
    def has_direction(self) -> bool:
        return self.rating.lower() in _DIRECTIONAL

    @property
    def has_levels(self) -> bool:
        return bool(self.entry or self.stop)

    def is_interesting(self) -> bool:
        """Directional call, or a Hold that still carries concrete levels
        (a defensive trim is actionable even when the rating is neutral)."""
        return self.has_direction or (self.rating.lower() == "hold" and self.has_levels)


def _label_value(text: str, label: str) -> str:
    """The first `**Label**: value` line for ``label`` in a report body."""
    pattern = re.compile(rf"\*\*{re.escape(label)}\*\*\s*:\s*(.+)")
    m = pattern.search(text)
    if not m:
        return ""
    return m.group(1).strip()


def extract_decision(ticker: str, report_md: Path) -> Decision:
    """Pull the structured decision fields out of a complete_report.md.

    The Trader/Portfolio Manager sections are machine-rendered from Pydantic
    models, so the `**Label**:` lines are stable; free-text lines stay as
    secondary context only.
    """
    body = report_md.read_text(encoding="utf-8", errors="replace") if report_md.is_file() else ""
    if not body:
        return Decision(ticker=ticker)

    # The rating comes from the Portfolio Manager section when present (the
    # authoritative final rating), else from the trader's rendered rating line.
    rating = _label_value(body, "Rating")
    # Normalize case for the direction test without changing display.
    m = _RATING_RE.search(rating)
    rating = m.group(1) if m else rating

    action = _label_value(body, "Action")
    ma = _ACTION_RE.search(action)
    action = ma.group(1) if ma else action

    return Decision(
        ticker=ticker,
        rating=rating,
        action=action,
        entry=_label_value(body, "Entry Price"),
        stop=_label_value(body, "Stop Loss"),
        sizing=_label_value(body, "Position Sizing"),
        target=_label_value(body, "Price Target"),
        horizon=_label_value(body, "Time Horizon"),
        executive=_label_value(body, "Executive Summary"),
    )


def collect_decisions(today_dir: Path) -> list[Decision]:
    """One Decision per ticker folder that has a readable complete_report.md."""
    decisions: list[Decision] = []
    for child in sorted(today_dir.iterdir()):
        if not child.is_dir():
            continue
        report = child / "complete_report.md"
        if not report.is_file():
            continue
        decisions.append(extract_decision(child.name, report))
    return decisions


# ---------------------------------------------------------------------------
# PDF rendering
# ---------------------------------------------------------------------------

_CSS = """
@page { size: A4; margin: 14mm 12mm; }
* { box-sizing: border-box; }
body { font-family: "Segoe UI", "Helvetica Neue", Arial, sans-serif;
       color: #1a1a1a; font-size: 10pt; line-height: 1.45; margin: 0; }
.hdr { display: flex; justify-content: space-between; align-items: baseline;
       border-bottom: 2px solid #1a1a1a; padding-bottom: 6px; margin-bottom: 14px; }
.hdr .t { font-size: 16pt; font-weight: 700; letter-spacing: .3px; }
.hdr .d { font-size: 9pt; color: #555; }
.section { font-size: 8pt; font-weight: 700; letter-spacing: 1px; color: #888;
           text-transform: uppercase; margin: 14px 0 6px; }
.card { border: 1px solid #d8d8d8; border-radius: 8px; padding: 12px 14px;
        margin-bottom: 12px; page-break-inside: avoid; }
.card .top { display: flex; justify-content: space-between; align-items: center;
             margin-bottom: 8px; }
.card .tk { font-size: 13pt; font-weight: 700; }
.pill { font-size: 8pt; font-weight: 700; padding: 3px 10px; border-radius: 12px;
        letter-spacing: .5px; }
.pill.buy,.pill.overweight { background: #e6f4ea; color: #137333; border: 1px solid #b6dfc2; }
.pill.sell,.pill.underweight { background: #fce8e6; color: #c5221f; border: 1px solid #f5c6c0; }
.pill.hold { background: #fef7e0; color: #b06000; border: 1px solid #f5d99b; }
.levels { display: flex; gap: 18px; flex-wrap: wrap; margin: 8px 0; }
.level { min-width: 90px; }
.level .k { font-size: 7pt; color: #888; text-transform: uppercase; letter-spacing: .8px; }
.level .v { font-size: 12pt; font-weight: 700; }
.exec { font-size: 9.5pt; margin: 6px 0; }
.moves { border-top: 1px dashed #ccc; margin-top: 8px; padding-top: 8px; }
.moves h4 { font-size: 8pt; text-transform: uppercase; letter-spacing: .8px;
            color: #555; margin: 0 0 4px; }
.moves ul { margin: 0; padding-left: 16px; }
.moves li { font-size: 9pt; margin-bottom: 2px; }
.quiet { text-align: center; padding: 60px 20px; }
.quiet .big { font-size: 20pt; font-weight: 700; margin-bottom: 8px; }
.quiet .sub { font-size: 10pt; color: #666; }
.foot { margin-top: 18px; font-size: 7.5pt; color: #999; border-top: 1px solid #e5e5e5;
        padding-top: 6px; }
"""


def _pill_class(rating: str) -> str:
    r = rating.lower()
    if r in ("buy", "overweight"):
        return "buy"
    if r in ("sell", "underweight"):
        return "sell"
    return "hold"


def _fmt_level(value: str) -> str:
    """Keep the level crisp: strip the 'not provided' placeholder the
    structured renderer emits for empty optional fields."""
    v = (value or "").strip()
    if not v or v.lower() == "not provided":
        return "—"
    return v


def _move_bullets(d: Decision) -> list[str]:
    """The exact levels a trader executes on, in execution order."""
    bullets: list[str] = []
    if d.entry:
        bullets.append(f"Entry: {_fmt_level(d.entry)}")
    if d.stop:
        bullets.append(f"Stop: {_fmt_level(d.stop)}")
    if d.sizing:
        bullets.append(f"Sizing: {d.sizing}")
    if d.target:
        bullets.append(f"Target: {_fmt_level(d.target)}")
    if d.horizon:
        bullets.append(f"Hold: {_fmt_level(d.horizon)}")
    return bullets


def _card_html(d: Decision) -> str:
    rating = d.rating or "No rating"
    pill = _pill_class(d.rating)
    exec_txt = html.escape(d.executive or d.action or "")
    moves = _move_bullets(d)
    moves_html = ""
    if moves:
        items = "".join(f"<li>{html.escape(m)}</li>" for m in moves)
        moves_html = f"<div class='moves'><h4>Today's move</h4><ul>{items}</ul></div>"
    return (
        "<div class='card'>"
        f"<div class='top'><span class='tk'>{html.escape(d.ticker)}</span>"
        f"<span class='pill {pill}'>{html.escape(rating.upper())}</span></div>"
        f"<div class='exec'>{exec_txt}</div>"
        f"{moves_html}"
        "</div>"
    )


def _quiet_html(date_str: str, scanned: int) -> str:
    return (
        "<div class='quiet'>"
        "<div class='big'>Have a restful day.</div>"
        f"<div class='sub'>No stock of interest across {scanned} analysed tickers on "
        f"{date_str} — every directional call was flat or low-conviction.</div>"
        "</div>"
    )


def render_pdf(decisions: list[Decision], date_str: str, scanned: int) -> Path:
    """Build the digest HTML and write it to DAILY_DIGEST_PDF (or default).

    ``decisions`` is the already-filtered list of interesting stocks; an
    empty list renders the quiet restful-day page.
    """
    # The path layer guarantees a full <...>.pdf target: if DAILY_DIGEST_PDF
    # named a directory, the dated PDF is dropped inside it.
    out = Path(daily_config.digest_pdf_path(date_str))
    out.parent.mkdir(parents=True, exist_ok=True)

    if decisions:
        body = "".join(_card_html(d) for d in decisions)
        header_title = "TradingAgents — Daily Action Digest"
        header_meta = f"{date_str} · {len(decisions)} of {scanned} tickers of interest"
        content = f"<div class='section'>Stocks of interest</div>{body}"
    else:
        header_title = "TradingAgents — Daily Action Digest"
        header_meta = f"{date_str} · no action needed"
        content = _quiet_html(date_str, scanned)

    doc = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<style>{_CSS}</style></head><body>"
        f"<div class='hdr'><span class='t'>{header_title}</span>"
        f"<span class='d'>{header_meta}</span></div>"
        f"{content}"
        "<div class='foot'>Auto-generated by TradingAgents daily digest. "
        "Ratings are the system's model output for analysis — not financial advice. "
        "Verify levels against the live market before executing.</div>"
        "</body></html>"
    )
    html_path = out.with_suffix(".html")
    html_path.write_text(doc, encoding="utf-8")

    # WeasyPrint renders HTML→PDF when its system libraries are present
    # (they are in the Docker image; a host without them still gets the
    # print-ready HTML). Try the PDF, fall back to HTML-only.
    try:
        from weasyprint import HTML  # type: ignore

        HTML(string=doc).write_pdf(str(out))
        print(f"[digest] PDF written: {out}", flush=True)
    except Exception as exc:
        print(
            f"[digest] WeasyPrint unavailable ({exc}); HTML written instead: {html_path}",
            flush=True,
        )
    return out


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run() -> int:
    config = config_module.build_default_config()
    Path(config["results_dir"])
    reports_root = daily_config.reports_dir()
    today = get_current_date()
    today_dir = reports_root / today

    if not today_dir.is_dir():
        print(
            f"[digest] no reports folder for {today} at {today_dir}; "
            f"the report sweep may not have run yet",
            flush=True,
        )
        return 1

    decisions = collect_decisions(today_dir)
    scanned = len(decisions)
    interesting = [d for d in decisions if d.is_interesting()]
    interesting.sort(key=lambda d: d.ticker)

    print(f"[digest] {today}: {len(interesting)} of {scanned} tickers of interest", flush=True)
    out = render_pdf(interesting, today, scanned)
    print(f"[digest] output: {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(run())
