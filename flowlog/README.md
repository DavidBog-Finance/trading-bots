# Flow log

A notebook for options-flow alerts. You write down an alert; it tells you what the stock did
1 trading day and 1 trading week (5 sessions) later, next to SPY, and keeps a scorecard.
It never trades.

## Log an alert

From your iPhone: the ntfy app only receives, so send from Safari. Open `ntfy.sh/<your FLOW_TOPIC name>`,
Share > Add to Home Screen, then tap that icon, type one alert per line in the box at the bottom and send.

    AAPL 12/20 230C ask $250K sweep
    TSLA 11/28 350 put bid 1.2M repeated
    NVDA 12/19 150C ask 512K on 11/18 guess bull

- First date = expiry. `on 11/18` = the day the alert happened (use it for delayed free flow).
- `ask` / `bid` (or 🛍️ / 🦴) is the side. Without it the read is "unclear".
- `guess bull` / `guess bear` records your own read, so the scorecard can grade you too.
- Tags it understands: sweep, repeated, multi-leg, block, split, vol>oi, opening, late, oseq, golden, floor.

1-day results arrive after each close. 1-week results are saved up and sent together with the scorecard
on Friday evening (after about 5:20 pm New York time, 2:20 pm in California).

Words you can send instead: `report`, `list`, `delete 12`, `help`.
Replies arrive in your normal alerts topic.

From GitHub (Actions > flow-log > Run workflow): pick `add` and type the alert, or `report`, `list`, `export`.

From a terminal: `python -m flowlog add "AAPL 12/20 230C ask $250K sweep"`, `python -m flowlog report`.

## How it is judged

- The option too: its price at the alert-day close vs the same later day (after expiry: what it paid out).
  Short-dated options can lose money even when the stock moves the right way.

- Read: call on the ask or put on the bid = bullish; put on the ask or call on the bid = bearish.
- Base price: the stock's close on the alert day. 1 day = next close, 1 week = 5 closes later.
- Right = the stock moved the way the read pointed. Edge vs SPY = that move minus SPY's move.
- Fair edge = the move minus what the market alone explains for that stock (beta x SPY's move, beta from
  the ~120 sessions before the alert). A stock that usually moves 2x the market no longer "wins" just
  because the market went up. This is the number to trust most.
- Counted by day = every alert is kept, but alerts from the same day are averaged first, so one big market
  day with 10 alerts counts once instead of 10 times.
- Under about 30 finished alerts the numbers are mostly luck; patterns by tag or size need a few hundred days.

## Where it runs

GitHub: `.github/workflows/flowlog.yml`, every 2 hours (ntfy keeps phone messages about 12 hours).
Own computer: `python -m flowlog watch` (checks every 5 minutes). Data: `logs/flowlog.db`.

## Automatic scan (free)

After each close, `python -m flowlog run --scan` looks at your watchlist's options in your free Alpaca
data and logs up to 8 contracts with unusually big new volume (volume above open interest, $100K+
traded, 1-45 days to expiry, at most 2 per stock), tagged `scan`. Alpaca's free data has no buy/sell
flag, so the side is estimated from price ticks (tag `est-side`) and left blank when mixed.
Alpaca's free option data is delayed and slightly altered, so scan alerts are tagged `delayed`: they show
what happened, not real-time flow you could have traded on.
The scorecard shows `tag scan` separately, so you can compare it with the alerts you log yourself.
Run it now for the last closed session: `python -m flowlog scan`, or the `scan` button on GitHub.
