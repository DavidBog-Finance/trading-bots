# Portfolio 1k: options-only day-trading agent

Buys 2-7 day calls or puts on SPY, QQQ, IWM and 9 mega caps when one of three setups fires
(opening-range breakout, trend pullback, strong news) and Claude agrees. One trade at a time,
a stop resting at Alpaca from the moment it fills, take profits at +40% (half) and +80%,
a setup stop, a 3:45 pm New York time stop, and nothing held into expiry or earnings.
Full plan: the "Portfolio 1k: options-only plan" doc. Paper first: 6+ weeks and 40+ trades.

| Hard limit (code, not Claude) | Value |
|---|---|
| Account floor (kill) | 50% of net deposits |
| Daily / weekly loss pause | -20% / -30% |
| Trades | 2 a day, 8 a week, one open at a time |
| Size | 50% of equity (stop -30%); full port 100% (stop -25%) only on A+ two-setup trades, max 1/week, never after a loss |
| Losing streak | 3 losses: pause 2 trading days, then 3 trades at standard size |
| Contract | delta 0.40-0.55, open interest 1,000+, spread <= 5% of mid (ETFs $0.03), volume 500+ when known |

Claude (your Pro plan) writes a morning game plan, scores watchlist news, and grades each trade
A+/A/B before it is placed. If Claude is unavailable, setups 1 and 2 still trade at standard size.

## Where it runs
- **Now: GitHub Actions** (`.github/workflows/options-agent.yml`), free on a public repo.
- **Later: any always-on computer.** `bash portfolio1k/deploy/install-linux.sh` (Linux, or Windows with WSL2)
  or `bash portfolio1k/deploy/install-mac.sh`. Same program, `python -m portfolio1k run`. Then set the GitHub
  variable `RUNNER=box`. To keep the trade history: Actions > options-agent > Run workflow > export-memory,
  download the artifact and put both files in `logs/` on the computer before installing.

## Commands
- Phone (ntfy, CONTROL_TOPIC): `status`, `pause`, `resume`, `close`, `kill`, `nofull`, `full` (" 1k" optional).
  On GitHub they are read while a run is active (market hours); outside those hours use the buttons.
- GitHub: Actions > options-agent > Run workflow: status, scorecard, run-now, close-trade, kill, export-memory.
- Terminal: `python -m portfolio1k status | scorecard | once | run`.

## Known limits on the free Alpaca data plan
Option quotes are Alpaca's indicative feed (not the official OPRA feed), so spreads and fills on paper are
estimates and live fills will be somewhat worse. Stock bars come from IEX. In GitHub, stop checks pause for
1-3 minutes between runs; the stop order at Alpaca covers that gap.
