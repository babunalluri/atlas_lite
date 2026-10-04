"""ATM±1 tick-rate depth tape (best bid/ask + sizes + 5-level book)."""

from __future__ import annotations

import asyncio
import gzip
import struct
import zipfile
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from atlas_lite.feed_engine import FeedEngine
from atlas_lite.instruments import AtmLegs, IndexOptionUniverse
from atlas_lite.kite_rest import KiteRest
from atlas_lite.kite_ws import QuoteBook, parse_binary_ticks
from atlas_lite.metrics import quote_ask_qty, quote_bid_qty, top_of_book
from atlas_lite.notebook import get_notebook
from atlas_lite.notebook_runtime import NotebookRuntime
from atlas_lite.recorder import (
    DEPTH_NAME_RE,
    SheetRecorder,
    depth_day_path,
    gzip_recording_file,
    list_recording_files,
    list_slot_recording_paths,
    maintain_recordings,
    recording_files_for_day,
    safe_recording_path,
    write_day_archive,
)

IST = ZoneInfo("Asia/Kolkata")


def _depth_row(
    *,
    bid: float | None,
    ask: float | None,
    bid_qty: int,
    ask_qty: int,
    ltp: float,
    buy_qty: int = 0,
    sell_qty: int = 0,
) -> dict:
    buy = [{"price": bid, "quantity": bid_qty, "orders": 1}] if bid is not None and bid_qty > 0 else [
        {"price": 0.0, "quantity": 0, "orders": 0}
    ]
    sell = (
        [{"price": ask, "quantity": ask_qty, "orders": 1}]
        if ask is not None and ask_qty > 0
        else [{"price": 0.0, "quantity": 0, "orders": 0}]
    )
    # pad to 5 levels
    while len(buy) < 5:
        buy.append({"price": 0.0, "quantity": 0, "orders": 0})
    while len(sell) < 5:
        sell.append({"price": 0.0, "quantity": 0, "orders": 0})
    if bid is not None and bid_qty > 0:
        buy[1] = {"price": bid - 0.05, "quantity": 50, "orders": 1}
    row = {
        "last_price": ltp,
        "bid": bid,
        "ask": ask,
        "buy_quantity": buy_qty or (bid_qty + 50 if bid_qty else 0),
        "sell_quantity": sell_qty or ask_qty,
        "depth": {"buy": buy, "sell": sell},
        "exchange_timestamp": 1_725_000_000,
        "last_trade_time": 1_725_000_001,
    }
    return row


def test_quote_bid_ask_qty_from_depth() -> None:
    row = _depth_row(bid=100.05, ask=100.2, bid_qty=750, ask_qty=1200, ltp=100.1)
    assert quote_bid_qty(row) == 750
    assert quote_ask_qty(row) == 1200
    book = top_of_book(row)
    assert book["ltp"] == 100.1
    assert book["bid"] == 100.05
    assert book["ask"] == 100.2
    assert book["bid_qty"] == 750.0
    assert book["ask_qty"] == 1200.0
    assert book["buy_qty"] == 800.0
    assert book["sell_qty"] == 1200.0
    assert book["buy"][0] == [100.05, 750.0]
    assert book["buy"][1] == [100.0, 50.0]
    assert book["exch_ts"] == 1_725_000_000
    assert book["trade_ts"] == 1_725_000_001


def test_quote_bid_qty_none_when_empty() -> None:
    assert quote_bid_qty(None) is None
    assert quote_ask_qty({"depth": {"buy": [], "sell": []}}) is None
    assert quote_bid_qty({"depth": {"buy": [{"price": 1.0, "quantity": 0}]}}) is None


def test_full_mode_clears_stale_bid_on_empty_side() -> None:
    """Empty bid side must write bid=None so merge cannot keep a crossed book."""
    packet = bytearray(184)
    token = 12107010  # NFO option segment
    struct.pack_into(">i", packet, 0, token)
    struct.pack_into(">i", packet, 4, 10_000)  # ltp 100.00
    struct.pack_into(">I", packet, 20, 0)  # total buy qty
    struct.pack_into(">I", packet, 24, 500)
    struct.pack_into(">I", packet, 44, 1_700_000_000)  # last trade time
    struct.pack_into(">I", packet, 48, 1_000)
    struct.pack_into(">I", packet, 60, 1_700_000_100)  # exchange ts
    # buy side empty (qty 0); sell top at 98.20
    struct.pack_into(">i", packet, 124, 250)
    struct.pack_into(">i", packet, 128, 9_820)
    struct.pack_into(">H", packet, 132, 1)
    payload = struct.pack(">HH", 1, 184) + bytes(packet)
    row = parse_binary_ticks(payload)[0]
    assert row["bid"] is None
    assert row["ask"] == 98.2
    assert row["last_trade_time"] == 1_700_000_000
    assert row["exchange_timestamp"] == 1_700_000_100

    book = QuoteBook()
    book.merge("NFO:X", {"bid": 99.9, "ask": 100.0, "depth": {"buy": [], "sell": []}})
    book.merge("NFO:X", row)
    merged = book.get("NFO:X")
    assert merged is not None
    assert merged["bid"] is None
    assert merged["ask"] == 98.2
    assert top_of_book(merged)["bid"] is None


def test_rest_overlay_does_not_overwrite_depth() -> None:
    engine = FeedEngine(rest=MagicMock(spec=KiteRest), data_dir=Path("data"))
    engine.book.merge(
        "NFO:CE",
        {
            "bid": 10.0,
            "ask": 10.2,
            "depth": {"buy": [{"price": 10.0, "quantity": 100}], "sell": [{"price": 10.2, "quantity": 80}]},
        },
    )
    engine._merge_quote_overlay(
        "NFO:CE",
        {
            "greeks": {"iv": 12.5, "delta": 0.5},
            "volume": 999,
            "oi": 12345,
            "bid": 9.0,
            "ask": 11.0,
            "depth": {"buy": [{"price": 9.0, "quantity": 1}], "sell": [{"price": 11.0, "quantity": 1}]},
        },
    )
    row = engine.book.get("NFO:CE")
    assert row is not None
    assert row["bid"] == 10.0
    assert row["ask"] == 10.2
    assert row["depth"]["buy"][0]["price"] == 10.0
    assert row["greeks"]["iv"] == 12.5
    assert row["volume"] == 999
    assert row["oi"] == 12345


def test_depth_day_path_and_name() -> None:
    now = datetime(2026, 10, 2, 11, 4, tzinfo=IST)  # Friday
    path = depth_day_path(Path("/tmp/rec"), now=now)
    assert path.name == "depth-2026-10-02.jsonl"
    assert DEPTH_NAME_RE.match(path.name)
    assert safe_recording_path(Path("/tmp/rec"), path.name).name == path.name
    assert safe_recording_path(Path("/tmp/rec"), path.name + ".gz").name == path.name + ".gz"


def test_append_depth_one_line(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ATLAS_LITE_RECORD", "1")
    now = datetime(2026, 10, 2, 11, 4, tzinfo=IST)  # Friday session
    rec = SheetRecorder(tmp_path)
    asyncio.run(rec.append_depth({"atm": 25000, "legs": {}}, now=now))
    path = depth_day_path(tmp_path, now=now)
    assert path.is_file()
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert '"atm":25000' in lines[0]


def test_day_archive_includes_depth(tmp_path: Path) -> None:
    day = "2026-10-02"
    (tmp_path / f"{day}_11-00.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / f"depth-{day}.jsonl").write_text("{}\n", encoding="utf-8")
    zip_path, _name = write_day_archive(tmp_path, day)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = set(zf.namelist())
    finally:
        zip_path.unlink(missing_ok=True)
    assert f"{day}_11-00.jsonl" in names
    assert f"depth-{day}.jsonl" in names


def test_history_list_puts_depth_after_structure(tmp_path: Path) -> None:
    day = "2026-10-02"
    slot = tmp_path / f"{day}_11-00.jsonl"
    structure = tmp_path / f"structure-{day}.jsonl"
    depth = tmp_path / f"depth-{day}.jsonl"
    slot.write_text("{}\n", encoding="utf-8")
    structure.write_text("{}\n", encoding="utf-8")
    depth.write_text("{}\n", encoding="utf-8")
    names = [row["name"] for row in list_recording_files(tmp_path)]
    assert names.index(slot.name) < names.index(structure.name)
    assert names.index(structure.name) < names.index(depth.name)


def test_maintain_recordings_gzips_and_prunes(tmp_path: Path) -> None:
    old = tmp_path / "depth-2025-01-01.jsonl"
    old.write_text('{"atm":1}\n' * 20, encoding="utf-8")
    keep = tmp_path / "depth-2026-10-01.jsonl"
    keep.write_text('{"atm":2}\n', encoding="utf-8")
    now = datetime(2026, 10, 2, 16, 0, tzinfo=IST)
    stats = maintain_recordings(tmp_path, now=now, keep_days=365)
    assert stats["gzipped"] >= 1
    assert not old.exists()
    # 2025-01-01 is outside 365-day window from 2026-10-02 → pruned
    assert not (tmp_path / "depth-2025-01-01.jsonl.gz").exists()
    assert (tmp_path / "depth-2026-10-01.jsonl.gz").is_file()


def test_maintain_skips_recently_closed_slot(tmp_path: Path) -> None:
    # Slot 11:00–11:30; at 11:30:30 it is still inside the 120s grace window.
    recent = tmp_path / "2026-10-02_11-00.jsonl"
    recent.write_text("{}\n", encoding="utf-8")
    now = datetime(2026, 10, 2, 11, 30, 30, tzinfo=IST)
    stats = maintain_recordings(tmp_path, now=now, keep_days=365, slot_grace_s=120)
    assert stats["gzipped"] == 0
    assert recent.is_file()


def test_gzip_recording_file(tmp_path: Path) -> None:
    path = tmp_path / "depth-2026-10-01.jsonl"
    path.write_text('{"a":1}\n{"a":2}\n', encoding="utf-8")
    gz = gzip_recording_file(path)
    assert gz is not None and gz.is_file()
    assert not path.exists()
    with gzip.open(gz, "rt", encoding="utf-8") as handle:
        assert '"a":1' in handle.read()


def test_prefer_gz_when_plain_and_compressed_exist(tmp_path: Path) -> None:
    day = "2026-10-02"
    plain = tmp_path / f"{day}_11-00.jsonl"
    gz = tmp_path / f"{day}_11-00.jsonl.gz"
    plain.write_text('{"tail":1}\n', encoding="utf-8")
    with gzip.open(gz, "wt", encoding="utf-8") as handle:
        handle.write('{"main":1}\n')
    slots = list_slot_recording_paths(tmp_path)
    assert [p.name for p in slots] == [gz.name]
    day_files = recording_files_for_day(tmp_path, day)
    assert [p.name for p in day_files] == [gz.name]
    zip_path, _ = write_day_archive(tmp_path, day)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            assert zf.namelist() == [gz.name]
    finally:
        zip_path.unlink(missing_ok=True)


def test_maintain_skips_when_lock_held(tmp_path: Path) -> None:
    import threading

    from atlas_lite import recorder as rec

    held = threading.Event()
    release = threading.Event()

    def holder() -> None:
        assert rec._MAINTAIN_LOCK.acquire(blocking=True)
        held.set()
        release.wait(timeout=2.0)
        rec._MAINTAIN_LOCK.release()

    t = threading.Thread(target=holder)
    t.start()
    assert held.wait(timeout=1.0)
    try:
        stats = maintain_recordings(tmp_path, now=datetime(2026, 10, 2, 16, 0, tzinfo=IST))
        assert stats["skipped"] == 1
        assert stats["gzipped"] == 0
    finally:
        release.set()
        t.join(timeout=2.0)


def test_enqueue_depth_tick_per_leg() -> None:
    uni = IndexOptionUniverse(
        name="NIFTY",
        fut_symbol="NFO:NIFTY26OCTFUT",
        fut_token=1,
        expiry=date(2026, 10, 7),
        prefix="NIFTY261007",
        strike_step=50,
    )
    atm = 25000
    engine = FeedEngine(rest=MagicMock(spec=KiteRest), data_dir=Path("data"))
    nb = NotebookRuntime(config=get_notebook("nifty"), enabled=True)
    nb.universe = uni
    nb.atm = AtmLegs(
        strike=atm,
        ce_symbol=uni.option_symbol(atm, "CE"),
        pe_symbol=uni.option_symbol(atm, "PE"),
    )
    engine.notebooks["nifty"] = nb
    engine._sync_depth_watch()
    engine._depth_recording = True
    sym = uni.option_symbol(atm, "CE")
    row = _depth_row(bid=10.0, ask=10.2, bid_qty=100, ask_qty=80, ltp=10.1)
    engine._enqueue_depth_tick(sym, row)
    engine._enqueue_depth_tick(sym, row)  # duplicate fingerprint ignored
    assert len(engine._depth_queue) == 1
    queued = engine._depth_queue[0]
    assert queued["leg"] == "+0CE"
    assert queued["bid"] == 10.0
    assert queued["buy_qty"] == 150.0
    assert len(queued["buy"]) >= 1
    assert queued["exch_ts"] == 1_725_000_000
