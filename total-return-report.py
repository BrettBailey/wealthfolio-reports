"""
Multi-stock total return report, reading Wealthfolio's SQLite DB directly.

For each ticker in TICKERS:
  - lists activities with running units/cost
  - computes total return (simple) and TWR over: current month, last month,
    current quarter, last quarter, YTD, since inception
Then aggregates a portfolio-level view across all tickers.

Conventions:
  - All amounts reported in GBP.
  - Activity prices/amounts are already in GBP in the DB (the `currency` column
    on activities is "GBP" across the board in this dataset).
  - Quote currency may be GBp (LSE pence) or USD; converted to GBP.
  - Splits are applied: a SPLIT row with amount=R means every share held
    before that date becomes R shares (cost basis per share divided by R).

Return definitions:
  - simple total return = (MV_end - MV_start) - net_invested_in_period
       where net_invested = buys - sells - dividends
  - TWR = product over market days of (MV_end - flow) / MV_start  - 1
       flow is +ve for buys, -ve for sells and dividends.
"""

from __future__ import annotations

import html
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

DB_PATH = os.path.join(os.environ["APPDATA"], "com.teymz.wealthfolio", "app.db")
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")

ACCOUNTS: list[str] | None = None   # list of account names; None = auto-discover all accounts in DB
TICKERS: list[str] | None = None    # list of tickers; None = auto-discover all currently-held tickers per account
TODAY = date.today()

# Benchmark used for comparison columns.  Must be a display_code present in the DB with
# sufficient quote history.  The TWR is computed price-only (no cash flows).
BENCHMARK_TICKER = "0P0000TKZO"   # Vanguard LifeStrategy 100% Equity A Acc (global equity proxy)
BENCHMARK_LABEL  = "VLS100 TWR %"  # column header shown in the report
ANN_MIN_DAYS = 365  # only show annualised figures when the window is at least this long


# ---------- helpers ----------

def to_decimal(value) -> Decimal:
    # Decimal() doesn't accept float directly without rounding surprises, so stringify first.
    return Decimal(str(value)) if value is not None else Decimal("0")


def parse_date(date_string: str) -> date:
    # DB stores dates as either "2024-01-15" or ISO-8601 with time "2024-01-15T00:00:00Z".
    return datetime.fromisoformat(date_string.replace("Z", "+00:00")).date() if "T" in date_string else date.fromisoformat(date_string)


# ---------- data model ----------

@dataclass
class Activity:
    activity_date: date
    kind: str        # BUY / SELL / DIVIDEND / SPLIT
    quantity: Decimal   # shares (BUY/SELL). For SPLIT this is 0; ratio lives in `amount`.
    price: Decimal      # GBP per share (BUY/SELL)
    amount: Decimal     # BUY/SELL: qty*price + fee (GBP). DIVIDEND: cash in GBP. SPLIT: ratio.
    fee: Decimal


@dataclass
class Stock:
    ticker: str
    name: str
    quote_currency: str
    activities: list[Activity]
    quotes_gbp: dict[date, Decimal] = field(default_factory=dict)  # close price per share, in GBP


# ---------- loading ----------

def _load_fx_gbp_per_usd(conn) -> dict[date, Decimal]:
    """USD/GBP asset stores close as 'GBP per 1 USD' (e.g. 0.738...)."""
    row = conn.execute("select id from assets where display_code = 'USD/GBP'").fetchone()
    if not row:
        return {}
    return {
        parse_date(r["day"]): to_decimal(r["close"])
        for r in conn.execute("select day, close from quotes where asset_id=? order by day", (row["id"],))
    }


def _load_stock(conn, ticker: str, fx_gbp_per_usd: dict[date, Decimal],
                account_id: str | None = None) -> Stock:
    asset = conn.execute(
        "select id, name, display_code, quote_ccy from assets where display_code = ?",
        (ticker,),
    ).fetchone()
    if not asset:
        raise SystemExit(f"no asset with display_code={ticker}")

    if account_id is None:
        raw_rows = list(conn.execute(
            """select activity_date, activity_type, quantity, unit_price, amount, fee, currency
               from activities where asset_id=? order by activity_date""",
            (asset["id"],),
        ))
    else:
        raw_rows = list(conn.execute(
            """select activity_date, activity_type, quantity, unit_price, amount, fee, currency
               from activities where asset_id=? and account_id=? order by activity_date""",
            (asset["id"], account_id),
        ))

    # Yahoo stores split-adjusted historical prices. We normalise activities into
    # the same "today's share count" reference frame by scaling any BUY/SELL that
    # happened BEFORE a split: units *= ratio, price /= ratio. After this, SPLIT
    # rows become no-ops (already baked into quotes AND into our activity numbers).
    split_ratios = [(parse_date(r["activity_date"]), to_decimal(r["amount"]))
                    for r in raw_rows if r["activity_type"] == "SPLIT"]

    def cumulative_split_ratio_after(activity_date: date) -> Decimal:
        # Returns the product of all split ratios that occurred AFTER activity_date.
        # We multiply this into historical quantities so everything is in post-split units.
        ratio = Decimal(1)
        for split_date, split_ratio in split_ratios:
            if split_date > activity_date:
                ratio *= split_ratio
        return ratio

    activities: list[Activity] = []
    for r in raw_rows:
        kind = r["activity_type"]
        activity_date = parse_date(r["activity_date"])
        fee = to_decimal(r["fee"])
        if kind in ("BUY", "SELL"):
            if r["currency"] != "GBP":
                raise SystemExit(f"{ticker}: non-GBP buy/sell currency {r['currency']!r} not handled")
            split_adj = cumulative_split_ratio_after(activity_date)
            quantity = to_decimal(r["quantity"]) * split_adj
            price = to_decimal(r["unit_price"]) / split_adj
            amount = quantity * price + fee  # unchanged by split adjustment
            activities.append(Activity(activity_date, kind, quantity, price, amount, fee))
        elif kind == "DIVIDEND":
            if r["currency"] != "GBP":
                raise SystemExit(f"{ticker}: non-GBP dividend currency {r['currency']!r} not handled")
            activities.append(Activity(activity_date, kind, Decimal(0), Decimal(0), to_decimal(r["amount"]), fee))
        elif kind == "SPLIT":
            pass  # no-op: already baked into BUY/SELL adjustment above and into Yahoo quotes

    # Load historical quotes, converting everything to GBP.
    quotes_gbp: dict[date, Decimal] = {}
    for r in conn.execute(
        "select day, close, currency from quotes where asset_id=? order by day",
        (asset["id"],),
    ):
        quote_date = parse_date(r["day"])
        close = to_decimal(r["close"])
        currency = r["currency"]
        if currency == "GBp":
            quotes_gbp[quote_date] = close / Decimal("100")  # pence to pounds
        elif currency == "GBP":
            quotes_gbp[quote_date] = close
        elif currency == "USD":
            fx_rate = fx_gbp_per_usd.get(quote_date) or _walk_back(fx_gbp_per_usd, quote_date, 10)
            if fx_rate is None:
                continue  # skip quote days with no FX
            quotes_gbp[quote_date] = close * fx_rate
        else:
            raise SystemExit(f"{ticker}: unknown quote currency {currency!r}")

    return Stock(ticker=ticker, name=asset["name"], quote_currency=asset["quote_ccy"],
                 activities=activities, quotes_gbp=quotes_gbp)


def _walk_back(series: dict[date, Decimal], target_date: date, max_days: int) -> Decimal | None:
    """Find the most recent value in `series` on or before `target_date`, up to max_days back."""
    current = target_date
    for _ in range(max_days + 1):
        if current in series:
            return series[current]
        current -= timedelta(days=1)
    return None


def load_benchmark_quotes(conn) -> dict[date, Decimal]:
    """Load GBp-quoted benchmark prices and convert to GBP."""
    row = conn.execute(
        "select id, quote_ccy from assets where display_code = ?", (BENCHMARK_TICKER,)
    ).fetchone()
    if not row:
        return {}
    quotes: dict[date, Decimal] = {}
    for r in conn.execute("select day, close, currency from quotes where asset_id=? order by day", (row["id"],)):
        quote_date = parse_date(r["day"])
        close = to_decimal(r["close"])
        currency = r["currency"]
        if currency == "GBp":
            quotes[quote_date] = close / Decimal("100")
        elif currency == "GBP":
            quotes[quote_date] = close
    return quotes


def benchmark_twr(quotes: dict[date, Decimal], start: date, end: date) -> Decimal | None:
    """Price-only TWR for the benchmark (no cash flows)."""
    days = sorted(d for d in quotes if start <= d <= end)
    if not days:
        return None
    prior = sorted(d for d in quotes if d < start)
    if not prior:
        if len(days) < 2:
            return None
        previous_day = days[0]
        days = days[1:]
    else:
        previous_day = prior[-1]
    chain = Decimal(1)
    for day in days:
        price_previous = quotes[previous_day]
        price_today = quotes[day]
        if price_previous > 0:
            chain *= price_today / price_previous
        previous_day = day
    return chain - Decimal(1)


def annualise(cumulative_return: Decimal | None, start: date, end: date) -> Decimal | None:
    """Convert a cumulative return to an annualised figure. Returns None if window < ANN_MIN_DAYS."""
    if cumulative_return is None:
        return None
    days = (end - start).days
    if days < ANN_MIN_DAYS:
        return None
    years = Decimal(str(days)) / Decimal("365.25")
    base = Decimal(1) + cumulative_return
    if base <= 0:
        return None
    return base ** (Decimal(1) / years) - Decimal(1)


def _resolve_account_id(conn, account_name: str | None) -> str | None:
    if account_name is None:
        return None
    row = conn.execute("select id from accounts where name = ?", (account_name,)).fetchone()
    if not row:
        raise SystemExit(f"no account named {account_name!r}")
    return row["id"]


def _tickers_for_account(conn, account_id: str | None) -> list[str]:
    if account_id is None:
        rows = conn.execute(
            "select distinct a.display_code from activities ac "
            "join assets a on a.id=ac.asset_id "
            "where a.display_code is not null "
            "order by a.display_code"
        ).fetchall()
    else:
        rows = conn.execute(
            "select distinct a.display_code from activities ac "
            "join assets a on a.id=ac.asset_id "
            "where ac.account_id=? and a.display_code is not null "
            "order by a.display_code",
            (account_id,),
        ).fetchall()
    return [r["display_code"] for r in rows]


def load_all(tickers: list[str] | None, account: str | None) -> tuple[list[Stock], str | None]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    fx_rates = _load_fx_gbp_per_usd(conn)
    account_id = _resolve_account_id(conn, account)
    if tickers is None:
        tickers = _tickers_for_account(conn, account_id)
    return [_load_stock(conn, ticker, fx_rates, account_id) for ticker in tickers], account_id


# ---------- point-in-time state per stock ----------

def state_on(stock: Stock, as_of: date):
    """Return (units, cost_basis, close_gbp, market_value_gbp) at end-of-day as_of."""
    units = Decimal(0)
    cost_basis = Decimal(0)
    for activity in stock.activities:
        if activity.activity_date > as_of:
            break
        if activity.kind == "BUY":
            units += activity.quantity
            cost_basis += activity.amount
        elif activity.kind == "SELL":
            if units > 0:
                # reduce cost basis proportionally to the fraction sold
                cost_basis -= cost_basis * (activity.quantity / units)
            units -= activity.quantity
    close = _walk_back(stock.quotes_gbp, as_of, 30)
    market_value = units * close if close is not None else Decimal("NaN")
    return units, cost_basis, close, market_value


def period_flows(stock: Stock, start: date, end: date):
    """Cash flows in (start, end]. start is the opening-snapshot day; exclude it."""
    buys = sells = dividends = Decimal(0)
    activity_rows: list[Activity] = []
    for activity in stock.activities:
        if start < activity.activity_date <= end:
            if activity.kind in ("BUY", "SELL", "DIVIDEND"):
                activity_rows.append(activity)
            if activity.kind == "BUY":
                buys += activity.amount
            elif activity.kind == "SELL":
                sells += activity.amount
            elif activity.kind == "DIVIDEND":
                dividends += activity.amount
    net_invested = buys - sells - dividends
    return activity_rows, buys, sells, dividends, net_invested


def twr(stock: Stock, start: date, end: date) -> Decimal | None:
    """Daily-chained TWR over (start, end]."""
    days_in_window = sorted(d for d in stock.quotes_gbp if start <= d <= end)
    if not days_in_window:
        return None
    prior_quote_days = sorted(d for d in stock.quotes_gbp if d < start)
    if prior_quote_days:
        previous_day = prior_quote_days[-1]
    else:
        # No prior quote: seed baseline from first in-window quote day.
        # Activities on/before that seed day are folded into opening units; the
        # "blind" stretch before first quote contributes 0 return.
        previous_day = days_in_window[0]
        days_in_window = days_in_window[1:]

    activities_by_day: dict[date, list[Activity]] = {}
    for activity in stock.activities:
        if start <= activity.activity_date <= end:
            activities_by_day.setdefault(activity.activity_date, []).append(activity)

    units_previous, _, _, _ = state_on(stock, previous_day)
    chain = Decimal(1)
    contributed = False

    for day in days_in_window:
        close_previous = stock.quotes_gbp[previous_day]
        close_today = stock.quotes_gbp[day]
        market_value_start = units_previous * close_previous

        # flow: positive = money in (buys), negative = money out (sells, dividends)
        flow = Decimal(0)
        units_today = units_previous
        for activity in activities_by_day.get(day, []):
            if activity.kind == "BUY":
                flow += activity.amount
                units_today += activity.quantity
            elif activity.kind == "SELL":
                flow -= activity.amount
                units_today -= activity.quantity
            elif activity.kind == "DIVIDEND":
                flow -= activity.amount

        market_value_end = units_today * close_today

        if market_value_start > 0:
            chain *= (market_value_end - flow) / market_value_start
            contributed = True
        elif market_value_end > 0 and flow > 0:
            contributed = True

        units_previous = units_today
        previous_day = day

    return chain - Decimal(1) if contributed else None


# ---------- periods ----------


def quarter_bounds(day: date):
    quarter_index = (day.month - 1) // 3  # 0=Q1, 1=Q2, 2=Q3, 3=Q4
    start = date(day.year, quarter_index * 3 + 1, 1)
    end_month = start.month + 2
    next_quarter_start = date(start.year + (1 if end_month == 12 else 0), 1 if end_month == 12 else end_month + 1, 1)
    return start, next_quarter_start - timedelta(days=1)


def prev_quarter(day: date):
    quarter_start, _ = quarter_bounds(day)
    return quarter_bounds(quarter_start - timedelta(days=1))


def _quarter_label(start: date) -> str:
    quarter_number = (start.month - 1) // 3 + 1
    return f"{start.year}-Q{quarter_number}"


# ---------- portfolio roll-up ----------

def portfolio_twr(stocks: list[Stock], start: date, end: date) -> Decimal | None:
    """
    Daily-chained TWR across the combined portfolio.
    Each day, MV = sum of stock MVs (using last-known GBP quote); flow = sum of stock flows.
    """
    all_quote_days: set[date] = set()
    for stock in stocks:
        all_quote_days.update(d for d in stock.quotes_gbp if start <= d <= end)
    days_in_window = sorted(all_quote_days)
    if not days_in_window:
        return None

    prior_quote_days: set[date] = set()
    for stock in stocks:
        prior_quote_days.update(d for d in stock.quotes_gbp if d < start)
    if prior_quote_days:
        previous_day = max(prior_quote_days)
    else:
        previous_day = days_in_window[0]
        days_in_window = days_in_window[1:]

    activities_by_day: dict[date, list[tuple[Stock, Activity]]] = {}
    for stock in stocks:
        for activity in stock.activities:
            if start <= activity.activity_date <= end:
                activities_by_day.setdefault(activity.activity_date, []).append((stock, activity))

    units_by_ticker = {stock.ticker: state_on(stock, previous_day)[0] for stock in stocks}

    chain = Decimal(1)
    contributed = False

    def portfolio_market_value_on(day: date) -> Decimal:
        total = Decimal(0)
        for stock in stocks:
            units = units_by_ticker[stock.ticker]
            if units == 0:
                continue
            price = _walk_back(stock.quotes_gbp, day, 30)
            if price is None:
                continue
            total += units * price
        return total

    for day in days_in_window:
        market_value_start = portfolio_market_value_on(previous_day)

        flow = Decimal(0)
        for stock, activity in activities_by_day.get(day, []):
            if activity.kind == "BUY":
                flow += activity.amount
                units_by_ticker[stock.ticker] += activity.quantity
            elif activity.kind == "SELL":
                flow -= activity.amount
                units_by_ticker[stock.ticker] -= activity.quantity
            elif activity.kind == "DIVIDEND":
                flow -= activity.amount

        market_value_end = portfolio_market_value_on(day)

        if market_value_start > 0:
            chain *= (market_value_end - flow) / market_value_start
            contributed = True
        elif market_value_end > 0:
            contributed = True

        previous_day = day

    return chain - Decimal(1) if contributed else None


# ---------- HTML rendering ----------

PERIOD_COLS = ["Period", "Window", "Start Valuation", "End Valuation", "Buy", "Sell",
               "Growth", "Dividend", "Total Return", "Simple %", "TWR %", "Yield %"]


def _fmt_money_html(value) -> str:
    if value is None or (isinstance(value, Decimal) and value.is_nan()):
        return "&nbsp;"
    css_class = "neg" if value < 0 else ""
    return f'<span class="{css_class}">{value:,.2f}</span>' if css_class else f"{value:,.2f}"


def _fmt_pct_html(value) -> str:
    if value is None:
        return "&nbsp;"
    css_class = "neg" if value < 0 else ""
    return f'<span class="{css_class}">{value:.2f}%</span>' if css_class else f"{value:.2f}%"


def _period_row_data(stocks_or_stock, period):
    """Compute period stats. Accepts a single Stock or list[Stock]. Returns dict."""
    label, period_start, period_end = period
    if isinstance(stocks_or_stock, Stock):
        stocks = [stocks_or_stock]
        is_single = True
    else:
        stocks = stocks_or_stock
        is_single = False

    market_value_start = market_value_end = Decimal(0)
    buys = sells_total = dividends_total = Decimal(0)
    any_units = False
    market_value_start_known = market_value_end_known = False

    for stock in stocks:
        units_start, _, _, mv_start = state_on(stock, period_start - timedelta(days=1))
        units_end, _, _, mv_end = state_on(stock, period_end)
        _, period_buys, period_sells, period_dividends, _ = period_flows(stock, period_start - timedelta(days=1), period_end)
        if units_start > 0 or units_end > 0 or period_buys > 0 or period_sells > 0:
            any_units = True
        if not mv_start.is_nan():
            market_value_start += mv_start
            if units_start > 0:
                market_value_start_known = True
        if not mv_end.is_nan():
            market_value_end += mv_end
            if units_end > 0:
                market_value_end_known = True
        buys += period_buys
        sells_total += period_sells
        dividends_total += period_dividends

    no_price = any_units and not market_value_start_known and not market_value_end_known

    growth = (market_value_end - market_value_start) - (buys - sells_total)
    total_return = growth + dividends_total
    denominator = market_value_start + buys
    simple_pct = (total_return / denominator * 100) if denominator > 0 else None
    twr_result = twr(stocks[0], period_start, period_end) if is_single else portfolio_twr(stocks, period_start, period_end)
    twr_pct = twr_result * 100 if twr_result is not None else None

    # yield %: dividends in window / (starting MV + buys). Only shown for year-scale rows.
    yield_pct = (dividends_total / denominator * 100) if (denominator > 0 and dividends_total > 0) else None

    return {
        "label": label, "start": period_start, "end": period_end,
        "market_value_start": market_value_start, "market_value_end": market_value_end,
        "buys": buys, "sells": sells_total,
        "growth": growth, "dividends": dividends_total, "total_return": total_return,
        "simple_pct": simple_pct, "twr_pct": twr_pct,
        "yield_pct": yield_pct,
        "partial": False, "no_price": no_price, "show_yield": False,
    }


def _prior_year_periods(inception: date, today: date) -> list[tuple[str, date, date]]:
    """Prior full calendar years back to inception year, newest first.
    If inception falls inside a year, clamp the window to (inception, Dec 31)."""
    result = []
    for year in range(today.year - 1, max(inception.year, today.year - 5) - 1, -1):
        start = date(year, 1, 1)
        end = date(year, 12, 31)
        if inception > start:
            start = inception
        result.append((str(year), start, end))
    return result


def _period_table_html(title: str | None, rows: list[dict], show_div_col: bool, show_yield_col: bool) -> str:
    # Build column list, omitting dividend and/or yield columns when not needed.
    headers = []
    for col in PERIOD_COLS:
        if col == "Dividend" and not show_div_col:
            continue
        if col == "Yield %" and not show_yield_col:
            continue
        headers.append(col)

    header_cells_html = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    body = []
    blank = "<td class='num'>&nbsp;</td>"
    for row in rows:
        start_class = " class='partial-start'" if row.get("partial") else ""
        window_cell = f"<td><span{start_class}>{row['start']}</span> &rarr; {row['end']}</td>"
        label_cell = f"<td>{html.escape(row['label'])}</td>"

        if row.get("no_price"):
            cells = [
                label_cell,
                window_cell,
                "<td class='num no-price' colspan='2'>no price data</td>",
                f"<td class='num'>{_fmt_money_html(row['buys'])}</td>",
                f"<td class='num'>{_fmt_money_html(row['sells'])}</td>",
                blank,  # growth
            ]
            if show_div_col:
                cells.append(f"<td class='num'>{_fmt_money_html(row['dividends'])}</td>")
            cells += [blank, blank, blank]  # total return, simple %, TWR %
            if show_yield_col:
                cells.append(f"<td class='num'>{_fmt_pct_html(row['yield_pct']) if row.get('show_yield') else '&nbsp;'}</td>")
        else:
            cells = [
                label_cell,
                window_cell,
                f"<td class='num'>{_fmt_money_html(row['market_value_start'])}</td>",
                f"<td class='num'>{_fmt_money_html(row['market_value_end'])}</td>",
                f"<td class='num'>{_fmt_money_html(row['buys'])}</td>",
                f"<td class='num'>{_fmt_money_html(row['sells'])}</td>",
                f"<td class='num'>{_fmt_money_html(row['growth'])}</td>",
            ]
            if show_div_col:
                cells.append(f"<td class='num'>{_fmt_money_html(row['dividends'])}</td>")
            cells += [
                f"<td class='num'>{_fmt_money_html(row['total_return'])}</td>",
                f"<td class='num'>{_fmt_pct_html(row['simple_pct'])}</td>",
                f"<td class='num'>{_fmt_pct_html(row['twr_pct'])}</td>",
            ]
            if show_yield_col:
                cells.append(f"<td class='num'>{_fmt_pct_html(row['yield_pct']) if row.get('show_yield') else '&nbsp;'}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    heading = f"<h3>{html.escape(title)}</h3>" if title else ""
    return (
        f"{heading}"
        f"<table class='periods'><thead><tr>{header_cells_html}</tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table>"
    )


def _activities_table_html(stock: Stock) -> str:
    headers = ["date", "kind", "qty", "price", "amount", "fee", "units", "cost", "mv@date"]
    header_cells_html = "".join(f"<th>{h}</th>" for h in headers)
    units = Decimal(0)
    cost_basis = Decimal(0)
    body = []
    for activity in stock.activities:
        if activity.kind == "BUY":
            units += activity.quantity
            cost_basis += activity.amount
        elif activity.kind == "SELL":
            if units > 0:
                cost_basis -= cost_basis * (activity.quantity / units)
            units -= activity.quantity
        price = _walk_back(stock.quotes_gbp, activity.activity_date, 30)
        market_value = units * price if price is not None else None
        body.append(
            "<tr>"
            f"<td>{activity.activity_date}</td><td>{activity.kind}</td>"
            f"<td class='num'>{activity.quantity:,.2f}</td>"
            f"<td class='num'>{activity.price:.4f}</td>"
            f"<td class='num'>{_fmt_money_html(activity.amount)}</td>"
            f"<td class='num'>{activity.fee:.2f}</td>"
            f"<td class='num'>{units:,.2f}</td>"
            f"<td class='num'>{_fmt_money_html(cost_basis)}</td>"
            f"<td class='num'>{_fmt_money_html(market_value)}</td>"
            "</tr>"
        )
    return (
        f"<table class='activities'><thead><tr>{header_cells_html}</tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table>"
    )


def _heading_annualised_suffix(simple_pct: Decimal | None, twr_pct: Decimal | None,
                                inception: date, today: date) -> str:
    """Build the '— X% p.a. (simple) / Y% p.a. (TWR)' suffix for portfolio-level headings."""
    annualised_simple = annualise(
        Decimal(str(simple_pct)) / 100 if simple_pct is not None else None,
        inception, today,
    )
    annualised_twr = annualise(
        Decimal(str(twr_pct)) / 100 if twr_pct is not None else None,
        inception, today,
    )
    if annualised_simple is None and annualised_twr is None:
        return ""
    parts = []
    if annualised_simple is not None:
        parts.append(f"{annualised_simple * 100:.1f}% p.a. (simple)")
    if annualised_twr is not None:
        parts.append(f"{annualised_twr * 100:.1f}% p.a. (TWR)")
    return " &mdash; " + " / ".join(parts)


def _stock_summary_row_html(simple_pct: Decimal | None, twr_pct: Decimal | None,
                             inception: date, today: date,
                             weight_pct: Decimal | None,
                             is_dividend_stock: bool,
                             annualised_yield: Decimal | None) -> str:
    """Build the sub-heading summary row shown beneath each individual stock heading.

    Shows: portfolio weight | annualised simple return | annualised TWR | annualised yield (dividend stocks only).
    Returns an empty string when there are no figures to display.
    """
    annualised_simple = annualise(
        Decimal(str(simple_pct)) / 100 if simple_pct is not None else None,
        inception, today,
    )
    annualised_twr = annualise(
        Decimal(str(twr_pct)) / 100 if twr_pct is not None else None,
        inception, today,
    )

    parts = []

    if weight_pct is not None:
        parts.append(f"Weight: <b>{weight_pct:.1f}%</b>")

    if annualised_simple is not None:
        parts.append(f"<b>{annualised_simple * 100:.1f}%</b> p.a. (simple)")

    if annualised_twr is not None:
        parts.append(f"<b>{annualised_twr * 100:.1f}%</b> p.a. (TWR)")

    if is_dividend_stock and annualised_yield is not None:
        parts.append(f"Yield: <b>{annualised_yield * 100:.1f}%</b> p.a.")

    if not parts:
        return ""
    return "<p class='stock-summary'>" + " &nbsp;|&nbsp; ".join(parts) + "</p>"


def build_account_html(account: str, held_stocks: list[Stock],
                       all_stocks: list[Stock],
                       common_periods, today: date) -> str:
    # portfolio block uses ALL stocks ever held (so sold-out positions contribute)
    earliest_activity = min(
        min((a.activity_date for a in stock.activities if a.kind in ("BUY", "SELL")), default=today)
        for stock in all_stocks
    )
    prior_years_portfolio = _prior_year_periods(earliest_activity, today)
    portfolio_periods = common_periods + prior_years_portfolio + [("Since inception", earliest_activity, today)]
    portfolio_rows = [_period_row_data(all_stocks, period) for period in portfolio_periods]

    # mark partial years (window doesn't start Jan 1)
    num_common_periods = len(common_periods)
    num_prior_years = len(prior_years_portfolio)
    for row, (_, period_start, _) in zip(portfolio_rows[num_common_periods:num_common_periods + num_prior_years], prior_years_portfolio):
        if period_start != date(period_start.year, 1, 1):
            row["partial"] = True

    # show yield column on YTD and each prior year; NOT on "Since inception" (not meaningful)
    ytd_index = num_common_periods - 1  # YTD is last of common_periods
    for row in portfolio_rows[ytd_index:ytd_index + 1]:
        row["show_yield"] = True
    for row in portfolio_rows[num_common_periods:num_common_periods + num_prior_years]:  # prior years only, not since-inception
        row["show_yield"] = True

    portfolio_has_dividends = any(row["dividends"] != 0 for row in portfolio_rows)
    portfolio_has_yield = any(row["yield_pct"] is not None for row in portfolio_rows)

    portfolio_inception_row = portfolio_rows[-1]

    # annualised yield for the portfolio (shown if any stock pays dividends)
    portfolio_inception_yield_pct = portfolio_inception_row["yield_pct"]
    portfolio_annualised_yield = annualise(
        Decimal(str(portfolio_inception_yield_pct)) / 100 if portfolio_inception_yield_pct is not None else None,
        earliest_activity, today,
    )
    portfolio_summary_row_html = _stock_summary_row_html(
        portfolio_inception_row["simple_pct"],
        portfolio_inception_row["twr_pct"],
        earliest_activity, today,
        weight_pct=None,  # weight not applicable at portfolio level
        is_dividend_stock=portfolio_has_dividends,
        annualised_yield=portfolio_annualised_yield,
    )

    portfolio_html = (
        f"<h2>{html.escape(account)} Account Total</h2>"
        + portfolio_summary_row_html
        + _period_table_html(None, portfolio_rows, portfolio_has_dividends, portfolio_has_yield)
    )

    # per-stock blocks — currently held only, sorted by current market value descending
    def _current_market_value(stock: Stock) -> Decimal:
        market_value = state_on(stock, today)[3]
        return Decimal(0) if market_value.is_nan() else market_value

    portfolio_total_market_value = sum(_current_market_value(stock) for stock in held_stocks)
    held_stocks_sorted = sorted(held_stocks, key=_current_market_value, reverse=True)

    stock_blocks = []
    for stock_index, stock in enumerate(held_stocks_sorted):
        inception = min((a.activity_date for a in stock.activities if a.kind in ("BUY", "SELL")), default=today)
        prior_years = _prior_year_periods(inception, today)
        stock_periods = common_periods + prior_years + [("Since inception", inception, today)]
        rows = [_period_row_data(stock, period) for period in stock_periods]
        num_prior_years_stock = len(prior_years)
        for row, (_, period_start, _) in zip(rows[num_common_periods:num_common_periods + num_prior_years_stock], prior_years):
            if period_start != date(period_start.year, 1, 1):
                row["partial"] = True

        # show yield on YTD and prior years; NOT on "Since inception" (misleading against initial cost)
        for row in rows[ytd_index:ytd_index + 1]:
            row["show_yield"] = True
        for row in rows[num_common_periods:num_common_periods + num_prior_years_stock]:  # prior years only, not since-inception
            row["show_yield"] = True

        stock_has_dividends = any(row["dividends"] != 0 for row in rows)
        stock_has_yield = any(row["yield_pct"] is not None for row in rows)

        activities_div_id = f"acts-{stock_index}"

        inception_row = rows[-1]

        # compute current weight within the portfolio
        stock_market_value = _current_market_value(stock)
        weight_pct = (stock_market_value / portfolio_total_market_value * 100
                      if portfolio_total_market_value > 0 else None)

        # annualised yield from the since-inception window (only meaningful for dividend stocks)
        inception_yield_pct = inception_row["yield_pct"]
        annualised_yield = annualise(
            Decimal(str(inception_yield_pct)) / 100 if inception_yield_pct is not None else None,
            inception, today,
        )

        summary_row_html = _stock_summary_row_html(
            inception_row["simple_pct"],
            inception_row["twr_pct"],
            inception, today,
            weight_pct,
            is_dividend_stock=stock_has_dividends,
            annualised_yield=annualised_yield,
        )

        stock_blocks.append(
            f"<section class='stock'>"
            f"<h2>{html.escape(stock.name)}&nbsp;[{html.escape(stock.ticker)}]</h2>"
            f"{summary_row_html}"
            f"{_period_table_html(None, rows, stock_has_dividends, stock_has_yield)}"
            f"<p class='activities-toggle'><a href='#' class='toggle' data-target='{activities_div_id}'>show activities &gt;</a></p>"
            f"<div id='{activities_div_id}' class='activities-wrap' style='display:none;'>{_activities_table_html(stock)}</div>"
            f"</section>"
        )

    css = """
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 24px; color: #222; }
    h1 { margin-bottom: 4px; }
    h2 { margin-top: 32px; margin-bottom: 0; }
    h3 { margin-top: 20px; font-size: 1em; }
    table { border-collapse: collapse; margin: 8px 0 20px; font-size: 0.88em; }
    th, td { padding: 4px 10px; border-bottom: 1px solid #eee; text-align: left; }
    th { background: #f4f4f4; border-bottom: 2px solid #ccc; }
    tbody tr:nth-child(even) { background: #f2f5fb; }
    td.num { text-align: right; font-variant-numeric: tabular-nums; }
    .neg { color: #c0392b; }
    section.stock { margin-top: 24px; }
    a.toggle { font-size: 0.9em; color: #559; text-decoration: none; }
    a.toggle:hover { text-decoration: underline; }
    p.activities-toggle { margin: 4px 0 12px; }
    .portfolio { background: #e8f0ff; padding: 12px 16px; border: 1px solid #88a; border-radius: 4px; }
    .portfolio h2 { margin-top: 0; }
    .partial-start { font-weight: bold; }
    .no-price { color: #888; font-style: italic; text-align: center !important; }
    p.stock-summary { margin: 2px 0 0; font-size: 0.88em; color: #555; padding-bottom: 6px; border-bottom: 2px solid #444; }
    """
    js = """
    document.querySelectorAll('a.toggle').forEach(function(link) {
      link.addEventListener('click', function(e) {
        e.preventDefault();
        var target = document.getElementById(this.dataset.target);
        var visible = target.style.display !== 'none';
        target.style.display = visible ? 'none' : '';
        this.textContent = visible ? 'show activities >' : 'hide activities <';
      });
    });
    """
    return f"""<!doctype html>
<html><head><meta charset='utf-8'>
<title>{html.escape(account)}</title>
<style>{css}</style>
</head><body>
<h1>{html.escape(account)} Return Report Generated {today.isoformat()}</h1>
<section class='portfolio'>{portfolio_html}</section>
{''.join(stock_blocks)}
<script>{js}</script>
</body></html>"""


# ---------- main ----------

def _list_account_names() -> list[str]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("select name from accounts where is_active=1 order by name").fetchall()
    return [r["name"] for r in rows]


def main():
    accounts = ACCOUNTS if ACCOUNTS is not None else _list_account_names()

    current_quarter_start, current_quarter_end = quarter_bounds(TODAY)
    current_quarter_end = min(current_quarter_end, TODAY)
    prev_q1_start, prev_q1_end = prev_quarter(TODAY)
    prev_q2_start, prev_q2_end = prev_quarter(prev_q1_start)
    ytd_start, ytd_end = date(TODAY.year, 1, 1), TODAY

    common_periods = [
        ("Current month",            TODAY.replace(day=1),    TODAY),
        ("Current quarter",          current_quarter_start,   current_quarter_end),
        (_quarter_label(prev_q1_start), prev_q1_start,        prev_q1_end),
        (_quarter_label(prev_q2_start), prev_q2_start,        prev_q2_end),
        ("Year to date",             ytd_start,               ytd_end),
    ]

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    for account in accounts:
        all_stocks, _ = load_all(TICKERS, account)
        all_stocks = [stock for stock in all_stocks if stock.activities]
        held_stocks = [stock for stock in all_stocks if state_on(stock, TODAY)[0] > 0]
        if not held_stocks:
            print(f"{account}: no currently-held tickers, skipping")
            continue

        html_doc = build_account_html(account, held_stocks, all_stocks, common_periods, TODAY)
        filename = f"{TODAY.strftime('%Y%m%d')}-{account}-report.html"
        filepath = os.path.join(OUTPUT_DIR, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(html_doc)
        sold_tickers = [stock.ticker for stock in all_stocks if stock not in held_stocks]
        extra = f" + sold: {', '.join(sold_tickers)}" if sold_tickers else ""
        print(f"Wrote {filepath}  ({len(held_stocks)} held: {', '.join(stock.ticker for stock in held_stocks)}{extra})")


if __name__ == "__main__":
    main()
