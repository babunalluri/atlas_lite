"""Model vote buyer: a vote every minute 09:16-15:00, entry on a confident vote, 30-minute exit, 15:14 square-off, restart,
and the shipped model file loads and scores in pure Python."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from atlas_lite.paper_model_vote import PaperModelVote, VoteModels, features

DAY = "2026-11-16"
CE, PE = "NFO:NIFTY26N1725000CE", "NFO:NIFTY26N1725000PE"


class Book:
    def __init__(self, ce: float = 100.0, pe: float = 100.0) -> None:
        self.px = {CE: ce, PE: pe}

    def get(self, s: str):
        p = self.px.get(s)
        return None if p is None else {"last_price": p, "bid": p - 0.5, "ask": p + 0.5}


class FixedVote:
    """Stands in for the models: returns `leg` for every vote and counts the calls."""

    version = "test"

    def __init__(self, leg: str | None) -> None:
        self.leg, self.calls = leg, 0

    def vote(self, x: list[float]) -> dict[str, Any]:
        self.calls += 1
        up = {"a": self.leg != "pe", "b": self.leg == "ce"}  # no leg: the two disagree
        return {"scores": {}, "up": up, "confidence": 0.05, "needed": 0.03, "leg": self.leg}


def t(hm: str, sec: int = 5) -> datetime:
    return datetime.fromisoformat(f"{DAY}T{hm}:{sec:02d}+05:30")


def hm_of(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def bars_until(until: str, drift: float = 0.0) -> list[dict]:
    out, m, px = [], 9 * 60 + 15, 25000.0
    while hm_of(m) < until:
        out.append({"t": f"{DAY} {hm_of(m)}", "o": px, "h": px + 5, "l": px - 5, "c": px + drift})
        px += drift
        m += 1
    return out


def feed(until: str, drift: float = 0.0) -> dict:
    return {"atm": 25000, "ce_symbol": CE, "pe_symbol": PE, "bars_today": bars_until(until, drift)}


def ledger(b: PaperModelVote, votes: bool = False) -> list[dict]:
    rows = [json.loads(x) for x in b.path.read_text().splitlines() if x.strip()]
    return rows if votes else [r for r in rows if r["event"] != "vote"]


def test_votes_every_minute_then_exits_after_30_min(tmp_path: Path) -> None:
    fv = FixedVote("ce")
    b = PaperModelVote(path=tmp_path / "v.jsonl", models=fv)  # type: ignore[arg-type]
    ev = b.on_frame(now=t("10:16"), feed=feed("10:16"), book=Book())  # candle 10:15 closed
    assert ev and ev["event"] == "open" and ev["leg"] == "ce" and ev["fill"] == 100.5 and ev["qty"] == 65
    for hm in ("10:17", "10:21", "10:33", "10:45"):
        assert b.on_frame(now=t(hm), feed=feed(hm), book=Book()) is None  # holding: no second trade
    assert fv.calls == 5  # voted every minute it was asked
    ev = b.on_frame(now=t("10:46"), feed=feed("10:46"), book=Book(ce=110.0))
    # 30 minutes after the entry the trade is sold and, the vote still agreeing, a new one opens in the same minute
    rows = ledger(b)
    assert [r["event"] for r in rows] == ["open", "close", "open"]
    assert rows[1]["reason"] == "hold_30m" and rows[1]["exit"] == 109.5 and rows[1]["pnl_gross"] == round(9.0 * 65, 2)
    assert ev and ev["event"] == "open"


def test_no_trade_without_a_vote_and_votes_from_0916_to_1500(tmp_path: Path) -> None:
    fv = FixedVote(None)
    b = PaperModelVote(path=tmp_path / "v.jsonl", models=fv)  # type: ignore[arg-type]
    assert b.on_frame(now=t("10:16"), feed=feed("10:16"), book=Book()) is None and fv.calls == 1
    fv2 = FixedVote("pe")
    b2 = PaperModelVote(path=tmp_path / "w.jsonl", models=fv2)  # type: ignore[arg-type]
    for hm in ("09:15", "15:01"):  # no closed candle yet / candle 15:00: outside the window
        assert b2.on_frame(now=t(hm), feed=feed(hm), book=Book()) is None
    assert fv2.calls == 0
    ev = b2.on_frame(now=t("09:16"), feed=feed("09:16"), book=Book())  # candle 09:15: the first vote
    assert ev and ev["leg"] == "pe"
    b3 = PaperModelVote(path=tmp_path / "x.jsonl", models=FixedVote("ce"))  # type: ignore[arg-type]
    ev = b3.on_frame(now=t("15:00"), feed=feed("15:00"), book=Book())  # candle 14:59: the last vote
    assert ev and ev["leg"] == "ce"


def test_square_off_at_1514_and_restart_restores_the_open_trade(tmp_path: Path) -> None:
    b = PaperModelVote(path=tmp_path / "v.jsonl", models=FixedVote("ce"))  # type: ignore[arg-type]
    b.on_frame(now=t("14:56"), feed=feed("14:56"), book=Book())
    again = PaperModelVote(path=tmp_path / "v.jsonl", models=FixedVote(None))  # type: ignore[arg-type]
    again._new_day(DAY)
    assert again.position is not None and again.position["symbol"] == CE
    assert again.on_frame(now=t("15:13"), feed=feed("15:13"), book=Book()) is None  # 17 min held, before 15:14: keep
    ev = again.on_frame(now=t("15:14"), feed={}, book=Book())
    assert ev and ev["reason"] == "square_off" and again.position is None


def test_shipped_models_load_and_vote_in_pure_python() -> None:
    vm = VoteModels()
    for drift in (0.0, 2.0, -2.0):
        x = features(bars_until("10:16", drift))
        assert x is not None and len(x) == vm.n_features
        v = vm.vote(x)
        assert set(v["scores"]) == {"lightgbm", "catboost", "logistic"}
        assert all(0.0 < s < 1.0 for s in v["scores"].values())
        assert v["leg"] in (None, "ce", "pe")
    early = features(bars_until("09:20"))  # 5 candles: the 15/60-minute inputs use what there is
    assert early is not None and early[10] == 0.0  # opening range still forming: no break
    assert features([]) is None


def test_snapshot_shows_the_last_vote_and_exit_time(tmp_path: Path) -> None:
    b = PaperModelVote(path=tmp_path / "v.jsonl", models=FixedVote("ce"))  # type: ignore[arg-type]
    b.on_frame(now=t("10:16"), feed=feed("10:16"), book=Book())
    snap = b.snapshot(Book())
    assert snap["book"] == "model_vote" and snap["live_orders"] is False
    assert snap["position"]["exit_by"] == "10:46" and snap["last_vote"]["candle"] == "10:15"


def test_every_vote_is_logged_with_its_decision(tmp_path: Path) -> None:
    fv = FixedVote(None)
    b = PaperModelVote(path=tmp_path / "v.jsonl", models=fv)  # type: ignore[arg-type]
    b.on_frame(now=t("09:16"), feed=feed("09:16"), book=Book())
    fv.leg = "ce"
    b.on_frame(now=t("09:17"), feed=feed("09:17"), book=Book())
    b.on_frame(now=t("09:18"), feed=feed("09:18"), book=Book())
    votes = [r for r in ledger(b, votes=True) if r["event"] == "vote"]
    assert [v["candle"] for v in votes] == ["09:15", "09:16", "09:17"]
    assert [v["action"] for v in votes] == ["no trade: models split", "buy CE", "signal, already holding"]
    assert votes[0]["nifty"] == 25000.0 and votes[0]["day"] == DAY
    again = PaperModelVote(path=tmp_path / "v.jsonl", models=FixedVote(None))  # type: ignore[arg-type]
    again._new_day(DAY)  # vote lines do not disturb the restart
    assert again.position is not None and again.trades == 1
