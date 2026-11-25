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
| Size | 50% of equity (stop -30%) on every trade, A+ included. Full port (100%, stop -25%) is retired; it can come back only if the owner sets `P1K_FULL_PORT=true` or sends `full` |
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
- Phone (ntfy, CONTROL_TOPIC): `status`, `help`, `pause`, `resume`, `close`, `kill`, `nofull`, `full` (" 1k" optional).
  If the secret `CONTROL_PIN` is set, every command except `status` and `help` must include it: `kill 1k 4821`.
  Commands older than 10 minutes are ignored, so an old message can never be replayed.
  On GitHub they are read while a run is active (market hours); outside those hours use the buttons.
- GitHub: Actions > options-agent > Run workflow: status, scorecard, run-now, close-trade, kill, export-memory.
- Terminal: `python -m portfolio1k status | scorecard | once | run`.

## Known limits on the free Alpaca data plan
Option quotes are Alpaca's indicative feed (not the official OPRA feed), so spreads and fills on paper are
estimates and live fills will be somewhat worse. Stock bars come from IEX. In GitHub, stop checks pause for
1-3 minutes between runs; the stop order at Alpaca covers that gap.

## Orders

- Buys: a limit at the mid, raised one tick every 10 s up to 2% over the mid, cancelled after 60 s.
- Profit-taking, trailing and time exits: a limit at the mid stepped down to the bid in ~5 s steps,
  then market for anything left. Stops, kills, expiry closes and your phone `close` sell at market at once.
- A stop for the full quantity must be resting at Alpaca at every check (every 15 s). If one is missing it is
  placed again; if that fails you get one alert per trade.
- Anything expiring today is sold by 3:00 pm New York (Alpaca force-sells expiring options from ~3:30 pm);
  anything expiring the next trading day is sold by 3:30 pm.
- Option quotes always name their feed (`P1K_OPTION_FEED`, default `indicative`, the free one; `opra` if you
  ever pay for Alpaca's real-time plan).

## Memory is rebuilt from Alpaca at every start

GitHub's cache can be evicted or roll back. At every start the program asks Alpaca what it really holds:
a position the memory lost is taken back (tagged `recovered`, normal stop), a position Alpaca no longer
holds is closed at its real fill price, a different quantity uses Alpaca's number, and today's / this
week's trade counts are rebuilt from Alpaca's order history so the daily and weekly limits still hold.
Each run stops at 330 minutes so the "Save memory" step always has time to run.

## Cost log and grade report card

Every trade records the bid, ask and fill at entry and exit, the feed, slippage versus the mid and estimated
fees (`P1K_FEE_PER_CONTRACT_SIDE`, default $0.05). The status, daily summary and scorecard show
"paid X% vs mid in, Y% out, spread Z%, about $N per round trip x trades/yr = $M/yr", plus win rate and
average result for each Claude grade (A+, A, B, rules only), so you can see whether the grades predict anything.

## Phone alerts

Every alert names the contract the same way: ticker, strike, C or P, expiration (e.g. `NVDA 142P 11/27`).

| Alert | Title | Body |
| --- | --- | --- |
| Buy filled | `BUY NVDA 142P 11/27` | contracts @ price = cost, stop, targets, setup, Claude's grade and win chance |
| Take profit 1 | `TRIM NVDA 142P 11/27 +41%` | sold @ price, profit locked in, what is left, breakeven stop |
| Trade closed | `SELL NVDA 142P 11/27 -26%` | contracts @ price (bought at), P&L $ and %, time held, why, account and today's P&L |
| Problem | `Needs attention` | what went wrong and what the bot did about it |
| Daily / weekly | `Daily summary`, `Weekly scorecard` | trades closed today, account, open trade, record, costs, grades |

Green circle = buy, check mark = win, red X = loss, warning sign = problem. Paper alerts carry a small
"paper" label; live alerts start with "LIVE".

## Near-miss report

After the close (4:15 pm New York), on any day a setup fired, your phone gets "Setups today":
how many setups fired, how many became trades, why the others were skipped (SPY against it, no good
contract, Claude said no, too expensive, not filled) and a few examples. It shows whether the bot is too picky.

## Honest paper results

Paper fills are kinder than real ones. The scorecard and the go-live check subtract
`P1K_REAL_COST_PER_CONTRACT` (default $6 per contract per round trip) from every trade before computing
win rate, profit factor, net and drawdown. The raw paper number is still shown as "paper".

## Missed-session warning (optional)

Add a GitHub secret `HEALTHCHECK_URL` (a healthchecks.io ping URL). Each scheduled run pings it when it
starts and pings `/fail` if the run fails, so healthchecks.io can warn you when a weekday passes with no run.

## Keeping GitHub happy

- Tries start at :17 and :47 past the hour from 5:17 am New York, away from GitHub's busy top-of-the-hour slot.
  GitHub's timer skipped almost every try on 11/23-11/24, so only one try has to land: each run then starts the
  next one itself (`python -m portfolio1k more-today`, then `gh workflow run`) until about 3:55 pm.
- Backup starter: whenever another workflow's timer lands (flow-log, agents, bots), its `kick-options` job starts
  the options agent if today's session has time left and no run is going (`.github/workflows/kick-options.yml`).
- Mac backup starter (optional): `portfolio1k/deploy/mac-backup-starter.sh` adds a launchd job on your Mac that checks
  every 15 min (weekdays 9:00 am-3:30 pm New York) and starts a run if none is going. It needs `gh auth login`; it never trades.
- healthchecks.io is pinged by every trading run (timer or run-now), so hand-off days don't look like missed days.
- The Claude token is cleaned of spaces and line breaks before use (`claude setup-token` wraps it over two lines).
- `keepalive.yml` re-enables the schedules once a month (GitHub turns them off after 60 quiet days in public repos).
- Dependabot opens a pull request once a month if a GitHub Actions step is out of date; tests run on it, you merge.

## Learning what works (added 2026-11-25)

- **Filter tests (logged only):** every trade stores whether four research-backed filters would have
  blocked it (`rules.SHADOW_FILTERS`: lunch window, strict puts, volatility gate, news as a veto only).
  The Friday scorecard shows how the trades each filter would have skipped actually did. Nothing changes
  until a filter passes an out-of-sample backtest (repo rule).
- **Claude as a forecaster:** each review now includes `win_prob`. The status shows its Brier score against
  simply guessing the average win rate.
- **Trades Claude said no to** are followed on paper with the same exit rules (filled at the mid) and
  compared with the trades it took.
- **Luck check:** average result per trade after costs with a 95% bootstrap range. If the range includes
  $0, the results so far can't be told from luck. It is also a go-live bar; see `GO_LIVE.md`.
- **Pinned versions:** GitHub installs `requirements-lock.txt` and a fixed Claude Code version, so an
  upstream release can't break trading mid-week. `requirements.txt` keeps ranges for your own computer.

