"""Edge GEX Lab FastAPI app (port 8240). Shapes follow the v1 API contract.

Run: launch.bat, or
  .venv\\Scripts\\python -m uvicorn gexlab.api:app --host 127.0.0.1 --port 8240
(launch.bat takes the port from GEXLAB_PORT, 8240 by default.)
GEXLAB_DEMO=1 serves synthetic fixtures with no keys and no network.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import date

from fastapi import FastAPI, HTTPException, Query
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from gexlab import candles, config, history, service, update_check
from gexlab.sources.base import SourceError

log = logging.getLogger("gexlab.api")


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    update_check.start_background_check()      # at most once a day, silent offline, GEXLAB_NO_UPDATE_CHECK=1 skips it
    history.start_recorder()                   # crypto GEX snapshots every 5 min; GEXLAB_RECORD=0 turns it off
    yield
    history.stop_recorder()


app = FastAPI(title="Edge GEX Lab", version=config.MODEL_VERSION, lifespan=_lifespan)
# Refuse rebound hostnames before any route can expose local data.
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"], www_redirect=False)
app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:5240", "http://localhost:5240"],
                   allow_methods=["GET", "POST"], allow_headers=["*"])


def _ticker(t: str) -> str:
    t = (t or "").strip().upper()
    if not t or len(t) > 10 or not t.replace(".", "").isalnum():
        raise HTTPException(400, "invalid ticker")
    return t


def _vendor_error(e: SourceError) -> HTTPException:
    if e.status == 404:
        return HTTPException(404, str(e))
    if e.status == 401:
        return HTTPException(503, "Alpaca rejected the configured keys (HTTP 401).")
    return HTTPException(503, f"Data source unavailable: {e}")


@app.get("/api/health")
def health():
    return service.health()


@app.get("/api/update")
def update():
    """Is a newer release out? From the day-old cache, or one request to GitHub (3 s timeout). Never fails."""
    return update_check.check()


@app.get("/api/tickers")
def tickers():
    return {"pinned": config.PINNED_TICKERS}


@app.get("/api/search")
def search(q: str = Query("", max_length=40)):
    return service.search(q)


@app.get("/api/expirations")
def expirations(ticker: str):
    try:
        return service.expirations(_ticker(ticker))
    except SourceError as e:
        raise _vendor_error(e)


@app.get("/api/candles")
def get_candles(ticker: str, bar: str = "15m", limit: int = Query(300, ge=10, le=300)):
    """Price candles (OKX swap) for the Terminal screen. Crypto only."""
    try:
        return candles.get_candles(_ticker(ticker), bar, limit)
    except SourceError as e:
        if e.status == 400:
            raise HTTPException(400, str(e))
        raise _vendor_error(e)


@app.get("/api/history/flow")
def history_flow(ticker: str, bar: str = "15m", limit: int = Query(300, ge=10, le=300)):
    """Per-candle modeled hedge flow and level history from the recorded snapshots (crypto)."""
    t = _ticker(ticker)
    try:
        c = candles.get_candles(t, bar, limit)
    except SourceError as e:
        if e.status == 400:
            raise HTTPException(400, str(e))
        raise _vendor_error(e)
    bar_s = {"5m": 300, "15m": 900, "1H": 3600, "4H": 14400, "1D": 86400}[bar]
    return {"ticker": t, "bar": bar, **history.candle_flows(t, c["candles"], bar_s),
            "method": "Modeled hedge for each candle's realized move (open to close) along the profile curve of the snapshot "
                      "taken nearest to the candle. Candles with no snapshot are left out. Not a measured flow."}


@app.get("/api/history/snapshots")
def history_snapshots(ticker: str, hours: int = Query(24, ge=1, le=24 * 30)):
    t = _ticker(ticker)
    if not config.is_crypto(t):
        raise HTTPException(404, "History covers the crypto coins only")
    import time as _t
    return {"ticker": t, "timestamps": history.timestamps(t, int(_t.time()) - hours * 3600),
            "recording": history.is_recording()}


@app.get("/api/history/snapshot")
def history_snapshot(ticker: str, ts: int):
    t = _ticker(ticker)
    snap = history.snapshot(t, ts)
    if snap is None:
        raise HTTPException(404, "No snapshot at or before that time")
    return snap


@app.get("/api/map")
def gex_map(ticker: str, expiries: str | None = None, signed: bool = True):
    exp = None
    if expiries:
        try:
            exp = [date.fromisoformat(x.strip()).isoformat() for x in expiries.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(400, "expiries must be YYYY-MM-DD, comma separated")
    try:
        return service.build_map(_ticker(ticker), exp, signed)
    except SourceError as e:
        raise _vendor_error(e)


@app.get("/api/futures")
def futures_view(ticker: str, override_price: float | None = Query(None, gt=0),
                 override_basis: float | None = None):
    """Futures view conversion for SPX, NDX, SPY and QQQ (display only; the map is unchanged)."""
    try:
        return service.futures_view(_ticker(ticker), override_price, override_basis)
    except SourceError as e:
        raise _vendor_error(e)


try:   # trade guidance (gexlab/guidance); optional, must never take down the core API
    from gexlab.guidance.api import router as _guidance_router
    app.include_router(_guidance_router)
except Exception as _e:  # noqa: BLE001
    log.warning("guidance router not loaded: %s", _e)

_dist = config.ROOT / "web" / "dist"
if _dist.is_dir():
    app.mount("/", StaticFiles(directory=_dist, html=True), name="web")
