"""The LLM buyer's dashboard card says why a flat book is not being asked (pause, stretch limit, no side, late)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from atlas_lite import llm_buyer as M
from atlas_lite.feed_engine import FeedEngine

DAY = "2026-11-11"


def t(hms: str) -> datetime:
    return datetime.fromisoformat(f"{DAY}T{hms}+05:30")


def test_dashboard_says_why_a_flat_buyer_is_not_asked(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(M, "DAY_DIRECTION_RULE", True)
    monkeypatch.setattr(M, "LOSS_PAUSE_MIN", 10)
    b = M.LlmBuyer(path=tmp_path / "l.jsonl", lot_size=65)
    bar = lambda hm, c: {"t": f"{DAY} {hm}", "o": c, "h": c, "l": c, "c": c}  # noqa: E731
    b.last_feed = {"bars_today": [bar("09:15", 24000.0), bar("09:16", 24010.0)]}
    assert "no side allowed" in FeedEngine._buyer_waiting(b, t("10:00:00"))
    b.last_feed = {"bars_today": [bar("09:15", 24000.0), bar("09:16", 24100.0)]}  # +0.42%: calls allowed
    assert FeedEngine._buyer_waiting(b, t("10:00:00")) is None
    b.last_loss_exit_at = f"{DAY}T09:59:00+05:30"
    assert FeedEngine._buyer_waiting(b, t("10:03:00")) == "paused after a loss until 10:09"
    b.last_feed = {"bars_today": [bar("09:15", 24000.0), bar("09:34", 24300.0)]}  # +1.25%
    assert FeedEngine._buyer_waiting(b, t("10:03:00")).startswith("no new trades today: NIFTY moved 1.2%")
    assert FeedEngine._buyer_waiting(b, t("15:05:00")) == "no new trades after 15:00"
