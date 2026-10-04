"""Atlas Lite — FastAPI entrypoint (single-page NIFTY sheet).

Mutating agent endpoints honor optional ``ATLAS_LITE_API_TOKEN`` via header
``X-Atlas-Token`` or ``Authorization: Bearer``. When unset, local/dev stays open.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from atlas_lite.config import STREAM_INTERVAL_MS, load_settings
from atlas_lite.feed_engine import FeedEngine
from atlas_lite.frame_util import frame_revision
from atlas_lite.kite_rest import KiteRest
from atlas_lite.log_util import setup_logging
from atlas_lite.metrics import evaluate_sheet
from atlas_lite.notebook import notebook_list, parse_notebook
from atlas_lite.recorder import (
    list_recording_files,
    read_recording_day,
    read_recording_file,
    record_dir,
    record_enabled,
    safe_recording_path,
    write_day_archive,
)

setup_logging()

STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
DATA_DIR = Path(__file__).resolve().parents[1] / "data"
_engine: FeedEngine | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _engine
    settings = load_settings()
    rest = KiteRest(settings.api_key, settings.access_token)
    _engine = FeedEngine(
        rest=rest,
        data_dir=DATA_DIR,
        credentials_path=settings.credentials_path,
    )
    await _engine.start()
    yield
    if _engine:
        await _engine.stop()
    _engine = None


app = FastAPI(title="Atlas Lite", version="0.1.0", lifespan=lifespan)


def _agent_write_authorized(
    request: Request,
    *,
    x_atlas_token: str | None = None,
) -> None:
    """Require ATLAS_LITE_API_TOKEN when set; no-op when unset (local default).

    Token must arrive via ``X-Atlas-Token`` or ``Authorization: Bearer`` — never
    as a query string (access logs).
    """
    expected = os.environ.get("ATLAS_LITE_API_TOKEN", "").strip()
    if not expected:
        return
    got = (x_atlas_token or "").strip()
    if not got:
        auth = request.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            got = auth[7:].strip()
    if not got or len(got) != len(expected) or not secrets.compare_digest(got, expected):
        raise HTTPException(status_code=401, detail="invalid_or_missing_token")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/pred30.js")
async def pred30_js() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "pred30.js",
        media_type="text/javascript",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/orderflow.js")
async def orderflow_js() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "orderflow.js",
        media_type="text/javascript",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/levels.js")
async def levels_js() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "levels.js",
        media_type="text/javascript",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/health")
async def health() -> JSONResponse:
    if _engine is None:
        return JSONResponse(
            {"ok": False, "error": "engine not started"},
            status_code=503,
        )
    body = _engine.health_status()
    if not body.get("ok"):
        return JSONResponse(body, status_code=503)
    return JSONResponse(body)


@app.get("/api/notebooks")
async def api_notebooks() -> dict[str, Any]:
    """Available notebook underlyings (NIFTY / SENSEX). No global active switch."""
    return {"ok": True, "default": "nifty", "available": notebook_list()}


@app.get("/stream")
async def stream(
    nb: str = Query(default="nifty", description="Notebook: nifty | sensex"),
) -> StreamingResponse:
    notebook = parse_notebook(nb)

    async def event_gen():
        last_revision: tuple[Any, ...] | None = None
        interval = STREAM_INTERVAL_MS / 1000.0
        while True:
            if _engine is None:
                payload = {"ok": False, "error": "engine not started"}
                yield f"data: {json.dumps(payload)}\n\n"
                await asyncio.sleep(1.0)
                continue
            frame = _engine.build_frame(notebook)
            frame["evaluation"] = evaluate_sheet(frame.get("feed") or {})
            rev = frame_revision(frame)
            if rev != last_revision:
                last_revision = rev
                yield f"data: {json.dumps(frame, default=str)}\n\n"
            else:
                yield ": keepalive\n\n"
            seen_seq = _engine.book.tick_seq
            await _engine.book.wait_for_update(seen_seq, interval)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/chain")
async def api_option_chain(
    wings: int = Query(default=0, ge=0, le=50),
    extras: int = Query(default=0, ge=0, le=1),
    nb: str = Query(default="nifty", description="Notebook: nifty | sensex"),
) -> dict[str, Any]:
    """Live option chain (Kite WS). wings=0 returns full listed chain.

    extras=1 shows Vol/Bid/Ask/IV/greeks and refreshes REST greeks for ATM±wings.
    """
    if _engine is None:
        raise HTTPException(status_code=503, detail="engine not started")
    wing_strikes = None if wings == 0 else wings
    show_extras = extras == 1
    notebook = parse_notebook(nb)
    if show_extras:
        try:
            await _engine.ensure_chain_greeks(notebook)
        except Exception:  # noqa: BLE001
            pass
    return _engine.build_option_chain(
        notebook,
        wing_strikes=wing_strikes,
        extras=show_extras,
    )


@app.get("/api/paper")
async def api_paper() -> dict[str, Any]:
    """Paper books (iron fly + optional VWAP long). Never sends live orders."""
    if _engine is None:
        raise HTTPException(status_code=503, detail="engine not started")
    return _engine.paper_snapshot()


@app.get("/api/paper/trades")
async def api_paper_trades(
    limit: int = Query(default=200, ge=1, le=1000),
    day: Optional[str] = Query(default=None, description="IST day YYYY-MM-DD; omit for all"),
) -> dict[str, Any]:
    """Paper fills across books (open/close). Latest first. Never live orders."""
    if _engine is None:
        raise HTTPException(status_code=503, detail="engine not started")
    return await asyncio.to_thread(_engine.list_paper_trades, limit=limit, day=day)


@app.get("/api/agent")
async def api_agent_status() -> dict[str, Any]:
    """Paper agent status, gates, last decision. Never live orders."""
    if _engine is None:
        raise HTTPException(status_code=503, detail="engine not started")
    return _engine.agent_status()


@app.post("/api/agent/advise")
async def api_agent_advise(
    request: Request,
    dry_run: bool = Query(default=False, description="If true, do not apply gates/intents"),
    force: bool = Query(
        default=False,
        description="If true, call LLM even when sparse gate would skip",
    ),
    x_atlas_token: Optional[str] = Header(default=None, alias="X-Atlas-Token"),
) -> dict[str, Any]:
    """Run one OpenRouter advise cycle (paper tools only)."""
    _agent_write_authorized(request, x_atlas_token=x_atlas_token)
    if _engine is None:
        raise HTTPException(status_code=503, detail="engine not started")
    return await asyncio.to_thread(_engine.agent_advise, dry_run=dry_run, force=force)


@app.post("/api/agent/gates")
async def api_agent_gates(
    request: Request,
    book: str = Query(...),
    mode: str = Query(..., description="allow | pause | skip_entries"),
    until: Optional[str] = Query(default=None),
    reason: Optional[str] = Query(default=None),
    x_atlas_token: Optional[str] = Header(default=None, alias="X-Atlas-Token"),
) -> dict[str, Any]:
    """Manual gate override (wins over agent until cleared/expired)."""
    _agent_write_authorized(request, x_atlas_token=x_atlas_token)
    if _engine is None:
        raise HTTPException(status_code=503, detail="engine not started")
    try:
        row = _engine.set_agent_gate(
            book, mode, until=until, reason=reason, source="manual"
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "gate": row}


@app.get("/api/agent/macro")
async def api_agent_macro() -> dict[str, Any]:
    """Timestamped macro/sentiment snapshot used by the agent."""
    from atlas_lite.macro_sentiment import fetch_macro_snapshot

    return await asyncio.to_thread(fetch_macro_snapshot)


@app.get("/api/candles")
async def api_candles(
    limit: int = Query(default=800, ge=50, le=2000),
    since: Optional[int] = Query(
        default=None,
        ge=0,
        description="Unix seconds of last bar; returns bars with time >= since",
    ),
    nb: str = Query(default="nifty", description="Notebook: nifty | sensex"),
) -> dict[str, Any]:
    """Index 1m candles from Kite (NIFTY or SENSEX)."""
    if _engine is None:
        raise HTTPException(status_code=503, detail="engine not started")
    return _engine.candles(parse_notebook(nb), limit=limit, since=since)


@app.get("/api/recordings")
async def api_recordings_list() -> dict[str, Any]:
    files = await asyncio.to_thread(list_recording_files, record_dir(DATA_DIR))
    return {"ok": True, "enabled": record_enabled(), "files": files}


@app.get("/api/recordings/archive/{day}")
async def api_recordings_archive(day: str) -> FileResponse:
    """Zip all JSONL slots for an IST day (YYYY-MM-DD)."""
    try:
        zip_path, filename = await asyncio.to_thread(
            write_day_archive,
            record_dir(DATA_DIR),
            day,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=filename,
        background=BackgroundTask(_unlink_quiet, zip_path),
    )


@app.get("/api/recordings/{name}/download")
async def api_recordings_download(name: str) -> FileResponse:
    try:
        path = safe_recording_path(record_dir(DATA_DIR), name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"recording not found: {name}")
    media = "application/gzip" if name.endswith(".gz") else "application/x-ndjson"
    return FileResponse(
        path,
        media_type=media,
        filename=name,
    )


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


@app.get("/api/recordings/day/{day}")
async def api_recordings_day(
    day: str,
    limit: int = Query(default=1000, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    entry: str = Query(default="all"),
    gates: str = Query(default="all"),
    failing: str = Query(default=""),
) -> dict[str, Any]:
    """Filter every 30-minute slot on an IST day (YYYY-MM-DD)."""
    try:
        payload = await asyncio.to_thread(
            read_recording_day,
            record_dir(DATA_DIR),
            day,
            limit=limit,
            offset=offset,
            entry=entry,
            gates=gates,
            failing=failing,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"no recordings for {day}") from exc
    return {"ok": True, **payload}


@app.get("/api/recordings/{name}")
async def api_recordings_read(
    name: str,
    limit: int = Query(default=1000, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    entry: str = Query(default="all"),
    gates: str = Query(default="all"),
    failing: str = Query(default=""),
) -> dict[str, Any]:
    try:
        payload = await asyncio.to_thread(
            read_recording_file,
            record_dir(DATA_DIR),
            name,
            limit=limit,
            offset=offset,
            entry=entry,
            gates=gates,
            failing=failing,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"recording not found: {name}") from exc
    return {"ok": True, **payload}
