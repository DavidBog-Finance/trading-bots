"""
Daily summary pushed to your iPhone (ntfy and/or Discord). Read-only: never trades.

  python summary.py
"""
import broker as B
import config as C


def main():
    client = B.trading_client()
    acct = client.get_account()
    eq, last = float(acct.equity), float(acct.last_equity or acct.equity)
    dep = B.net_deposits(client)
    lines = [f"Equity ${eq:,.2f} ({eq - last:+,.2f} today)"]
    if dep > 0:
        pnl = eq - dep
        lines.append(f"Deposited ${dep:,.2f} | P&L {pnl:+,.2f} ({pnl / dep:+.1%})")
        floor = dep * (1 - C.MAX_LOSS_PCT)
        lines.append(f"Loss-limit floor ${floor:,.2f} ({(eq - floor) / eq:.0%} away)")
    positions = client.get_all_positions()
    if positions:
        for p in sorted(positions, key=lambda p: -abs(float(p.market_value or 0))):
            lines.append(f"{p.symbol}: ${float(p.market_value or 0):,.2f} ({float(p.unrealized_plpc or 0):+.1%})")
    else:
        lines.append("All cash")
    cash = float(acct.cash)
    lines.append(f"Cash ${cash:,.2f}")
    if C.KILL_SWITCH:
        lines.append("KILL SWITCH IS ON")
    B.notify("\n".join(lines), "Daily summary", "low")


if __name__ == "__main__":
    main()
