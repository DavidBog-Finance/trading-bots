"""Turn one typed or pasted flow alert into fields. Pure functions, no network.

Accepted, in any order after the ticker (case does not matter):
    AAPL 12/20 230C ask $250K sweep
    $TSLA 11/28 350 put bid 1.2M repeated
    NVDA 12/19/2026 150C 🛍️ 512k vol>oi on 11/18 guess bull
The first date is the expiry. "on 11/18" (or "on 2026-11-18") is the day the alert happened; use it
for delayed (free) flow, otherwise today is used. "guess bull" / "guess bear" records your own read."""
import re
from dataclasses import dataclass, field
from datetime import date

_TICKER = re.compile(r"^\$?([A-Z]{1,5}(?:\.[A-Z])?)$")
_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})(?:/(\d{2}|\d{4}))?$|^(\d{4})-(\d{2})-(\d{2})$")
_STRIKE_CP = re.compile(r"^\$?(\d+(?:\.\d+)?)([CP])$")
_NUMBER = re.compile(r"^\$?(\d+(?:\.\d+)?)$")
_PREMIUM = re.compile(r"^\$?(\d+(?:\.\d+)?)([KM])$")
_ASK = {"ask", "a", "🛍️", "🛍", "aa", "above-ask", "above_ask"}
_BID = {"bid", "b", "🦴", "bb", "below-bid", "below_bid"}
_TAGS = {"sweep": "sweep", "sweeps": "sweep", "repeated": "repeated", "repeat": "repeated", "hits": "repeated",
         "multi-leg": "multi-leg", "multileg": "multi-leg", "spread": "multi-leg", "block": "block",
         "split": "split", "vol>oi": "vol>oi", "opening": "opening", "late": "late", "oseq": "oseq",
         "golden": "golden", "floor": "floor"}


@dataclass
class Alert:
    symbol: str
    expiry: date | None = None
    strike: float | None = None
    kind: str = ""            # "call" or "put"
    side: str = ""            # "ask", "bid" or "" (unknown / mid)
    premium: float = 0.0      # dollars
    tags: list = field(default_factory=list)
    alert_day: date | None = None
    guess: str = ""           # "bull" / "bear" / ""
    text: str = ""

    @property
    def direction(self) -> str:
        return direction(self.kind, self.side)

    def short(self) -> str:
        bits = [self.symbol]
        if self.expiry:
            bits.append(f"{self.expiry.month}/{self.expiry.day}")
        if self.strike is not None and self.kind:
            bits.append(f"{self.strike:g}{self.kind[0].upper()}")
        if self.side:
            bits.append(self.side)
        if self.premium:
            bits.append(money(self.premium))
        bits += self.tags
        return " ".join(bits)


def direction(kind: str, side: str) -> str:
    """Call bought at the ask = bullish, put bought at the ask = bearish. On the bid it is usually
    someone SELLING, so the read flips (but it could also be a holder closing). Unknown side = no read."""
    if kind not in ("call", "put") or side not in ("ask", "bid"):
        return "unclear"
    bullish = (kind == "call") == (side == "ask")
    return "bullish" if bullish else "bearish"


def money(x: float) -> str:
    if x >= 1_000_000:
        return f"${x / 1_000_000:.1f}M"
    if x >= 1_000:
        return f"${x / 1_000:.0f}K"
    return f"${x:.0f}"


def _date(tok: str, today: date) -> date | None:
    m = _DATE.match(tok)
    if not m:
        return None
    try:
        if m.group(4):
            return date(int(m.group(4)), int(m.group(5)), int(m.group(6)))
        mo, d, y = int(m.group(1)), int(m.group(2)), m.group(3)
        if y:
            year = int(y) + (2000 if len(y) == 2 else 0)
            return date(year, mo, d)
        return date(today.year, mo, d)
    except ValueError:
        return None


def parse(text: str, today: date) -> Alert:
    """Raises ValueError with a plain-English reason if there is no ticker or no call/put."""
    raw = " ".join(text.replace(",", " ").split())
    toks = raw.split(" ")
    if not toks or not raw:
        raise ValueError("empty alert")
    m = _TICKER.match(toks[0].upper())
    if not m:
        raise ValueError(f"start with the ticker, e.g. 'AAPL 12/20 230C ask $250K' (got '{toks[0]}')")
    a = Alert(symbol=m.group(1), text=raw)
    pending_number = None
    i = 1
    while i < len(toks):
        tok, up, low = toks[i], toks[i].upper(), toks[i].lower()
        nxt = toks[i + 1].lower() if i + 1 < len(toks) else ""
        if low in ("on", "date", "day") and nxt:
            d = _date(toks[i + 1], today)
            if d:
                a.alert_day = d
                i += 2
                continue
        if low == "guess" and nxt[:4] in ("bull", "bear"):
            a.guess = nxt[:4]
            i += 2
            continue
        d = _date(tok, today)
        if d and a.expiry is None:
            # an expiry without a year that is already well in the past means next year (12/19 typed in Dec for Jan)
            if tok.count("/") == 1 and (today - d).days > 180:
                d = d.replace(year=d.year + 1)
            a.expiry = d
        elif (s := _STRIKE_CP.match(up)) and not a.kind:
            a.strike, a.kind = float(s.group(1)), "call" if s.group(2) == "C" else "put"
        elif low in ("call", "calls", "put", "puts") and not a.kind:
            a.kind = "call" if low.startswith("call") else "put"
            if pending_number is not None and a.strike is None:
                a.strike = pending_number
        elif low in _ASK:
            a.side = "ask"
        elif low in _BID:
            a.side = "bid"
        elif (p := _PREMIUM.match(up)):
            a.premium = float(p.group(1)) * (1_000 if p.group(2) == "K" else 1_000_000)
        elif (n := _NUMBER.match(tok)):
            val = float(n.group(1))
            if a.strike is None and not a.kind:
                pending_number = val
            elif val >= 1000 and not a.premium:
                a.premium = val
        elif low in _TAGS and _TAGS[low] not in a.tags:
            a.tags.append(_TAGS[low])
        i += 1
    if not a.kind:
        raise ValueError("say whether it is a call or a put, e.g. '230C' or '230 call'")
    return a
