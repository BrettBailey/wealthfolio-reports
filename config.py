"""
User-tunable settings for total-return-report.py.
"""

import os

DB_PATH = os.path.join(os.environ["APPDATA"], "com.teymz.wealthfolio", "app.db")
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")

ACCOUNTS: list[str] | None = None   # list of account names; None = auto-discover all accounts in DB
TICKERS: list[str] | None = None    # list of tickers; None = auto-discover all currently-held tickers per account

# Benchmark used for comparison columns.  Must be a display_code present in the DB with
# sufficient quote history.  The TWR is computed price-only (no cash flows).
BENCHMARK_TICKER = "0P0000TKZO"   # Vanguard LifeStrategy 100% Equity A Acc (global equity proxy)
BENCHMARK_LABEL  = "VLS100 TWR %"  # column header shown in the report

ANN_MIN_DAYS = 365  # only show annualised figures when the window is at least this long
RECENTLY_CLOSED_DAYS = 90  # show a sold-out position for this long after its last SELL

# Themes: named groups of tickers within an account, each rendered as its own
# aggregate return block (like a mini portfolio total) underneath the account
# total. A ticker can appear in more than one theme, and the same ticker can
# appear in themes under different accounts.
THEMES: dict[str, dict[str, list[str]]] = {
    "ISA": {
        "Space": ["ASTS", "FLY", "LUNR", "SPCX", "RKLB"],
    },
}
