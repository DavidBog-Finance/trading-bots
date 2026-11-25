# Portfolio 1k: go-live checklist

Written 2026-11-25, before any real money. Paper results are the only evidence, so the bar is set now,
while nothing is riding on it, and is not lowered later. The Friday scorecard prints which bars are still open
(`rules.go_live_ready`).

## 1. Bars the bot checks for you

| Bar | Why |
| --- | --- |
| At least 40 closed trades on ONE rule version | Fewer is mostly luck. Changing rules restarts the count. |
| Profit factor 1.3+ after estimated real costs ($6/contract) | Paper fills are kinder than real ones. |
| Max drawdown under 35% | A bigger paper drawdown will be bigger live. |
| Average stop slippage under 10 points | Stops must work as planned. |
| Luck check: the 95% range of the average result per trade is above $0 | With a ~50% win rate this usually takes 80-330 trades, so expect 3-6 months. |
| No rule breaks | Any manual override or bug that broke a rule resets the clock. |

## 2. Bars you check yourself

- [ ] Every "problem" alert from the last 4 weeks is explained and fixed.
- [ ] Filter tests (Friday report) have been reviewed; any filter you want to switch on has been
      backtested out of sample first (repo rule), which starts a new rule version.
- [ ] Claude's win chances beat "guessing the average" over 30+ trades, or Claude stays advisory only.
- [ ] Trades Claude rejected did not clearly beat the ones it took.
- [ ] The bot runs from a host you trust for money (your own computer or a server), with the repo private.
- [ ] Bet size revisited (you chose 50% of the account per trade for paper; research suggests 5-10% at risk).

## 3. How to start live, if every box is ticked

1. Fund with the amount you can lose (about $500 of risk was your limit).
2. Set `ALPACA_PAPER=false` and `I_UNDERSTAND_LIVE_RISK=yes` on the live host only.
3. First 20 live trades at 1 contract, whatever the budget allows.
4. Compare each live fill with what paper would have got (the cost log does this). If live costs run
   more than double paper's, stop and review.

## 4. Automatic stop rules once live

- The 50%-of-deposits kill switch stays on (it already is).
- Two weeks in a row below the weekly loss limit: pause and review before trading again.
- Any day with a "problem" alert you don't understand: pause from your phone (`pause 1k <PIN>`).
