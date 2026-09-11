"""Atlas Lite — FastAPI entrypoint (single-page NIFTY sheet, no auth)."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
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


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "index.html",
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
    nb: str = Query(default="nifty", description="Notebook: nifty | sensex"),
) -> dict[str, Any]:
    """Live option chain (Kite WS). wings=0 returns full listed chain."""
    if _engine is None:
        raise HTTPException(status_code=503, detail="engine not started")
    wing_strikes = None if wings == 0 else wings
    return _engine.build_option_chain(parse_notebook(nb), wing_strikes=wing_strikes)


@app.get("/api/paper")
async def api_paper() -> dict[str, Any]:
    """Paper long-straddle state. Never sends live orders."""
    if _engine is None:
        raise HTTPException(status_code=503, detail="engine not started")
    return _engine.paper_snapshot()


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
    return FileResponse(
        path,
        media_type="application/x-ndjson",
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
