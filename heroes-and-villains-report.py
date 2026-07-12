"""
Heroes & Villains report: biggest-moving holdings side by side (heroes = top
gainers, villains = top losers), for a selectable period (day/week/month/qtr/
6mth/12mth/3yr/5yr). Ranking can be toggled between simple % return and cash
total-return (£); both the simple/TWR % and the cash figure (growth +
dividends) are shown for each entry regardless of which ranking is active.

Covers currently-held stocks and recently-closed positions (per
RECENTLY_CLOSED_DAYS in config.py) across all accounts combined, plus each
configured THEMES entry as an aggregate competing alongside individual stocks
in the same ranking.

Data loading and return calculations live in calculations.py (shared with
total-return-report.py). This file covers period selection, ranking, and HTML rendering.
"""

from __future__ import annotations

import html
import os
from datetime import date, timedelta
from decimal import Decimal

from calculations import (
    Stock,
    last_sell_date,
    list_account_names,
    load_all,
    period_flows,
    portfolio_twr,
    state_on,
)
from config import ACCOUNTS, OUTPUT_DIR, RECENTLY_CLOSED_DAYS, THEMES, TICKERS

TODAY = date.today()

TOP_N = 5

PERIODS = [
    ("Day", timedelta(days=1)),
    ("Week", timedelta(days=7)),
    ("Month", timedelta(days=30)),
    ("3 Months", timedelta(days=91)),
    ("6 Months", timedelta(days=182)),
    ("12 Months", timedelta(days=365)),
    ("3 Years", timedelta(days=365 * 3)),
    ("5 Years", timedelta(days=365 * 5)),
]

DEFAULT_PERIOD_KEY = "3-months"


def _last_two_trading_days(entries: list[Entry], today: date) -> tuple[date, date]:
    """Returns (prior_trading_day, last_trading_day): the most recent trading day
    on/before `today`, and the trading day before that, from the union of every
    entry's quote calendar. A fixed 1-calendar-day window (today vs yesterday)
    collapses to a single trading day over a weekend/holiday, which would always
    show 0% — so "Day" walks the actual quote calendar instead."""
    trading_days: set[date] = set()
    for entry in entries:
        for stock in entry.stocks:
            trading_days.update(d for d in stock.quotes_gbp if d <= today)
    recent_days = sorted(trading_days, reverse=True)[:2]
    if len(recent_days) == 2:
        return recent_days[1], recent_days[0]
    return today - timedelta(days=1), today


# ---------- entries (a ticker or a theme, each backed by one or more Stocks) ----------

class Entry:
    def __init__(self, label: str, kind: str, stocks: list[Stock], closed_on: date | None = None):
        self.label = label            # display name
        self.kind = kind              # "stock", "closed", or "theme"
        self.stocks = stocks          # one Stock per account holding this ticker (or all member stocks for a theme)
        self.closed_on = closed_on    # last sell date, for recently-closed stocks


def _load_entries() -> list[Entry]:
    accounts = ACCOUNTS if ACCOUNTS is not None else list_account_names()

    stocks_by_ticker: dict[str, list[Stock]] = {}
    for account in accounts:
        all_stocks, _ = load_all(TICKERS, account)
        for stock in all_stocks:
            if not stock.activities:
                continue
            stocks_by_ticker.setdefault(stock.ticker, []).append(stock)

    entries: list[Entry] = []
    for ticker, stocks in stocks_by_ticker.items():
        total_units = sum(state_on(stock, TODAY)[0] for stock in stocks)
        name = stocks[0].name
        if total_units > 0:
            entries.append(Entry(f"{name} [{ticker}]", "stock", stocks))
        else:
            closed_dates = [last_sell_date(stock) for stock in stocks if last_sell_date(stock)]
            closed_on = max(closed_dates) if closed_dates else None
            if closed_on and (TODAY - closed_on).days <= RECENTLY_CLOSED_DAYS:
                entries.append(Entry(f"{name} [{ticker}]", "closed", stocks, closed_on=closed_on))

    # themes: aggregate member tickers (across all accounts) into one entry each
    for account_themes in THEMES.values():
        for theme_name, tickers in account_themes.items():
            member_stocks: list[Stock] = []
            for ticker in tickers:
                member_stocks.extend(stocks_by_ticker.get(ticker, []))
            if member_stocks:
                entries.append(Entry(f"{theme_name} Theme", "theme", member_stocks))

    return entries


# ---------- period stats per entry ----------

def entry_period_stats(entry: Entry, start: date, end: date):
    """Compute simple % / cash and TWR % for one entry over (start, end]."""
    market_value_start = market_value_end = Decimal(0)
    buys = sells_total = dividends_total = Decimal(0)

    for stock in entry.stocks:
        _, _, _, mv_start = state_on(stock, start - timedelta(days=1))
        _, _, _, mv_end = state_on(stock, end)
        _, period_buys, period_sells, period_dividends, _ = period_flows(stock, start - timedelta(days=1), end)
        if not mv_start.is_nan():
            market_value_start += mv_start
        if not mv_end.is_nan():
            market_value_end += mv_end
        buys += period_buys
        sells_total += period_sells
        dividends_total += period_dividends

    growth = (market_value_end - market_value_start) - (buys - sells_total)
    total_return_cash = growth + dividends_total
    denominator = market_value_start + buys
    simple_pct = (total_return_cash / denominator * 100) if denominator > 0 else None
    twr_result = portfolio_twr(entry.stocks, start, end)
    twr_pct = twr_result * 100 if twr_result is not None else None

    return {
        "market_value_start": market_value_start,
        "market_value_end": market_value_end,
        "total_return_cash": total_return_cash,
        "dividends": dividends_total,
        "simple_pct": simple_pct,
        "twr_pct": twr_pct,
    }


# ---------- HTML rendering ----------

PERIOD_SELECTOR_CSS = """
p.period-selector, p.rank-selector { margin: 4px 0 20px; }
p.period-selector button, p.rank-selector button {
    font-size: 0.88em; padding: 5px 12px; margin-right: 6px; margin-bottom: 6px;
    border: 1px solid #99a; border-radius: 4px; background: #eef1fb; color: #336; cursor: pointer;
}
p.period-selector button:hover, p.rank-selector button:hover { background: #dde3f7; }
p.period-selector button.active, p.rank-selector button.active { background: #336; color: #fff; border-color: #336; }
p.rank-selector { font-size: 0.85em; }
"""

PERIOD_SELECTOR_JS = """
(function() {
  var PERIOD_KEY = 'heroesVillainsPeriod';
  var RANK_KEY = 'heroesVillainsRank';
  var DEFAULT_PERIOD = '__DEFAULT_PERIOD_KEY__';
  var periodButtons = document.querySelectorAll('button.period-btn');
  var rankButtons = document.querySelectorAll('button.rank-btn');
  var panels = document.querySelectorAll('div.period-panel');
  var state = { period: null, rank: null };

  function apply() {
    periodButtons.forEach(function(b) { b.classList.toggle('active', b.dataset.period === state.period); });
    rankButtons.forEach(function(b) { b.classList.toggle('active', b.dataset.rank === state.rank); });
    panels.forEach(function(p) {
      p.style.display = (p.dataset.period === state.period && p.dataset.rank === state.rank) ? '' : 'none';
    });
    try {
      localStorage.setItem(PERIOD_KEY, state.period);
      localStorage.setItem(RANK_KEY, state.rank);
    } catch (e) {}
  }

  var savedPeriod, savedRank;
  try { savedPeriod = localStorage.getItem(PERIOD_KEY); savedRank = localStorage.getItem(RANK_KEY); } catch (e) {}
  state.period = (savedPeriod && document.querySelector("button.period-btn[data-period='" + savedPeriod + "']")) ? savedPeriod : DEFAULT_PERIOD;
  state.rank = (savedRank && document.querySelector("button.rank-btn[data-rank='" + savedRank + "']")) ? savedRank : rankButtons[0].dataset.rank;
  apply();

  periodButtons.forEach(function(b) {
    b.addEventListener('click', function() { state.period = b.dataset.period; apply(); });
  });
  rankButtons.forEach(function(b) {
    b.addEventListener('click', function() { state.rank = b.dataset.rank; apply(); });
  });
})();
"""

CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 24px; color: #222; }
h1 { margin-bottom: 4px; }
h2 { margin-top: 0; }
.columns { display: flex; gap: 24px; flex-wrap: wrap; }
.column { flex: 1 1 380px; min-width: 320px; }
.column.heroes h2 { color: #1e7d34; }
.column.villains h2 { color: #c0392b; }
ul.movers { list-style: none; margin: 0; padding: 0; }
li.mover {
    display: flex; justify-content: space-between; align-items: baseline;
    padding: 10px 14px; margin-bottom: 8px; border-radius: 6px; border: 1px solid #ddd;
}
.column.heroes li.mover { background: #eaf7ee; border-color: #bfe3c8; }
.column.villains li.mover { background: #fbeceb; border-color: #eec5c1; }
.mover .name { font-weight: 600; }
.mover .name .badge { font-weight: normal; font-size: 0.78em; margin-left: 6px; }
.mover .figures { display: flex; flex-direction: column; text-align: right; white-space: nowrap; }

/* headline figure: pct by default, cash when ranking by cash (data-rank='cash' on the ancestor panel) */
.mover .headline { font-size: 1.05em; font-weight: 700; order: 1; }
.mover .secondary { font-size: 0.85em; font-weight: 400; color: #555; order: 2; }
.column.heroes .headline { color: #1e7d34; }
.column.villains .headline { color: #c0392b; }
.mover .twr { font-size: 0.78em; color: #888; order: 3; }

.period-panel[data-rank='cash'] .mover .pct.headline { font-size: 0.85em; font-weight: 400; color: #555; order: 2; }
.period-panel[data-rank='cash'] .mover .cash.secondary { font-size: 1.05em; font-weight: 700; color: inherit; order: 1; }
.period-panel[data-rank='cash'] .column.heroes .cash.secondary { color: #1e7d34; }
.period-panel[data-rank='cash'] .column.villains .cash.secondary { color: #c0392b; }
.empty-note { color: #888; font-style: italic; padding: 10px 14px; }
span.badge.theme { background: #6b5b95; color: #fff; border-radius: 3px; padding: 2px 6px; }
span.badge.closed { background: #555; color: #fff; border-radius: 3px; padding: 2px 6px; }

p.totals-bar {
    display: flex; gap: 20px; flex-wrap: wrap; margin: 0 0 20px;
    padding: 10px 16px; background: #f4f4f8; border: 1px solid #ddd; border-radius: 6px;
    font-size: 0.92em;
}
p.totals-bar .totals-item b { font-size: 1.05em; }
p.totals-bar .winners b { color: #1e7d34; }
p.totals-bar .losers b { color: #c0392b; }
p.totals-bar .net.pos b { color: #1e7d34; }
p.totals-bar .net.neg b { color: #c0392b; }
""" + PERIOD_SELECTOR_CSS


def _fmt_pct(value: Decimal | None) -> str:
    if value is None:
        return "&ndash;"
    return f"{value:+.1f}%"


def _fmt_cash(value: Decimal | None) -> str:
    if value is None:
        return ""
    sign = "-" if value < 0 else "+"
    return f"{sign}&pound;{abs(value):,.2f}"


def _mover_li_html(entry: Entry, stats: dict) -> str:
    badge = ""
    if entry.kind == "theme":
        badge = "<span class='badge theme'>theme</span>"
    elif entry.kind == "closed":
        closed_label = f"closed {entry.closed_on.strftime('%d/%m/%Y')}" if entry.closed_on else "closed"
        badge = f"<span class='badge closed'>{html.escape(closed_label)}</span>"
    return (
        "<li class='mover'>"
        f"<span class='name'>{html.escape(entry.label)}{' ' + badge if badge else ''}</span>"
        "<span class='figures'>"
        f"<div class='headline pct'>{_fmt_pct(stats['simple_pct'])}</div>"
        f"<div class='secondary cash'>{_fmt_cash(stats['total_return_cash'])}</div>"
        f"<div class='twr'>TWR {_fmt_pct(stats['twr_pct'])}</div>"
        "</span>"
        "</li>"
    )


RANK_MODES = [
    ("cash", "Cash £", "total_return_cash"),
    ("pct", "Simple %", "simple_pct"),
]


def _totals_bar_html(ranked: list[tuple[Entry, dict]]) -> str:
    """Total gain/loss this period across every individual stock/closed-position
    entry (themes excluded, since their member stocks are already counted
    separately and would double-count)."""
    non_theme = [(e, s) for e, s in ranked if e.kind != "theme"]
    winners_cash = sum((s["total_return_cash"] for e, s in non_theme if s["total_return_cash"] > 0), Decimal(0))
    losers_cash = sum((s["total_return_cash"] for e, s in non_theme if s["total_return_cash"] < 0), Decimal(0))
    net_cash = winners_cash + losers_cash
    net_class = "pos" if net_cash >= 0 else "neg"
    return (
        "<p class='totals-bar'>"
        f"<span class='totals-item winners'>Total gains: <b>{_fmt_cash(winners_cash)}</b></span>"
        f"<span class='totals-item losers'>Total losses: <b>{_fmt_cash(losers_cash)}</b></span>"
        f"<span class='totals-item net {net_class}'>Net change: <b>{_fmt_cash(net_cash)}</b></span>"
        "</p>"
    )


def _period_panel_html(period_key: str, rank_key: str, sort_field: str,
                       entries: list[Entry], start: date, end: date,
                       display_start: date | None = None) -> str:
    ranked = []
    for entry in entries:
        stats = entry_period_stats(entry, start, end)
        if stats["simple_pct"] is None:
            continue
        ranked.append((entry, stats))
    ranked.sort(key=lambda pair: pair[1][sort_field], reverse=True)

    heroes = ranked[:TOP_N]
    losers = ranked[TOP_N:] if len(ranked) > TOP_N else []
    villains = list(reversed(losers[-TOP_N:]))

    def _list_html(pairs):
        if not pairs:
            return "<p class='empty-note'>No data for this period.</p>"
        return "<ul class='movers'>" + "".join(_mover_li_html(e, s) for e, s in pairs) + "</ul>"

    window_start = display_start if display_start is not None else start
    return (
        f"<div class='period-panel' data-period='{html.escape(period_key)}' data-rank='{html.escape(rank_key)}' style='display:none;'>"
        f"<p class='window-note'>{window_start.isoformat()} &rarr; {end.isoformat()}</p>"
        "<div class='columns'>"
        f"<div class='column heroes'><h2>Heroes</h2>{_list_html(heroes)}</div>"
        f"<div class='column villains'><h2>Villains</h2>{_list_html(villains)}</div>"
        "</div>"
        f"{_totals_bar_html(ranked)}"
        "</div>"
    )


def build_html(entries: list[Entry], today: date) -> str:
    period_buttons = []
    panels = []
    prior_trading_day, last_trading_day = _last_two_trading_days(entries, today)

    for label, delta in PERIODS:
        period_key = label.lower().replace(" ", "-")
        display_start = None
        if label == "Day":
            # entry_period_stats snapshots the opening MV at (start - 1 day), so
            # start must be prior_trading_day + 1 day for that snapshot to land
            # exactly on prior_trading_day's close. display_start shows the actual
            # trading day being compared from, not this internal snapshot offset.
            start, end = prior_trading_day + timedelta(days=1), last_trading_day
            display_start = prior_trading_day
        else:
            start, end = today - delta, today
        period_buttons.append(f"<button class='period-btn' data-period='{period_key}'>{html.escape(label)}</button>")
        for rank_key, _, sort_field in RANK_MODES:
            panels.append(_period_panel_html(period_key, rank_key, sort_field, entries, start, end, display_start))

    rank_buttons = [f"<button class='rank-btn' data-rank='{rank_key}'>Rank by: {html.escape(rank_label)}</button>"
                    for rank_key, rank_label, _ in RANK_MODES]

    period_selector_html = "<p class='period-selector'>" + "".join(period_buttons) + "</p>"
    rank_selector_html = "<p class='rank-selector'>" + "".join(rank_buttons) + "</p>"

    return f"""<!doctype html>
<html><head><meta charset='utf-8'>
<title>Heroes &amp; Villains</title>
<style>{CSS}</style>
</head><body>
<h1>Heroes &amp; Villains Report Generated {today.isoformat()}</h1>
<p>Biggest movers across all accounts (including dividends).</p>
{period_selector_html}
{rank_selector_html}
{''.join(panels)}
<script>{PERIOD_SELECTOR_JS.replace('__DEFAULT_PERIOD_KEY__', DEFAULT_PERIOD_KEY)}</script>
</body></html>"""


# ---------- main ----------

def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    entries = _load_entries()
    if not entries:
        print("No eligible stocks or themes found.")
        return
    doc = build_html(entries, TODAY)
    filepath = os.path.join(OUTPUT_DIR, f"{TODAY.strftime('%Y%m%d')}-Heroes-and-Villains-report.html")
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(doc)
    print(f"Wrote {filepath}  ({len(entries)} entries)")


if __name__ == "__main__":
    main()
