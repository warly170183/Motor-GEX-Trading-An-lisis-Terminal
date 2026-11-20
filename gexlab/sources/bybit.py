"""Bybit public REST: the option book (USDT-settled) as a third venue next to Deribit and OKX.

IMPORTANT: written from Bybit's v5 documentation and tested on a synthetic payload in the documented shape. Bybit blocks
the cloud sandbox this was built in (HTTP 403, country block), so it has NOT been run against the live API. Until it has
been (tools/check_bybit.py, run from a machine Bybit serves), the map says so in its warnings. Any failure degrades to the
other venues like any other venue failure; implausible values (IV outside 1%-1000%) are dropped, not guessed at.

  GET /v5/market/tickers?category=option&baseCoin=BTC   one request, every option of the coin:
      symbol         BTC-27NOV26-60000-C-USDT (older style without the -USDT suffix is accepted too)
      markIv         mark IV as a FRACTION (0.65 = 65%)
      openInterest   in contracts; one Bybit option contract = one coin, so oi_coin = openInterest, multiplier 1
      underlyingPrice  the forward of that expiry
      indexPrice     the index
      bid1Price, ask1Price   quoted in USDT (dollars)
An empty list for a base coin means Bybit lists no options for it (NoOptions, not a failure).
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone

import httpx

from gexlab import config
from gexlab.models import Contract, Spot
from gexlab.sources.base import Hasher, NoOptions, SourceError, VenuePart, get_with_backoff

UTC = timezone.utc
_NAME_RE = re.compile(r"^([A-Z0-9]{2,10})-(\d{1,2})([A-Z]{3})(\d{2})-(\d+(?:\.\d+)?)-([CP])(?:-[A-Z]{3,5})?$")
_MONTHS = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}
_HEADERS = {"Accept": "application/json", "User-Agent": "EdgeGEXLab-crypto/1.0 (personal research)"}
IV_MIN, IV_MAX = 0.01, 10.0


def parse_instrument(name: str) -> tuple[str, date, float, str]:
    m = _NAME_RE.match(name.strip())
    if not m or m.group(3) not in _MONTHS:
        raise ValueError(f"not a Bybit option symbol: {name!r}")
    root, d, mon, yy, k, cp = m.groups()
    return root, date(2000 + int(yy), _MONTHS[mon], int(d)), float(k), "call" if cp == "C" else "put"


def _num(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x else None


def parse_part(ticker: str, rows: list[dict], now: datetime, raw_hash: str | None = None, requests: int = 1) -> VenuePart:
    """Pure parser over the `result.list` of one tickers call. Tests feed it a payload."""
    ticker = ticker.upper()
    if not rows:
        raise NoOptions(f"Bybit lists no {ticker} options")
    idx = next((p for p in (_num(r.get("indexPrice")) for r in rows) if p and p > 0), None)
    if idx is None:
        raise SourceError(f"Bybit returned no {ticker} index price")
    band = config.band("fetch", ticker)
    lo, hi = idx * (1 - band), idx * (1 + band)
    today = now.astimezone(UTC).date()
    contracts, skipped, bad_iv = [], 0, 0
    for r in rows:
        try:
            root, expiry, strike, typ = parse_instrument(r["symbol"])
        except (KeyError, ValueError):
            skipped += 1
            continue
        if root != ticker or (expiry - today).days > config.CRYPTO_WINDOW_DAYS or not (lo <= strike <= hi):
            continue
        fwd = _num(r.get("underlyingPrice"))
        fwd = fwd if fwd and fwd > 0 else None
        iv = _num(r.get("markIv"))
        if iv is not None and not (IV_MIN <= iv <= IV_MAX):
            bad_iv += 1
            iv = None
        oi = _num(r.get("openInterest"))
        bid, ask = _num(r.get("bid1Price")), _num(r.get("ask1Price"))
        contracts.append(Contract(
            symbol=r["symbol"], root=ticker, expiry=expiry, settlement=config.CRYPTO_SETTLEMENT, strike=strike, type=typ,
            multiplier=1, oi=oi, oi_basis="reported" if oi is not None else None, oi_source="bybit",
            bid=bid or None, ask=ask or None, quote_ts=now, iv=iv, iv_source="bybit_mark" if iv else None,
            iv_ts=now if iv else None, fwd=fwd, venue="bybit"))
    if not contracts:
        raise SourceError(f"no usable {ticker} options in the Bybit data")
    warnings = ["Bybit data has not been verified against the live API yet (see tools/check_bybit.py)."]
    if skipped:
        warnings.append(f"{skipped} Bybit symbols with an unreadable name were skipped.")
    if bad_iv:
        warnings.append(f"{bad_iv} Bybit contracts had an implausible mark IV and are treated as having none.")
    return VenuePart("bybit", contracts, Spot(idx, now, "bybit_index", "index"), now, now, raw_hash, requests, warnings)


def fetch_part(ticker: str, now: datetime, client: httpx.Client | None = None) -> VenuePart:
    ticker = ticker.upper()
    if not config.is_crypto(ticker):
        raise SourceError(f"{ticker} is not a configured crypto ticker", status=404)
    own = client is None
    client = client or httpx.Client(timeout=config.BYBIT_TIMEOUT_S)
    try:
        r = get_with_backoff(client, f"{config.BYBIT_BASE}/market/tickers",
                             params={"category": "option", "baseCoin": ticker}, headers=_HEADERS)
        if r.status_code != 200:
            raise SourceError(f"Bybit tickers failed with HTTP {r.status_code}", status=r.status_code)
        try:
            body = r.json()
        except ValueError:
            raise SourceError("Bybit tickers returned something that is not JSON")
        if body.get("retCode") != 0:
            raise SourceError(f"Bybit tickers: {body.get('retMsg') or body.get('retCode')}")
        h = Hasher()
        h.add(r.content)
        return parse_part(ticker, (body.get("result") or {}).get("list") or [], now, h.hexdigest(), 1)
    except SourceError as e:
        if e.status == 403:
            raise SourceError("Bybit refused the request (HTTP 403, usually a country block)", status=403)
        raise
    finally:
        if own:
            client.close()
