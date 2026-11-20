"""Price candles for the Terminal screen: OKX public swap candles (no keys). Crypto only.

GET market/candles?instId=BTC-USDT-SWAP&bar=15m&limit=300 -> newest first, each [ts_ms, o, h, l, c, vol, volCcy,
volCcyQuote, confirm]. Returned oldest first, timestamps in seconds, volume in coin units, `confirm` false on the bar still
forming. Demo mode serves a recorded file (tools/snapshot_crypto.py) so the screen works offline.
"""
from __future__ import annotations

import json
import threading
import time as _time

import httpx

from gexlab import config
from gexlab.sources.base import SourceError, get_with_backoff

BARS = {"5m": "5m", "15m": "15m", "1H": "1H", "4H": "4H", "1D": "1Dutc"}
_TTL_S = 20
_LOCK = threading.Lock()
_CACHE: dict[tuple[str, str, int], tuple[float, dict]] = {}


def parse(rows: list[list]) -> list[dict]:
    out = []
    for r in rows:
        try:
            out.append({"t": int(r[0]) // 1000, "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]),
                        "v": float(r[6]) if len(r) > 6 and r[6] not in ("", None) else float(r[5]),
                        "confirmed": str(r[8]) == "1" if len(r) > 8 else True})
        except (TypeError, ValueError, IndexError):
            continue
    out.sort(key=lambda c: c["t"])
    return out


def get_candles(ticker: str, bar: str = "15m", limit: int = 300) -> dict:
    ticker, limit = ticker.upper(), max(10, min(int(limit), 300))
    if not config.is_crypto(ticker):
        raise SourceError("The Terminal chart covers the crypto coins only", status=404)
    if bar not in BARS:
        raise SourceError(f"bar must be one of {', '.join(BARS)}", status=400)
    if config.is_demo():
        p = config.FIXTURES_DIR / f"candles_{ticker}_{bar}.json"
        if not p.is_file():
            raise SourceError(f"no demo candles for {ticker} {bar}", status=404)
        j = json.loads(p.read_text("utf-8"))
        return {"ticker": ticker, "bar": bar, "source": "okx_swap_recorded", "candles": parse(j)[-limit:]}
    key = (ticker, bar, limit)
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and _time.monotonic() - hit[0] < _TTL_S:
            return hit[1]
    inst = config.CRYPTO_OKX_SWAP[ticker]
    with httpx.Client(timeout=config.OKX_TIMEOUT_S) as c:
        r = get_with_backoff(c, f"{config.OKX_BASE}/market/candles",
                             params={"instId": inst, "bar": BARS[bar], "limit": limit},
                             headers={"Accept": "application/json", "User-Agent": "EdgeGEXLab-crypto/1.0 (personal research)"})
    if r.status_code != 200:
        raise SourceError(f"OKX candles failed with HTTP {r.status_code}", status=r.status_code)
    body = r.json()
    if str(body.get("code")) != "0":
        raise SourceError(f"OKX candles: {body.get('msg') or body.get('code')}")
    res = {"ticker": ticker, "bar": bar, "source": f"okx:{inst}", "candles": parse(body.get("data") or [])}
    if not res["candles"]:
        raise SourceError(f"OKX returned no candles for {inst}")
    with _LOCK:
        _CACHE[key] = (_time.monotonic(), res)
    return res
