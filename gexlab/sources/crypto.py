"""Crypto aggregator: asks every venue (Deribit, OKX) at once and merges what answers into one Chain.

Rules (honesty first):
  * A venue that lists no options for the coin (NoOptions) is a fact, reported as `no_options`, never an error.
  * A venue that fails (network, HTTP, bad payload) is reported as `unavailable` and the map is built from the others, with
    a warning that it is partial. If EVERY venue fails the call raises; if every venue lists no options it raises NoOptions
    (the API turns that into a 404 with a clear message). Nothing is ever invented.
  * Contracts are never netted across venues: each keeps its own `venue`, and the engine sums GEX per strike, which is the
    right answer because open interest on different exchanges is different positions.
  * Spot is the first available venue index in config.VENUES order (Deribit, then OKX). If two indexes differ by more than
    SPOT_DISAGREE_PCT a warning says so.
"""
from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from gexlab import config
from gexlab.models import Chain
from gexlab.sources import deribit, okx
from gexlab.sources.base import NoOptions, SourceError, VenuePart

SPOT_DISAGREE_PCT = 0.5

_FETCHERS = {"deribit": deribit.fetch_part, "okx": okx.fetch_part}


def _attempt(venue: str, ticker: str, now: datetime):
    try:
        return venue, _FETCHERS[venue](ticker, now), None
    except NoOptions as e:
        return venue, None, ("no_options", str(e))
    except SourceError as e:
        return venue, None, ("unavailable", str(e))
    except Exception as e:                      # a parser bug on one venue must not take the other down
        return venue, None, ("unavailable", f"{type(e).__name__}: {e}")


def merge(ticker: str, now: datetime, outcomes: list[tuple[str, VenuePart | None, tuple[str, str] | None]]) -> Chain:
    """Pure merge of per-venue outcomes (venue, part, (status, message)) into one Chain. Tests call this directly."""
    ticker = ticker.upper()
    order = {v: i for i, v in enumerate(config.VENUES)}
    outcomes = sorted(outcomes, key=lambda o: order.get(o[0], 99))
    parts = [p for _, p, _ in outcomes if p is not None]
    rows, warnings = [], []
    for v, p, err in outcomes:
        label = config.VENUE_LABELS.get(v, v)
        if p is not None:
            rows.append({"name": v, "label": label, "status": "ok", "contracts": len(p.contracts), "error": None,
                         "spot": p.spot.price})
            warnings.extend(p.warnings)
        else:
            status, msg = err or ("unavailable", "no answer")
            rows.append({"name": v, "label": label, "status": status, "contracts": 0, "error": msg})
    if not parts:
        if all(r["status"] == "no_options" for r in rows):
            raise NoOptions(f"No venue lists {ticker} options ({', '.join(r['label'] for r in rows)}).")
        raise SourceError("; ".join(f"{r['label']}: {r['error']}" for r in rows if r["error"]) or "no venue answered")
    down = [r["label"] for r in rows if r["status"] == "unavailable"]
    if down:
        warnings.append(f"Partial map: {', '.join(down)} did not answer, so its open interest is missing from the walls and totals.")
    first = parts[0]
    if len(parts) > 1:
        lo, hi = min(p.spot.price for p in parts), max(p.spot.price for p in parts)
        if lo > 0 and (hi - lo) / lo * 100 > SPOT_DISAGREE_PCT:
            warnings.append("Venue index prices differ by more than "
                            f"{SPOT_DISAGREE_PCT}%: the map uses {config.VENUE_LABELS[first.venue]}'s ({first.spot.price:g}).")
    contracts = [c for p in parts for c in p.contracts]
    names = [p.venue for p in parts]
    feed = "+".join(names)
    labels = " + ".join(config.VENUE_LABELS[v] for v in names)
    h = hashlib.sha256("|".join(p.raw_hash or "" for p in parts).encode()).hexdigest() if any(p.raw_hash for p in parts) else None
    tss = [p.source_ts for p in parts if p.source_ts]
    vts = [p.valuation_ts for p in parts if p.valuation_ts]
    return Chain(ticker=ticker, as_of=now, spot=first.spot, feed=feed, contracts=contracts, oi_date=None,
                 source_ts=max(tss) if tss else None, complete=not down, raw_hash=h, warnings=warnings,
                 requests=sum(p.requests for p in parts), oi_source=feed,
                 oi_info={"source": feed, "label": f"{labels} open interest, live, in coin units", "basis": "reported",
                          "oi_date": None},
                 valuation_ts=max(vts) if vts else None, venues=rows)


def fetch_chain(ticker: str, now: datetime) -> Chain:
    ticker = ticker.upper()
    if not config.is_crypto(ticker):
        raise SourceError(f"{ticker} is not a configured crypto ticker", status=404)
    with ThreadPoolExecutor(max_workers=len(config.VENUES)) as ex:
        outcomes = list(ex.map(lambda v: _attempt(v, ticker, now), config.VENUES))
    return merge(ticker, now, outcomes)
