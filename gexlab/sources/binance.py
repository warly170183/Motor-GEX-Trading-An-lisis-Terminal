"""Binance European Options public REST: the option book (USDT-settled) as a fourth venue next to Deribit, OKX and Bybit.

IMPORTANT: written from Binance's eapi documentation and the public dashboard `crypto-gamma-dashboard` (sfdcShrikant), and
tested on a synthetic payload in the documented shape. Binance blocks the cloud sandbox this was built in (HTTP 451, country
block), so it has NOT been run against the live API. Until it has been (tools/check_binance.py, run from a machine Binance
serves), the map says so in its warnings. Any failure degrades to the other venues like any other venue failure; implausible
values (IV outside 1%-1000%) are dropped, not guessed at.

  GET /eapi/v1/mark                                    every option of every coin in one call:
      symbol       BTC-261225-110000-C  (expiry is YYMMDD)
      markIV       mark IV as a FRACTION (0.55 = 55%)
      markPrice    in USDT (dollars)
  GET /eapi/v1/index?underlying=BTCUSDT                the index (indexPrice)
  GET /eapi/v1/openInterest?underlyingAsset=BTC&expiration=YYMMDD   one request per expiry:
      symbol, sumOpenInterest (one Binance option contract = one coin, unit = 1, so oi_coin = sumOpenInterest, multiplier 1)

Binance gives no per-expiry forward and no bid/ask in these calls: fwd is None (the map falls back to spot, fwd_ratio 1) and
the quotes stay empty. The gamma Binance publishes is NOT used: the engine recomputes it (Black-76) from IV and strike, so all
four venues are comparable.
An underlying with no symbol in /mark means Binance lists no options for it (NoOptions, not a failure).
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

import httpx

from gexlab import config
from gexlab.models import Contract, Spot
from gexlab.sources.base import Hasher, NoOptions, SourceError, VenuePart, get_with_backoff

UTC = timezone.utc
_NAME_RE = re.compile(r"^([A-Z0-9]{2,10})-(\d{6})-(\d+(?:\.\d+)?)-([CP])$")
_HEADERS = {"Accept": "application/json", "User-Agent": "EdgeGEXLab-crypto/1.0 (personal research)"}
IV_MIN, IV_MAX = 0.01, 10.0
OI_WORKERS = 6


def parse_instrument(name: str) -> tuple[str, date, float, str]:
    m = _NAME_RE.match(name.strip())
    if not m:
        raise ValueError(f"not a Binance option symbol: {name!r}")
    root, ymd, k, cp = m.groups()
    try:
        expiry = date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:6]))
    except ValueError:
        raise ValueError(f"not a Binance option symbol: {name!r}")
    return root, expiry, float(k), "call" if cp == "C" else "put"


def _num(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x else None


def parse_part(ticker: str, marks: list[dict], index_price, oi_rows: dict[str, float], now: datetime,
               raw_hash: str | None = None, requests: int = 1, failed_expiries: list[str] | None = None) -> VenuePart:
    """Pure parser. `marks` = the /mark rows (any coins), `oi_rows` = {symbol: sumOpenInterest}. Tests feed it a payload."""
    ticker = ticker.upper()
    mine = [r for r in marks if str(r.get("symbol", "")).startswith(ticker + "-")]
    if not mine:
        raise NoOptions(f"Binance lists no {ticker} options")
    idx = _num(index_price)
    if not idx or idx <= 0:
        raise SourceError(f"Binance returned no {ticker} index price")
    band = config.band("fetch", ticker)
    lo, hi = idx * (1 - band), idx * (1 + band)
    today = now.astimezone(UTC).date()
    contracts, skipped, bad_iv, no_oi = [], 0, 0, 0
    for r in mine:
        try:
            root, expiry, strike, typ = parse_instrument(r["symbol"])
        except (KeyError, ValueError):
            skipped += 1
            continue
        if root != ticker or (expiry - today).days > config.CRYPTO_WINDOW_DAYS or not (lo <= strike <= hi):
            continue
        oi = oi_rows.get(r["symbol"])
        if oi is None:
            no_oi += 1
            continue
        iv = _num(r.get("markIV"))
        if iv is not None and not (IV_MIN <= iv <= IV_MAX):
            bad_iv += 1
            iv = None
        contracts.append(Contract(
            symbol=r["symbol"], root=ticker, expiry=expiry, settlement=config.CRYPTO_SETTLEMENT, strike=strike, type=typ,
            multiplier=1, oi=oi, oi_basis="reported", oi_source="binance",
            bid=None, ask=None, quote_ts=now, iv=iv, iv_source="binance_mark" if iv else None,
            iv_ts=now if iv else None, fwd=None, venue="binance"))
    if not contracts:
        raise SourceError(f"no usable {ticker} options in the Binance data")
    warnings = ["Binance data has not been verified against the live API yet (see tools/check_binance.py)."]
    if skipped:
        warnings.append(f"{skipped} Binance symbols with an unreadable name were skipped.")
    if bad_iv:
        warnings.append(f"{bad_iv} Binance contracts had an implausible mark IV and are treated as having none.")
    if failed_expiries:
        warnings.append(f"Binance open interest failed for expiries {', '.join(sorted(failed_expiries))}: those expiries "
                        "are missing from the Binance part.")
    return VenuePart("binance", contracts, Spot(idx, now, "binance_index", "index"), now, now, raw_hash, requests, warnings)


def _json(r: httpx.Response, what: str):
    """Decode one response; returns (parsed json, raw body) so the caller can hash the bytes in a stable order."""
    if r.status_code != 200:
        raise SourceError(f"Binance {what} failed with HTTP {r.status_code}", status=r.status_code)
    try:
        return r.json(), r.content
    except ValueError:
        raise SourceError(f"Binance {what} returned something that is not JSON")


def fetch_part(ticker: str, now: datetime, client: httpx.Client | None = None) -> VenuePart:
    ticker = ticker.upper()
    if not config.is_crypto(ticker):
        raise SourceError(f"{ticker} is not a configured crypto ticker", status=404)
    own = client is None
    client = client or httpx.Client(timeout=config.BINANCE_TIMEOUT_S)
    base = config.BINANCE_BASE
    try:
        h = Hasher()
        marks, body = _json(get_with_backoff(client, f"{base}/mark", headers=_HEADERS), "mark")
        h.add(body)
        if not isinstance(marks, list):
            raise SourceError("Binance mark returned an unexpected payload")
        mine = [r for r in marks if str(r.get("symbol", "")).startswith(ticker + "-")]
        if not mine:
            raise NoOptions(f"Binance lists no {ticker} options")
        ix, body = _json(get_with_backoff(client, f"{base}/index", params={"underlying": ticker + "USDT"},
                                          headers=_HEADERS), "index")
        h.add(body)
        idx = ix.get("indexPrice") if isinstance(ix, dict) else None
        today = now.astimezone(UTC).date()
        ymds = set()
        for r in mine:
            try:
                _root, expiry, _k, _t = parse_instrument(r["symbol"])
            except (KeyError, ValueError):
                continue
            if 0 <= (expiry - today).days <= config.CRYPTO_WINDOW_DAYS:
                ymds.add(expiry.strftime("%y%m%d"))

        def one(ymd: str):
            try:
                rows, body = _json(get_with_backoff(client, f"{base}/openInterest",
                                                    params={"underlyingAsset": ticker, "expiration": ymd},
                                                    headers=_HEADERS), "openInterest")
                return ymd, rows, body
            except SourceError as e:
                if e.status == 451:
                    raise
                return ymd, None, b""

        oi_rows, failed = {}, []
        with ThreadPoolExecutor(max_workers=OI_WORKERS) as ex:
            for ymd, rows, body in ex.map(one, sorted(ymds)):   # map keeps the order, so the hash is stable
                if not isinstance(rows, list):
                    failed.append(ymd)
                    continue
                h.add(body)
                for row in rows:
                    v = _num(row.get("sumOpenInterest"))
                    if v is not None and "symbol" in row:
                        oi_rows[row["symbol"]] = v
        if ymds and len(failed) == len(ymds):
            raise SourceError("Binance open interest failed for every expiry")
        return parse_part(ticker, mine, idx, oi_rows, now, h.hexdigest(), 2 + len(ymds), failed)
    except SourceError as e:
        if e.status in (403, 451):
            raise SourceError(f"Binance refused the request (HTTP {e.status}, usually a country block)", status=e.status)
        raise
    finally:
        if own:
            client.close()
