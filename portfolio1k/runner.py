"""The Portfolio 1k loop. One program for every phase:

  python -m portfolio1k run                     # always-on computer: runs forever, sleeps when the market is shut
  python -m portfolio1k run --max-minutes 340   # GitHub Actions: one long run inside a job, then exits
  python -m portfolio1k status | scorecard | once

Every LOOP_SEC (15 s) during market hours it: reads phone commands, manages the open trade
(stop, take-profits, setup stop, time stop), and if no trade is open looks for a setup.
"""
import json
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import broker as B
import config as C
import news as N
import state as global_state
from portfolio1k import brain, data, rules as R, settings as S, store, trader

ET = ZoneInfo("America/New_York")


def now_et() -> datetime:
    return datetime.now(ET)


def occ_root(symbol: str) -> str:
    i = 0
    while i < len(symbol) and symbol[i].isalpha():
        i += 1
    return symbol[:i]


def fmt_money(x: float) -> str:
    return f"-${-x:,.2f}" if x < 0 else f"${x:,.2f}"


class Runner:
    def __init__(self, client=None):
        self.client = client or B.trading_client()
        self.st = store.state()
        self.bars, self.daily = {}, {}
        self.last_cmd = 0.0
        self.last_news = 0.0

    # ---------- plumbing ----------
    def save(self):
        store.save_state(self.st)

    def position(self) -> dict | None:
        return store.get("position")

    def set_position(self, p: dict | None):
        store.put("position", p)

    def account(self):
        a = self.client.get_account()
        if a.trading_blocked or a.account_blocked:
            raise RuntimeError("Alpaca account is blocked from trading")
        return a

    def deposits(self) -> float:
        cached = store.get("deposits")
        if cached and cached.get("day") == date.today().isoformat():
            return cached["value"]
        v = B.net_deposits(self.client)
        store.put("deposits", {"day": date.today().isoformat(), "value": v})
        return v

    def notify(self, msg: str, title: str, prio: str = "default"):
        store.event(title, msg)
        B.notify(msg, f"1k {title}", prio)

    def problem(self, msg: str):
        self.st["problem_day"] = now_et().date().isoformat()
        store.event("problem", msg)
        B.alert(f"[1k] {msg}")

    # ---------- rebuilding memory from Alpaca ----------
    def startup(self):
        """Run once when the program starts. GitHub's cache can be evicted or roll back to an older copy,
        so Alpaca (the broker) is the source of truth: adopt a position the memory lost, close one that is
        gone, fix a quantity, and rebuild today's and this week's trade counts from Alpaca's order history."""
        if C.DRY_RUN:
            return
        try:
            self.roll(now_et(), float(self.account().equity))
            self._reconcile()
            self._rebuild_counts(now_et())
        except Exception as e:
            B.log.warning("[1k] startup check with Alpaca failed: %s", e)

    def _held_options(self) -> dict:
        """Long option positions on the watchlist, skipping any root that also has a short leg (a spread
        from another bot is never adopted)."""
        held, short_roots = {}, set()
        for pos in self.client.get_all_positions():
            info = R.parse_occ(pos.symbol)
            if not info:
                continue
            q = int(float(pos.qty))
            if q < 0:
                short_roots.add(info["root"])
            elif info["root"] in S.WATCHLIST:
                held[pos.symbol] = (q, float(pos.avg_entry_price or 0))
        return {s: v for s, v in held.items() if R.parse_occ(s)["root"] not in short_roots}

    def _reconcile(self):
        held = self._held_options()
        local = self.position()
        action, sym = R.reconcile(local, {s: q for s, (q, _) in held.items()})
        if action == "resize":
            old = local["qty"]
            local["qty"] = held[sym][0]
            self.set_position(local)
            self.problem(f"Memory said {old} {sym}, Alpaca holds {local['qty']}. Using Alpaca's number.")
        elif action == "adopt":
            q, entry = held[sym]
            info = R.parse_occ(sym)
            stop = round(entry * (1 - S.STANDARD_STOP), 2)
            opened = datetime.now(timezone.utc).isoformat(timespec="seconds")
            review = {"take": True, "grade": "none", "reason": "recovered from Alpaca after the memory was lost"}
            tid = store.open_trade(opened=opened, symbol=sym, underlying=info["root"], setup="recovered",
                                   direction=info["direction"], tier="standard", qty=q, entry=entry, cost=entry * q * 100,
                                   equity_before=float(self.account().equity), claude=json.dumps(review), feed=S.OPTION_FEED)
            self.set_position({"trade_id": tid, "symbol": sym, "root": info["root"], "direction": info["direction"],
                               "setup": "recovered", "tier": "standard", "qty": q, "qty_open": q, "entry": entry,
                               "stop": stop, "stop_id": trader.open_stop_for(self.client, sym), "tp1_done": False,
                               "high": entry, "level": None, "expiry": info["expiry"].isoformat(),
                               "cost": entry * q * 100, "realized": 0.0, "opened": opened})
            self.notify(f"Memory was missing an open trade, so I took it back from Alpaca: {q} {sym} bought at "
                        f"${entry:.2f}. Managing it with the normal rules (stop ${stop:.2f}).", "Recovered trade")
        # "gone" is handled by manage() on the first tick, using the real closing fill price

    def _rebuild_counts(self, now: datetime):
        day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week0 = day0 - timedelta(days=now.weekday())
        orders = trader.recent_orders(self.client, week0.astimezone(timezone.utc))
        if not orders:
            return
        st = self.st
        today, wk = now.date().isoformat(), R.iso_week(now.date())
        n_day = R.bot_buys_since(orders, day0.astimezone(timezone.utc).isoformat())
        n_week = R.bot_buys_since(orders, week0.astimezone(timezone.utc).isoformat())
        if st.get("day") == today and n_day > st.get("trades_day", 0):
            st["trades_day"] = n_day
        if st.get("week") == wk and n_week > st.get("trades_week", 0):
            st["trades_week"] = n_week
        self.save()

    # ---------- day bookkeeping ----------
    def roll(self, now: datetime, equity: float):
        today = now.date().isoformat()
        if self.st.get("day") != today:
            self.st.update(day=today, day_start_equity=equity, trades_day=0, seen=[])
        wk = R.iso_week(now.date())
        if self.st.get("week") != wk:
            self.st.update(week=wk, week_start_equity=equity, trades_week=0)
        self.save()

    def prep(self, now: datetime):
        """Once a day from 08:00 New York: earnings calendar, daily closes, Claude's game plan."""
        today = now.date().isoformat()
        p = store.get("prep") or {}
        if p.get("day") == today or now.hour < 8:
            return
        earn = data.earnings_dates(S.WATCHLIST, now.date())
        plan = brain.game_plan(S.WATCHLIST, earn) if S.USE_CLAUDE and not brain.available() else None
        store.put("prep", {"day": today, "earnings": earn, "plan": plan})
        if plan:
            store.event("plan", plan.get("summary", ""))
        B.log.info("[1k] prep done: earnings %s; plan: %s", earn, (plan or {}).get("summary", "none")[:200])

    # ---------- phone commands ----------
    def commands(self):
        if not C.CONTROL_TOPIC or time.time() - self.last_cmd < 30:
            return
        self.last_cmd = time.time()
        since = store.get("cmd_since") or "10m"
        try:
            with urllib.request.urlopen(f"https://ntfy.sh/{C.CONTROL_TOPIC}/json?poll=1&since={since}", timeout=10) as r:
                lines = r.read().decode().splitlines()
        except Exception as e:
            B.log.warning("[1k] command poll failed: %s", e)
            return
        for line in lines:
            msg = json.loads(line)
            if msg.get("event") != "message":
                continue
            store.put("cmd_since", msg["id"])
            age = time.time() - float(msg.get("time") or time.time())
            cmd, problem = R.parse_command(msg.get("message") or "", S.CONTROL_PIN, age)
            if problem == "missing or wrong PIN":
                B.log.warning("[1k] phone command '%s' ignored: missing or wrong PIN", cmd)
                B.notify(f"Ignored '{cmd}': it needs your PIN, e.g. '{cmd} 1k <PIN>'.", "1k Command ignored", "default")
            elif problem:
                B.log.info("[1k] phone message ignored (%s)", problem)
            else:
                self.handle(cmd)

    def handle(self, cmd: str):
        B.log.info("[1k] phone command: %s", cmd)
        if cmd == "status":
            B.notify(self.status_text(), "1k Status", "low")
        elif cmd == "pause":
            self.st["paused"] = True
            self.notify("No new trades. The open trade (if any) is still managed. Send 'resume 1k'.", "Paused")
        elif cmd == "resume":
            self.st.update(paused=False, killed=False)
            self.notify("Trading again.", "Resumed")
        elif cmd == "close":
            if self.position():
                self.exit(self.position(), R.Exit(self.position()["qty"], "you asked from your phone", "manual"))
            else:
                B.notify("No open trade.", "1k", "low")
        elif cmd == "kill":
            self.kill("kill command from your phone")
        elif cmd in ("nofull", "full"):
            self.st["full_enabled"] = cmd == "full"
            self.notify(f"Full-port trades {'ON' if cmd == 'full' else 'OFF'}.", "Setting")
        elif cmd == "help":
            B.notify("status | help (no PIN needed)\npause | resume | close | kill | nofull | full"
                     + (" (add your PIN, e.g. 'kill 1k 1234')" if S.CONTROL_PIN else ""), "1k Commands", "low")
        self.save()

    def kill(self, why: str):
        p = self.position()
        if p:
            self.exit(p, R.Exit(p["qty"], why, "kill"))
        self.st["killed"] = True
        self.save()
        self.notify(f"{why}. Sold everything and stopped. Send 'resume 1k' to restart.", "KILLED", "high")

    # ---------- managing the open trade ----------
    def manage(self, now: datetime, next_day: date):
        p = self.position()
        if not p:
            return
        # 1) Did the stop at Alpaca fill (maybe while this program was down)?
        q, px = trader.stop_filled(self.client, p.get("stop_id"))
        if q:
            self.finish(p, px, q, R.Exit(q, "stop filled at Alpaca", "stop"))
            return
        held = self.held_qty(p["symbol"])
        if held == 0 and not C.DRY_RUN:
            px = self.closing_fill(p) or p.get("last_mark", p["entry"])
            self.problem(f"{p['symbol']} is no longer in the account but this program did not sell it. "
                         f"Recording it closed at ${px:.2f}.")
            self.finish(p, px, p["qty"], R.Exit(p["qty"], "position gone", "unknown"))
            return
        # 2) Make sure a stop for the full quantity is resting at Alpaca (DAY orders expire every evening)
        active, stop_qty = trader.stop_state(self.client, p.get("stop_id"))
        if active and stop_qty is not None and stop_qty != p["qty"]:
            trader.cancel(self.client, p.get("stop_id"))
            active = False
        if not active:
            p["stop_id"] = trader.place_stop(self.client, p["symbol"], occ_root(p["symbol"]), p["qty"], p["stop"])
            if not p["stop_id"] and not p.get("stop_alerted"):
                p["stop_alerted"] = True
                self.problem(f"No stop order is resting at Alpaca for {p['symbol']} and placing one failed. "
                             f"This program still watches the -{R.stop_pct(p['tier']):.0%} stop every {S.LOOP_SEC:.0f} s "
                             f"while it runs, but nothing protects the trade if it stops. Check the Alpaca app.")
            elif p["stop_id"] and p.get("stop_alerted"):
                p["stop_alerted"] = False
                self.notify(f"Stop order for {p['symbol']} is resting at Alpaca again (${p['stop']:.2f}).", "Stop restored")
            self.set_position(p)
        # 3) Price-based exits
        bid, ask, ts = data.option_quote(p["symbol"])
        fresh = ts is not None and (datetime.now(timezone.utc) - ts).total_seconds() <= S.QUOTE_MAX_AGE_SEC
        if bid <= 0 or ask <= 0:
            return
        mark = (bid + ask) / 2
        p["last_mark"] = mark
        p["high"] = max(p.get("high", p["entry"]), mark)
        self.set_position(p)
        under = self.bars.get(p["root"])
        last_close = float(under["close"].iloc[-1]) if under is not None and len(under) else None
        ex = R.exit_decision({**p, "expiry": date.fromisoformat(p["expiry"])}, mark, now, last_close, next_day)
        if ex and (fresh or ex.kind in ("time", "expiry", "setup_stop")):
            self.exit(p, ex, bid, ask)

    def held_qty(self, symbol: str) -> int:
        try:
            return int(float(self.client.get_open_position(symbol).qty))
        except Exception:
            return 0

    def exit(self, p: dict, ex: R.Exit, bid: float | None = None, ask: float | None = None):
        if bid is None or ask is None:
            try:
                bid, ask, _ = data.option_quote(p["symbol"])
            except Exception:
                bid, ask = p.get("last_mark", p["entry"]), 0.0
        trader.cancel(self.client, p.get("stop_id"))
        q, px = trader.stop_filled(self.client, p.get("stop_id"))  # filled while we were cancelling?
        if q:
            self.finish(p, px, q, R.Exit(q, "stop filled at Alpaca", "stop"))
            return
        qty = min(ex.qty, p["qty"])
        px = trader.sell(self.client, p["symbol"], qty, bid, ask or 0.0, urgent=R.exit_is_urgent(ex.kind))
        fc = R.fill_cost("sell", bid or 0, ask or 0, px)
        if fc["slip"] is not None:
            p["exit_slip_q"] = p.get("exit_slip_q", 0.0) + fc["slip"] * qty
            p["exit_slip_pct_q"] = p.get("exit_slip_pct_q", 0.0) + fc["slip_pct"] * qty
            p["exit_q"] = p.get("exit_q", 0) + qty
            p["exit_bid"], p["exit_ask"] = bid, ask
        if qty < p["qty"]:   # take profit 1 on 2+ contracts: keep the rest with a breakeven stop
            p["realized"] = p.get("realized", 0.0) + (px - p["entry"]) * qty * 100
            p["qty"] -= qty
            p["tp1_done"] = True
            p["stop"] = p["entry"]
            p["stop_id"] = trader.place_stop(self.client, p["symbol"], occ_root(p["symbol"]), p["qty"], p["stop"])
            self.set_position(p)
            self.notify(f"Sold {qty} {p['symbol']} at ${px:.2f} ({px / p['entry'] - 1:+.0%}): {ex.reason}. "
                        f"Holding {p['qty']}, stop moved to breakeven ${p['entry']:.2f}.", "SELL (half)")
            return
        if ex.kind == "tp1":
            p["tp1_done"] = True
        self.finish(p, px, qty, ex)

    def closing_fill(self, p: dict) -> float | None:
        """Average price of the sells that closed this contract while the program was not watching."""
        since = datetime.fromisoformat(p.get("opened") or (datetime.now(timezone.utc) - timedelta(days=7)).isoformat())
        rows = [o for o in trader.recent_orders(self.client, since)
                if o["symbol"] == p["symbol"] and o["side"] == "sell" and o["status"] == "filled" and o["qty"] > 0]
        q = sum(o["qty"] for o in rows)
        return sum(o["qty"] * o["price"] for o in rows) / q if q else None

    def finish(self, p: dict, px: float, qty: int, ex: R.Exit):
        pnl = p.get("realized", 0.0) + (px - p["entry"]) * qty * 100
        slip = None
        if ex.kind == "stop" and not p.get("tp1_done"):
            planned = -R.stop_pct(p["tier"])
            slip = round((planned - (px / p["entry"] - 1)) * 100, 1)
        result = "win" if pnl > 0 else "loss" if pnl < 0 else "flat"
        eq_n = p.get("exit_q", 0)
        store.close_trade(p["trade_id"], closed=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                          exit=px, pnl=round(pnl, 2), kind=ex.kind, slip=slip, reason=ex.reason,
                          exit_bid=p.get("exit_bid"), exit_ask=p.get("exit_ask"),
                          exit_slip=round(p["exit_slip_q"] / eq_n, 4) if eq_n else None,
                          exit_slip_pct=round(p["exit_slip_pct_q"] / eq_n, 4) if eq_n else None,
                          fees=round(S.FEE_PER_CONTRACT_SIDE * 2 * p.get("qty_open", qty), 2))
        self.st = R.after_close(self.st, result, now_et().date())
        self.save()
        self.set_position(None)
        eq = float(self.account().equity) if not C.DRY_RUN else 0
        self.notify(f"Closed {p['symbol']} x{qty} at ${px:.2f}: {ex.reason}.\nTrade P&L {fmt_money(pnl)} "
                    f"({pnl / p['cost']:+.0%}). Account {fmt_money(eq)}."
                    + (f"\nPaused until {self.st['pause_until']}: {self.st['pause_why']}." if self.st.get("pause_until", "") > now_et().date().isoformat() else ""),
                    "SELL")

    # ---------- looking for a trade ----------
    def scan(self, now: datetime, acct, equity: float):
        st = self.st
        why = R.entry_block(st, now, equity, self.deposits(), bool(self.position()), S.FOMC_DAYS)
        if why:
            return why
        prep = store.get("prep") or {}
        earn = prep.get("earnings", {})
        avoid = set((prep.get("plan") or {}).get("avoid", []))
        spy = self.bars.get("SPY")
        if spy is None or not data.last_trade_age_ok(spy, now):
            return "SPY data stale"
        news_rows = global_state.recent_scores(hours=2, min_materiality=S.NEWS_MIN_MATERIALITY)
        sigs = []
        for sym in S.WATCHLIST:
            df = self.bars.get(sym)
            if df is None or not data.last_trade_age_ok(df, now):
                continue
            if sym in avoid or earn.get(sym) == "unknown":
                continue
            for sig in (R.orb_signal(sym, df, now),
                        R.pullback_signal(sym, df, self.daily.get(sym, []), now) if sym in self.daily else None,
                        R.news_signal(sym, df, R.strong_news([r for r in news_rows if r["symbol"] == sym],
                                                             datetime.now(timezone.utc)), now)):
                if sig:
                    key = f"{sym}|{sig.setup}|{sig.direction}|{df.index[-1].isoformat()}"
                    if key not in st.get("seen", []):
                        st.setdefault("seen", []).append(key)
                        sigs.append(sig)
        self.save()
        for sig in R.merge_signals(sigs):
            spy_ok = R.spy_agrees(spy, sig.direction)
            if not spy_ok:
                store.event("skip", f"{sig.symbol} {sig.setup} {sig.direction}: SPY disagrees")
                continue
            if self.try_trade(sig, now, acct, equity, earn):
                return None
        return None

    def try_trade(self, sig: R.Signal, now: datetime, acct, equity: float, earn: dict) -> bool:
        df = self.bars[sig.symbol]
        spot = float(df["close"].iloc[-1])
        cash = float(acct.cash)
        today = now.date()
        e = earn.get(sig.symbol)
        cands = data.candidates(self.client, sig.symbol, sig.direction, spot, today)
        full_budget = R.budget_for("full", equity, cash)
        pick, reasons = R.pick_contract(cands, sig.symbol, today, date.fromisoformat(e) if e else None,
                                        R.budget_for("standard", equity, cash))
        if not pick:
            store.event("skip", f"{sig.symbol} {sig.setup} {sig.direction}: no contract passed ({reasons})")
            return False
        review = None
        if S.USE_CLAUDE:
            review = brain.review({
                "setup": sig.setup, "also": sig.extra.get("also", []), "direction": sig.direction, "symbol": sig.symbol,
                "why": sig.note, "underlying_last_12_bars_5min": df.tail(12).round(2).reset_index().astype(str).values.tolist(),
                "vwap": round(float(R.vwap(df).iloc[-1]), 2), "spy_last": round(float(self.bars["SPY"]["close"].iloc[-1]), 2),
                "spy_vwap": round(float(R.vwap(self.bars["SPY"]).iloc[-1]), 2),
                "news": [r for r in global_state.recent_scores(sig.symbol, hours=24)][:8],
                "game_plan": (store.get("prep") or {}).get("plan"),
                "contract": {k: pick[k] for k in ("symbol", "expiry", "strike", "delta", "bid", "ask", "oi")},
                "account_equity": round(equity, 2), "time_new_york": now.strftime("%H:%M"),
                "recent_trades": store.closed_trades()[-5:]})
        if review is None:
            if sig.setup == "news" and not sig.extra.get("also"):
                store.event("skip", f"{sig.symbol} news setup: Claude unavailable")
                return False
            review = {"take": True, "grade": "A", "reason": "rules only (Claude unavailable)"}
        if not review["take"]:
            store.event("skip", f"{sig.symbol} {sig.setup} {sig.direction}: Claude {review['grade']}: {review['reason']}")
            return False
        tier, tier_why = R.choose_tier(sig, review["grade"], True, self.st, R.iso_week(today))
        budget = full_budget if tier == "full" else R.budget_for("standard", equity, cash)
        qty = R.contracts_for(budget, pick["ask"])
        if qty < 1:
            return False
        filled, px = trader.buy(self.client, pick["symbol"], sig.symbol, qty, pick["bid"], pick["ask"])
        if not filled:
            store.event("skip", f"{pick['symbol']} not filled within 60 s")
            return False
        stop = round(px * (1 - R.stop_pct(tier)), 2)
        stop_id = trader.place_stop(self.client, pick["symbol"], sig.symbol, filled, stop)
        cost = px * filled * 100
        opened = datetime.now(timezone.utc).isoformat(timespec="seconds")
        fc = R.fill_cost("buy", pick["bid"], pick["ask"], px)
        tid = store.open_trade(opened=opened, symbol=pick["symbol"],
                               underlying=sig.symbol, setup="+".join([sig.setup, *sig.extra.get("also", [])]),
                               direction=sig.direction, tier=tier, qty=filled, entry=px, cost=cost,
                               equity_before=equity, claude=json.dumps(review), feed=S.OPTION_FEED,
                               entry_bid=pick["bid"], entry_ask=pick["ask"], entry_slip=fc["slip"],
                               entry_slip_pct=fc["slip_pct"], entry_spread_pct=fc["spread_pct"])
        self.set_position({"trade_id": tid, "symbol": pick["symbol"], "root": sig.symbol, "direction": sig.direction,
                           "setup": sig.setup, "tier": tier, "qty": filled, "qty_open": filled, "entry": px, "stop": stop,
                           "stop_id": stop_id, "tp1_done": False, "high": px, "level": sig.level,
                           "expiry": pick["expiry"].isoformat(), "cost": cost, "realized": 0.0, "opened": opened})
        st = self.st
        st["trades_day"] = st.get("trades_day", 0) + 1
        st["trades_week"] = st.get("trades_week", 0) + 1
        if tier == "full":
            wk = R.iso_week(today)
            st["full_count"] = (st.get("full_count", 0) if st.get("full_week") == wk else 0) + 1
            st["full_week"] = wk
        self.save()
        self.notify(f"Bought {filled} {pick['symbol']} ({sig.direction}, {pick['expiry']}, strike {pick['strike']:g}) "
                    f"at ${px:.2f} = {fmt_money(cost)}, {tier.upper()} size.\n"
                    f"Setup: {sig.setup} - {sig.note}\nClaude {review['grade']}: {review['reason']}\n"
                    f"Stop ${stop:.2f} (-{R.stop_pct(tier):.0%}) at Alpaca; take profit +{S.TP1:.0%} / +{S.TP2:.0%}."
                    + (f"\nStandard size because: {tier_why}" if tier == "standard" and review["grade"] == "A+" else ""),
                    "BUY")
        return True

    # ---------- news ----------
    def news(self, now: datetime):
        """Score fresh watchlist headlines with Claude every 5 minutes, 09:00-14:30 New York."""
        if not S.USE_CLAUDE or time.time() - self.last_news < 300 or not R.between(now, "09:00", S.ENTRY_END):
            return
        self.last_news = time.time()
        try:
            arts = [a for a in N.fetch_new_articles() if set(a["symbols"]) & set(S.WATCHLIST)]
            if arts and brain.available() is None and brain.CC.allow("held"):
                for i in range(0, len(arts), N.BATCH):
                    N.score(arts[i:i + N.BATCH])
        except Exception as e:
            B.log.warning("[1k] news scoring failed: %s", e)

    # ---------- reports ----------
    def status_text(self) -> str:
        acct = self.account()
        eq = float(acct.equity)
        dep = self.deposits()
        lines = [f"Equity {fmt_money(eq)} (floor {fmt_money(dep * S.KILL_PCT)}), cash {fmt_money(float(acct.cash))}"]
        p = self.position()
        if p:
            lines.append(f"Open: {p['qty']} {p['symbol']} from ${p['entry']:.2f}, last ${p.get('last_mark', p['entry']):.2f}, "
                         f"stop ${p['stop']:.2f}, {p['tier']}")
        st = self.st
        lines.append(f"Trades today {st.get('trades_day', 0)}/{S.MAX_TRADES_DAY}, week {st.get('trades_week', 0)}/{S.MAX_TRADES_WEEK}")
        flags = [k for k in ("killed", "paused") if st.get(k)]
        if st.get("pause_until", "") > now_et().date().isoformat():
            flags.append(f"paused until {st['pause_until']}")
        if not st.get("full_enabled", S.FULL_PORT_ENABLED):
            flags.append("full port off")
        if flags:
            lines.append("Flags: " + ", ".join(flags))
        trades = store.closed_trades()
        card = R.scorecard(trades, S.REAL_COST_PER_CONTRACT)
        lines.append(scorecard_line(card))
        lines += [x for x in (cost_line(trades), grade_lines(trades)) if x]
        return "\n".join(lines)

    def daily_summary(self, now: datetime):
        today = now.date().isoformat()
        if now.time() < R.hm(S.SUMMARY) or self.st.get("summary_day") == today:
            return
        self.st["summary_day"] = today
        self.save()
        traded = any(t["closed"] and t["closed"][:10] >= (now - timedelta(days=1)).date().isoformat() for t in store.closed_trades())
        if traded or self.st.get("trades_day") or self.st.get("problem_day") == today or now.weekday() == 4:
            title = "Weekly scorecard" if now.weekday() == 4 else "Daily summary"
            card = R.scorecard(store.closed_trades(), S.REAL_COST_PER_CONTRACT)
            extra = ""
            if now.weekday() == 4:
                todo = R.go_live_ready(card, 0)
                extra = "\nGo-live bars still open: " + (", ".join(todo) if todo else "none, all met")
            B.notify(self.status_text() + extra, f"1k {title}", "low")

    # ---------- main ----------
    def tick(self):
        now = now_et()
        self.commands()
        clock = self.client.get_clock()
        acct = self.account()
        equity = float(acct.equity)
        self.roll(now, equity)
        dep = self.deposits()
        if dep > 0 and equity <= dep * S.KILL_PCT and not self.st.get("killed"):
            self.kill(f"Equity {fmt_money(equity)} hit the floor ({S.KILL_PCT:.0%} of {fmt_money(dep)} deposited)")
            return
        self.prep(now)
        if not clock.is_open:
            self.daily_summary(now)
            return
        syms = sorted(set(S.WATCHLIST) | {"SPY"} | ({self.position()["root"]} if self.position() else set()))
        self.bars = data.intraday_bars(syms, now)
        if not self.daily:
            self.daily = data.daily_closes(S.WATCHLIST)
        next_day = clock.next_open.astimezone(ET).date() if clock.next_open.astimezone(ET).date() > now.date() \
            else R.add_trading_days(now.date(), 1)
        self.manage(now, next_day)
        self.news(now)
        why = self.scan(now, acct, equity)
        if why and why != self.st.get("last_block"):
            self.st["last_block"] = why
            B.log.info("[1k] no new entries: %s", why)

    def run(self, max_minutes: float | None = None):
        start = time.time()
        self.startup()
        end_et = None
        if max_minutes:
            today_end = now_et().replace(hour=16, minute=20, second=0, microsecond=0)
            end_et = min(now_et() + timedelta(minutes=max_minutes), today_end)
            clock = self.client.get_clock()
            nxt = clock.next_open.astimezone(ET)
            if now_et() >= today_end or (not clock.is_open and nxt.date() != now_et().date()):
                B.log.info("[1k] no session left today; exiting")
                self.tick_safely()  # still answers phone commands and sends the summary
                return
        B.log.info("[1k] running%s", f" until {end_et:%H:%M} New York" if end_et else " (always on)")
        while True:
            self.tick_safely()
            if end_et and now_et() >= end_et:
                B.log.info("[1k] session slice done after %.0f min", (time.time() - start) / 60)
                return
            time.sleep(S.LOOP_SEC if self.market_open_cached() else 60)

    def market_open_cached(self) -> bool:
        if time.time() - getattr(self, "_clock_t", 0) > 60:
            self._clock_t = time.time()
            try:
                self._clock_open = bool(self.client.get_clock().is_open)
            except Exception:
                self._clock_open = False
        return self._clock_open

    def tick_safely(self) -> bool:
        try:
            self.tick()
            return True
        except Exception as e:
            B.log.exception("[1k] tick failed")
            last = store.get("last_error_alert") or 0
            if time.time() - last > 3600:
                store.put("last_error_alert", time.time())
                self.problem(f"Error (it keeps running; the stop at Alpaca still protects any trade): {type(e).__name__}: {str(e)[:200]}")
            return False


def cost_line(trades: list) -> str:
    """The cost log in one line: what the fills gave up versus the mid, and what that adds up to per year."""
    if not trades:
        return ""
    first = datetime.fromisoformat(trades[0]["opened"]) if trades[0].get("opened") else datetime.now(timezone.utc)
    days = max(1.0, (datetime.now(timezone.utc) - first).total_seconds() / 86400)
    c = R.cost_report(trades, S.FEE_PER_CONTRACT_SIDE, days)
    if not c:
        return ""
    return (f"Costs ({c['trades']} trades, {S.OPTION_FEED} quotes): paid {c['entry_slip_pct']:+.1%} vs mid in, "
            f"{c['exit_slip_pct']:+.1%} out, spread {c['spread_pct']:.1%}; about {fmt_money(c['avg_round_trip'])} per round trip "
            f"x {c['trades_per_year']:.0f} trades/yr = {fmt_money(c['cost_per_year'])}/yr")


def grade_lines(trades: list) -> str:
    """Report card for Claude's grades: does A+ really beat A and B after costs?"""
    card = R.grade_card(trades, S.REAL_COST_PER_CONTRACT)
    if not card:
        return ""
    order = ["A+", "A", "B", "rules only", "none"]
    rows = [f"{g}: {c['trades']} trades, win {c['win_rate']:.0%}, avg {fmt_money(c['avg'])}"
            for g, c in sorted(card.items(), key=lambda kv: order.index(kv[0]) if kv[0] in order else 9)]
    return "Grades: " + "; ".join(rows) + (" (under 20 trades each is mostly luck)" if min(c["trades"] for c in card.values()) < 20 else "")


def scorecard_line(card: dict) -> str:
    if not card["trades"]:
        return "No closed trades yet."
    pf = "inf" if card["profit_factor"] == float("inf") else f"{card['profit_factor']:.2f}"
    slip = f", stop slip {card['avg_stop_slip']:.0f} pts" if card["avg_stop_slip"] is not None else ""
    return (f"{card['trades']} trades, win {card['win_rate']:.0%}, profit factor {pf}, net {fmt_money(card['net'])} "
            f"after est. real costs (paper {fmt_money(card['paper_net'])}), max DD {card['max_drawdown']:.0%}{slip}")
