"""Paper COMBO: chart votes + 8/−6, 12m, Side —, max 4, cooldown."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from atlas_lite.paper_combo import (
    PaperCombo,
    closed_bars_before,
    combo_letter_side,
    in_combo_entry_window,
)

IST = ZoneInfo("Asia/Kolkata")


class _Book:
    def __init__(self, rows: dict) -> None:
        self.rows = rows

    def get(self, symbol: str):
        return self.rows.get(symbol)


def _now(hm: str, day: str = "2026-09-04") -> datetime:
    return datetime.fromisoformat(f"{day}T{hm}:00+05:30")


def _bot(path: Path, **kwargs) -> PaperCombo:
    return PaperCombo(path=path / "combo.jsonl", lot_size=65, lots=1, **kwargs)


def _feed(**kwargs) -> dict:
    feed = {
        "ce": 120.0,
        "pe": 100.0,
        "ce_symbol": "NFO:CE",
        "pe_symbol": "NFO:PE",
    }
    feed.update(kwargs)
    return feed


def _book(ce: float, pe: float) -> _Book:
    return _Book({"NFO:CE": {"last_price": ce}, "NFO:PE": {"last_price": pe}})


def _combo(hm: str, signal=None, side=None, bull=4, bear=0, day: str = "2026-09-04") -> dict:
    return {
        "t": f"{day} {hm}",
        "signal": signal,
        "side": side,
        "bull": bull,
        "bear": bear,
    }


def _frame(
    bot: PaperCombo,
    hm: str = "10:24",
    *,
    day: str = "2026-09-04",
    ce: float = 120.0,
    pe: float = 100.0,
    combo: dict | None = None,
    signal_minute: str | None = None,
    **feed_kw,
) -> dict | None:
    row = combo if combo is not None else _combo("10:23", "B", "B")
    return bot.on_frame(
        now=_now(hm, day=day),
        feed=_feed(ce=ce, pe=pe, **feed_kw),
        book=_book(ce, pe),
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=23400,
        combo=row,
        signal_minute=signal_minute or str(row.get("t") or ""),
    )


def test_letter_maps_to_wing() -> None:
    assert combo_letter_side("B") == "ce"
    assert combo_letter_side("S") == "pe"
    assert combo_letter_side(None) is None


def test_entry_window() -> None:
    assert in_combo_entry_window(_now("09:29")) is False
    assert in_combo_entry_window(_now("09:30")) is True
    assert in_combo_entry_window(_now("14:45")) is True
    assert in_combo_entry_window(_now("14:46")) is False


def test_closed_bars_drop_forming_minute() -> None:
    bars = [
        {"t": "2026-09-04 10:22", "c": 1},
        {"t": "2026-09-04 10:23", "c": 2},
        {"t": "2026-09-04 10:24", "c": 3},
    ]
    out = closed_bars_before(bars, _now("10:24"))
    assert [b["t"] for b in out] == ["2026-09-04 10:22", "2026-09-04 10:23"]


def test_opens_ce_on_b(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    ev = _frame(bot)
    assert ev is not None
    assert ev["event"] == "open"
    assert ev["side"] == "ce"
    assert ev["letter"] == "B"
    assert ev["entry"] == 120.0
    assert ev["target"] == 129.6
    assert ev["stop"] == 112.8


def test_opens_pe_on_s(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    ev = _frame(bot, combo=_combo("10:23", "S", "S", bull=0, bear=4), pe=100.0)
    assert ev is not None
    assert ev["side"] == "pe"
    assert ev["letter"] == "S"
    assert ev["entry"] == 100.0


def test_target_is_profit(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _frame(bot) is not None
    closed = _frame(bot, "10:26", ce=130.0, combo=_combo("10:25", None, "B"))
    assert closed is not None
    assert closed["reason"] == "target"
    assert closed["pnl"] > 0


def test_stop_is_loss(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _frame(bot) is not None
    closed = _frame(bot, "10:26", ce=110.0, combo=_combo("10:25", None, "B"))
    assert closed is not None
    assert closed["reason"] == "stop"
    assert closed["pnl"] < 0


def test_time_stop_at_12m(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _frame(bot) is not None
    assert _frame(bot, "10:35", ce=121.0, combo=_combo("10:34", None, "B")) is None
    closed = _frame(bot, "10:36", ce=121.0, combo=_combo("10:35", None, "B"))
    assert closed is not None
    assert closed["reason"] == "time"


def test_flatten_when_confluence_lost(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _frame(bot) is not None
    closed = _frame(
        bot,
        "10:25",
        ce=121.0,
        combo=_combo("10:24", None, None, bull=3, bear=1),
    )
    assert closed is not None
    assert closed["reason"] == "confluence"
    assert bot.position is None


def test_same_letter_reprint_does_not_exit(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _frame(bot) is not None
    assert (
        _frame(bot, "10:25", ce=121.0, combo=_combo("10:24", "B", "B")) is None
    )
    assert bot.position is not None
    assert bot.position.letter == "B"


def test_opposite_flip_reverses(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _frame(bot) is not None
    ev = _frame(
        bot,
        "10:25",
        ce=121.0,
        pe=100.0,
        combo=_combo("10:24", "S", "S", bull=0, bear=4),
    )
    assert ev is not None
    assert ev["event"] == "open"
    assert ev["side"] == "pe"
    assert ev["letter"] == "S"
    assert bot.entries_today == 2


def test_cooldown_after_confluence(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _frame(bot) is not None
    assert _frame(bot, "10:25", ce=121.0, combo=_combo("10:24", None, None, bull=3, bear=1))
    assert bot.position is None
    assert (
        _frame(bot, "10:30", combo=_combo("10:29", "B", "B")) is None
    )
    opened = _frame(bot, "10:34", combo=_combo("10:33", "B", "B"))
    assert opened is not None
    assert opened["event"] == "open"


def test_max_four_per_day(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    slots = (("09:40", "09:42"), ("10:10", "10:12"), ("10:40", "10:42"), ("11:10", "11:12"))
    for open_hm, close_hm in slots:
        assert _frame(bot, open_hm, combo=_combo(open_hm, "B", "B")) is not None
        closed = _frame(bot, close_hm, ce=130.0, combo=_combo(close_hm, None, "B"))
        assert closed is not None
        assert closed["event"] == "close"
    assert bot.entries_today == 4
    assert _frame(bot, "12:00", combo=_combo("11:59", "B", "B")) is None


def test_same_closed_minute_fires_once(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    first = _frame(bot, "10:24", signal_minute="2026-09-04 10:23")
    assert first is not None
    assert _frame(bot, "10:26", ce=130.0, combo=_combo("10:25", None, "B")) is not None
    bot.last_exit_at = None
    assert _frame(bot, "10:34", combo=_combo("10:33", "B", "B"), signal_minute="2026-09-04 10:23") is None
    opened = _frame(bot, "10:34", combo=_combo("10:33", "B", "B"), signal_minute="2026-09-04 10:24")
    assert opened is not None


def test_restores_open(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _frame(bot) is not None
    again = PaperCombo(path=tmp_path / "combo.jsonl", lot_size=65, lots=1)
    assert again.position is not None
    assert again.position.side == "ce"
    assert again.position.letter == "B"
    assert again.position.entry == 120.0
    assert again.entries_today == 1


def test_restores_letter_from_side_if_missing(tmp_path: Path) -> None:
    import json

    bot = _bot(tmp_path)
    assert _frame(bot, combo=_combo("10:23", "S", "S", bull=0, bear=4)) is not None
    line = (tmp_path / "combo.jsonl").read_text(encoding="utf-8").strip().splitlines()[-1]
    ev = json.loads(line)
    del ev["letter"]
    (tmp_path / "combo.jsonl").write_text(json.dumps(ev) + "\n", encoding="utf-8")
    again = PaperCombo(path=tmp_path / "combo.jsonl", lot_size=65, lots=1)
    assert again.position is not None
    assert again.position.side == "pe"
    assert again.position.letter == "S"


def test_fourth_flip_does_not_reverse(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    slots = (("09:40", "09:42"), ("10:10", "10:12"), ("10:40", "10:42"))
    for open_hm, close_hm in slots:
        assert _frame(bot, open_hm, combo=_combo(open_hm, "B", "B")) is not None
        assert _frame(bot, close_hm, ce=130.0, combo=_combo(close_hm, None, "B")) is not None
    assert _frame(bot, "11:10", combo=_combo("11:10", "B", "B")) is not None
    closed = _frame(
        bot,
        "11:12",
        ce=121.0,
        pe=100.0,
        combo=_combo("11:12", "S", "S", bull=0, bear=4),
    )
    assert closed is not None
    assert closed["event"] == "close"
    assert closed["reason"] == "flip"
    assert bot.position is None
    assert bot.entries_today == 4


def test_leftover_flatten_does_not_cap_the_day(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _frame(bot) is not None
    closed = _frame(bot, "09:20", day="2026-09-07", ce=121.0)
    assert closed is not None
    assert closed["reason"] == "session_gap"
    assert bot.entries_today == 1
    assert bot.position is None
    opened = _frame(
        bot,
        "09:40",
        day="2026-09-07",
        combo=_combo("09:39", "B", "B", day="2026-09-07"),
    )
    assert opened is not None
    assert opened["event"] == "open"
    assert bot.entries_today == 2


def test_weekend_flatten_reason(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _frame(bot) is not None
    closed = _frame(bot, "10:00", day="2026-09-05", ce=121.0)
    assert closed is not None
    assert closed["reason"] == "weekend"


def test_close_write_fail_keeps_position(tmp_path: Path, monkeypatch) -> None:
    bot = _bot(tmp_path)
    assert _frame(bot) is not None
    monkeypatch.setattr(bot, "_append", lambda ev: None)
    closed = _frame(bot, "10:36", ce=121.0, combo=_combo("10:35", None, "B"))
    assert closed is None
    assert bot.position is not None
