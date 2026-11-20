"""python -m flowlog add "AAPL 12/20 230C ask $250K sweep"   log one alert (several lines OK)
python -m flowlog run [--scan] one pass: read the phone topic, fill results; --scan adds the after-close scan
python -m flowlog scan         scan the last closed session now (once per session)
python -m flowlog watch        keep running (always-on computer)
python -m flowlog report       print the scorecard (also pushes it to your phone)
python -m flowlog list | delete N | export [file.csv]"""
import sys

from flowlog import app


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    cmd, rest = argv[0], " ".join(argv[1:])
    if cmd == "add":
        replies = app.add_text(rest.replace("\\n", "\n"))
        print("\n".join(replies))
        app.say("\n".join(replies))
    elif cmd == "run":
        app.run_once(scan_too="--scan" in argv)
    elif cmd == "scan":
        lines = app.scan(catch_up=True)
        print(f"logged {len(lines)} scan alerts")
    elif cmd == "watch":
        app.watch()
    elif cmd in ("report", "list", "help") or (cmd == "delete" and rest):
        text = app.command(f"{cmd} {rest}".strip())
        print(text)
        if cmd == "report":
            app.say(text, "Flow scorecard")
    elif cmd == "export":
        path = rest or "logs/flowlog.csv"
        print(f"wrote {app.export(path)} alerts to {path}")
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
