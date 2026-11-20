"""python -m portfolio1k run [--max-minutes N] | once | status | scorecard | kill"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import broker as B  # noqa: E402
from portfolio1k import rules as R, runner, settings as S, store  # noqa: E402


def main():
    ap = argparse.ArgumentParser(prog="portfolio1k")
    ap.add_argument("cmd", choices=["run", "once", "status", "scorecard", "kill"])
    ap.add_argument("--max-minutes", type=float, default=None,
                    help="exit after this many minutes or at 16:20 New York (GitHub Actions); omit to run forever")
    a = ap.parse_args()
    if a.cmd == "scorecard":
        trades = store.closed_trades()
        card = R.scorecard(trades, S.REAL_COST_PER_CONTRACT)
        print(runner.scorecard_line(card))
        for line in (runner.cost_line(trades), runner.grade_lines(trades)):
            if line:
                print(line)
        todo = R.go_live_ready(card, 0)
        print("Go-live bars still open:", ", ".join(todo) if todo else "none, all met")
        return
    r = runner.Runner()
    if a.cmd == "run":
        r.run(a.max_minutes)
    elif a.cmd == "once":
        r.startup()
        r.tick_safely()
    elif a.cmd == "status":
        txt = r.status_text()
        print(txt)
        B.notify(txt, "1k Status", "low")
    elif a.cmd == "kill":
        r.kill("kill button pressed")


if __name__ == "__main__":
    main()
