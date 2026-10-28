"""AI agent book is independent of the scorecard agent."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from atlas_lite.ai_agent_model import (
    FEATURE_NAMES,
    MIN_TEST_DAYS,
    MIN_TEST_TRADES,
    _model_decide,
    predict_action,
    save_model,
    simulate_action,
    skip_rising_short,
    walk_forward,
)
from atlas_lite.paper_agent import PaperAgent
from atlas_lite.ai_agent_learn import learn_due, run_daily_learn
from atlas_lite.paper_ai_agent import APPROVAL_NAME, PaperAiAgent, paper_ai_agent_enabled

IST = ZoneInfo("Asia/Kolkata")


class Book:
    def __init__(self) -> None:
        self.quotes: dict[str, dict] = {}

    def get(self, symbol: str):
        return self.quotes.get(symbol)


def _stamp(day: str, hm: str) -> datetime:
    return datetime.fromisoformat(f"{day}T{hm}:00+05:30")


def _rows_for_pattern() -> list[dict]:
    """Spot-up mornings where the put pays and the call loses. Later minutes are flat."""
    days = [
        "2026-09-01",
        "2026-09-02",
        "2026-09-03",
        "2026-09-04",
        "2026-09-07",
        "2026-09-08",
        "2026-09-09",
        "2026-09-10",
        "2026-09-11",
    ]
    rows: list[dict] = []
    for day in days:
        for minute in range(9 * 60 + 45, 10 * 60 + 6):
            hm = f"{minute // 60:02d}:{minute % 60:02d}"
            early = minute <= 9 * 60 + 48
            if minute <= 9 * 60 + 48:
                ce, pe = 100.0, 100.0
            elif minute == 9 * 60 + 49:
                ce, pe = 94.0, 106.0
            else:
                ce, pe = 88.0, 112.0
            rows.append(
                {
                    "date": day,
                    "hm": hm,
                    "spot": 22520.0,
                    "spot_chg_open_pct": 0.4 if early else 0.0,
                    "ce": ce,
                    "pe": pe,
                    "adx": 30.0 if early else 10.0,
                    "atr": 8.0,
                    "pcr": 1.0,
                    "nifty_chg": 0.3 if early else 0.0,
                }
            )
    return rows


def test_option_path_uses_the_same_strike_only() -> None:
    from atlas_lite.ai_agent_model import FEATURE_NAMES, _attach_premium_change, _train_target, features_from_row

    assert _train_target(5000) == 1300
    assert _train_target(-5000) == -1300
    days = {
        "2026-10-08": {
            "10:00": {"date": "2026-10-08", "hm": "10:00", "atm": 22500, "ce": 100.0, "pe": 100.0, "spot": 22510.0},
            "10:15": {
                "date": "2026-10-08",
                "hm": "10:15",
                "atm": 22500,
                "ce": 110.0,
                "pe": 90.0,
                "spot": 22520.0,
                "expiry": "2026-10-13",
            },
            "10:16": {"date": "2026-10-08", "hm": "10:16", "atm": 22550, "ce": 80.0, "pe": 120.0, "spot": 22540.0},
        }
    }
    _attach_premium_change(days)
    assert days["2026-10-08"]["10:15"]["ce_chg_15"] == 10
    assert days["2026-10-08"]["10:16"]["ce_chg_15"] is None
    feats = features_from_row(days["2026-10-08"]["10:15"], day_open=22500.0)
    assert len(feats) == len(FEATURE_NAMES)
    assert abs(feats[FEATURE_NAMES.index("strike_gap")] - 0.4) < 1e-9
    assert abs(feats[FEATURE_NAMES.index("ce_chg")] - 1.0) < 1e-9
    assert feats[FEATURE_NAMES.index("days_left")] > 5
    blank = dict(days["2026-10-08"]["10:15"])
    blank["expiry"] = None
    derived = features_from_row(blank, day_open=22500.0)
    assert derived[FEATURE_NAMES.index("days_left")] > 5


def test_tuesday_holiday_expiry_moves_to_monday() -> None:
    from datetime import date

    from atlas_lite.ai_agent_model import _weekly_expiry

    assert _weekly_expiry(date(2026, 9, 4)) == date(2026, 9, 8)
    assert _weekly_expiry(date(2026, 10, 19)) == date(2026, 10, 19)


def test_premium_is_remembered_before_the_entry_window(tmp_path) -> None:
    paper = PaperAiAgent(path=tmp_path / "paper_ai_agent.jsonl", model_path=tmp_path / "missing.json", lot_size=65)
    kwargs = {
        "feed": {"ce": 100.0, "pe": 100.0, "atm": 22500},
        "book": Book(),
        "ce_symbol": "NIFTY25O1322500CE",
        "pe_symbol": "NIFTY25O1322500PE",
        "atm": 22500,
    }
    assert paper.on_frame(now=_stamp("2026-10-13", "09:20"), **kwargs) is None
    kwargs["feed"] = {"ce": 110.0, "pe": 90.0, "atm": 22500}
    assert paper.on_frame(now=_stamp("2026-10-13", "09:35"), **kwargs) is None
    ce_chg, pe_chg = paper._premium_change(_stamp("2026-10-13", "09:35"), 22500)
    assert ce_chg == 10
    assert pe_chg == -10


def test_sticky_feed_strike_is_not_treated_as_one_contract() -> None:
    """Spot still rounds to 22450, but the feed has stepped up to 22500."""
    minutes = {
        "10:00": {"date": "2026-10-08", "hm": "10:00", "spot": 22470.0, "atm": 22450, "ce": 120.0, "pe": 110.0},
        "10:01": {"date": "2026-10-08", "hm": "10:01", "spot": 22472.0, "atm": 22450, "ce": 121.0, "pe": 109.0},
        "10:02": {"date": "2026-10-08", "hm": "10:02", "spot": 22474.0, "atm": 22500, "ce": 98.0, "pe": 132.0},
    }
    outcome = simulate_action(minutes, minutes["10:00"], "short_ce", qty=65)
    assert outcome is not None
    net, exit_hm = outcome
    assert exit_hm == "10:01"
    assert net < 0


def test_frozen_tape_is_left_out_of_the_fit() -> None:
    rows = _rows_for_pattern()
    rows.append({"date": "2026-10-02", "hm": "10:00", "spot": 22421.0, "ce": 128.0, "pe": 128.0, "atm": 22400})
    rows.append({"date": "2026-10-02", "hm": "10:01", "spot": 22421.0, "ce": 128.0, "pe": 128.0, "atm": 22400})
    report = walk_forward(rows)
    assert "2026-10-02" not in (report.get("train_days") or [])
    assert "2026-10-02" not in (report.get("test_days") or [])


def test_strike_change_does_not_use_the_new_contract() -> None:
    minutes = {
        "10:00": {"date": "2026-10-12", "hm": "10:00", "spot": 22510.0, "ce": 100.0, "pe": 100.0},
        "10:04": {"date": "2026-10-12", "hm": "10:04", "spot": 22520.0, "ce": 101.0, "pe": 99.0},
        "10:05": {"date": "2026-10-12", "hm": "10:05", "spot": 22560.0, "ce": 180.0, "pe": 40.0},
    }
    outcome = simulate_action(minutes, minutes["10:00"], "long_ce", qty=65)
    assert outcome is not None
    net, exit_hm = outcome
    assert exit_hm == "10:04"
    assert net < 100


def test_restart_does_not_carry_yesterday_pnl_into_an_open_trade(tmp_path) -> None:
    path = tmp_path / "paper_ai_agent.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event": "close",
                        "strategy": "ai_agent",
                        "ts": "2026-10-09T15:10:00+05:30",
                        "day": "2026-10-09",
                        "day_pnl": -1800.0,
                        "pnl": -1800.0,
                    }
                ),
                json.dumps(
                    {
                        "event": "open",
                        "strategy": "ai_agent",
                        "ts": "2026-10-12T10:00:00+05:30",
                        "day": "2026-10-12",
                        "atm": 22500,
                        "side": "ce",
                        "style": "long",
                        "symbol": "NIFTY25O1322500CE",
                        "ce_symbol": "NIFTY25O1322500CE",
                        "pe_symbol": "NIFTY25O1322500PE",
                        "qty": 65,
                        "lots": 1,
                        "entry": 100.0,
                        "target": 110.0,
                        "stop": 94.0,
                        "hold_until": "2026-10-12T10:20:00+05:30",
                        "charges": 20.0,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    book = PaperAiAgent(path=path, model_path=tmp_path / "missing.json", lot_size=65)
    assert book.position is not None
    assert book.position.day == "2026-10-12"
    assert book.day_pnl == 0.0
    assert book.last_exit_at is None


def test_learn_runs_once_after_the_close() -> None:
    monday = datetime(2026, 10, 12, 15, 40, tzinfo=IST)
    assert learn_due(monday, "") is True
    assert learn_due(monday.replace(hour=15, minute=39), "") is False
    assert learn_due(monday, "2026-10-12") is False
    assert learn_due(monday.replace(day=17), "") is False


def test_short_sample_does_not_replace_the_model(tmp_path) -> None:
    model = tmp_path / "ai_agent_model.json"
    model.write_text('{"promoted": true, "weights": {"long_ce": [1]}}\n', encoding="utf-8")
    report = run_daily_learn(tmp_path, tmp_path / "recordings")
    assert report["reason"] == "not_enough_days"
    assert '"promoted": true' in model.read_text(encoding="utf-8")


def test_reload_picks_up_the_after_close_fit(tmp_path) -> None:
    paper = PaperAiAgent(path=tmp_path / "paper_ai_agent.jsonl", lot_size=65)
    assert paper.model_path == tmp_path / "ai_agent_model.json"
    weights = {name: [0.0] * len(FEATURE_NAMES) for name in ("long_ce", "long_pe", "short_ce", "short_pe")}
    weights["long_pe"][0] = 5000.0
    save_model(
        paper.model_path,
        {"promoted": True, "reason": "test", "feature_names": list(FEATURE_NAMES), "weights": weights},
    )
    assert paper.reload_model() is True
    assert paper.promoted is True
    assert paper.last_action is None


def test_default_is_on(monkeypatch) -> None:
    monkeypatch.delenv("ATLAS_LITE_PAPER_AI_AGENT", raising=False)
    assert paper_ai_agent_enabled() is True


def test_flat_round_trip_pays_half_a_point_each_side() -> None:
    minutes = {}
    for minute in range(10 * 60, 10 * 60 + 21):
        hm = f"{minute // 60:02d}:{minute % 60:02d}"
        minutes[hm] = {"date": "2026-10-08", "hm": hm, "spot": 22520.0, "atm": 22500, "ce": 100.0, "pe": 100.0}
    outcome = simulate_action(minutes, minutes["10:00"], "long_ce", qty=65)
    assert outcome is not None
    net, exit_hm = outcome
    assert exit_hm == "10:20"
    assert net < -65


def test_history_window_moves_with_the_date() -> None:
    from datetime import date

    from scripts.fetch_ai_agent_history import _listed_cutoff

    assert _listed_cutoff(date(2026, 10, 12)) == date(2026, 11, 16)
    assert _listed_cutoff(date(2026, 12, 1)) > date(2026, 11, 3)


def test_small_sample_is_not_promoted() -> None:
    report = walk_forward(_rows_for_pattern())
    assert report["promoted"] is False
    assert len(report["test_days"]) >= MIN_TEST_DAYS
    assert report["test_trades"] < MIN_TEST_TRADES
    assert report["reason"] == "not_enough_test_trades"


def test_flat_tape_is_not_promoted() -> None:
    rows = []
    for day in ("2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-07", "2026-09-08"):
        for minute in range(9 * 60 + 45, 10 * 60):
            rows.append(
                {
                    "date": day,
                    "hm": f"{minute // 60:02d}:{minute % 60:02d}",
                    "spot": 22500.0,
                    "ce": 100.0,
                    "pe": 100.0,
                    "adx": 10.0,
                    "atr": 4.0,
                    "pcr": 1.0,
                    "nifty_chg": 0.0,
                }
            )
    report = walk_forward(rows)
    assert report["promoted"] is False


def test_missing_model_does_not_open_and_does_not_touch_the_agent_ledger(tmp_path) -> None:
    agent_path = tmp_path / "paper_agent.jsonl"
    agent = PaperAgent(path=agent_path, lot_size=65)
    book = PaperAiAgent(path=tmp_path / "paper_ai_agent.jsonl", model_path=tmp_path / "missing.json", lot_size=65)
    quotes = Book()
    quotes.quotes["NIFTY25O1322500CE"] = {"last_price": 100.0}
    quotes.quotes["NIFTY25O1322500PE"] = {"last_price": 100.0}
    now = _stamp("2026-10-12", "10:00")
    frame = dict(
        feed={"ce": 100.0, "pe": 100.0, "adx": 30.0, "spot_chg_open_pct": 0.4, "atr": 8.0, "pcr": 1.0},
        book=quotes,
        ce_symbol="NIFTY25O1322500CE",
        pe_symbol="NIFTY25O1322500PE",
        atm=22500,
        spot=22570.0,
    )
    assert book.on_frame(now=now, **frame) is None
    opened = book.on_frame(now=now + timedelta(minutes=1), **frame)
    assert opened is None
    assert book.last_reject == "model_missing"
    assert book.position is None
    assert agent.position is None
    assert not agent_path.exists()
    assert not (tmp_path / "paper_ai_agent.jsonl").exists()


def test_failed_model_is_shadow_only(tmp_path) -> None:
    model_path = tmp_path / "ai_agent_model.json"
    weights = {name: [0.0] * len(FEATURE_NAMES) for name in ("long_ce", "long_pe", "short_ce", "short_pe")}
    weights["long_ce"][0] = 5000.0
    save_model(
        model_path,
        {
            "promoted": False,
            "reason": "does_not_beat_scorecard",
            "feature_names": list(FEATURE_NAMES),
            "weights": weights,
        },
    )
    paper = PaperAiAgent(path=tmp_path / "paper_ai_agent.jsonl", model_path=model_path, lot_size=65)
    quotes = Book()
    ce = "NIFTY25O1322500CE"
    pe = "NIFTY25O1322500PE"
    quotes.quotes[ce] = {"last_price": 100.0}
    quotes.quotes[pe] = {"last_price": 90.0}
    frame = dict(
        feed={"ce": 100.0, "pe": 90.0, "adx": 25.0, "spot_chg_open_pct": 0.2, "atr": 5.0, "pcr": 1.0},
        book=quotes,
        ce_symbol=ce,
        pe_symbol=pe,
        atm=22500,
        spot=22540.0,
    )
    assert paper.on_frame(now=_stamp("2026-10-12", "10:00"), **frame) is None
    assert paper.shadow is None
    opened = paper.on_frame(now=_stamp("2026-10-12", "10:01"), **frame)
    assert paper.position is None
    assert paper.shadow is not None
    assert opened is not None and opened["event"] == "shadow_open"
    quotes.quotes[ce] = {"last_price": 112.0}
    closed = paper.on_frame(
        now=_stamp("2026-10-12", "10:05"),
        feed={"ce": 112.0, "pe": 90.0},
        book=quotes,
        ce_symbol=ce,
        pe_symbol=pe,
        atm=22500,
        spot=22580.0,
    )
    assert closed is not None and closed["event"] == "shadow_close"
    assert closed["reason"] == "target"
    assert paper.position is None
    assert paper.day_pnl == 0.0


def _approve(model_path) -> None:
    trained_at = json.loads(model_path.read_text(encoding="utf-8"))["trained_at"]
    (model_path.parent / APPROVAL_NAME).write_text(trained_at + "\n", encoding="utf-8")


def test_promoted_model_without_approval_only_shadows(tmp_path) -> None:
    model_path = tmp_path / "ai_agent_model.json"
    weights = {name: [0.0] * len(FEATURE_NAMES) for name in ("long_ce", "long_pe", "short_ce", "short_pe")}
    weights["long_ce"][0] = 5000.0
    save_model(model_path, {"promoted": True, "reason": "test", "feature_names": list(FEATURE_NAMES), "weights": weights})
    paper = PaperAiAgent(path=tmp_path / "paper_ai_agent.jsonl", model_path=model_path, lot_size=65)
    assert paper.promoted is True
    assert paper.approved is False
    quotes = Book()
    ce = "NIFTY25O1322500CE"
    quotes.quotes[ce] = {"last_price": 100.0}
    quotes.quotes["NIFTY25O1322500PE"] = {"last_price": 100.0}
    frame = dict(
        feed={"ce": 100.0, "pe": 90.0, "adx": 25.0, "spot_chg_open_pct": 0.2, "atr": 5.0, "pcr": 1.0},
        book=quotes,
        ce_symbol=ce,
        pe_symbol="NIFTY25O1322500PE",
        atm=22500,
        spot=22540.0,
    )
    assert paper.on_frame(now=_stamp("2026-10-12", "10:00"), **frame) is None
    opened = paper.on_frame(now=_stamp("2026-10-12", "10:01"), **frame)
    assert paper.position is None
    assert opened is not None and opened["event"] == "shadow_open"


def test_open_shadow_is_restored_after_a_restart(tmp_path) -> None:
    path = tmp_path / "paper_ai_agent.jsonl"
    path.write_text(
        json.dumps(
            {
                "event": "shadow_open",
                "strategy": "ai_agent",
                "ts": "2026-10-12T10:00:00+05:30",
                "day": "2026-10-12",
                "atm": 22500,
                "side": "ce",
                "style": "long",
                "symbol": "NIFTY25O1322500CE",
                "ce_symbol": "NIFTY25O1322500CE",
                "pe_symbol": "NIFTY25O1322500PE",
                "qty": 65,
                "lots": 1,
                "entry": 100.0,
                "target": 110.0,
                "stop": 94.0,
                "hold_until": "2026-10-12T10:20:00+05:30",
                "charges": 20.0,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    paper = PaperAiAgent(path=path, model_path=tmp_path / "missing.json", lot_size=65)
    assert paper.position is None
    assert paper.shadow is not None
    assert paper.shadow.symbol == "NIFTY25O1322500CE"
    assert paper.shadow.entry == 100.0
    assert paper.entries_today == 1


def test_loaded_model_opens_its_own_ledger_and_exits_on_target(tmp_path) -> None:
    model_path = tmp_path / "ai_agent_model.json"
    weights = {name: [0.0] * len(FEATURE_NAMES) for name in ("long_ce", "long_pe", "short_ce", "short_pe")}
    weights["long_ce"][0] = 5000.0
    save_model(
        model_path,
        {
            "promoted": True,
            "reason": "test",
            "feature_names": list(FEATURE_NAMES),
            "weights": weights,
            "test_net": 1.0,
            "scorecard_test_net": 0.0,
        },
    )
    _approve(model_path)
    paper = PaperAiAgent(path=tmp_path / "paper_ai_agent.jsonl", model_path=model_path, lot_size=65)
    quotes = Book()
    ce = "NIFTY25O1322500CE"
    pe = "NIFTY25O1322500PE"
    quotes.quotes[ce] = {"last_price": 100.0}
    quotes.quotes[pe] = {"last_price": 100.0}
    now = _stamp("2026-10-12", "10:00")
    frame = dict(
        feed={"ce": 100.0, "pe": 90.0, "adx": 25.0, "spot_chg_open_pct": 0.2, "atr": 5.0, "pcr": 1.0, "index_nifty_chg": 0.2},
        book=quotes,
        ce_symbol=ce,
        pe_symbol=pe,
        atm=22500,
        spot=22540.0,
    )
    assert paper.on_frame(now=now, **frame) is None
    opened = paper.on_frame(now=now + timedelta(minutes=1), **frame)
    assert opened is not None
    assert opened["strategy"] == "ai_agent"
    assert opened["side"] == "ce"
    assert opened["style"] == "long"
    assert paper.position is not None
    assert paper.position.target == 110.0
    quotes.quotes[ce] = {"last_price": 112.0}
    closed = paper.on_frame(
        now=_stamp("2026-10-12", "10:05"),
        feed={"ce": 112.0, "pe": 90.0},
        book=quotes,
        ce_symbol=ce,
        pe_symbol=pe,
        atm=22500,
        spot=22580.0,
    )
    assert closed is not None
    assert closed["reason"] == "target"
    assert closed["pnl"] is not None and closed["pnl"] > 0
    assert paper.position is None
    text = (tmp_path / "paper_ai_agent.jsonl").read_text(encoding="utf-8")
    assert "\"strategy\":\"ai_agent\"" in text
    action, predicted = predict_action(weights, [1.0] + [0.0] * (len(FEATURE_NAMES) - 1))
    assert action == "long_ce"
    assert predicted > 0


def _weights(bias_action: str) -> dict:
    weights = {name: [0.0] * len(FEATURE_NAMES) for name in ("long_ce", "long_pe", "short_ce", "short_pe")}
    weights[bias_action][0] = 5000.0
    return weights


def test_a_spike_inside_the_minute_does_not_open(tmp_path) -> None:
    model_path = tmp_path / "ai_agent_model.json"
    save_model(model_path, {"promoted": False, "reason": "test", "feature_names": list(FEATURE_NAMES), "weights": _weights("short_pe")})
    paper = PaperAiAgent(path=tmp_path / "paper_ai_agent.jsonl", model_path=model_path, lot_size=65)
    quotes = Book()
    ce, pe = "NIFTY26O1923000CE", "NIFTY26O1923000PE"
    quotes.quotes[ce] = {"last_price": 40.0}
    quotes.quotes[pe] = {"last_price": 80.0}
    frame = dict(
        feed={"ce": 40.0, "pe": 80.0, "adx": 25.0, "spot_chg_open_pct": -0.4, "atr": 8.0, "pcr": 1.2},
        book=quotes,
        ce_symbol=ce,
        pe_symbol=pe,
        atm=23000,
        spot=22980.0,
    )
    assert paper.on_frame(now=_stamp("2026-10-19", "11:34"), **frame) is None
    quotes.quotes[pe] = {"last_price": 95.0}
    frame["feed"] = {"ce": 30.0, "pe": 95.0, "adx": 25.0, "spot_chg_open_pct": -0.6, "atr": 8.0, "pcr": 1.3}
    assert paper.on_frame(now=_stamp("2026-10-19", "11:34").replace(second=38), **frame) is None
    assert paper.shadow is None
    opened = paper.on_frame(now=_stamp("2026-10-19", "11:35"), **frame)
    assert opened is not None and opened["event"] == "shadow_open"
    assert opened["style"] == "short"
    assert opened["side"] == "pe"


def test_short_is_skipped_when_that_option_is_already_up(tmp_path) -> None:
    model_path = tmp_path / "ai_agent_model.json"
    save_model(model_path, {"promoted": False, "reason": "test", "feature_names": list(FEATURE_NAMES), "weights": _weights("short_pe")})
    paper = PaperAiAgent(path=tmp_path / "paper_ai_agent.jsonl", model_path=model_path, lot_size=65)
    quotes = Book()
    ce, pe = "NIFTY26O1923000CE", "NIFTY26O1923000PE"
    quotes.quotes[ce] = {"last_price": 40.0}
    quotes.quotes[pe] = {"last_price": 70.0}

    def frame(hm: str, put: float, atm: int = 23000):
        quotes.quotes[pe] = {"last_price": put}
        return paper.on_frame(
            now=_stamp("2026-10-19", hm),
            feed={"ce": 40.0, "pe": put, "adx": 25.0, "spot_chg_open_pct": -0.4, "atr": 8.0, "pcr": 1.2},
            book=quotes,
            ce_symbol=ce,
            pe_symbol=pe,
            atm=atm,
            spot=22980.0,
        )

    assert frame("09:30", 40.0) is None
    assert frame("09:45", 70.0) is None
    assert frame("09:46", 70.0) is None
    assert paper.shadow is None
    assert paper.last_reject == "rising_premium"
    assert skip_rising_short("short_pe", None, 30.0) == "wait"
    assert skip_rising_short("short_pe", None, -5.0) == "short_pe"
    assert skip_rising_short("short_pe", None, None) == "short_pe"
    assert skip_rising_short("long_ce", 12.0, 12.0) == "long_ce"


def test_a_new_strike_can_still_be_sold(tmp_path) -> None:
    model_path = tmp_path / "ai_agent_model.json"
    save_model(model_path, {"promoted": False, "reason": "test", "feature_names": list(FEATURE_NAMES), "weights": _weights("short_pe")})
    paper = PaperAiAgent(path=tmp_path / "paper_ai_agent.jsonl", model_path=model_path, lot_size=65)
    quotes = Book()
    old_pe, new_pe = "NIFTY26O1923050PE", "NIFTY26O1923000PE"
    ce = "NIFTY26O1923000CE"
    quotes.quotes[ce] = {"last_price": 50.0}
    quotes.quotes[old_pe] = {"last_price": 40.0}
    quotes.quotes[new_pe] = {"last_price": 79.0}
    assert paper.on_frame(
        now=_stamp("2026-10-19", "09:30"),
        feed={"ce": 70.0, "pe": 40.0, "adx": 20.0, "spot_chg_open_pct": -0.2, "atr": 8.0, "pcr": 1.1},
        book=quotes,
        ce_symbol="NIFTY26O1923050CE",
        pe_symbol=old_pe,
        atm=23050,
        spot=23040.0,
    ) is None
    assert paper.on_frame(
        now=_stamp("2026-10-19", "09:45"),
        feed={"ce": 50.0, "pe": 79.0, "adx": 28.0, "spot_chg_open_pct": -0.5, "atr": 8.0, "pcr": 1.0},
        book=quotes,
        ce_symbol=ce,
        pe_symbol=new_pe,
        atm=23000,
        spot=22990.0,
    ) is None
    opened = paper.on_frame(
        now=_stamp("2026-10-19", "09:46"),
        feed={"ce": 50.0, "pe": 79.0, "adx": 28.0, "spot_chg_open_pct": -0.5, "atr": 8.0, "pcr": 1.0},
        book=quotes,
        ce_symbol=ce,
        pe_symbol=new_pe,
        atm=23000,
        spot=22990.0,
    )
    assert opened is not None and opened["event"] == "shadow_open"
    assert opened["side"] == "pe"
    assert opened["atm"] == 23000


def test_replay_skips_a_short_that_is_already_up() -> None:
    decide = _model_decide(_weights("short_pe"))
    row = {
        "hm": "10:00",
        "date": "2026-10-13",
        "spot": 23000.0,
        "atm": 23000,
        "ce": 40.0,
        "pe": 80.0,
        "adx": 20.0,
        "atr": 8.0,
        "pcr": 1.1,
        "ce_chg_15": -4.0,
        "pe_chg_15": 12.0,
    }
    assert decide(row, 23100.0) == "wait"
    row["pe_chg_15"] = -5.0
    assert decide(row, 23100.0) == "short_pe"
    row["pe_chg_15"] = None
    assert decide(row, 23100.0) == "short_pe"


def test_a_missing_quote_is_retried_on_the_same_bar(tmp_path) -> None:
    model_path = tmp_path / "ai_agent_model.json"
    save_model(model_path, {"promoted": False, "reason": "test", "feature_names": list(FEATURE_NAMES), "weights": _weights("short_pe")})
    paper = PaperAiAgent(path=tmp_path / "paper_ai_agent.jsonl", model_path=model_path, lot_size=65)
    quotes = Book()
    ce, pe = "NIFTY26O1923000CE", "NIFTY26O1923000PE"
    frame = dict(
        feed={"ce": 40.0, "pe": 80.0, "adx": 25.0, "spot_chg_open_pct": -0.4, "atr": 8.0, "pcr": 1.2},
        book=quotes,
        ce_symbol=ce,
        pe_symbol=pe,
        atm=23000,
        spot=22980.0,
    )
    assert paper.on_frame(now=_stamp("2026-10-19", "11:34"), **frame) is None
    missed = paper.on_frame(now=_stamp("2026-10-19", "11:35"), **frame)
    assert missed is None
    assert paper.shadow is None
    assert paper.last_reject == "no_quote"
    quotes.quotes[ce] = {"last_price": 40.0}
    quotes.quotes[pe] = {"last_price": 80.0}
    opened = paper.on_frame(now=_stamp("2026-10-19", "11:35").replace(second=1), **frame)
    assert opened is not None and opened["event"] == "shadow_open"
    assert opened["entry"] == 80.0


def test_approve_writes_stamp_for_promoted_model(tmp_path) -> None:
    model_path = tmp_path / "ai_agent_model.json"
    weights = {name: [0.0] * len(FEATURE_NAMES) for name in ("long_ce", "long_pe", "short_ce", "short_pe")}
    save_model(
        model_path,
        {
            "promoted": True,
            "reason": "test",
            "feature_names": list(FEATURE_NAMES),
            "weights": weights,
        },
    )
    trained_at = json.loads(model_path.read_text(encoding="utf-8"))["trained_at"]
    paper = PaperAiAgent(path=tmp_path / "paper_ai_agent.jsonl", model_path=model_path, lot_size=65)
    assert paper.approved is False
    assert paper.can_open is False
    out = paper.approve()
    assert out["ok"] is True
    assert out["approved"] is True
    assert out["can_open"] is True
    assert paper.approved is True
    assert paper.can_open is True
    stamp = (tmp_path / APPROVAL_NAME).read_text(encoding="utf-8").strip()
    assert stamp == trained_at


def test_approve_rejects_when_not_promoted(tmp_path) -> None:
    model_path = tmp_path / "ai_agent_model.json"
    weights = {name: [0.0] * len(FEATURE_NAMES) for name in ("long_ce", "long_pe", "short_ce", "short_pe")}
    save_model(
        model_path,
        {
            "promoted": False,
            "reason": "test",
            "feature_names": list(FEATURE_NAMES),
            "weights": weights,
            "trained_at": "2026-10-28T10:16:03+00:00",
        },
    )
    paper = PaperAiAgent(path=tmp_path / "paper_ai_agent.jsonl", model_path=model_path, lot_size=65)
    out = paper.approve()
    assert out["ok"] is False
    assert out["error"] == "not_promoted"
    assert paper.approved is False
    assert not (tmp_path / APPROVAL_NAME).exists()


def test_approve_rejects_missing_trained_at(tmp_path) -> None:
    model_path = tmp_path / "ai_agent_model.json"
    weights = {name: [0.0] * len(FEATURE_NAMES) for name in ("long_ce", "long_pe", "short_ce", "short_pe")}
    # Bypass save_model so trained_at is absent.
    model_path.write_text(
        json.dumps(
            {
                "promoted": True,
                "reason": "test",
                "feature_names": list(FEATURE_NAMES),
                "weights": weights,
            }
        ),
        encoding="utf-8",
    )
    paper = PaperAiAgent(path=tmp_path / "paper_ai_agent.jsonl", model_path=model_path, lot_size=65)
    out = paper.approve()
    assert out["ok"] is False
    assert out["error"] == "no_trained_at"


def test_approve_ai_agent_engine_disabled(tmp_path) -> None:
    from unittest.mock import MagicMock

    from atlas_lite.feed_engine import FeedEngine
    from atlas_lite.kite_rest import KiteRest

    eng = FeedEngine(rest=MagicMock(spec=KiteRest), data_dir=tmp_path)
    eng._paper_ai_agent = None
    out = eng.approve_ai_agent()
    assert out["ok"] is False
    assert out["error"] == "ai_agent_disabled"


def test_approve_api_returns_400_when_disabled(tmp_path, monkeypatch) -> None:
    from unittest.mock import MagicMock

    from fastapi.testclient import TestClient

    import atlas_lite.main as main
    from atlas_lite.feed_engine import FeedEngine
    from atlas_lite.kite_rest import KiteRest

    eng = FeedEngine(rest=MagicMock(spec=KiteRest), data_dir=tmp_path)
    eng._paper_ai_agent = None
    monkeypatch.setattr(main, "_engine", eng)
    client = TestClient(main.app)
    resp = client.post("/api/paper/ai_agent/approve")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "ai_agent_disabled"
