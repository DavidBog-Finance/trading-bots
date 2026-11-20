"""Portfolio 1k settings. Every value can be overridden with an environment variable of the
same name prefixed with P1K_ (e.g. P1K_MAX_TRADES_DAY=1). Defaults follow the plan doc."""
import os


def _f(name, default):
    v = os.getenv("P1K_" + name, "")
    return float(v) if v.strip() else default


def _b(name, default):
    v = os.getenv("P1K_" + name, "")
    return v.strip().lower() in ("1", "true", "yes", "on") if v.strip() else default


def _l(name, default):
    v = os.getenv("P1K_" + name, "") or default
    return [s.strip().upper() for s in v.split(",") if s.strip()]


WATCHLIST = _l("WATCHLIST", "SPY,QQQ,IWM,AAPL,MSFT,NVDA,AMZN,META,GOOGL,TSLA,AMD,AVGO")
ETFS = {"SPY", "QQQ", "IWM"}             # no earnings; tight $0.03 spread rule

# Contract filters ("What to trade")
MIN_DTE, MAX_DTE = int(_f("MIN_DTE", 2)), int(_f("MAX_DTE", 7))
MIN_DELTA, MAX_DELTA = _f("MIN_DELTA", 0.40), _f("MAX_DELTA", 0.55)
MAX_SPREAD_PCT = _f("MAX_SPREAD_PCT", 0.05)
MAX_SPREAD_ETF = _f("MAX_SPREAD_ETF", 0.03)
MIN_OI = _f("MIN_OI", 1000)
MIN_VOLUME = _f("MIN_VOLUME", 500)

# Sizing
STANDARD_PCT, FULL_PCT = _f("STANDARD_PCT", 0.50), _f("FULL_PCT", 1.00)
STANDARD_STOP, FULL_STOP = _f("STANDARD_STOP", 0.30), _f("FULL_STOP", 0.25)
FULL_PORT_ENABLED = _b("FULL_PORT", True)
FULL_PER_WEEK = int(_f("FULL_PER_WEEK", 1))

# Exits
TP1, TP2, TRAIL = _f("TP1", 0.40), _f("TP2", 0.80), _f("TRAIL", 0.20)
HOLD_OVERNIGHT_GAIN, HOLD_OVERNIGHT_DTE = _f("HOLD_GAIN", 0.20), int(_f("HOLD_DTE", 3))

# Risk limits
KILL_PCT = _f("KILL_PCT", 0.50)          # kill at 50% of net deposits
DAILY_LOSS, WEEKLY_LOSS = _f("DAILY_LOSS", 0.20), _f("WEEKLY_LOSS", 0.30)
MAX_TRADES_DAY, MAX_TRADES_WEEK = int(_f("MAX_TRADES_DAY", 2)), int(_f("MAX_TRADES_WEEK", 8))
STREAK_LOSSES, STREAK_PAUSE_DAYS, STREAK_STANDARD_TRADES = 3, 2, 3
QUOTE_MAX_AGE_SEC = _f("QUOTE_MAX_AGE", 30)

# Strong news = |score| >= 0.6 and materiality >= 0.7 (the plan's "8+ out of 10")
NEWS_MIN_SCORE, NEWS_MIN_MATERIALITY, NEWS_MAX_AGE_MIN = 0.6, 0.7, 90

# Times, New York (the plan's Pacific times + 3 h). Market 09:30-16:00.
PREMARKET = "08:30"
ORB_START, ORB_END = "09:45", "11:00"
PULLBACK_START = "10:00"
NEWS_START = "09:45"
ENTRY_END = "14:30"
EXPIRY_CLOSE = "15:30"                   # close anything expiring next trading day
TIME_STOP = "15:45"
SUMMARY = "16:15"
LOOP_SEC = _f("LOOP_SEC", 15)

# Fed decision days (14:00 ET): no entries 13:30-14:30. CPI/jobs come out at 08:30, before entries start.
# Add next year's dates with P1K_FOMC_DAYS=2027-01-27,... when the Fed publishes them.
FOMC_DAYS = set(_l("FOMC_DAYS", "2026-12-09"))

USE_CLAUDE = _b("USE_CLAUDE", True)
DB_PATH = os.getenv("P1K_DB", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "portfolio1k.db"))
