"""OKX public REST: the BTC and ETH option books, as a second venue next to Deribit (crypto profile).

OKX lists options for BTC and ETH only (families BTC-USD and ETH-USD, coin-margined). For any other coin its option
endpoints answer error 51000 ("Parameter instFamily error"), which is read as "OKX lists no options for this coin", not
as a failure.

Source: https://www.okx.com/api/v5/   (all public, no key)
  public/instruments?instType=OPTION&instFamily=BTC-USD   contract spec per option: ctVal x ctMult = coin per contract
                                                           (BTC 1 x 0.01, ETH 1 x 0.1) and the exact expiry time. Static,
                                                           so it is cached for an hour.
  public/opt-summary?instFamily=BTC-USD                    markVol (mark IV as a FRACTION, 0.45 = 45%) and fwdPx (the forward
                                                           of that expiry) for every option, plus OKX's own greeks (unused:
                                                           the engine computes gamma itself, the same formula for every venue)
  public/open-interest?instType=OPTION&instFamily=BTC-USD open interest per option, in contracts
  market/tickers?instType=OPTION&instFamily=BTC-USD        bid and ask prices in coin
  market/index-tickers?instId=BTC-USD                      the index price (idxPx) and its timestamp

Units. The engine keeps open interest in COIN units for every venue (Deribit reports coins already). OKX reports contracts,
so oi_coin = oi x ctVal x ctMult, multiplier 1. A strike's open interest is then the same unit whichever venue it came from.

Expiry. Every OKX option settles at 08:00 UTC on its expiry date, the same instant as Deribit's, so a Deribit and an OKX
contract on the same strike and date are one position in the engine's eyes and their GEX adds.
"""
from __future__ import annotations

import re
import threading
import time as _time
from datetime import date, datetime, timezone

import httpx

from gexlab import config
from gexlab.models import Contract, Spot
from gexlab.sources.base import Hasher, NoOptions, SourceError, VenuePart, get_with_backoff

UTC = timezone.utc
# BTC-USD-261121-54000-C
_NAME_RE = re.compile(r"^([A-Z0-9]{2,8})-([A-Z]{3,4})-(\d{2})(\d{2})(\d{2})-(\d+(?:\.\d+)?)-([CP])$")
_NO_FAMILY = {"51000"}          # OKX: "Parameter instFamily error", the family does not exist
_HEADERS = {"Accept": "application/json", "User-Agent": "EdgeGEXLab-crypto/1.0 (personal research)"}

_SPECS_LOCK = threading.Lock()
_SPECS: dict[str, tuple[float, list[dict]]] = {}   # family -> (fetched at, instrument rows)


def parse_instrument(inst_id: str) -> tuple[str, date, float, str]:
    """('BTC', date(2026,11,21), 54000.0, 'call') from 'BTC-USD-261121-54000-C'. Raises ValueError if malformed."""
    m = _NAME_RE.match(inst_id.strip())
    if not m:
        raise ValueError(f"not an OKX option id: {inst_id!r}")
    root, _quote, yy, mm, dd, k, cp = m.groups()
    return root, date(2000 + int(yy), int(mm), int(dd)), float(k), "call" if cp == "C" else "put"


def _num(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x else None


def _ms(v) -> datetime | None:
    n = _num(v)
    try:
        return datetime.fromtimestamp(n / 1000.0, UTC) if n else None
    except (OverflowError, OSError, ValueError):
        return None


def _by_inst(rows: list[dict]) -> dict[str, dict]:
    return {r["instId"]: r for r in rows if isinstance(r, dict) and r.get("instId")}


def parse_part(ticker: str, instruments: list[dict], summary: list[dict], open_interest: list[dict],
               tickers: list[dict], index: dict, now: datetime, raw_hash: str | None = None,
               requests: int = 5) -> VenuePart:
    """The OKX side of a map from the five decoded `data` lists. Pure: tests feed it saved payloads."""
    ticker = ticker.upper()
    if not instruments:
        raise NoOptions(f"OKX lists no {ticker} options")
    irow = (index or {}).get("data") or []
    spot_px = _num(irow[0].get("idxPx")) if irow else None
    if not spot_px or spot_px <= 0:
        raise SourceError(f"OKX returned no {ticker} index price")
    sts = _ms(irow[0].get("ts")) or now
    today = sts.astimezone(UTC).date()
    band = config.band("fetch", ticker)
    lo, hi = spot_px * (1 - band), spot_px * (1 + band)
    summ, oi_by, tk = _by_inst(summary), _by_inst(open_interest), _by_inst(tickers)

    contracts: list[Contract] = []
    skipped = off_clock = 0
    newest: datetime | None = None
    for ins in instruments:
        if ins.get("state") not in (None, "", "live"):
            continue
        try:
            root, expiry, strike, typ = parse_instrument(ins["instId"])
        except (KeyError, ValueError):
            skipped += 1
            continue
        if root != ticker or (expiry - today).days > config.CRYPTO_WINDOW_DAYS or not (lo <= strike <= hi):
            continue
        exp_ts = _ms(ins.get("expTime"))
        if exp_ts and (exp_ts.date() != expiry or exp_ts.timetz().replace(tzinfo=None) != config.OKX_SETTLE_UTC):
            off_clock += 1                      # not the 08:00 UTC the engine assumes: counted and reported
        mult = (_num(ins.get("ctVal")) or 0.0) * (_num(ins.get("ctMult")) or 0.0)
        if mult <= 0:
            skipped += 1
            continue
        s = summ.get(ins["instId"], {})
        fwd = _num(s.get("fwdPx"))
        if not fwd or fwd <= 0:
            fwd = None
        iv = _num(s.get("markVol"))
        iv = iv if iv and iv > 0 else None
        oi_ct = _num((oi_by.get(ins["instId"]) or {}).get("oi"))
        oi_coin = oi_ct * mult if oi_ct is not None else None          # contracts -> coin units
        t = tk.get(ins["instId"], {})
        bid, ask = _num(t.get("bidPx")), _num(t.get("askPx"))          # quoted in coin
        px = fwd or spot_px
        ts = _ms(s.get("ts"))
        if ts and (newest is None or ts > newest):
            newest = ts
        contracts.append(Contract(
            symbol=ins["instId"], root=ticker, expiry=expiry, settlement=config.CRYPTO_SETTLEMENT,
            strike=strike, type=typ, multiplier=1, oi=oi_coin, oi_basis="reported" if oi_coin is not None else None,
            oi_source="okx", bid=bid * px if bid else None, ask=ask * px if ask else None,
            quote_ts=ts, iv=iv, iv_source="okx_mark" if iv else None, iv_ts=ts if iv else None, fwd=fwd,
            venue="okx"))
    if not contracts:
        raise SourceError(f"no usable {ticker} options in the OKX data")
    warnings = []
    if skipped:
        warnings.append(f"{skipped} OKX instruments with an unreadable id or contract size were skipped.")
    if off_clock:
        warnings.append(f"{off_clock} OKX contracts expire at a time other than 08:00 UTC; they are valued as if they did.")
    return VenuePart("okx", contracts, Spot(spot_px, sts, "okx_index", "index"), sts, newest or sts, raw_hash,
                     requests, warnings)


def _get(client: httpx.Client, path: str, params: dict, hasher: Hasher | None = None) -> list[dict]:
    """One OKX call -> its `data` list. Error 51000 means the family does not exist: NoOptions, not a failure."""
    r = get_with_backoff(client, f"{config.OKX_BASE}/{path}", params=params, headers=_HEADERS)
    if r.status_code not in (200, 400):
        raise SourceError(f"OKX {path} failed with HTTP {r.status_code}", status=r.status_code)
    try:
        body = r.json()
    except ValueError:
        raise SourceError(f"OKX {path} returned something that is not JSON")
    code = str(body.get("code", ""))
    if code in _NO_FAMILY:
        raise NoOptions("OKX lists no options for this coin")
    if code != "0":
        raise SourceError(f"OKX {path}: {body.get('msg') or code}", status=r.status_code if r.status_code != 200 else None)
    if hasher is not None:
        hasher.add(r.content)
    return body.get("data") or []


def _specs(client: httpx.Client, family: str, hasher: Hasher) -> tuple[list[dict], int]:
    """Contract specs per family, cached for config.OKX_SPECS_TTL_S. Returns (rows, requests made)."""
    now = _time.monotonic()
    with _SPECS_LOCK:
        hit = _SPECS.get(family)
        if hit and now - hit[0] < config.OKX_SPECS_TTL_S:
            return hit[1], 0
    rows = _get(client, "public/instruments", {"instType": "OPTION", "instFamily": family}, hasher)
    with _SPECS_LOCK:
        _SPECS[family] = (now, rows)
    return rows, 1


def fetch_part(ticker: str, now: datetime, client: httpx.Client | None = None) -> VenuePart:
    ticker = ticker.upper()
    if not config.is_crypto(ticker):
        raise SourceError(f"{ticker} is not a configured crypto ticker", status=404)
    family = config.CRYPTO_COINS[ticker]["okx"]["family"]
    if not family:
        raise NoOptions(f"OKX lists no {ticker} options")
    own = client is None
    client = client or httpx.Client(timeout=config.OKX_TIMEOUT_S)
    try:
        h = Hasher()
        specs, n = _specs(client, family, h)
        fam = {"instType": "OPTION", "instFamily": family}
        summary = _get(client, "public/opt-summary", {"instFamily": family}, h)
        oi = _get(client, "public/open-interest", fam, h)
        tickers = _get(client, "market/tickers", fam, h)
        idx = {"data": _get(client, "market/index-tickers", {"instId": family}, h)}
        return parse_part(ticker, specs, summary, oi, tickers, idx, now, h.hexdigest(), n + 4)
    finally:
        if own:
            client.close()
