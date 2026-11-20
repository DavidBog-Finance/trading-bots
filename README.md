# Trading bots + Claude trading agents (stocks, crypto, options)

> **Current focus (Nov 2026): options only.** The Portfolio 1k options agent (`portfolio1k/`, see its README)
> runs on GitHub Actions (`options-agent.yml`). The stock and crypto bots and agents below are switched off;
> turn them back on with the GitHub variable `STOCKS_CRYPTO=on`.

Tested daily rules bots plus two Claude agents (stock and crypto) that watch the whole market and the news
all day from your Mac and decide trades within hard limits. Built for Alpaca, run and modified with Claude Code.
The full step-by-step guide (brokers, research, taxes, risk, setup) is the companion web page.

| Bot | Rule (one line) | Out-of-sample backtest | Buy & hold, same period |
|---|---|---|---|
| `stock_bot.py` | 50% "SPY above 200-day average" + 50% Connors RSI(2) pullbacks; rest in cash | 2000-2025: ~6-7%/yr, max drawdown -12% | 8.0%/yr, max drawdown -55% |
| `crypto_bot.py` | BTC trend vote across 6 lookbacks, sized to 40% target volatility | 2021-Oct 2026: ~21%/yr, max drawdown -34% | 20%/yr, max drawdown -77% |
| `options_bot.py` | SPY 25-delta bull put spread, 3% wide, ~45 days, only in uptrends; off by default | Model only (no option quotes): 9-16%/yr on the options sleeve, max drawdown -24 to -32% | n/a |

These are backtests, after estimated fees, not promises. Both bots mainly sell *smaller losses*, not bigger gains.

| Agent | What it does | Evidence |
|---|---|---|
| `agent.py stock` | Claude picks from ~1,000+ liquid US stocks/ETFs using a momentum ranking, live movers and Claude-scored news; check-ins 10:00/12:30/15:10 ET plus instant wake-ups on news and big moves | Ingredients have research support (momentum; LLM news scores predict next-day returns). The agent as a whole **cannot be honestly backtested**: paper trade it |
| `agent.py crypto` | Same for every Alpaca coin (trend vote + news), every 6 h plus wake-ups | Same |

Hard limits in code (`agent.RiskGate`): sleeve size, max 6 positions, max 30% of the sleeve each, 10 orders/day,
long only, no leveraged ETFs or stocks under $5 / $20M daily volume, automatic stop-loss (-12% stocks, -20% crypto),
PDT guard, Claude spend cap per day.

## Quick start (paper trading)

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                   # paste your Alpaca PAPER keys
python -m pytest -q tests                              # 57 tests, no network
DRY_RUN=1 FORCE_RUN=1 python stock_bot.py              # see what it would do
python crypto_bot.py                                   # places a paper order if needed
python backtest.py crypto --download --split 2021-01-01
```

**Always-on Mac (bots + agents):** log in to Claude Code once (`claude`), then `bash mac/install.sh`. The agents use your
Claude Pro plan via `claude -p` (`CLAUDE_BACKEND=pro`, default) or the API (`CLAUDE_BACKEND=api` + `ANTHROPIC_API_KEY`).
launchd keeps `daemon.py` running and restarts it if it crashes; it schedules the rules bots, scores news,
runs the agents and enforces stop-losses. Logs: `tail -f logs/runner.log`. Stop: `bash mac/uninstall.sh`.
Set the GitHub variable `RUNNER=mac` so the cloud schedule doesn't trade twice.

**Cloud only (bots + agents, no computer needed):** push this folder to a private GitHub repo and add the secrets
`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `CLAUDE_CODE_OAUTH_TOKEN` (run `claude setup-token` on your Mac; uses your
Claude plan, no API key), `NTFY_TOPIC` and `CONTROL_TOPIC`, plus the variable `NET_DEPOSITS_OVERRIDE` (your paper balance).
`.github/workflows/bots.yml` runs the rules bots at fixed times; `.github/workflows/agents.yml` runs `python daemon.py --once`
every 30 min in market hours and every 2 h otherwise (agents, news scoring, stop-losses, phone commands). The agents'
memory (`logs/state.db`) is kept between runs in the Actions cache. Differences from the home server: stop-losses are
checked each run instead of every minute, and there is no local AI. Moving to the server later: set the repository
variable `RUNNER=server` so the cloud schedules stop and nothing trades twice.

Sizing is a share of account equity (`STOCK_PCT`, `CRYPTO_PCT`, `OPTIONS_PCT`), so deposits are used automatically.
`python validate.py` (on your Mac) tests extra ETFs and coins before you add them. `python summary.py` pushes a status report to your phone.

## Safety rails
- Paper by default. Live needs `ALPACA_PAPER=false` **and** `I_UNDERSTAND_LIVE_RISK=yes`.
- `MAX_LOSS_PCT`: if equity falls that far below your net deposits, every bot sells and stops.
- `KILL_SWITCH=true` or the `kill-all` button in GitHub Actions: everything is sold on the next run.
- Optional dollar caps per bot (`*_MAX_USD`); no order over `MAX_ORDER_PCT` of equity.
- Stale-data, price-jump, max-order and dust checks before every order.
- iPhone commands via ntfy (`CONTROL_TOPIC`): `status`, `pause`, `pause stock`, `resume`, `kill`, `run crypto`.
- Phone pushes only when trades have filled (one message per run, with the agent's reasoning) or something is wrong.

## Home server (Beelink SER9 Pro, Ubuntu; the Aero works too)
See `linux/README.md`. Local AI screens every headline and Claude handles what matters (held positions, critical
news, a 10% audit), paced through the hour by `CLAUDE_CALLS_PER_HOUR`. Local models never trade.

## Files
`strategies.py` signals (shared by bots and backtest) · `broker.py` Alpaca, alerts + safety · `config.py` settings · `options_bot.py` · `summary.py` · `validate.py` ·
`daemon.py` runner (Mac, Linux, or `--once` in GitHub Actions) · `agent.py` Claude agents + risk gate · `news.py` headline scoring · `market.py` whole-market scanner · `state.py` memory (SQLite) · `claude_client.py` · `mac/` launchd installer · `linux/` home-server setup, installer, Docker services ·
`local_llm.py` Ollama (local AI) · `datastore.py` DuckDB market data + options recorder · `scorecard.py` is the AI earning its keep · `health.py` temps/disk alerts ·
`backtest.py` reproducible tests · `research/` the scripts that chose the rules · `tests/` unit + smoke tests.

Not financial advice. You can lose money, including all of the allocation.
