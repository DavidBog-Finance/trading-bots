"""python -m portfolio1k run [--max-minutes N] | once | status | scorecard | kill | more-today"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import broker as B  # noqa: E402
from portfolio1k import rules as R, runner, settings as S, store  # noqa: E402


def main():
    ap = argparse.ArgumentParser(prog="portfolio1k")
    ap.add_argument("cmd", choices=["run", "once", "status", "scorecard", "kill", "more-today"])
    ap.add_argument("--max-minutes", type=float, default=None,
                    help="exit after this many minutes or at 16:20 New York (GitHub Actions); omit to run forever")
    a = ap.parse_args()
    if a.cmd == "scorecard":
        trades = store.closed_trades()
        card = R.scorecard(trades, S.REAL_COST_PER_CONTRACT)
        print(runner.scorecard_line(card))
        for line in (runner.cost_line(trades), runner.grade_lines(trades), runner.luck_line(trades),
                     runner.claude_line(trades), runner.shadow_lines(trades)):
            if line:
                print(line)
        todo = R.go_live_ready(card, 0, R.expectancy_ci(trades, S.REAL_COST_PER_CONTRACT))
        print("Go-live bars still open:", ", ".join(todo) if todo else "none, all met")
        return
    if a.cmd == "more-today":
        # exit code 0 = today's session still has time left, so the cloud run should hand off to a new one
        from portfolio1k.runner import ET, now_et
        more = None
        for _ in range(3):
            try:
                clock = B.trading_client().get_clock()
                more = R.more_session_today(now_et(), clock.is_open, clock.next_open.astimezone(ET))
                break
            except Exception as e:
                print("clock check failed:", e)
        if more is None:   # Alpaca unreachable: assume a normal weekday session rather than drop the day
            n = now_et()
            more = n.weekday() < 5 and R.hm("09:00") <= n.time() < R.hm("15:55")
        print("more session today:", "yes" if more else "no")
        sys.exit(0 if more else 1)
    r = runner.Runner()
    if a.cmd == "run":
        r.run(a.max_minutes)
    elif a.cmd == "once":
        r.startup()
        r.tick_safely()
    elif a.cmd == "status":
        txt = r.status_text()
        if S.USE_CLAUDE:
            from portfolio1k import brain
            txt += "\nClaude: " + brain.check()
        print(txt)
        B.notify(txt, "Status", "low", "bar_chart")
    elif a.cmd == "kill":
        r.kill("kill button pressed")


if __name__ == "__main__":
    main()
