"""Live order layer for the long straddle. Holdings come only from this book's own orders.

Design (replaces position-based reconciliation, which mixed in manual trades and could not tell a lost
order from one never sent):

- Every order gets a unique tag, written to the ledger ("order_intent") BEFORE it is sent. If that write
  fails, the order is not sent.
- Orders are sent and never waited on. Each frame reads Kite's order book once and matches our orders by
  tag: status, filled quantity, average price. A reply lost in the network, a 5xx, a timeout or a crash
  all resolve the same way, by the tag. "Never placed" is decided only when safe: at once for a clean
  refusal (http 4xx); never for an order that came back with an order id (it is working until the book
  shows it final); UNCERTAIN_LOST_S after the call returned for a network error or 5xx, or after a
  restart for an order whose send outcome was not recorded. A late listing still revives it.
  If the order book can't be read, nothing is decided that frame; after BOOK_ALARM_S it raises
  manual_needed.
- What the book holds per leg = its own filled buys minus its own filled sells, less anything sold by
  hand. Hand trades are read from the same order-book snapshot: today's filled MIS orders in that option
  without one of our tags. They are recomputed every frame (never recorded once and kept), so a stale
  read can't permanently drop a leg. Hand buys never add to what the book sells; hand sells reduce it,
  so the book never sells into a short. Kite's net position is not used at all. Any hand trade in the
  option raises manual_needed and marks the P&L estimated.
- One rule: no new order on a leg while one of ours on that leg is still working. A working order older
  than CANCEL_AFTER_S is cancelled (and read again on the next frames).
- A sell is never written off as "never placed", however long Kite takes to list it (selling again could
  go short); a buy may be, after UNCERTAIN_LOST_S, and the trade then keeps watching until 15:20.
- The stop needs this frame's live bids below the level for STOP_CONFIRM_S without a gap.
- Phases (each change is written to the ledger):
    entering: both buys sent; when neither is working, equal holdings -> holding; unequal -> exiting;
              none -> done (entry failed, nothing held).
    holding : exits on the stop (held legs' bids at stop_loss below their cost) or at exit_at.
    exiting : sell exactly what is held per leg; the next sell for a leg is allowed 5 s, 15 s, 60 s...
              after the previous one was sent (and only once it is final); after MAX_SELLS a "manual_needed" flag is raised and it keeps
              trying every MANUAL_RETRY_S. Done when every leg is at zero and nothing is working.
- From square_off_late (15:10: Zerodha squares F&O MIS off at 15:14, so no order may race it) or on a
  later day: no new orders; working
  orders are cancelled; anything still held is valued at the last bids seen on the trade's own day
  (saved to the ledger at most once a minute; the cost price if none) and the P&L is marked estimated.
  It still counts toward the monthly loss limit.
- P&L comes from the actual fills (and Kite charges on them).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from atlas_lite.kite_charges import kite_nfo_charges

TAG_PREFIX = "lsd"
UNCERTAIN_LOST_S = 120.0  # network error / 5xx on send: decide "never placed" only this long after
BOOK_ALARM_S = 180.0  # order book unreadable this long: manual_needed
MARK_EVERY_S = 60.0
WORKING_ALARM_S = 90.0  # an order still working this long raises manual_needed
STOP_CONFIRM_S = 10.0  # live prices below the stop for this long before real sells (one bad tick isn't enough)
CANCEL_AFTER_S = 30.0
SELL_BACKOFF_S = (0.0, 5.0, 15.0, 60.0, 60.0)
MAX_SELLS = 5
MANUAL_RETRY_S = 120.0
FINAL = frozenset({"COMPLETE", "REJECTED", "CANCELLED", "CANCELED", "NOT_PLACED"})
LEGS = ("ce", "pe")


@dataclass
class Intent:
    tag: str
    leg: str
    side: str  # BUY / SELL
    qty: int
    symbol: str
    sent_at: float
    oid: str = ""
    send: str = ""  # "" (outcome unknown) | ok | refused | uncertain
    returned_at: float = 0.0
    status: str = "SENT"  # SENT until seen in the order book
    filled: int = 0
    avg: float = 0.0
    seen: bool = False
    cancel_at: float = 0.0  # last cancel request (re-sent every CANCEL_AFTER_S while still working)
    logged: tuple = ()  # last (status, filled, avg) written to the ledger (the order book only lists today)

    @property
    def final(self) -> bool:
        return self.status in FINAL


@dataclass
class LiveStraddle:
    day: str
    strike: int
    ce_symbol: str
    pe_symbol: str
    qty: int
    expiry_day: bool
    log: Callable[[dict[str, Any]], dict[str, Any] | None]  # durable ledger append; None = write failed
    phase: str = "entering"
    exit_reason: str = ""
    intents: list[Intent] = field(default_factory=list)
    seq: int = 0
    sells: dict[str, int] = field(default_factory=lambda: {"ce": 0, "pe": 0})
    next_sell_at: dict[str, float] = field(default_factory=lambda: {"ce": 0.0, "pe": 0.0})
    manual_needed: bool = False
    last_bids: dict[str, float] = field(default_factory=dict)
    book_error: str = ""
    book_error_since: float = 0.0
    hand: dict[str, int] = field(default_factory=lambda: {"ce": 0, "pe": 0})  # signed hand net, this frame
    alarms: set = field(default_factory=set)
    stop_since: float = 0.0  # when live prices first went below the stop (0 = not below)
    marked_at: float = 0.0

    # ---- holdings from our own fills ----
    def symbol(self, leg: str) -> str:
        return self.ce_symbol if leg == "ce" else self.pe_symbol

    def own(self, leg: str) -> int:
        """Shares from this book's own fills (buys minus sells)."""
        return sum(i.filled if i.side == "BUY" else -i.filled for i in self.intents if i.leg == leg)

    def held(self, leg: str) -> int:
        """What the book can still sell: its own shares, less any sold by hand (hand buys don't count)."""
        own = self.own(leg)
        return max(0, own - min(own, max(0, -self.hand.get(leg, 0))))

    def _alarm(self, key: str, why: str) -> None:
        if key in self.alarms:
            return
        self.alarms.add(key)
        self.manual_needed = True
        self.log({"event": "manual_needed", "key": key, "why": why})

    def working(self, leg: str | None = None) -> list[Intent]:
        return [i for i in self.intents if not i.final and (leg is None or i.leg == leg)]

    def cost(self, leg: str) -> tuple[float, int]:
        """(average buy price, shares bought) for a leg."""
        q = sum(i.filled for i in self.intents if i.leg == leg and i.side == "BUY")
        v = sum(i.filled * i.avg for i in self.intents if i.leg == leg and i.side == "BUY")
        return (v / q if q else 0.0), q

    # ---- orders ----
    def place(self, client: Any, leg: str, side: str, qty: int) -> bool:
        """Durable intent first, then send. Returns False if the intent could not be written (nothing sent)."""
        self.seq += 1
        tag = f"{TAG_PREFIX}{self.day[5:7]}{self.day[8:10]}{self.seq:02d}{leg}{'b' if side == 'BUY' else 's'}"
        it = Intent(tag=tag, leg=leg, side=side, qty=int(qty), symbol=self.symbol(leg), sent_at=time.time())
        if self.log({"event": "order_intent", "tag": tag, "leg": leg, "side": side, "qty": it.qty,
                     "symbol": it.symbol, "sent_at": it.sent_at, "seq": self.seq}) is None:
            self.seq -= 1
            return False
        self.intents.append(it)
        try:
            it.oid = str(client.place_market(symbol=it.symbol, transaction_type=side, qty=it.qty, tag=tag) or "")
            it.send = "ok" if it.oid else "uncertain"
        except Exception as exc:  # noqa: BLE001 - whatever happened, the order book (by tag) decides
            it.send = "refused" if str(exc).startswith("http_4") else "uncertain"
            self.log({"event": "order_send_error", "tag": tag, "error": str(exc), "send": it.send})
        it.returned_at = time.time()
        self.log({"event": "order_sent", "tag": tag, "order_id": it.oid, "send": it.send,
                  "returned_at": it.returned_at})
        return True

    def refresh(self, client: Any) -> bool:
        """Read the order book once; update our intents by tag. False (decide nothing) if unreadable."""
        try:
            rows = client.list_orders()
        except Exception as exc:  # noqa: BLE001
            now = time.time()
            if not self.book_error_since:
                self.book_error_since = now
            self.book_error = str(exc)
            if now - self.book_error_since >= BOOK_ALARM_S and not self.manual_needed:
                self.manual_needed = True
                self.log({"event": "manual_needed", "why": f"order book unreadable for {BOOK_ALARM_S:.0f}s: {exc}"})
            return False
        self.book_error, self.book_error_since = "", 0.0
        by_tag: dict[str, dict[str, Any]] = {}
        for r in rows:
            tags = [r.get("tag")] + list(r.get("tags") or [])
            for t in tags:
                if t:
                    by_tag[str(t)] = r
        now = time.time()
        ours = {it.tag for it in self.intents}
        for leg in LEGS:  # hand trades: filled MIS orders in this option without one of our tags
            ts = self.symbol(leg).split(":", 1)[-1]
            net = 0
            for r in rows:
                rtags = {str(x) for x in [r.get("tag")] + list(r.get("tags") or []) if x}
                if str(r.get("tradingsymbol") or "") != ts or rtags & ours:
                    continue
                if str(r.get("product") or "MIS").upper() != "MIS":
                    continue
                f = int(float(r.get("filled_quantity") or 0))
                if str(r.get("transaction_type") or "").upper() == "BUY":
                    net += f
                else:  # a hand sell still working counts as sold: it may fill after ours
                    working = str(r.get("status") or "").upper() not in FINAL
                    net -= max(f, int(float(r.get("quantity") or 0))) if working else f
            self.hand[leg] = net
            if net:
                self._alarm(f"hand:{leg}:{net}", f"hand trade in {ts}: net {net:+d} (book sells only its own)")
        for it in self.intents:
            r = by_tag.get(it.tag)
            if r is not None:
                status = str(r.get("status") or "").upper() or "OPEN"
                filled = int(float(r.get("filled_quantity") or 0))
                if it.final and it.status != "NOT_PLACED" and status not in FINAL:
                    continue  # a stale book never moves a final order backwards
                it.seen = True
                it.oid = str(r.get("order_id") or it.oid)
                it.status = status
                it.filled = max(it.filled, filled) if it.status != "NOT_PLACED" else filled
                it.avg = float(r.get("average_price") or it.avg or 0.0)
            elif not it.seen and not it.final and self._never_placed(it, now):
                it.status = "NOT_PLACED"  # never reached the exchange
            if not it.final and now - (it.returned_at or it.sent_at) >= WORKING_ALARM_S:
                self._alarm(f"working:{it.tag}", f"order {it.tag} still working after {WORKING_ALARM_S:.0f}s")
            state = (it.status, it.filled, it.avg)
            if it.final and state != it.logged:  # every change of a final order, a late fill included
                if self.log({"event": "order_final", "tag": it.tag, "status": it.status, "filled": it.filled,
                             "avg": it.avg, "order_id": it.oid}) is not None:
                    it.logged = state
        return True

    @staticmethod
    def _never_placed(it: Intent, now: float) -> bool:
        """Safe to call an unlisted order never placed? Only on a clean refusal, or long after an uncertain
        send. An order that came back with an id is placed: it waits for the book, however slow."""
        if it.send == "refused":
            return True
        if it.send == "ok" or it.side == "SELL":  # a sell is never written off: selling again could go short
            return False
        return it.returned_at > 0 and now - it.returned_at >= UNCERTAIN_LOST_S

    def _cancel_stale(self, client: Any, force: bool = False) -> None:
        """Cancel our orders working longer than CANCEL_AFTER_S (all of them if force), and keep asking
        every CANCEL_AFTER_S until the order book shows them final."""
        now = time.time()
        for it in self.working():
            if not it.oid or now - it.cancel_at < CANCEL_AFTER_S:
                continue
            if force or now - it.sent_at >= CANCEL_AFTER_S:
                it.cancel_at = now
                try:
                    client.cancel(it.oid)
                    self.log({"event": "order_cancel_sent", "tag": it.tag, "order_id": it.oid})
                except Exception:  # noqa: BLE001 - it may have just finished: the book will tell
                    pass

    def _set_phase(self, phase: str, reason: str = "") -> None:
        self.phase = phase
        if reason:
            self.exit_reason = reason
        self.log({"event": "live_phase", "phase": phase, "reason": reason})

    # ---- one frame ----
    def step(self, client: Any, *, bids: dict[str, float | None], hm: str, today: str, stop_loss: float | None,
             exit_at: str, square_off_late: str) -> dict[str, Any] | None:
        """Advance one frame. Returns the finished result dict once done (else None)."""
        if self.day == today:  # another day's trade is never valued at today's prices
            for leg in LEGS:
                b = bids.get(leg)
                if b and b > 0:
                    self.last_bids[leg] = float(b)
            if self.last_bids and time.time() - self.marked_at >= MARK_EVERY_S:
                if self.log({"event": "live_mark", "bids": dict(self.last_bids)}) is not None:
                    self.marked_at = time.time()
        if self.day != today:  # the broker squared it off; today's book can't tell us more
            return self._finish("broker_square_off", estimated=True)
        if hm >= square_off_late:
            self.refresh(client)  # best effort: cancel what is known, then close (estimated if anything left)
            self._cancel_stale(client, force=True)
            return self._finish("broker_square_off",
                                estimated=any(self.held(leg) > 0 for leg in LEGS) or bool(self.book_error))
        if not self.refresh(client):
            return None
        self._cancel_stale(client)
        live_bids = {leg: bids.get(leg) for leg in LEGS}
        if bool(stop_loss) and self._stop_hit(stop_loss, live_bids):
            self.stop_since = self.stop_since or time.time()
        else:
            self.stop_since = 0.0
        stop_confirmed = bool(self.stop_since) and time.time() - self.stop_since >= STOP_CONFIRM_S
        if self.phase == "entering":
            if self.working():
                if hm >= exit_at or stop_confirmed:  # whatever is held is still protected
                    self._set_phase("exiting", "time" if hm >= exit_at else "stop")
                    return self._exit_step(client)
                return None
            h = {leg: self.held(leg) for leg in LEGS}
            if h["ce"] == 0 and h["pe"] == 0:
                if self._written_off_uncertain():
                    return None  # an order written off after an uncertain send may still be listed: watch
                return self._finish("entry_failed", estimated=False)
            if h["ce"] == h["pe"]:
                self._set_phase("holding")
                return None
            self._set_phase("exiting", "entry_incomplete")
        if self.phase == "holding":
            reason = ""
            if hm >= exit_at:
                reason = "time"
            elif stop_confirmed:
                reason = "stop"
            if not reason:
                return None
            self._set_phase("exiting", reason)
        if self.phase == "exiting":
            return self._exit_step(client)
        return None

    def _stop_hit(self, stop_loss: float, bids: dict[str, float | None]) -> bool:
        """This frame's live bids only: a missing price is not a stop (and resets the confirmation)."""
        value = cost = 0.0
        for leg in LEGS:
            q = self.held(leg)
            if q <= 0:
                continue
            b = bids.get(leg)
            avg, _ = self.cost(leg)
            if not b or avg <= 0:
                return False
            value += b * q
            cost += avg * q
        return cost > 0 and value <= cost * (1 - stop_loss)

    def _exit_step(self, client: Any) -> dict[str, Any] | None:
        now = time.time()
        for leg in LEGS:
            q = self.held(leg)
            if q < 0:  # can't happen by construction; never add to it
                if not self.manual_needed:
                    self.manual_needed = True
                    self.log({"event": "manual_needed", "why": f"{leg} oversold {q}"})
                continue
            if q == 0 or self.working(leg) or now < self.next_sell_at[leg]:
                continue
            if not self.place(client, leg, "SELL", q):
                continue
            self.sells[leg] += 1
            n = self.sells[leg]
            wait = SELL_BACKOFF_S[n] if n < len(SELL_BACKOFF_S) else MANUAL_RETRY_S
            self.next_sell_at[leg] = now + wait
            if n >= MAX_SELLS and not self.manual_needed:
                self.manual_needed = True
                self.log({"event": "manual_needed", "why": f"{leg} still held after {n} sells"})
        if all(self.held(leg) == 0 for leg in LEGS) and not self.working():
            if self._written_off_uncertain():
                self._alarm("written_off", "an order written off after an uncertain send: watching until 15:20")
                return None  # it may still be listed; the 15:20 / next-day path closes the trade
            return self._finish(self.exit_reason or "exit", estimated=False)
        return None

    def _written_off_uncertain(self) -> bool:
        return any(i.status == "NOT_PLACED" and i.send != "refused" for i in self.intents)

    def _finish(self, reason: str, *, estimated: bool) -> dict[str, Any]:
        fills = [(i.avg, i.filled, "buy" if i.side == "BUY" else "sell") for i in self.intents if i.filled and i.avg]
        bought = sum(p * q for p, q, s in fills if s == "buy")
        sold = sum(p * q for p, q, s in fills if s == "sell")
        left_value = 0.0
        estimated = estimated or any(self.own(leg) != self.held(leg) for leg in LEGS) or bool(self.working())
        for leg in LEGS:
            q = self.own(leg)  # still held, or sold by hand at a price this book doesn't own
            if q > 0:  # value at the trade day's last bid seen; the cost price if none was seen
                px = self.last_bids.get(leg) or self.cost(leg)[0]
                left_value += q * px
                fills.append((px, q, "sell"))
        charges = round(float(kite_nfo_charges(fills)["total"]), 2) if fills else 0.0
        gross = round(sold + left_value - bought, 2)
        pnl = round(gross - charges, 2)
        ce_avg, ce_q = self.cost("ce")
        pe_avg, pe_q = self.cost("pe")
        self.phase = "done"
        return {"reason": reason, "pnl": pnl, "pnl_gross": gross, "charges": charges, "estimated": estimated,
                "debit": round(ce_avg + pe_avg, 2), "ce_qty": ce_q, "pe_qty": pe_q,
                "exit": round(sold / max(ce_q, pe_q, 1), 2) if not estimated else None}

    # ---- restore ----
    @classmethod
    def restore(cls, start: dict[str, Any], events: list[dict[str, Any]],
                log: Callable[[dict[str, Any]], dict[str, Any] | None]) -> "LiveStraddle":
        ls = cls(day=str(start["day"]), strike=int(start.get("atm") or 0), ce_symbol=str(start["ce_symbol"]),
                 pe_symbol=str(start["pe_symbol"]), qty=int(start["qty"]), expiry_day=bool(start.get("expiry_day")),
                 log=log)
        by_tag: dict[str, Intent] = {}
        for ev in events:
            kind = ev.get("event")
            if kind == "order_intent":
                it = Intent(tag=str(ev["tag"]), leg=str(ev["leg"]), side=str(ev["side"]), qty=int(ev["qty"]),
                            symbol=str(ev["symbol"]), sent_at=float(ev.get("sent_at") or 0.0))
                ls.intents.append(it)
                by_tag[it.tag] = it
                ls.seq = max(ls.seq, int(ev.get("seq") or 0))
                if it.side == "SELL":
                    ls.sells[it.leg] += 1
            elif kind == "order_sent" and ev.get("tag") in by_tag:
                it = by_tag[ev["tag"]]
                it.oid = str(ev.get("order_id") or "")
                it.send = str(ev.get("send") or ("ok" if it.oid else "uncertain"))
                it.returned_at = float(ev.get("returned_at") or 0.0)
            elif kind == "live_mark":
                ls.last_bids.update({k: float(v) for k, v in (ev.get("bids") or {}).items() if v})
            elif kind == "order_cancel_sent" and ev.get("tag") in by_tag:
                pass  # cancels are re-sent while an order is working; nothing to restore
            elif kind == "order_final" and ev.get("tag") in by_tag:
                it = by_tag[ev["tag"]]
                it.status, it.filled, it.avg = str(ev["status"]), int(ev["filled"]), float(ev["avg"])
                it.oid, it.seen = str(ev.get("order_id") or it.oid), it.status != "NOT_PLACED"
                it.logged = (it.status, it.filled, it.avg)
            elif kind == "live_phase":
                ls.phase = str(ev.get("phase") or ls.phase)
                ls.exit_reason = str(ev.get("reason") or ls.exit_reason)
            elif kind == "manual_needed":
                ls.manual_needed = True
                ls.alarms.add(str(ev.get("key") or ev.get("why") or ""))
        now = time.time()
        for it in ls.intents:
            if not it.final and (it.send == "" or (it.send == "uncertain" and not it.returned_at)):
                it.send, it.returned_at = "uncertain", now  # send outcome never recorded: wait from here
        for leg in LEGS:  # the sell backoff continues from the last sell sent before the restart
            sent = [i.sent_at for i in ls.intents if i.leg == leg and i.side == "SELL"]
            n = ls.sells[leg]
            if sent and n:
                ls.next_sell_at[leg] = max(sent) + (SELL_BACKOFF_S[n] if n < len(SELL_BACKOFF_S) else MANUAL_RETRY_S)
            if it.status == "NOT_PLACED" and it.send != "refused":
                it.status, it.seen = "SENT", False  # a later listing may still show it: look again
        return ls
