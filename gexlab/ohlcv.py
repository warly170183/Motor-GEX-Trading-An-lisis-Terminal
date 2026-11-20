"""Local price history: OKX swap candles for every crypto coin, downloaded once and kept in the app's SQLite file.

When the app starts live, a background thread fills (and then keeps topping up) one series per coin and timeframe:

    5m   14 days      15m  60 days      1H  365 days      4H  730 days      1D  1095 days

It pages OKX's public history-candles endpoint backwards (100 bars a request, politely paced), stores only CONFIRMED bars
(the forming bar is never stored) and is idempotent: a restart only fetches what is missing, both the newest gap and, if a
series is still shallower than its target, older bars. Nothing is estimated: if OKX has fewer bars (a young coin) the series is
just shorter. GEXLAB_HISTORY=0 turns the download off; GEXLAB_HISTORY_PACE_S changes the pause between requests (0.15 s).

This is PRICE history. The GEX history is what history.py records from now on: the venues do not publish past open interest.
"""
from __future__ import annotations

import logging
import os
import threading
import time as _time
from typing import Callable

import httpx

from gexlab import candles as C
from gexlab import config, store
from gexlab.sources.base import SourceError, get_with_backoff

log = logging.getLogger("gexlab.ohlcv")
DAYS = {"5m": 14, "15m": 60, "1H": 365, "4H": 730, "1D": 1095}
STEP_S = C.BAR_SECONDS if hasattr(C, "BAR_SECONDS") else {"5m": 300, "15m": 900, "1H": 3600, "4H": 14400, "1D": 86400}
DDL = """
CREATE TABLE IF NOT EXISTS ohlcv (
    ticker TEXT NOT NULL, bar TEXT NOT NULL, t INTEGER NOT NULL,
    o REAL NOT NULL, h REAL NOT NULL, l REAL NOT NULL, c REAL NOT NULL, v REAL NOT NULL,
    PRIMARY KEY (ticker, bar, t)
) WITHOUT ROWID;
"""
Page = Callable[[str, str, int | None], list[list]]      # (instId, okx bar, after_ms) -> raw rows, newest first
_STATE = {"running": False, "done": 0, "total": 0, "current": None, "error": None, "finished": False}
_LOCK = threading.Lock()


def _init(conn) -> None:
    conn.executescript(DDL)


def upsert(ticker: str, bar: str, rows: list[dict], conn=None) -> int:
    data = [(ticker, bar, r["t"], r["o"], r["h"], r["l"], r["c"], r["v"]) for r in rows if r.get("confirmed", True)]
    if not data:
        return 0

    def _w(c):
        _init(c)
        c.executemany("INSERT OR REPLACE INTO ohlcv (ticker, bar, t, o, h, l, c, v) VALUES (?,?,?,?,?,?,?,?)", data)
    if conn is not None:
        _w(conn)
    else:
        with store.session() as c:
            _w(c)
    return len(data)


def read(ticker: str, bar: str, limit: int, before_t: int | None = None) -> list[dict]:
    """Newest `limit` stored bars strictly before `before_t` (all when None), oldest first."""
    with store.session() as c:
        _init(c)
        q = "SELECT t,o,h,l,c,v FROM ohlcv WHERE ticker=? AND bar=?" + (" AND t<?" if before_t is not None else "")
        args = (ticker, bar) + ((before_t,) if before_t is not None else ()) + (limit,)
        rows = c.execute(q + " ORDER BY t DESC LIMIT ?", args).fetchall()
    return [{"t": r["t"], "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"], "v": r["v"], "confirmed": True} for r in reversed(rows)]


def span(ticker: str, bar: str) -> tuple[int | None, int | None, int]:
    with store.session() as c:
        _init(c)
        r = c.execute("SELECT MIN(t) a, MAX(t) b, COUNT(*) n FROM ohlcv WHERE ticker=? AND bar=?", (ticker, bar)).fetchone()
    return r["a"], r["b"], r["n"]


def _http_page(client: httpx.Client) -> Page:
    def page(inst: str, okx_bar: str, after_ms: int | None) -> list[list]:
        params = {"instId": inst, "bar": okx_bar, "limit": 100}
        if after_ms is not None:
            params["after"] = after_ms
        r = get_with_backoff(client, f"{config.OKX_BASE}/market/history-candles", params=params,
                             headers={"Accept": "application/json", "User-Agent": "EdgeGEXLab-crypto/1.0 (personal research)"})
        if r.status_code != 200:
            raise SourceError(f"OKX history-candles HTTP {r.status_code}", status=r.status_code)
        body = r.json()
        if str(body.get("code")) != "0":
            raise SourceError(f"OKX history-candles: {body.get('msg') or body.get('code')}")
        return body.get("data") or []
    return page


def sync_series(ticker: str, bar: str, page: Page, now: int | None = None, pace_s: float = 0.0,
                stop: threading.Event | None = None) -> int:
    """Bring one series up to date: the newest gap first, then older bars down to the target depth. Returns bars stored."""
    inst, okx_bar = config.CRYPTO_OKX_SWAP[ticker], C.BARS[bar]
    now = now or int(_time.time())
    cutoff = now - DAYS[bar] * 86400
    lo, hi, _ = span(ticker, bar)
    stored = 0

    def run(after_s: int | None, until: Callable[[int], bool]) -> None:
        nonlocal stored
        after = None if after_s is None else after_s * 1000
        while not (stop and stop.is_set()):
            raw = page(inst, okx_bar, after)
            rows = C.parse(raw)
            if not rows:
                return
            stored += upsert(ticker, bar, [r for r in rows if until(r["t"])])
            oldest = rows[0]["t"]
            if not until(oldest) or len(raw) < 100:
                return
            after = oldest * 1000
            if pace_s:
                _time.sleep(pace_s)

    if hi is None:                                    # nothing yet: from now back to the target depth
        run(None, lambda t: t >= cutoff)
    else:
        run(None, lambda t: t > hi)                   # newest gap
        if lo is not None and lo > cutoff + STEP_S[bar] * 3:
            run(lo, lambda t: t >= cutoff)            # still shallower than the target: older bars
    return stored


def sync_all(page: Page | None = None, pace_s: float | None = None, stop: threading.Event | None = None) -> dict:
    own = page is None
    client = httpx.Client(timeout=config.OKX_TIMEOUT_S) if own else None
    page = page or _http_page(client)
    pace = float(os.getenv("GEXLAB_HISTORY_PACE_S", "0.15")) if pace_s is None else pace_s
    jobs = [(t, b) for t in sorted(config.CRYPTO_TICKERS) for b in DAYS]
    with _LOCK:
        _STATE.update(running=True, done=0, total=len(jobs), error=None, finished=False)
    out = {}
    try:
        for t, b in jobs:
            if stop and stop.is_set():
                break
            with _LOCK:
                _STATE["current"] = f"{t} {b}"
            try:
                out[f"{t}:{b}"] = sync_series(t, b, page, pace_s=pace, stop=stop)
            except SourceError as e:               # one series failing must not stop the rest
                log.warning("ohlcv %s %s: %s", t, b, e)
                out[f"{t}:{b}"] = -1
                with _LOCK:
                    _STATE["error"] = f"{t} {b}: {e}"
            with _LOCK:
                _STATE["done"] += 1
    finally:
        with _LOCK:
            _STATE.update(running=False, current=None, finished=not (stop and stop.is_set()))
        if client:
            client.close()
    return out


def status() -> dict:
    with _LOCK:
        s = dict(_STATE)
    with store.session() as c:
        _init(c)
        n = c.execute("SELECT COUNT(*) n FROM ohlcv").fetchone()["n"]
    s["stored_bars"] = n
    s["targets_days"] = DAYS
    return s


_STOP = threading.Event()
_THREAD: threading.Thread | None = None


def _loop() -> None:
    while not _STOP.is_set():
        try:
            sync_all(stop=_STOP)
        except Exception as e:  # noqa: BLE001
            log.warning("ohlcv sync failed: %s", e)
        if _STOP.wait(3600):                       # top up once an hour
            return


def start() -> bool:
    global _THREAD
    if config.is_demo() or os.getenv("GEXLAB_HISTORY", "1") == "0" or (_THREAD and _THREAD.is_alive()):
        return False
    _STOP.clear()
    _THREAD = threading.Thread(target=_loop, name="gex-ohlcv", daemon=True)
    _THREAD.start()
    return True


def stop() -> None:
    _STOP.set()


if __name__ == "__main__":     # python -m gexlab.ohlcv  -> download everything now, in the foreground
    print(sync_all())
