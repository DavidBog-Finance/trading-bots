"""
CRYPTO BOT: trend ensemble + volatility targeting on each coin in CRYPTO_SYMBOLS,
run once a day just after 00:00 UTC (5:00 pm Pacific in summer, 4:00 pm in winter).

Size = CRYPTO_PCT of account equity, split evenly across coins. Each coin's weight
comes from the same rule that was backtested on BTC.

  python crypto_bot.py
"""
import sys

import broker as B
import config as C
import strategies as S

BOT = "crypto"


def main():
    client = B.trading_client()
    eq = B.safety_gate(client, BOT, C.CRYPTO_SYMBOLS)
    budget = B.sleeve_budget(eq, C.CRYPTO_PCT, C.CRYPTO_MAX_USD)
    per_coin = budget / len(C.CRYPTO_SYMBOLS)
    B.log.info("[%s] equity $%.2f, crypto budget $%.2f ($%.2f per coin)", BOT, eq, budget, per_coin)

    problems = 0
    for sym in C.CRYPTO_SYMBOLS:
        closes = B.crypto_daily_closes(sym)
        if len(closes) < 200 or B.stale(closes.index[-1], max_age_days=2):
            B.alert(f"[{BOT}] {sym}: not enough fresh data ({len(closes)} days). Skipping this coin.")
            problems += 1
            continue
        target_w = float(S.crypto_raw_weights(closes, C.CRYPTO_TARGET_VOL).iloc[-1])
        value, _ = B.position_value(client, sym)
        current_w = value / per_coin if per_coin else 0.0
        B.log.info("[%s] %s close %.2f | holding %.0f%% | target %.0f%%", BOT, sym, closes.iloc[-1], current_w * 100, target_w * 100)
        if S.needs_rebalance(current_w, target_w, C.CRYPTO_REBALANCE_BAND):
            B.rebalance(client, BOT, sym, target_w * per_coin, float(closes.iloc[-1]), crypto=True, eq=eq)
        else:
            B.log.info("[%s] %s within the %.0f%% band. No trade.", BOT, sym, C.CRYPTO_REBALANCE_BAND * 100)
    if problems:
        sys.exit(1)


if __name__ == "__main__":
    main()
