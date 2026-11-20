"""
ALWAYS-ON RUNNER. One process that runs all day and night on the home server
(the SER9 Pro or Aero on Ubuntu, kept alive by systemd: linux/install.sh) or on a Mac
(launchd: mac/install.sh). Replaces the GitHub Actions schedule.

Every 30 seconds it checks what's due:
  - Rules bots (tested): stock + options bot ~25-45 min before the close,
    crypto bot just after 00:10 UTC. Same code and timing as before.
  - News watcher every NEWS_POLL_MIN minutes: local AI screens every headline,
    Claude scores the ones that matter, paced through the hour.
  - Daily, after the close: SPY option-chain snapshot for the data store.
  - Daily in the heavy-jobs window (weekdays, while you're at work): scorecard and
    local-AI calibration; the scorecard is pushed on Fridays.
  - Server health (temperatures, disk, memory) every 5 minutes.
  - Stop-losses for agent positions: every minute, no AI involved.
  - Stock agent: check-ins at 10:00, 12:30 and 15:10 New York time on market days.
  - Crypto agent: a check-in every 6 hours.
  - Either agent is woken early by: a material headline on something it holds or
    a strong one on something it could buy, a holding moving 5% (stocks) / 8%
    (crypto) in a day, SPY down 2% or BTC moving 5%.
  - Commands you send from the ntfy app on your iPhone (CONTROL_TOPIC):
      status | pause | pause stock | pause crypto | resume | kill | run stock | run crypto

Your phone only buzzes when trades have filled, or when something is wrong.

  python daemon.py             # run in the foreground (Ctrl+C to stop)
  python daemon.py --once      # CLOUD MODE: one pass, then exit (GitHub Actions agents.yml runs this
                               # every 30 min). Timers and queued wake-ups live in logs/state.db, which
                               # the workflow saves between runs. Rules bots stay on bots.yml's schedule.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import agent as A
import broker as B
import health as H
import claude_client as CC
import config as C
import market as M
import news as N
import state

ET = ZoneInfo("America/New_York")
HERE = os.path.dirname(os.path.abspath(__file__))
TICK_SEC = 30
STOCK_CHECKINS = ("10:00", "12:30", "15:10")  # New York time
CRYPTO_EVERY_H = 6
EVENT_GAP_MIN = 20          # an agent is woken by events at most every 20 minutes
SPY_DROP, BTC_MOVE = 0.02, 0.05


# ---------- small pure helpers (unit tested) ----------

def due_checkins(now_et: datetime, done: list) -> list:
    """Check-in times that have passed today and haven't run yet."""
    hhmm = now_et.strftime("%H:%M")
    return [t for t in STOCK_CHECKINS if t <= hhmm and t not in done]


def in_hours(spec: str, hour: int) -> bool:
    """'12-20' -> 12 <= hour < 20 (wraps past midnight, e.g. '22-6')."""
    a, b = (int(x) for x in spec.split("-"))
    return a <= hour < b if a <= b else hour >= a or hour < b


def heavy_window(now_et: datetime) -> bool:
    return now_et.weekday() < 5 and in_hours(C.HEAVY_HOURS, now_et.hour)


def parse_command(text: str):
    """'Pause crypto' -> ('pause', 'crypto'). Unknown -> (None, None)."""
    words = text.strip().lower().split()
    if not words or words[0] not in ("status", "pause", "resume", "kill", "run", "help"):
        return None, None
    arg = words[1] if len(words) > 1 else "all"
    return words[0], arg


def news_wakes(rows: list, held: dict, universe_stock: set, universe_crypto: set) -> dict:
    """Which agents a batch of freshly scored headlines should wake, with the reason.

    held: {"stock": set(symbols), "crypto": set(symbols without slash)}.
    """
    wakes = {}
    for r in rows:
        if r["materiality"] < C.NEWS_WAKE_MATERIALITY:
            continue
        sym = r["symbol"]
        if sym in held.get("crypto", set()) or (sym.endswith("USD") and sym[:-3] + "/USD" in universe_crypto):
            agent = "crypto"
        elif sym in held.get("stock", set()) or sym in universe_stock:
            agent = "stock"
        else:
            continue
        is_held = sym in held.get(agent, set())
        critical = r["tag"] in ("hack_or_exploit", "delisting")
        if r.get("source") == "local" and not (is_held or critical):
            continue  # buy ideas need Claude's read, not just the local model's
        if is_held or r["score"] >= 0.6 or critical:
            why = f"{'HELD ' if is_held else ''}{sym} news {r['score']:+.1f} ({r['tag']}): {r['headline'][:120]}"
            wakes.setdefault(agent, []).append(why)
    return wakes


# ---------- the runner ----------

class Runner:
    def __init__(self, once: bool = False):
        self.client = B.trading_client()
        self.once = once  # cloud mode: one pass per process, so timers and queues persist in state.db
        self.last_tick = time.time()
        self.last = {"news": 0.0, "stops": 0.0, "shocks": 0.0, "health": 0.0, "cmd": 0.0}
        self.last_error = 0.0
        self.pending = {"stock": [], "crypto": []}  # event reasons waiting for the next allowed run
        if once:
            self.pending = state.get("pending_wakes") or {"stock": [], "crypto": []}

    # --- plumbing ---
    def every(self, key: str, seconds: float) -> bool:
        last = (state.get(f"every_{key}") or 0.0) if self.once else self.last[key]
        if time.time() - last >= seconds:
            if self.once:
                state.put(f"every_{key}", time.time())
            else:
                self.last[key] = time.time()
            return True
        return False

    def run_bot(self, script: str, extra_env: dict | None = None):
        B.log.info("running %s", script)
        env = {**os.environ, **(extra_env or {})}
        try:
            r = subprocess.run([sys.executable, os.path.join(HERE, script)], cwd=HERE, env=env, timeout=900)
            if r.returncode not in (0, None):
                B.log.warning("%s exited with %s (it sends its own alert)", script, r.returncode)
        except subprocess.TimeoutExpired:
            B.alert(f"{script} took over 15 minutes and was stopped")

    def wake(self, agent: str, reason: str, force: bool = False):
        key = f"last_event_{agent}"
        if not force and time.time() - (state.get(key) or 0) < EVENT_GAP_MIN * 60:
            self.pending[agent].append(reason)
            return
        if agent == "stock" and not self.client.get_clock().is_open and not force:
            self.pending[agent].append(reason)  # wait for the market to open
            return
        reasons = self.pending[agent] + [reason]
        self.pending[agent] = []
        state.put(key, time.time())
        if state.get("paused_all") or state.get(f"paused_{agent}"):
            B.log.info("[%s agent] paused; skipped: %s", agent, reasons)
            return
        try:
            A.run(agent, " | ".join(reasons[-8:]), self.client)
        except SystemExit:
            pass  # safety gate already flattened and alerted

    # --- jobs ---
    def detect_sleep(self):
        gap = time.time() - self.last_tick
        self.last_tick = time.time()
        if gap > 10 * 60:
            B.alert(f"The runner was asleep or offline for {gap / 60:.0f} minutes. Catching up now. "
                    "Check the machine's sleep and power settings (see the guide).")

    def rules_bots(self, clock):
        if state.get("paused_all"):
            return
        now = datetime.now(timezone.utc)
        today_et = state.today()
        mins_to_close = (clock.next_close - clock.timestamp).total_seconds() / 60
        if clock.is_open and 20 <= mins_to_close <= 45 and state.get("ran_stock") != today_et:
            state.put("ran_stock", today_et)
            self.run_bot("stock_bot.py")
            self.run_bot("options_bot.py")
        utc_day = now.date().isoformat()
        if now.strftime("%H:%M") >= "00:10" and state.get("ran_crypto") != utc_day:
            state.put("ran_crypto", utc_day)
            self.run_bot("crypto_bot.py")

    def agent_schedule(self, clock):
        now_et = datetime.now(ET)
        if clock.is_open:
            key = f"checkins_{state.today()}"
            done = state.get(key, [])
            due = due_checkins(now_et, done)
            if due:
                state.put(key, done + due)
                self.wake("stock", f"scheduled check-in {due[-1]} New York time", force=True)
            elif self.pending["stock"]:
                self.wake("stock", "events since last run")
        last_c = state.get("last_crypto_checkin") or 0
        if time.time() - last_c >= CRYPTO_EVERY_H * 3600:
            state.put("last_crypto_checkin", time.time())
            self.wake("crypto", f"scheduled {CRYPTO_EVERY_H}-hour check-in", force=True)
        elif self.pending["crypto"]:
            self.wake("crypto", "events since last run")

    def held(self) -> dict:
        return {a: {p.symbol for p in A.agent_positions(self.client, a)} for a in A.AGENTS}

    def news_job(self):
        if CC.available() and not C.LOCAL_LLM_ENABLED:
            return  # no Claude and no local AI: headlines wait for the next poll
        held = self.held()
        rows = N.poll(held["stock"] | held["crypto"])
        if not rows:
            return
        uni_s = set((state.get("universe") or {}).get("symbols", []))
        uni_c = set(M.crypto_universe(self.client))
        for agent, reasons in news_wakes(rows, held, uni_s, uni_c).items():
            for r in reasons[:-1]:
                self.pending[agent].append(r)
            self.wake(agent, reasons[-1])

    def shocks(self, clock):
        held = self.held()
        syms = ([*held["stock"], "SPY"] if clock.is_open else []) + [A.to_order_symbol("crypto", s) for s in held["crypto"]] + ["BTC/USD"]
        live = M.live_prices(syms)
        seen_key = f"shocks_{state.today()}"
        seen = set(state.get(seen_key, []))
        for sym, (price, chg) in live.items():
            crypto = "/" in sym
            agent = "crypto" if crypto else "stock"
            limit = C.CRYPTO_SHOCK_PCT if crypto else C.STOCK_SHOCK_PCT
            tag = f"{sym}:{'up' if chg > 0 else 'down'}"
            if sym == "SPY":
                hit = chg <= -SPY_DROP
            elif sym == "BTC/USD":
                hit = abs(chg) >= BTC_MOVE
            else:
                hit = abs(chg) >= limit
            if hit and tag not in seen:
                seen.add(tag)
                state.put(seen_key, sorted(seen))
                self.wake(agent, f"{sym} is {chg:+.1%} today (price {price:,.2f})")

    def commands(self):
        if not C.CONTROL_TOPIC:
            return
        since = state.get("cmd_since") or ("35m" if self.once else "2m")
        url = f"https://ntfy.sh/{C.CONTROL_TOPIC}/json?poll=1&since={since}"
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                lines = r.read().decode().splitlines()
        except Exception as e:
            B.log.warning("command poll failed: %s", e)
            return
        for line in lines:
            msg = json.loads(line)
            if msg.get("event") != "message":
                continue
            state.put("cmd_since", msg["id"])
            self.handle(*parse_command(msg.get("message", "")))

    def handle(self, cmd, arg):
        if cmd is None:
            return
        B.log.info("phone command: %s %s", cmd, arg)
        if cmd == "status":
            B.notify(self.status_text(), "Status", "low")
        elif cmd == "pause":
            state.put("paused_all" if arg == "all" else f"paused_{arg}", True)
            B.notify(f"Paused {arg}. Stop-losses still run. Send 'resume' to restart.", "Paused")
        elif cmd == "resume":
            for k in ("paused_all", "paused_stock", "paused_crypto"):
                state.put(k, False)
            B.notify("Resumed. Bots and agents are trading again.", "Resumed")
        elif cmd == "kill":
            state.put("paused_all", True)
            A.flatten_all(self.client, "kill command from phone")
            for bot in ("options_bot.py", "stock_bot.py", "crypto_bot.py"):
                self.run_bot(bot, {"KILL_SWITCH": "true", "FORCE_RUN": "1"})
            B.notify("Sold everything the bots and agents hold and paused all trading. "
                     "Send 'resume' to restart.", "KILLED", "high")
        elif cmd == "run" and arg in A.AGENTS:
            self.wake(arg, "you asked from your phone", force=True)
        elif cmd == "help":
            B.notify("status | pause [stock|crypto] | resume | kill | run stock | run crypto", "Commands", "low")

    def status_text(self) -> str:
        acct = self.client.get_account()
        eq, last = float(acct.equity), float(acct.last_equity or acct.equity)
        lines = [f"Equity ${eq:,.2f} ({eq - last:+,.2f} today), cash ${float(acct.cash):,.2f}"]
        for a in A.AGENTS:
            pos = A.agent_positions(self.client, a)
            j = state.journal(a, 1)
            lines.append(f"{a} agent: {len(pos)} positions" + (f", last run {j[0]['ts'][11:16]} UTC" if j else ""))
        if CC.use_pro():
            lines.append(f"Claude (Pro plan): {state.calls_today()} calls today" + (f", {CC.available()}" if CC.available() else ""))
        else:
            lines.append(f"Claude cost: ${state.spend_today():.2f} today, ${state.spend_month():.2f} this month")
        if not self.once:
            lines.append(H.snapshot())  # the home server's temps/disk; meaningless in the cloud
        flags = [k for k in ("paused_all", "paused_stock", "paused_crypto") if state.get(k)]
        if flags or C.KILL_SWITCH:
            lines.append("PAUSED: " + ", ".join(flags + (["KILL_SWITCH env"] if C.KILL_SWITCH else [])))
        return "\n".join(lines)

    def daily_summary(self, clock):
        if not C.DAILY_SUMMARY:
            return
        now_et = datetime.now(ET)
        if now_et.strftime("%H:%M") >= "16:15" and state.get("summary_day") != state.today():
            state.put("summary_day", state.today())
            B.notify(self.status_text(), "Daily summary", "low")

    def daily_jobs(self, clock):
        now_et = datetime.now(ET)
        today = state.today()
        if (C.OPTIONS_RECORDER and now_et.weekday() < 5 and not clock.is_open and now_et.hour == 16
                and state.get("options_recorded") != today):
            state.put("options_recorded", today)
            import datastore
            datastore.record_options(C.OPTIONS_UNDERLYING)
        if heavy_window(now_et) and state.get("scorecard_day") != today:
            state.put("scorecard_day", today)
            import scorecard
            N.calibrate()
            card = scorecard.build()
            if now_et.weekday() == 4:  # Friday
                B.notify(scorecard.text(card), "Weekly scorecard", "low")

    def ping_health(self):
        if C.HEALTHCHECK_URL:
            try:
                urllib.request.urlopen(C.HEALTHCHECK_URL, timeout=10)
            except Exception as e:
                B.log.warning("health ping failed: %s", e)

    # --- main loop ---
    def tick(self):
        self.detect_sleep()
        if self.every("health", 300):
            self.ping_health()
            H.check()
        if self.every("cmd", 30):
            self.commands()
        clock = self.client.get_clock()
        self.rules_bots(clock)
        if self.every("stops", 60):
            A.enforce_stops(self.client)
        quiet = in_hours(C.QUIET_HOURS, datetime.now(ET).hour)
        if self.every("news", C.NEWS_POLL_MIN * 60 * (2 if quiet else 1)):  # fewer GPU bursts at night
            self.news_job()
        if self.every("shocks", 300):
            self.shocks(clock)
        self.agent_schedule(clock)
        self.daily_jobs(clock)
        self.daily_summary(clock)

    def tick_once(self):
        """Cloud mode: everything except the rules bots (bots.yml runs those on its own schedule),
        server health and the home-server data jobs. Stop-losses are checked each run, so in the
        cloud they act within ~30 minutes instead of 1; the agent's own exits still apply."""
        try:
            self.commands()
            clock = self.client.get_clock()
            A.enforce_stops(self.client)
            if self.every("news", C.NEWS_POLL_MIN * 60 - 120):  # every run, minus scheduler jitter
                self.news_job()
            self.shocks(clock)
            self.agent_schedule(clock)
            self.daily_summary(clock)
        finally:
            state.put("pending_wakes", self.pending)
            state.put("last_cloud_tick", datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def loop(self):
        B.notify("Runner started. You'll hear from me when trades fill or something needs you.", "Runner online", "low")
        while True:
            try:
                self.tick()
            except KeyboardInterrupt:
                raise
            except SystemExit:
                pass  # a safety gate already acted and alerted
            except Exception as e:
                B.log.exception("tick failed")
                if time.time() - self.last_error > 3600:  # at most one error push per hour
                    self.last_error = time.time()
                    B.alert(f"Runner error (it keeps running): {type(e).__name__}: {str(e)[:200]}")
            time.sleep(TICK_SEC)


def run_once():
    try:
        Runner(once=True).tick_once()
    except SystemExit:
        pass  # a safety gate already acted and alerted
    except Exception as e:
        B.log.exception("cloud tick failed")
        last = state.get("last_error_alert") or 0
        if time.time() - last > 3600:  # at most one error push per hour
            state.put("last_error_alert", time.time())
            B.alert(f"Cloud run error: {type(e).__name__}: {str(e)[:200]}")
        raise


if __name__ == "__main__":
    if "--once" in sys.argv:
        run_once()
    else:
        Runner().loop()
