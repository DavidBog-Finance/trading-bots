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

Words you can send instead: `report`, `list`, `delete 12`, `help`.
Replies arrive in your normal alerts topic.

From GitHub (Actions > flow-log > Run workflow): pick `add` and type the alert, or `report`, `list`, `export`.

From a terminal: `python -m flowlog add "AAPL 12/20 230C ask $250K sweep"`, `python -m flowlog report`.

## How it is judged

- Read: call on the ask or put on the bid = bullish; put on the ask or call on the bid = bearish.
- Base price: the stock's close on the alert day. 1 day = next close, 1 week = 5 closes later.
- Right = the stock moved the way the read pointed. Edge vs SPY = that move minus SPY's move.
- Under about 30 finished alerts the numbers are mostly luck.

## Where it runs

GitHub: `.github/workflows/flowlog.yml`, every 2 hours (ntfy keeps phone messages about 12 hours).
Own computer: `python -m flowlog watch` (checks every 5 minutes). Data: `logs/flowlog.db`.
