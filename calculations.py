"""
Shared data loading and return calculations for the Wealthfolio reports
(total-return-report.py and heroes-and-villains-report.py), reading
Wealthfolio's SQLite DB directly.

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

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from config import DB_PATH


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


def load_benchmark_quotes(conn, benchmark_ticker: str) -> dict[date, Decimal]:
    """Load GBp-quoted benchmark prices and convert to GBP."""
    row = conn.execute(
        "select id, quote_ccy from assets where display_code = ?", (benchmark_ticker,)
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


def annualise(cumulative_return: Decimal | None, start: date, end: date, ann_min_days: int) -> Decimal | None:
    """Convert a cumulative return to an annualised figure. Returns None if window < ann_min_days."""
    if cumulative_return is None:
        return None
    days = (end - start).days
    if days < ann_min_days:
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


def list_account_names() -> list[str]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("select name from accounts where is_active=1 order by name").fetchall()
    return [r["name"] for r in rows]


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


def last_sell_date(stock: Stock) -> date | None:
    sell_dates = [a.activity_date for a in stock.activities if a.kind == "SELL"]
    return max(sell_dates) if sell_dates else None


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


def quarter_label(start: date) -> str:
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

    # Keyed by object identity, not ticker: the same ticker can appear as a
    # separate Stock instance in multiple accounts when rolling up the whole portfolio.
    units_by_stock = {id(stock): state_on(stock, previous_day)[0] for stock in stocks}

    chain = Decimal(1)
    contributed = False

    def portfolio_market_value_on(day: date) -> Decimal:
        total = Decimal(0)
        for stock in stocks:
            units = units_by_stock[id(stock)]
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
                units_by_stock[id(stock)] += activity.quantity
            elif activity.kind == "SELL":
                flow -= activity.amount
                units_by_stock[id(stock)] -= activity.quantity
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
