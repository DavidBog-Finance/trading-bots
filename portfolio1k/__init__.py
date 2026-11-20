"""Portfolio 1k: an options-only day-trading agent for a small Alpaca account.

Buys short-dated calls or puts on a liquid watchlist, one trade at a time, when a tested
setup fires and Claude agrees. Every trade gets a stop at Alpaca the moment it fills plus
bot-managed take-profits and time stops. Hard limits are plain code, so they work even
when Claude is paused or out of usage. See portfolio1k/README.md.
"""
