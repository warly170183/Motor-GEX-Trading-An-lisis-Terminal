"""Deribit public REST: the whole BTC or ETH option chain in two requests (crypto profile).

Source: https://www.deribit.com/api/v2/public/
  get_book_summary_by_currency?currency=BTC&kind=option   one row per listed option (about 1,000 for BTC)
  get_book_summary_by_currency?currency=USDC&kind=option  every LINEAR option (SOL_USDC-..., TRX_USDC-..., about 3,000 rows)
  get_index_price?index_name=btc_usd                       the spot index
Inverse books (BTC, ETH) are coin-margined; linear books (SOL, TRX, ...) are USDC-margined. Both report open_interest in
units of the underlying coin, so the engine treats them the same way (multiplier 1).

What a row gives us, and how it is used:
  open_interest      in coin units (1 contract = 1 BTC / 1 ETH), live, not one session behind. multiplier = 1.
  mark_iv            Deribit's own implied volatility, in PERCENT. We use it as is (never solved from a mid): the mark IV is
                     defined for every listed strike, including ones with no bid or ask.
  underlying_price   the FORWARD of that row's expiry (the matching future's price), not spot. It carries the basis of
                     each expiry, so gamma is computed on it (Black-76, r = 0 on the forward). See engine/gex.py.
  bid_price/ask_price in coin units; shown to the engine in dollars (x underlying_price) so "live quote" tests work.

Spot is the Deribit index (get_index_price). Its response carries the server clock in `usOut` (microseconds), which is
used as the data time and the valuation clock, so a skewed local clock cannot move T.

Public endpoints only: no keys, no websocket, no trading call. Deribit rate-limits public calls per IP generously
(a refresh every 60 s per ticker is far below it); get_with_backoff retries 429 and 5xx.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone

import httpx

from gexlab import config
from gexlab.models import Chain, Contract, Spot
import threading
import time as _time

from gexlab.sources.base import Hasher, NoOptions, SourceError, VenuePart, get_with_backoff

UTC = timezone.utc
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
# BTC-27MAR26-80000-C  |  BTC-4DEC26-62000-P  |  ETH-27MAR26-2500-C ("d" is the decimal point on cheap coins: 87d5 = 87.5)
_NAME_RE = re.compile(r"^([A-Z]{2,6}(?:_[A-Z]{3,5})?)-(\d{1,2})([A-Z]{3})(\d{2})-(\d+(?:d\d+)?)-([CP])$")

OI_LABEL = "Deribit open interest, live, in coin units"


def parse_instrument(name: str) -> tuple[str, date, float, str]:
    """('BTC', date(2026,3,27), 80000.0, 'call') from 'BTC-27MAR26-80000-C'. A linear book keeps its quote currency in
    the root: 'SOL_USDC-27MAR26-150-C' gives 'SOL_USDC'. Raises ValueError if malformed."""
    m = _NAME_RE.match(name.strip())
    if not m:
        raise ValueError(f"not a Deribit option name: {name!r}")
    root, dd, mon, yy, k, cp = m.groups()
    if mon not in _MONTHS:
        raise ValueError(f"unknown month in {name!r}")
    return (root, date(2000 + int(yy), _MONTHS[mon], int(dd)),
            float(k.replace("d", ".")), "call" if cp == "C" else "put")


def _ms(v) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(v) / 1000.0, UTC) if v else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _num(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x else None


def server_time(body: dict) -> datetime | None:
    """The response's own clock: `usOut` is microseconds since the epoch."""
    try:
        return datetime.fromtimestamp(float(body["usOut"]) / 1e6, UTC)
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return None


def parse_part(ticker: str, summary: dict, index: dict, now: datetime, raw_hash: str | None = None,
               requests: int = 2) -> VenuePart:
    """The Deribit side of a map, from the two decoded responses. Pure: no network, so tests feed it saved payloads.
    Raises NoOptions when Deribit lists nothing for this coin, SourceError when what it sent is unusable."""
    ticker = ticker.upper()
    cfg = config.CRYPTO_COINS[ticker]["deribit"]
    rows = summary.get("result")
    if not isinstance(rows, list):
        raise SourceError(f"Deribit returned no {ticker} options")
    prefix = cfg["prefix"] + "-"
    mine = [r for r in rows if str(r.get("instrument_name", "")).startswith(prefix)]
    if not mine:
        raise NoOptions(f"Deribit lists no {ticker} options")
    ires = index.get("result") or {}
    spot_px = _num(ires.get("index_price"))
    if not spot_px or spot_px <= 0:
        raise SourceError(f"Deribit returned no {ticker} index price")
    sts = server_time(index) or server_time(summary) or now
    today = sts.astimezone(UTC).date()
    lo, hi = spot_px * (1 - config.band("fetch", ticker)), spot_px * (1 + config.band("fetch", ticker))

    contracts: list[Contract] = []
    skipped = 0
    newest: datetime | None = None
    for r in mine:
        try:
            root, expiry, strike, typ = parse_instrument(r["instrument_name"])
        except (KeyError, ValueError):
            skipped += 1
            continue
        if root != cfg["prefix"] or (expiry - today).days > config.CRYPTO_WINDOW_DAYS or not (lo <= strike <= hi):
            continue
        fwd = _num(r.get("underlying_price"))
        if not fwd or fwd <= 0:
            fwd = None                                   # no forward for the row: it falls back to spot
        mark_iv = _num(r.get("mark_iv"))
        iv = mark_iv / 100.0 if mark_iv and mark_iv > 0 else None
        oi = _num(r.get("open_interest"))
        bid, ask = _num(r.get("bid_price")), _num(r.get("ask_price"))
        # Inverse books quote in coin (x forward = dollars); linear books already quote in USDC.
        scale = (fwd or spot_px) if cfg["currency"] != "USDC" else 1.0
        ts = _ms(r.get("creation_timestamp"))
        if ts and (newest is None or ts > newest):
            newest = ts
        contracts.append(Contract(
            symbol=r["instrument_name"], root=ticker, expiry=expiry, settlement=config.CRYPTO_SETTLEMENT,
            strike=strike, type=typ, multiplier=1, oi=oi, oi_basis="reported" if oi is not None else None,
            oi_source="deribit", bid=bid * scale if bid else None, ask=ask * scale if ask else None,
            quote_ts=ts, iv=iv, iv_source="deribit_mark" if iv else None, iv_ts=ts if iv else None, fwd=fwd,
            venue="deribit"))
    if not contracts:
        raise SourceError(f"no usable {ticker} options in the Deribit summary")
    warnings = [f"{skipped} Deribit rows with an unreadable name were skipped."] if skipped else []
    return VenuePart("deribit", contracts, Spot(spot_px, sts, "deribit_index", "index"), sts, newest or sts,
                     raw_hash, requests, warnings)


def parse_chain(ticker: str, summary: dict, index: dict, now: datetime, raw_hash: str | None = None,
                requests: int = 2) -> Chain:
    """A single-venue Chain from Deribit alone (used by tests and tools; the app merges venues in sources/crypto.py)."""
    p = parse_part(ticker, summary, index, now, raw_hash, requests)
    return Chain(ticker=ticker.upper(), as_of=now, spot=p.spot, feed="deribit", contracts=p.contracts, oi_date=None,
                 source_ts=p.source_ts, complete=True, raw_hash=raw_hash, warnings=p.warnings, requests=requests,
                 oi_source="deribit",
                 oi_info={"source": "deribit", "label": OI_LABEL, "basis": "reported", "oi_date": None},
                 valuation_ts=p.valuation_ts,
                 venues=[{"name": "deribit", "label": "Deribit", "status": "ok", "contracts": len(p.contracts),
                          "error": None}])


def _get_json(client: httpx.Client, path: str, params: dict, hasher: Hasher) -> dict:
    r = get_with_backoff(client, f"{config.DERIBIT_BASE}/{path}", params=params,
                         headers=_HEADERS)
    if r.status_code != 200:
        raise SourceError(f"Deribit {path} failed with HTTP {r.status_code}", status=r.status_code)
    hasher.add(r.content)
    try:
        body = r.json()
    except ValueError:
        raise SourceError(f"Deribit {path} returned something that is not JSON")
    if isinstance(body, dict) and body.get("error"):
        raise SourceError(f"Deribit {path}: {body['error'].get('message', body['error'])}")
    return body


_SUMMARY_LOCK = threading.Lock()
_SUMMARY_CACHE: dict[str, tuple[float, bytes]] = {}   # currency -> (fetched at, body): SOL and TRX share one USDC request


def _summary_body(client: httpx.Client, currency: str) -> bytes:
    """The raw book summary for a currency. The USDC book (about 1 MB, every linear coin) is shared for a few seconds so
    a SOL and a TRX refresh that land together cost one request, not two."""
    now = _time.monotonic()
    with _SUMMARY_LOCK:
        hit = _SUMMARY_CACHE.get(currency)
        if hit and now - hit[0] < config.DERIBIT_SUMMARY_TTL_S:
            return hit[1]
    r = get_with_backoff(client, f"{config.DERIBIT_BASE}/get_book_summary_by_currency",
                         params={"currency": currency, "kind": "option"}, headers=_HEADERS)
    if r.status_code != 200:
        raise SourceError(f"Deribit get_book_summary_by_currency failed with HTTP {r.status_code}", status=r.status_code)
    with _SUMMARY_LOCK:
        _SUMMARY_CACHE[currency] = (now, r.content)
    return r.content


_HEADERS = {"Accept": "application/json", "User-Agent": "EdgeGEXLab-crypto/1.0 (personal research)"}


def fetch_part(ticker: str, now: datetime, client: httpx.Client | None = None) -> VenuePart:
    ticker = ticker.upper()
    if not config.is_crypto(ticker):
        raise SourceError(f"{ticker} is not a configured crypto ticker", status=404)
    cfg = config.CRYPTO_COINS[ticker]["deribit"]
    own = client is None
    client = client or httpx.Client(timeout=config.DERIBIT_TIMEOUT_S)
    try:
        h = Hasher()
        raw = _summary_body(client, cfg["currency"])
        h.add(raw)
        try:
            summary = json.loads(raw)
        except ValueError:
            raise SourceError("Deribit get_book_summary_by_currency returned something that is not JSON")
        if isinstance(summary, dict) and summary.get("error"):
            raise SourceError(f"Deribit get_book_summary_by_currency: {summary['error'].get('message', summary['error'])}")
        if not any(str(r.get("instrument_name", "")).startswith(cfg["prefix"] + "-") for r in summary.get("result") or []):
            raise NoOptions(f"Deribit lists no {ticker} options")      # skip the index request: nothing to price
        index = _get_json(client, "get_index_price", {"index_name": cfg["index"]}, h)
        return parse_part(ticker, summary, index, now, h.hexdigest(), h.count)
    finally:
        if own:
            client.close()
