"""Log scorecard/propose blocks and paper-track what the signal might have made.

Shadows bind to the ATM option *symbol* at block time so marks do not jump when
the live ATM rolls. Open shadows persist across restarts.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from atlas_lite.kite_charges import kite_nfo_charges
from atlas_lite.log_util import get_logger
from atlas_lite.metrics import quote_ltp
from atlas_lite.paper_agent import (
    HOLD_MINUTES,
    SQUARE_OFF,
    agent_exit_profile,
    initial_target_stop,
    trail_gap_px,
)

IST = ZoneInfo("Asia/Kolkata")
log = get_logger("atlas_lite.agent_block_log")
BLOCKED_FILE = "agent_blocked_entries.jsonl"
SHADOWS_FILE = "agent_blocked_shadows.json"


def _f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _hm_ge(now: datetime, hhmm: tuple[int, int]) -> bool:
    return (now.hour, now.minute) >= (hhmm[0], hhmm[1])


def _parse_ts(raw: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def _open_side(style: str) -> str:
    return "sell" if style == "short" else "buy"


def _close_side(style: str) -> str:
    return "buy" if style == "short" else "sell"


def _gross(style: str, entry: float, exit_px: float, qty: int) -> float:
    if style == "short":
        return round((float(entry) - float(exit_px)) * int(qty), 2)
    return round((float(exit_px) - float(entry)) * int(qty), 2)


@dataclass
class _Shadow:
    id: str
    side: str
    style: str
    symbol: str
    entry_px: float
    entry_ts: datetime
    target: float
    stop: float
    trail_arm_pct: float
    trail_pct: float
    trail_pts: float
    best_px: float
    qty: int
    charges_open: float
    trail_armed: bool = False
    rule: str = ""
    score: float | None = None
    spot: float | None = None
    atm: int | None = None
    ce_symbol: str | None = None
    pe_symbol: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "side": self.side,
            "style": self.style,
            "symbol": self.symbol,
            "ce_symbol": self.ce_symbol,
            "pe_symbol": self.pe_symbol,
            "entry_px": self.entry_px,
            "entry_ts": self.entry_ts.isoformat(timespec="seconds"),
            "target": self.target,
            "stop": self.stop,
            "trail_arm_pct": self.trail_arm_pct,
            "trail_pct": self.trail_pct,
            "trail_pts": self.trail_pts,
            "best_px": self.best_px,
            "qty": self.qty,
            "charges_open": self.charges_open,
            "trail_armed": self.trail_armed,
            "rule": self.rule,
            "score": self.score,
            "spot": self.spot,
            "atm": self.atm,
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> _Shadow | None:
        ts = _parse_ts(str(row.get("entry_ts") or ""))
        symbol = str(row.get("symbol") or "").strip()
        entry = _f(row.get("entry_px"))
        if ts is None or not symbol or entry is None or entry <= 0:
            return None
        side = str(row.get("side") or "").strip().lower()
        style = str(row.get("style") or "long").strip().lower() or "long"
        if side not in ("ce", "pe") or style not in ("long", "short"):
            return None
        try:
            qty = int(row.get("qty") or 65)
        except (TypeError, ValueError):
            qty = 65
        return cls(
            id=str(row.get("id") or "")[:80] or f"restored_{ts.strftime('%H%M%S')}",
            side=side,
            style=style,
            symbol=symbol,
            ce_symbol=str(row.get("ce_symbol") or "") or None,
            pe_symbol=str(row.get("pe_symbol") or "") or None,
            entry_px=float(entry),
            entry_ts=ts,
            target=float(row.get("target") or entry),
            stop=float(row.get("stop") or entry),
            trail_arm_pct=float(row.get("trail_arm_pct") or 0.04),
            trail_pct=float(row.get("trail_pct") or 0.03),
            trail_pts=float(row.get("trail_pts") or 1.5),
            best_px=float(row.get("best_px") or entry),
            qty=max(1, qty),
            charges_open=float(row.get("charges_open") or 0.0),
            trail_armed=bool(row.get("trail_armed")),
            rule=str(row.get("rule") or "")[:80],
            score=_f(row.get("score")),
            spot=_f(row.get("spot")),
            atm=int(row["atm"]) if row.get("atm") not in (None, "") else None,
        )


class AgentBlockLog:
    """Append-only blocked-entry ledger + short-lived paper shadows."""

    def __init__(self, path: Path, *, shadows_path: Path | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.shadows_path = (
            Path(shadows_path)
            if shadows_path is not None
            else self.path.parent / SHADOWS_FILE
        )
        self._lock = threading.RLock()
        self._shadows: list[_Shadow] = []
        self._load_shadows()

    def log_blocked(
        self,
        *,
        now: datetime,
        rule: str,
        side: str,
        style: str,
        score: float | None = None,
        min_entry_score: float | None = None,
        spot: float | None = None,
        spot_chg_open_pct: float | None = None,
        atm: int | None = None,
        entry_px: float | None = None,
        option_symbol: str | None = None,
        ce_symbol: str | None = None,
        pe_symbol: str | None = None,
        lot_size: int = 65,
        reasons: list[str] | None = None,
        detail: str | None = None,
        track_paper: bool = True,
    ) -> dict[str, Any]:
        """Record a blocked entry. Optionally paper-track the *same* option symbol."""
        with self._lock:
            return self._log_blocked_unlocked(
                now=now,
                rule=rule,
                side=side,
                style=style,
                score=score,
                min_entry_score=min_entry_score,
                spot=spot,
                spot_chg_open_pct=spot_chg_open_pct,
                atm=atm,
                entry_px=entry_px,
                option_symbol=option_symbol,
                ce_symbol=ce_symbol,
                pe_symbol=pe_symbol,
                lot_size=lot_size,
                reasons=reasons,
                detail=detail,
                track_paper=track_paper,
            )

    def _log_blocked_unlocked(
        self,
        *,
        now: datetime,
        rule: str,
        side: str,
        style: str,
        score: float | None = None,
        min_entry_score: float | None = None,
        spot: float | None = None,
        spot_chg_open_pct: float | None = None,
        atm: int | None = None,
        entry_px: float | None = None,
        option_symbol: str | None = None,
        ce_symbol: str | None = None,
        pe_symbol: str | None = None,
        lot_size: int = 65,
        reasons: list[str] | None = None,
        detail: str | None = None,
        track_paper: bool = True,
    ) -> dict[str, Any]:
        side_n = str(side or "").strip().lower()
        style_n = str(style or "long").strip().lower() or "long"
        ce_sym = str(ce_symbol or "").strip() or None
        pe_sym = str(pe_symbol or "").strip() or None
        opt_sym = str(option_symbol or "").strip() or None
        if not opt_sym:
            opt_sym = pe_sym if side_n == "pe" else ce_sym
        try:
            qty = max(1, int(lot_size or 65))
        except (TypeError, ValueError):
            qty = 65
        row: dict[str, Any] = {
            "event": "blocked",
            "ts": now.isoformat(timespec="seconds"),
            "day": now.strftime("%Y-%m-%d"),
            "rule": str(rule or "unknown")[:80],
            "side": side_n,
            "style": style_n,
            "score": score,
            "min_entry_score": min_entry_score,
            "spot": spot,
            "spot_chg_open_pct": spot_chg_open_pct,
            "atm": atm,
            "entry_px": entry_px,
            "option_symbol": opt_sym,
            "ce_symbol": ce_sym,
            "pe_symbol": pe_sym,
            "reasons": list(reasons or [])[:8],
            "detail": (detail or "")[:240] or None,
            "paper_status": "skipped_no_px",
            "paper_pnl": None,
            "paper_exit_reason": None,
        }
        can_track = (
            track_paper
            and entry_px is not None
            and float(entry_px) > 0
            and side_n in ("ce", "pe")
            and style_n in ("long", "short")
            and bool(opt_sym)
        )
        if track_paper and not opt_sym:
            row["paper_status"] = "skipped_no_symbol"
        if can_track:
            t_pct, s_pct, arm, tr_pct, tr_pts = agent_exit_profile(now, style_n)
            target, stop = initial_target_stop(
                float(entry_px),
                style_n,  # type: ignore[arg-type]
                target_pct=t_pct,
                stop_pct=s_pct,
            )
            charges_open = float(
                kite_nfo_charges([(float(entry_px), qty, _open_side(style_n))])["total"]
            )
            shadow_id = f"{now.strftime('%H%M%S')}_{style_n}_{side_n}_{rule}"[:80]
            self._shadows.append(
                _Shadow(
                    id=shadow_id,
                    side=side_n,
                    style=style_n,
                    symbol=str(opt_sym),
                    ce_symbol=ce_sym,
                    pe_symbol=pe_sym,
                    entry_px=float(entry_px),
                    entry_ts=now,
                    target=target,
                    stop=stop,
                    trail_arm_pct=float(arm),
                    trail_pct=float(tr_pct),
                    trail_pts=float(tr_pts),
                    best_px=float(entry_px),
                    qty=qty,
                    charges_open=charges_open,
                    rule=str(rule or "")[:80],
                    score=score,
                    spot=spot,
                    atm=atm,
                )
            )
            row["paper_status"] = "open"
            row["paper_id"] = shadow_id
            row["paper_target"] = target
            row["paper_stop"] = stop
            row["paper_charges_open"] = charges_open
            row["qty"] = qty
            self._persist_shadows()
        self._append(row)
        return row

    def on_book(
        self,
        now: datetime,
        book: Any,
        *,
        lot_size: int | None = None,
    ) -> list[dict[str, Any]]:
        """Advance open shadows using book quotes for each bound symbol."""
        with self._lock:
            return self._on_book_unlocked(now, book, lot_size=lot_size)

    def _on_book_unlocked(
        self,
        now: datetime,
        book: Any,
        *,
        lot_size: int | None = None,
    ) -> list[dict[str, Any]]:
        closed_rows: list[dict[str, Any]] = []
        keep: list[_Shadow] = []
        dirty = False
        for sh in self._shadows:
            px = quote_ltp(book.get(sh.symbol)) if book is not None else None
            if px is None or px <= 0:
                keep.append(sh)
                continue
            px = float(px)
            before = (sh.stop, sh.best_px, sh.trail_armed)
            if sh.style == "long":
                if px > sh.best_px:
                    sh.best_px = px
                if (not sh.trail_armed) and px >= sh.entry_px * (1.0 + sh.trail_arm_pct):
                    sh.trail_armed = True
                if sh.trail_armed:
                    gap = trail_gap_px(
                        sh.entry_px, trail_pct=sh.trail_pct, trail_pts=sh.trail_pts
                    )
                    sh.stop = max(sh.stop, round(sh.best_px - gap, 2))
                hit_target = px >= sh.target
                hit_stop = px <= sh.stop
            else:
                # Short paper shadows: stop or square-off only (no trail/target/time).
                hit_target = False
                hit_stop = px >= sh.stop

            hold_due = (
                sh.style == "long"
                and now >= sh.entry_ts + timedelta(minutes=int(HOLD_MINUTES))
            )
            square = _hm_ge(now, SQUARE_OFF)
            reason: str | None = None
            if hit_target:
                reason = "target"
            elif hit_stop:
                reason = "trail" if sh.trail_armed else "stop"
            elif square:
                reason = "square_off"
            elif hold_due:
                reason = "time"
            if reason is None:
                if (sh.stop, sh.best_px, sh.trail_armed) != before:
                    dirty = True
                keep.append(sh)
                continue

            qty = int(sh.qty)
            if lot_size is not None:
                try:
                    qty = max(1, int(lot_size))
                except (TypeError, ValueError):
                    pass
            gross = _gross(sh.style, sh.entry_px, px, qty)
            close_ch = float(
                kite_nfo_charges([(px, qty, _close_side(sh.style))])["total"]
            )
            charges = round(float(sh.charges_open) + close_ch, 2)
            paper_pnl = round(gross - charges, 2)
            done = {
                "event": "blocked_paper_close",
                "ts": now.isoformat(timespec="seconds"),
                "day": now.strftime("%Y-%m-%d"),
                "paper_id": sh.id,
                "rule": sh.rule,
                "side": sh.side,
                "style": sh.style,
                "score": sh.score,
                "atm": sh.atm,
                "option_symbol": sh.symbol,
                "ce_symbol": sh.ce_symbol,
                "pe_symbol": sh.pe_symbol,
                "entry_px": sh.entry_px,
                "exit_px": px,
                "qty": qty,
                "gross": gross,
                "charges": charges,
                "paper_pnl": paper_pnl,
                "paper_exit_reason": reason,
                "paper_status": "closed",
                "held_s": int((now - sh.entry_ts).total_seconds()),
            }
            self._append(done)
            closed_rows.append(done)
            dirty = True
        self._shadows = keep
        if dirty or closed_rows:
            self._persist_shadows()
        return closed_rows

    def on_marks(
        self,
        now: datetime,
        *,
        ce_px: float | None = None,
        pe_px: float | None = None,
        marks: dict[str, float] | None = None,
        lot_size: int = 65,
    ) -> list[dict[str, Any]]:
        """Test/helper path: mark by symbol map, or by side if shadow has no book."""

        class _MapBook:
            def __init__(self, by_sym: dict[str, float], by_side: dict[str, float]) -> None:
                self.by_sym = by_sym
                self.by_side = by_side

            def get(self, symbol: str):
                if symbol in self.by_sym:
                    return {"last_price": self.by_sym[symbol]}
                # Legacy side-keyed fake symbols used in unit tests.
                if symbol in ("ce", "pe") and symbol in self.by_side:
                    return {"last_price": self.by_side[symbol]}
                return None

        by_sym = {str(k): float(v) for k, v in (marks or {}).items() if v is not None}
        by_side: dict[str, float] = {}
        if ce_px is not None:
            by_side["ce"] = float(ce_px)
        if pe_px is not None:
            by_side["pe"] = float(pe_px)
        with self._lock:
            # If shadows use real symbols but only side marks given, map via side.
            for sh in self._shadows:
                if sh.symbol not in by_sym and sh.side in by_side:
                    by_sym[sh.symbol] = by_side[sh.side]
            return self._on_book_unlocked(
                now, _MapBook(by_sym, by_side), lot_size=lot_size
            )

    def open_count(self) -> int:
        with self._lock:
            return len(self._shadows)

    def _load_shadows(self) -> None:
        if not self.shadows_path.is_file():
            return
        try:
            raw = json.loads(self.shadows_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("blocked shadow load failed: %s", exc)
            return
        rows = raw.get("shadows") if isinstance(raw, dict) else raw
        if not isinstance(rows, list):
            return
        loaded: list[_Shadow] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            sh = _Shadow.from_dict(row)
            if sh is not None:
                loaded.append(sh)
        self._shadows = loaded

    def _persist_shadows(self) -> None:
        payload = {"shadows": [sh.to_dict() for sh in self._shadows]}
        try:
            self.shadows_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.shadows_path.with_suffix(self.shadows_path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, default=str), encoding="utf-8")
            tmp.replace(self.shadows_path)
        except OSError as exc:
            log.warning("blocked shadow persist failed: %s", exc)

    def _append(self, row: dict[str, Any]) -> None:
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, default=str) + "\n")
        except OSError as exc:
            log.warning("blocked entry log write failed: %s", exc)
