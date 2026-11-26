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
FULL_PORT_ENABLED = _b("FULL_PORT", False)   # retired 2026-11-20 by the owner; A+ trades use standard size
FULL_PER_WEEK = int(_f("FULL_PER_WEEK", 1))

# Exits
TP1, TP2, TRAIL = _f("TP1", 0.40), _f("TP2", 0.80), _f("TRAIL", 0.20)
# Paper fills are kinder than real ones. The scorecard and go-live check subtract this many dollars per
# contract per round trip (about 3 cents worse on each fill, x100 shares, plus small exchange fees).
REAL_COST_PER_CONTRACT = _f("REAL_COST_PER_CONTRACT", 6.0)
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
EXPIRY_DAY_CLOSE = "15:00"               # and anything expiring TODAY by 3 pm (Alpaca force-sells from ~3:30)
TIME_STOP = "15:45"
SUMMARY = "16:15"
LOOP_SEC = _f("LOOP_SEC", 15)

# Fed decision days (14:00 ET): no entries 13:30-14:30. CPI/jobs come out at 08:30, before entries start.
# Override with P1K_FOMC_DAYS=... if the Fed moves a date.
# Decision days (2nd day of each meeting) from the Fed's calendar as of Nov 2026; tentative until confirmed.
FOMC_DAYS = set(_l("FOMC_DAYS", "2026-12-09,2027-01-27,2027-03-17,2027-04-28,2027-06-09,2027-07-28,"
                                "2027-09-15,2027-10-27,2027-11-17"))

# CPI and jobs-report days (08:30 ET releases), from the BLS schedule as of Nov 2026. Logged as a shadow
# test only ("macro_day"): the bot still trades them. Override with P1K_MACRO_DAYS=... if BLS moves one.
MACRO_DAYS = set(_l("MACRO_DAYS",
                    "2026-12-10,2027-01-13,2027-02-11,2027-03-10,2027-04-13,2027-05-12,2027-06-10,2027-07-14,"
                    "2027-08-11,2027-09-14,2027-10-14,2027-11-10,2027-12-10,"           # CPI
                    "2026-12-04,2027-01-08,2027-02-05,2027-03-05,2027-04-02,2027-05-07,2027-06-04,2027-07-02,"
                    "2027-08-06,2027-09-03,2027-10-08,2027-11-05,2027-12-03"))          # jobs report

# NYSE full-day closures, so "pause 2 trading days" skips holidays too.
MARKET_HOLIDAYS = set(_l("MARKET_HOLIDAYS",
                         "2026-11-26,2026-12-25,2027-01-01,2027-01-18,2027-02-15,2027-03-26,2027-05-31,"
                         "2027-06-18,2027-07-05,2027-09-06,2027-11-25,2027-12-24"))

USE_CLAUDE = _b("USE_CLAUDE", True)

# Option quotes: "indicative" is Alpaca's free feed (delayed trades, slightly altered quotes); "opra" needs
# the $99/month plan and a signed agreement. Named explicitly because Alpaca changed the default on 2026-11-10.
OPTION_FEED = (os.getenv("P1K_OPTION_FEED", "") or "indicative").strip().lower()
# Estimated exchange/regulatory fees per contract per side, used by the cost log (Alpaca passes these through).
FEE_PER_CONTRACT_SIDE = _f("FEE_PER_CONTRACT_SIDE", 0.05)
# Phone commands that change anything (pause, resume, close, kill, full, nofull) need this PIN in the message
# when it is set, e.g. "kill 4821". Set it as the GitHub secret CONTROL_PIN. status and help never need it.
CONTROL_PIN = (os.getenv("CONTROL_PIN", "") or "").strip()
DB_PATH = os.getenv("P1K_DB", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "portfolio1k.db"))
