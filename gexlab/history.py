"""Crypto GEX history: a snapshot of each coin's map every few minutes while the app runs.

Why: the engine only knows GEX NOW. Open-interest history is not available from the venues' public APIs, so the only honest
way to have a per-candle hedge flow and a replay is to record the map as it happens and read it back. History therefore starts
the day the app starts recording; nothing is back-filled or invented.

Stored per coin per snapshot (zlib JSON, a few KB): spot, levels, totals, venue rows, the strike ladder and the profile curve
within +-10% of spot. Retention GEXLAB_SNAP_DAYS (default 14). Recording is on by default when the app runs live;
GEXLAB_RECORD=0 turns it off, GEXLAB_RECORD_EVERY_S changes the 300 s period. Demo mode never records on its own.

Per-candle flow: for each candle, the snapshot taken closest to the candle's midpoint prices the candle's realized move
(open to close) along that snapshot's profile curve (engine.gex.hedge_flow_move). It is the modeled hedge for the move that
actually happened, not a measured flow.
"""
from __future__ import annotations

import json
import logging
import threading
import time as _time
import zlib
from datetime import datetime, timezone

from gexlab import config, store
from gexlab.engine import gex as G
from gexlab.sources.base import SourceError

log = logging.getLogger("gexlab.history")
UTC = timezone.utc
BAND = 0.10
DDL = """
CREATE TABLE IF NOT EXISTS crypto_snaps (
    ticker   TEXT NOT NULL,
    ts       INTEGER NOT NULL,          -- unix seconds, UTC
    spot     REAL NOT NULL,
    net_gex  REAL,
    call_wall REAL,
    put_wall REAL,
    flip     REAL,
    regime   TEXT,
    blob     BLOB NOT NULL,
    PRIMARY KEY (ticker, ts)
);
"""


def _conn_init(conn) -> None:
    conn.executescript(DDL)


def pack(m: dict) -> tuple[dict, bytes]:
    """The columns and the compressed body to store for one map payload."""
    s = m["spot"]["price"]
    near = lambda v: abs(v - s) / s <= BAND  # noqa: E731
    strikes = [{k: (round(r[k], 2) if isinstance(r[k], float) else r[k]) for k in ("strike", "call_gex", "put_gex", "net_gex")}
               for r in m["strikes"] if near(r["strike"])]
    prof = [{"spot": round(p["spot"], 6), "net_gex": round(p["net_gex"], 1)} for p in m["profile_curve"] if near(p["spot"])]
    body = {"spot": s, "levels": m["levels"], "totals": m["totals"], "strikes": strikes, "profile": prof,
            "venues": m.get("venues") or [], "lead": m.get("lead")}
    lv = m["levels"]
    cols = {"spot": s, "net_gex": m["totals"]["net_gex"], "call_wall": lv.get("call_wall"), "put_wall": lv.get("put_wall"),
            "flip": lv.get("nearest_zero_gamma"), "regime": lv.get("regime")}
    return cols, zlib.compress(json.dumps(body, separators=(",", ":")).encode(), 6)


def unpack(blob: bytes) -> dict:
    return json.loads(zlib.decompress(blob).decode())


def record(ticker: str, ts: int | None = None, conn=None) -> bool:
    """Store one snapshot of `ticker`'s map. False when the coin has no map (no options anywhere / venue down)."""
    from gexlab import service
    ticker = ticker.upper()
    try:
        m = service.build_map(ticker, None, True)
    except SourceError as e:
        log.info("history: %s skipped: %s", ticker, e)
        return False
    ts = int(ts if ts is not None else _time.time())
    cols, blob = pack(m)

    def _w(c):
        _conn_init(c)
        c.execute("INSERT OR REPLACE INTO crypto_snaps (ticker, ts, spot, net_gex, call_wall, put_wall, flip, regime, blob) "
                  "VALUES (?,?,?,?,?,?,?,?,?)",
                  (ticker, ts, cols["spot"], cols["net_gex"], cols["call_wall"], cols["put_wall"], cols["flip"], cols["regime"], blob))
    if conn is not None:
        _w(conn)
    else:
        with store.session() as c:
            _w(c)
    return True


def record_all(ts: int | None = None) -> dict[str, bool]:
    out = {}
    for t in sorted(config.CRYPTO_TICKERS - config.NO_OPTIONS_COINS):
        try:
            out[t] = record(t, ts)
        except Exception as e:  # noqa: BLE001  one coin must never stop the others
            log.warning("history: %s failed: %s", t, e)
            out[t] = False
    return out


def prune(days: int | None = None) -> int:
    days = days if days is not None else int(__import__("os").getenv("GEXLAB_SNAP_DAYS", "14"))
    cutoff = int(_time.time()) - days * 86400
    with store.session() as c:
        _conn_init(c)
        return c.execute("DELETE FROM crypto_snaps WHERE ts < ?", (cutoff,)).rowcount


def timestamps(ticker: str, since: int | None = None, limit: int = 2000) -> list[int]:
    with store.session() as c:
        _conn_init(c)
        rows = c.execute("SELECT ts FROM crypto_snaps WHERE ticker=? AND ts>=? ORDER BY ts DESC LIMIT ?",
                         (ticker.upper(), since or 0, limit)).fetchall()
    return sorted(r["ts"] for r in rows)


def snapshot(ticker: str, ts: int) -> dict | None:
    """The stored snapshot at or just before `ts`, with its modeled hedge-flow bins rebuilt from its profile curve."""
    with store.session() as c:
        _conn_init(c)
        r = c.execute("SELECT ts, blob FROM crypto_snaps WHERE ticker=? AND ts<=? ORDER BY ts DESC LIMIT 1",
                      (ticker.upper(), ts)).fetchone()
    if not r:
        return None
    b = unpack(r["blob"])
    b["ts"] = r["ts"]
    b["ticker"] = ticker.upper()
    b["hedge_flow"] = G.hedge_flow_bins(b["profile"], b["spot"]) if b["profile"] else None
    return b


def candle_flows(ticker: str, candles: list[dict], bar_s: int) -> dict:
    """Per candle: modeled hedge flow for its realized move and the levels at that time. Only candles with a snapshot
    within max(10 min, one bar) of their midpoint get a row; the rest are left out, never estimated."""
    if not candles:
        return {"rows": [], "coverage": {"snaps": 0, "first_ts": None, "last_ts": None}}
    t0, t1 = candles[0]["t"] - 3600, candles[-1]["t"] + bar_s + 3600
    with store.session() as c:
        _conn_init(c)
        snaps = c.execute("SELECT ts, call_wall, put_wall, flip, net_gex, blob FROM crypto_snaps WHERE ticker=? AND ts BETWEEN ? AND ? "
                          "ORDER BY ts", (ticker.upper(), t0, t1)).fetchall()
    if not snaps:
        return {"rows": [], "coverage": {"snaps": 0, "first_ts": None, "last_ts": None}}
    tol = max(600, bar_s)
    rows, j = [], 0
    for k in candles:
        mid = k["t"] + bar_s / 2
        while j + 1 < len(snaps) and abs(snaps[j + 1]["ts"] - mid) <= abs(snaps[j]["ts"] - mid):
            j += 1
        s = snaps[j]
        if abs(s["ts"] - mid) > tol:
            continue
        flow = G.hedge_flow_move(unpack(s["blob"])["profile"], k["o"], k["c"])
        if flow is None:
            continue
        rows.append({"t": k["t"], "flow_usd": flow, "call_wall": s["call_wall"], "put_wall": s["put_wall"],
                     "flip": s["flip"], "net_gex": s["net_gex"], "snap_ts": s["ts"]})
    return {"rows": rows, "coverage": {"snaps": len(snaps), "first_ts": snaps[0]["ts"], "last_ts": snaps[-1]["ts"]}}


# ---------------------------------------------------------------- background recorder

_THREAD: threading.Thread | None = None
_STOP = threading.Event()


def _loop(period: int) -> None:
    last_prune = 0.0
    while not _STOP.is_set():
        now = _time.time()
        wait = period - (now % period)                  # wake on the period boundary (5 min marks)
        if _STOP.wait(wait):
            return
        try:
            record_all(int(_time.time()) // period * period)
            if _time.time() - last_prune > 86400:
                prune()
                last_prune = _time.time()
        except Exception as e:  # noqa: BLE001
            log.warning("history recorder cycle failed: %s", e)


def start_recorder() -> bool:
    """Start the recorder thread if enabled. Live mode only; GEXLAB_RECORD=0 disables."""
    import os
    global _THREAD
    if config.is_demo() or os.getenv("GEXLAB_RECORD", "1") == "0" or (_THREAD and _THREAD.is_alive()):
        return False
    period = max(60, int(os.getenv("GEXLAB_RECORD_EVERY_S", "300")))
    _STOP.clear()
    _THREAD = threading.Thread(target=_loop, args=(period,), name="gex-history", daemon=True)
    _THREAD.start()
    log.info("history recorder started, every %ss", period)
    return True


def is_recording() -> bool:
    return bool(_THREAD and _THREAD.is_alive())


def stop_recorder() -> None:
    _STOP.set()


if __name__ == "__main__":     # python -m gexlab.history  -> one snapshot of every coin now
    print(record_all())
