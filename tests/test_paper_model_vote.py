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


def test_holds_while_the_vote_agrees_or_is_unsure_and_reverses_on_an_opposite_vote(tmp_path: Path) -> None:
    fv = FixedVote("ce")
    b = PaperModelVote(path=tmp_path / "v.jsonl", models=fv)  # type: ignore[arg-type]
    ev = b.on_frame(now=t("10:16"), feed=feed("10:16"), book=Book())  # candle 10:15 closed
    assert ev and ev["event"] == "open" and ev["leg"] == "ce" and ev["fill"] == 100.5 and ev["qty"] == 65
    for hm, leg in (("10:17", "ce"), ("10:50", None), ("11:30", "ce")):  # agrees / unsure: hold, no time limit
        fv.leg = leg
        assert b.on_frame(now=t(hm), feed=feed(hm), book=Book()) is None
    assert fv.calls == 4  # voted every minute it was asked
    fv.leg = "pe"  # confident vote for the other side: sell the CE, buy the PE in the same minute
    ev = b.on_frame(now=t("11:31"), feed=feed("11:31"), book=Book(ce=110.0, pe=90.0))
    rows = ledger(b)
    assert [r["event"] for r in rows] == ["open", "close", "open"]
    assert rows[1]["reason"] == "vote_reverse" and rows[1]["exit"] == 109.5 and rows[1]["pnl_gross"] == round(9.0 * 65, 2)
    assert ev and ev["event"] == "open" and ev["leg"] == "pe" and ev["fill"] == 90.5
    actions = [r["action"] for r in ledger(b, votes=True) if r["event"] == "vote"]
    assert actions == ["buy CE", "hold CE: vote agrees", "hold CE: no opposite signal", "hold CE: vote agrees",
                       "sell CE, buy PE"]


def test_after_the_last_entry_candle_an_opposite_vote_only_sells(tmp_path: Path) -> None:
    fv = FixedVote("ce")
    b = PaperModelVote(path=tmp_path / "v.jsonl", models=fv)  # type: ignore[arg-type]
    b.on_frame(now=t("14:56"), feed=feed("14:56"), book=Book())
    fv.leg = "pe"
    ev = b.on_frame(now=t("15:06"), feed=feed("15:06"), book=Book())  # candle 15:05: votes still run for exits
    assert ev and ev["event"] == "close" and ev["reason"] == "vote_reverse" and b.position is None
    assert ledger(b, votes=True)[-1]["action"] == "sell CE (no new entries after 14:59)"
    assert b.on_frame(now=t("15:07"), feed=feed("15:07"), book=Book()) is None  # flat after 14:59: no new trade


def test_no_trade_without_a_vote_and_votes_from_0916_to_1500(tmp_path: Path) -> None:
    fv = FixedVote(None)
    b = PaperModelVote(path=tmp_path / "v.jsonl", models=fv)  # type: ignore[arg-type]
    assert b.on_frame(now=t("10:16"), feed=feed("10:16"), book=Book()) is None and fv.calls == 1
    fv2 = FixedVote("pe")
    b2 = PaperModelVote(path=tmp_path / "w.jsonl", models=fv2)  # type: ignore[arg-type]
    for hm in ("09:15", "15:01"):  # no closed candle yet / candle 15:00: after the last entry candle
        assert b2.on_frame(now=t(hm), feed=feed(hm), book=Book()) is None
    assert fv2.calls == 1  # 15:00 is voted (exits) but opens nothing
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
    assert vm.conf_cut == 0.0370  # top 10% of 2025 (operator's call 27 Nov 2026; the file's top-20% cut is 0.0288)
    early = features(bars_until("09:20"))  # 5 candles: the 15/60-minute inputs use what there is
    assert early is not None and early[10] == 0.0  # opening range still forming: no break
    assert features([]) is None


def test_snapshot_shows_the_last_vote_and_exit_time(tmp_path: Path) -> None:
    b = PaperModelVote(path=tmp_path / "v.jsonl", models=FixedVote("ce"))  # type: ignore[arg-type]
    b.on_frame(now=t("10:16"), feed=feed("10:16"), book=Book())
    snap = b.snapshot(Book())
    assert snap["book"] == "model_vote" and snap["live_orders"] is False
    assert snap["position"]["exit_by"] == "PE vote or 15:14" and snap["last_vote"]["candle"] == "10:15"


def test_every_vote_is_logged_with_its_decision(tmp_path: Path) -> None:
    fv = FixedVote(None)
    b = PaperModelVote(path=tmp_path / "v.jsonl", models=fv)  # type: ignore[arg-type]
    b.on_frame(now=t("09:16"), feed=feed("09:16"), book=Book())
    fv.leg = "ce"
    b.on_frame(now=t("09:17"), feed=feed("09:17"), book=Book())
    b.on_frame(now=t("09:18"), feed=feed("09:18"), book=Book())
    votes = [r for r in ledger(b, votes=True) if r["event"] == "vote"]
    assert [v["candle"] for v in votes] == ["09:15", "09:16", "09:17"]
    assert [v["action"] for v in votes] == ["no trade: models split", "buy CE", "hold CE: vote agrees"]
    assert votes[0]["nifty"] == 25000.0 and votes[0]["day"] == DAY
    again = PaperModelVote(path=tmp_path / "v.jsonl", models=FixedVote(None))  # type: ignore[arg-type]
    again._new_day(DAY)  # vote lines do not disturb the restart
    assert again.position is not None and again.trades == 1


def feed_at(hm: str, close: float) -> dict:
    """Flat 25000 candles to `hm`, the last closed candle at `close`."""
    f = feed(hm)
    f["bars_today"][-1] = {**f["bars_today"][-1], "c": close}
    return f


def test_trailing_sl_sells_then_the_book_stays_flat_for_the_day(tmp_path: Path) -> None:
    fv = FixedVote("ce")
    b = PaperModelVote(path=tmp_path / "v.jsonl", models=fv)  # type: ignore[arg-type]
    b.on_frame(now=t("10:16"), feed=feed_at("10:16", 25000.0), book=Book())  # buy CE with NIFTY at 25000
    fv.leg = None
    b.on_frame(now=t("10:17"), feed=feed_at("10:17", 25070.0), book=Book())  # +70: not armed
    b.on_frame(now=t("10:18"), feed=feed_at("10:18", 25100.0), book=Book())  # +100: armed, best 100
    b.on_frame(now=t("10:19"), feed=feed_at("10:19", 25060.0), book=Book())  # +60: gave back 40%, keep
    assert b.position is not None
    fv.leg = "ce"  # a fresh CE signal in the same minute must not buy again after the stop
    ev = b.on_frame(now=t("10:20"), feed=feed_at("10:20", 25050.0), book=Book(ce=130.0))  # +50: gave back 50% -> sell
    assert ev and ev["event"] == "close" and ev["reason"] == "trail_stop" and ev["exit"] == 129.5
    assert ev["pnl_gross"] == round((129.5 - 100.5) * 65, 2) and b.position is None
    votes = [r for r in ledger(b, votes=True) if r["event"] == "vote"]
    assert votes[2]["trail"]["armed"] is True and votes[-1]["trail"]["trail_stop"] is True
    assert votes[-1]["action"] == "sell CE (trailing SL), flat for the rest of the day"
    fv.leg = "pe"
    assert b.on_frame(now=t("10:21"), feed=feed_at("10:21", 25000.0), book=Book()) is None  # flat for the day
    assert [r["event"] for r in ledger(b)] == ["open", "close"]
    assert ledger(b, votes=True)[-1]["action"] == "no trade: trailing SL hit today"
    assert b.snapshot(Book())["trail"]["done"]["exit"] == 129.5


def test_trailing_sl_survives_a_restart(tmp_path: Path) -> None:
    fv = FixedVote("pe")
    b = PaperModelVote(path=tmp_path / "v.jsonl", models=fv)  # type: ignore[arg-type]
    b.on_frame(now=t("10:16"), feed=feed_at("10:16", 25000.0), book=Book())  # buy PE at 25000
    fv.leg = None
    b.on_frame(now=t("10:17"), feed=feed_at("10:17", 24900.0), book=Book())  # +100 the PE's way
    again = PaperModelVote(path=tmp_path / "v.jsonl", models=FixedVote(None))  # type: ignore[arg-type]
    again.on_frame(now=t("10:18"), feed=feed_at("10:18", 24940.0), book=Book())  # rebuilt: best 100, now +60
    assert again.trail is not None and again.trail["best"] == 100.0
    ev = again.on_frame(now=t("10:19"), feed=feed_at("10:19", 24955.0), book=Book())  # +45: sold
    assert ev and ev["reason"] == "trail_stop" and again.position is None
    third = PaperModelVote(path=tmp_path / "v.jsonl", models=FixedVote("ce"))  # type: ignore[arg-type]
    assert third.on_frame(now=t("10:20"), feed=feed_at("10:20", 25000.0), book=Book()) is None  # still flat today
    assert third.trail_done is not None and third.position is None
