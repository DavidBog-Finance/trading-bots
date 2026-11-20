"""
All settings come from environment variables (a .env file on your Mac, or GitHub
Actions secrets/variables in the cloud). Defaults are the safe choice.

Sizing is a percentage of account equity, so money you deposit is picked up
automatically on the next run. No settings need to change when you add funds.
"""
import os
import shutil

from dotenv import load_dotenv

load_dotenv()


def _bool(name, default=False):
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _float(name, default):
    v = os.getenv(name, "")
    return float(v) if v.strip() else default


def _list(name, default):
    v = os.getenv(name, "")
    return [s.strip() for s in (v or default).split(",") if s.strip()]


ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")

# Paper trading unless you deliberately flip BOTH switches.
PAPER = _bool("ALPACA_PAPER", True)
LIVE_CONFIRMED = os.getenv("I_UNDERSTAND_LIVE_RISK", "") == "yes"

DRY_RUN = _bool("DRY_RUN", False)          # print orders, send nothing
KILL_SWITCH = _bool("KILL_SWITCH", False)  # sell everything the bots manage, then stop

# ---- How the account is split (fractions of total equity; keep the sum <= 1) ----
# Rules bots (tested in the guide) ...
STOCK_PCT = _float("STOCK_PCT", 0.30)
CRYPTO_PCT = _float("CRYPTO_PCT", 0.20)
OPTIONS_PCT = _float("OPTIONS_PCT", 0.15)
# ... and the Claude agents (not backtestable; paper trade them first)
AGENT_STOCK_PCT = _float("AGENT_STOCK_PCT", 0.20)
AGENT_CRYPTO_PCT = _float("AGENT_CRYPTO_PCT", 0.15)

# Optional hard caps in dollars per bot (0 = no cap). Handy while you're nervous.
STOCK_MAX_USD = _float("STOCK_MAX_USD", 0)
CRYPTO_MAX_USD = _float("CRYPTO_MAX_USD", 0)
OPTIONS_MAX_USD = _float("OPTIONS_MAX_USD", 0)

# ---- Account-level loss limit, deposit aware ----
# If equity falls more than this fraction below the money you've put in (deposits
# minus withdrawals), every bot sells and stops until you raise the limit.
MAX_LOSS_PCT = _float("MAX_LOSS_PCT", 0.35)
# Paper accounts have no deposit history: tell the bot what the "deposit" was.
NET_DEPOSITS_OVERRIDE = _float("NET_DEPOSITS_OVERRIDE", 0)

MAX_ORDER_PCT = _float("MAX_ORDER_PCT", 0.60)  # no single order bigger than this share of equity
MIN_TRADE_USD = _float("MIN_TRADE_USD", 5)     # skip dust trades

# ---- Stock bot ----
STOCK_TREND_SYMBOL = os.getenv("STOCK_TREND_SYMBOL", os.getenv("STOCK_SYMBOL", "SPY"))
# The RSI(2) pullback sleeve can watch several index ETFs for more trades.
# SPY is the only one tested in the guide; run the validation backtest before adding others.
STOCK_RSI2_SYMBOLS = _list("STOCK_RSI2_SYMBOLS", "SPY")
STOCK_MODE = os.getenv("STOCK_MODE", "blend")  # "blend" (trend + RSI2) or "trend"
CASH_SYMBOL = os.getenv("CASH_SYMBOL", "")     # e.g. "SGOV" to earn T-bill yield on idle stock-sleeve cash

# ---- Crypto bot ----
CRYPTO_SYMBOLS = _list("CRYPTO_SYMBOLS", os.getenv("CRYPTO_SYMBOL", "BTC/USD"))
CRYPTO_TARGET_VOL = _float("CRYPTO_TARGET_VOL", 0.40)
CRYPTO_REBALANCE_BAND = _float("CRYPTO_REBALANCE_BAND", 0.10)

# ---- Options bot ----
OPTIONS_ENABLED = _bool("OPTIONS_ENABLED", False)
OPTIONS_UNDERLYING = os.getenv("OPTIONS_UNDERLYING", "SPY")
# Most you can lose on one spread position, as a fraction of the options sleeve.
OPTIONS_RISK_PER_TRADE = _float("OPTIONS_RISK_PER_TRADE", 0.20)
# Alpaca multi-leg orders: a credit is entered as a negative limit price. Verify on paper.
OPTIONS_CREDIT_IS_NEGATIVE = _bool("OPTIONS_CREDIT_IS_NEGATIVE", True)

# ---- Alerts to your iPhone ----
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")                # ntfy app: pick a long random topic name
ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "")  # Discord or Slack incoming webhook (optional)
NOTIFY_TRADES = _bool("NOTIFY_TRADES", True)            # one push per run listing filled trades
FILL_WAIT_SEC = _float("FILL_WAIT_SEC", 30)             # how long to wait for a fill before reporting
DAILY_SUMMARY = _bool("DAILY_SUMMARY", False)           # extra end-of-day status push from the Mac runner
HEALTHCHECK_URL = os.getenv("HEALTHCHECK_URL", "")      # healthchecks.io ping: tells you if the Mac goes offline
# Send commands from the ntfy app on your iPhone: status, pause, resume, kill, run stock|crypto|agents
CONTROL_TOPIC = os.getenv("CONTROL_TOPIC", "")

# ---- Claude agents (always-on Mac runner) ----
# "pro": runs through Claude Code on your Mac using your Claude Pro/Max subscription (no API key).
#        Uses your plan's usage limits, shared with your own Claude use.
# "api": calls the Claude API with ANTHROPIC_API_KEY, billed per token.
CLAUDE_BACKEND = os.getenv("CLAUDE_BACKEND", "pro").strip().lower()
CLAUDE_BIN = os.getenv("CLAUDE_BIN", "") or shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
CLAUDE_CODE_MODEL = os.getenv("CLAUDE_CODE_MODEL", "")      # "" = your plan's default; or opus / sonnet / haiku
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# ---- Local AI + Claude together (home server) ----
LOCAL_LLM_ENABLED = _bool("LOCAL_LLM_ENABLED", False)
LOCAL_LLM_URL = os.getenv("LOCAL_LLM_URL", "http://localhost:11434")
LOCAL_MODEL = os.getenv("LOCAL_MODEL", "llama3.1:8b")
# Claude pacing: calls per rolling hour. Lower-priority work (news screening, audits) backs off
# first as the hour fills, so agent runs and news about your holdings always get Claude.
CLAUDE_CALLS_PER_HOUR = int(_float("CLAUDE_CALLS_PER_HOUR", 30))
# A headline goes to Claude when the local model rates its materiality at least this high
# (adjusted automatically each night from how often the two disagree).
ESCALATE_MATERIALITY = _float("ESCALATE_MATERIALITY", 0.5)
AUDIT_SHARE = _float("AUDIT_SHARE", 0.10)
DATASTORE_ENABLED = _bool("DATASTORE_ENABLED", True)   # keep price history in data/market.duckdb
OPTIONS_RECORDER = _bool("OPTIONS_RECORDER", True)     # save a daily SPY option-chain snapshot
# Bedroom mode: heavy jobs (research, scorecard, big local model) only in this window, New York time,
# on weekdays. Nights stay quiet: only the bots and short local-AI bursts run.
HEAVY_HOURS = os.getenv("HEAVY_HOURS", "12-20")   # 9 am-5 pm Los Angeles = 12-20 New York
QUIET_HOURS = os.getenv("QUIET_HOURS", "1-11")    # 10 pm-8 am Los Angeles = 1-11 New York   # share of "unimportant" headlines Claude double-checks
AGENTS_ENABLED = _bool("AGENTS_ENABLED", True)
AGENT_MODEL = os.getenv("AGENT_MODEL", "claude-opus-5-5")   # claude-haiku-5-5 is ~40x cheaper
AGENT_EFFORT = os.getenv("AGENT_EFFORT", "medium")          # low | medium | high
NEWS_MODEL = os.getenv("NEWS_MODEL", "claude-opus-5-5")     # scores every headline; runs often
NEWS_POLL_MIN = _float("NEWS_POLL_MIN", 15)
NEWS_DAILY_BUDGET_USD = _float("NEWS_DAILY_BUDGET_USD", 0.75)     # news scoring's share of the budget below
CLAUDE_DAILY_BUDGET_USD = _float("CLAUDE_DAILY_BUDGET_USD", 2.00)  # agents + news stop for the day past this
AGENT_MAX_RUNS_PER_DAY = int(_float("AGENT_MAX_RUNS_PER_DAY", 12))  # per agent
# Hard limits the agents cannot override (checked in code on every order)
AGENT_MAX_POSITIONS = int(_float("AGENT_MAX_POSITIONS", 6))
AGENT_MAX_POSITION_PCT = _float("AGENT_MAX_POSITION_PCT", 0.30)  # of that agent's sleeve
AGENT_MAX_ORDERS_PER_DAY = int(_float("AGENT_MAX_ORDERS_PER_DAY", 10))
AGENT_STOCK_STOP_PCT = _float("AGENT_STOCK_STOP_PCT", 0.12)      # sold automatically below entry -12%
AGENT_CRYPTO_STOP_PCT = _float("AGENT_CRYPTO_STOP_PCT", 0.20)
AGENT_SLEEVE_MAX_DD = _float("AGENT_SLEEVE_MAX_DD", 0.25)         # agent paused if its sleeve falls 25% from peak
AGENT_MIN_PRICE = _float("AGENT_MIN_PRICE", 5)
AGENT_MIN_DOLLAR_VOL = _float("AGENT_MIN_DOLLAR_VOL", 20_000_000)  # avg daily $ volume, stocks
# Wake an agent early when a holding or the market moves this much (fraction, intraday)
STOCK_SHOCK_PCT = _float("STOCK_SHOCK_PCT", 0.05)
CRYPTO_SHOCK_PCT = _float("CRYPTO_SHOCK_PCT", 0.08)
NEWS_WAKE_MATERIALITY = _float("NEWS_WAKE_MATERIALITY", 0.7)
