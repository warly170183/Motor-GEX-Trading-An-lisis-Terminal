"""Refresh the crypto demo fixtures from Deribit's and OKX's public APIs (no keys).

For every coin in config.CRYPTO_COINS that a venue lists options for, writes:
  fixtures/deribit_<T>_summary.json, deribit_<T>_index.json
  fixtures/okx_<T>_{instruments,summary,oi,tickers,index}.json     (BTC and ETH only)
and merges an entry for the coin into fixtures/adv.json and fixtures/atr.json from the Deribit perpetual's daily bars.
Coins with no listed options anywhere (SUI, EGLD) get no files: the demo answers them with the same 404 as live.

Run from the repo root:  python tools/snapshot_crypto.py
The files are a RECORDED snapshot of real market data, labeled as such on screen.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from gexlab import config, magnitude  # noqa: E402
from gexlab.engine.calendar import UTC  # noqa: E402
from gexlab.sources import deribit, okx  # noqa: E402
from gexlab.sources.base import Hasher, NoOptions  # noqa: E402


def dump(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, separators=(",", ":")), "utf-8")


def snap_deribit(c: httpx.Client, t: str, fx: Path):
    cfg = config.CRYPTO_COINS[t]["deribit"]
    r = c.get(f"{config.DERIBIT_BASE}/get_book_summary_by_currency", params={"currency": cfg["currency"], "kind": "option"})
    r.raise_for_status()
    summary = r.json()
    rows_all = [x for x in summary["result"] if str(x["instrument_name"]).startswith(cfg["prefix"] + "-")]
    if not rows_all:
        return None
    index = deribit._get_json(c, "get_index_price", {"index_name": cfg["index"]}, Hasher())
    now = deribit.server_time(index) or datetime.now(UTC)
    kept = deribit.parse_part(t, {"result": rows_all}, index, now).contracts      # same filter the live path applies
    names = {k.symbol for k in kept}
    rows = [x for x in rows_all if x["instrument_name"] in names]
    dump(fx / f"deribit_{t}_summary.json", {"result": rows, "usOut": summary.get("usOut")})
    dump(fx / f"deribit_{t}_index.json", index)
    return now, len(rows), index["result"]["index_price"]


def snap_okx(c: httpx.Client, t: str, fx: Path, now: datetime):
    fam = config.CRYPTO_COINS[t]["okx"]["family"]
    if not fam:
        return None
    h = Hasher()
    try:
        specs = okx._get(c, "public/instruments", {"instType": "OPTION", "instFamily": fam}, h)
        summ = okx._get(c, "public/opt-summary", {"instFamily": fam}, h)
        oi = okx._get(c, "public/open-interest", {"instType": "OPTION", "instFamily": fam}, h)
        tk = okx._get(c, "market/tickers", {"instType": "OPTION", "instFamily": fam}, h)
        idx = {"data": okx._get(c, "market/index-tickers", {"instId": fam}, h)}
    except NoOptions:
        return None
    kept = {k.symbol for k in okx.parse_part(t, specs, summ, oi, tk, idx, now).contracts}
    pick = lambda rows: [x for x in rows if x.get("instId") in kept]  # noqa: E731
    for name, rows in (("instruments", pick(specs)), ("summary", pick(summ)), ("oi", pick(oi)), ("tickers", pick(tk))):
        dump(fx / f"okx_{t}_{name}.json", rows)
    dump(fx / f"okx_{t}_index.json", idx)
    return len(kept)


def main() -> None:
    fx = config.FIXTURES_DIR
    adv_p, atr_p = fx / "adv.json", fx / "atr.json"
    adv, atr = json.loads(adv_p.read_text("utf-8")), json.loads(atr_p.read_text("utf-8"))
    with httpx.Client(timeout=config.DERIBIT_TIMEOUT_S) as c:
        for t in sorted(config.CRYPTO_TICKERS):
            d = snap_deribit(c, t, fx)
            if d is None:
                print(f"{t}: Deribit lists no options")
            now = d[0] if d else datetime.now(UTC)
            n_okx = snap_okx(c, t, fx, now)
            if d is None and n_okx is None:
                print(f"{t}: no options on any venue, nothing recorded")
                continue
            if d is None:
                continue
            last = magnitude.crypto_last_completed(now)
            bars = magnitude.fetch_daily_bars_crypto(config.CRYPTO_VOLUME_INSTRUMENT[t],
                                                     last - timedelta(days=config.ADV_LOOKBACK_DAYS), last)
            note = f"Recorded Deribit snapshot ({now:%Y-%m-%d})"
            adv[t] = {**magnitude.adv_from_bars(bars, last), "note": note}
            atr[t] = {**magnitude.atr_from_bars(bars, last), "note": note}
            print(f"{t}: deribit {d[1]} rows, okx {n_okx} rows, spot {d[2]}, ADV {adv[t]['adv_usd']:,.0f}")
    adv_p.write_text(json.dumps(adv, indent=1) + "\n", "utf-8")
    atr_p.write_text(json.dumps(atr, indent=1) + "\n", "utf-8")


if __name__ == "__main__":
    main()
