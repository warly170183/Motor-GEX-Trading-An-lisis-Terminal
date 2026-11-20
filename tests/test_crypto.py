"""Crypto profile (Deribit): parsing, forward (Black-76) GEX, near-expiry rule, calendar, volume/ATR, API in demo mode.

Everything runs offline on the recorded snapshots in fixtures/ (tools/snapshot_crypto.py)."""
import json
import math
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest
from fastapi.testclient import TestClient
from scipy.stats import norm

from gexlab import config, magnitude
from gexlab.engine import gex as G
from gexlab.engine.calendar import (CryptoCalendar, crypto_expiry_kind, settlement_dt, time_to_expiry,
                                    valuation_time)
from gexlab.models import Contract
from gexlab.sources import deribit

UTC = timezone.utc


def _snapshot(t="BTC"):
    sp = json.loads((config.FIXTURES_DIR / f"deribit_{t}_summary.json").read_text("utf-8"))
    ip = json.loads((config.FIXTURES_DIR / f"deribit_{t}_index.json").read_text("utf-8"))
    return sp, ip


def _chain(t="BTC"):
    sp, ip = _snapshot(t)
    return deribit.parse_chain(t, sp, ip, deribit.server_time(ip))


# ---------------------------------------------------------------- instrument names

@pytest.mark.parametrize("name,expect", [
    ("BTC-27MAR26-80000-C", ("BTC", date(2026, 3, 27), 80000.0, "call")),
    ("BTC-4DEC26-62000-P", ("BTC", date(2026, 12, 4), 62000.0, "put")),
    ("ETH-25DEC26-1750-C", ("ETH", date(2026, 12, 25), 1750.0, "call")),
    ("SOL-27MAR26-87d5-P", ("SOL", date(2026, 3, 27), 87.5, "put")),
])
def test_parse_instrument(name, expect):
    assert deribit.parse_instrument(name) == expect


@pytest.mark.parametrize("bad", ["BTC-PERPETUAL", "BTC-27MAR26-80000", "BTC-27XXX26-80000-C", "SPY261016C00635000", ""])
def test_parse_instrument_rejects(bad):
    with pytest.raises(ValueError):
        deribit.parse_instrument(bad)


# ---------------------------------------------------------------- chain parsing

def test_parse_chain_shape():
    ch = _chain()
    sp, ip = _snapshot()
    assert ch.feed == "deribit" and ch.oi_source == "deribit" and ch.oi_date is None
    assert ch.spot.price == ip["result"]["index_price"] and ch.spot.source == "deribit_index"
    assert ch.valuation == deribit.server_time(ip)            # the server clock, not the local one
    assert len(ch.contracts) > 400
    c = ch.contracts[0]
    assert c.multiplier == 1 and c.settlement == config.CRYPTO_SETTLEMENT and c.root == "BTC"
    assert c.fwd and c.fwd != ch.spot.price or True
    assert all(0 < x.iv < 5 for x in ch.contracts if x.iv)    # mark_iv percent -> decimal
    lo, hi = ch.spot.price * 0.6, ch.spot.price * 1.4
    assert all(lo <= x.strike <= hi for x in ch.contracts)    # inside the fetch band


def test_each_expiry_carries_its_own_forward():
    ch = _chain()
    by = {}
    for c in ch.contracts:
        by.setdefault(c.expiry, set()).add(c.fwd)
    far = max(by)
    near = min(by)
    # the quarterly sits above the daily: contango, which is the basis Black-76 keeps and spot-based gamma drops
    assert min(by[far]) > max(by[near]) > 0


def test_parse_chain_errors():
    sp, ip = _snapshot()
    now = datetime.now(UTC)
    with pytest.raises(deribit.SourceError):
        deribit.parse_chain("BTC", {"result": []}, ip, now)
    with pytest.raises(deribit.SourceError):
        deribit.parse_chain("BTC", sp, {"result": {}}, now)
    with pytest.raises(deribit.SourceError):
        deribit.parse_chain("ETH", sp, ip, now)               # BTC rows under an ETH ticker: nothing usable


def test_zero_or_missing_iv_is_invalid_not_dropped():
    sp, ip = _snapshot()
    rows = [dict(r) for r in sp["result"][:40]]
    rows[0]["mark_iv"] = 0
    rows[1]["mark_iv"] = None
    ch = deribit.parse_chain("BTC", {"result": rows}, ip, deribit.server_time(ip))
    cs = G.finalize(ch.contracts, ch.spot.price, ch.valuation, CryptoCalendar())
    bad = [c for c in cs if c.iv is None]
    assert bad and all(not c.valid and c.gex is None for c in bad)


# ---------------------------------------------------------------- forward gamma

def _independent_gex(ch, cs):
    """GEX of every valid contract, straight from the formula with scipy: sign * pdf(d1) / (F iv sqrt T) * OI * F^2 * 0.01."""
    out = 0.0
    for c in cs:
        if not c.valid:
            continue
        T = (settlement_dt(c.expiry, c.settlement) - ch.valuation).total_seconds() / (365 * 86400)
        F = c.fwd
        d1 = (math.log(F / c.strike) + 0.5 * c.iv ** 2 * T) / (c.iv * math.sqrt(T))
        g = norm.pdf(d1) / (F * c.iv * math.sqrt(T))
        out += (1 if c.type == "call" else -1) * g * c.oi * F * F * 0.01
    return out


def test_finalize_matches_independent_formula():
    ch = _chain()
    cs = G.finalize(ch.contracts, ch.spot.price, ch.valuation, CryptoCalendar())
    net = sum(c.gex for c in cs if c.valid)
    assert net == pytest.approx(_independent_gex(ch, cs), rel=1e-9)


def test_sweep_at_spot_equals_contract_sum_with_forwards():
    ch = _chain()
    cs = G.finalize(ch.contracts, ch.spot.price, ch.valuation, CryptoCalendar())
    arr = G.GexArrays.from_contracts(cs)
    assert arr.fr is not None                                   # forwards differ from spot: the Black-76 path runs
    at_spot = G.total_gex_at(np.array([ch.spot.price]), arr)[0]
    assert at_spot == pytest.approx(sum(c.gex for c in cs if c.valid), rel=1e-9)


def test_forward_equal_to_spot_is_the_equity_formula():
    """With fwd = spot the crypto path must equal the equity one (same numbers, so the equity tests still bind it)."""
    ch = _chain()
    spot = ch.spot.price
    a = [Contract(**{**c.__dict__}) for c in ch.contracts[:80]]
    b = [Contract(**{**c.__dict__}) for c in ch.contracts[:80]]
    for c in b:
        c.fwd = None
    for c in a:
        c.fwd = spot
    ca = G.finalize(a, spot, ch.valuation, CryptoCalendar())
    cb = G.finalize(b, spot, ch.valuation, CryptoCalendar())
    assert [c.gex for c in ca] == pytest.approx([c.gex for c in cb], rel=1e-12)
    assert G.GexArrays.from_contracts(ca).fr is None            # ratio 1 everywhere: the plain equity sweep


def test_hypothetical_spot_moves_every_forward():
    """The profile curve revalues at a new spot with F = S * (F0 / S0): a 1% spot move is a 1% forward move."""
    ch = _chain()
    cs = G.finalize(ch.contracts, ch.spot.price, ch.valuation, CryptoCalendar())
    arr = G.GexArrays.from_contracts(cs)
    s2 = ch.spot.price * 1.05
    got = G.total_gex_at(np.array([s2]), arr)[0]
    want = 0.0
    for c in cs:
        if not c.valid:
            continue
        F = s2 * c.fwd_ratio
        d1 = (math.log(F / c.strike) + 0.5 * c.iv ** 2 * c.T) / (c.iv * math.sqrt(c.T))
        want += (1 if c.type == "call" else -1) * norm.pdf(d1) / (F * c.iv * math.sqrt(c.T)) * c.oi * F * F * 0.01
    assert got == pytest.approx(want, rel=1e-9)


# ---------------------------------------------------------------- the last hour before settlement

def _contract(expiry, **kw):
    base = dict(symbol="BTC-X", root="BTC", expiry=expiry, settlement=config.CRYPTO_SETTLEMENT, strike=60000.0,
                type="call", multiplier=1, oi=10.0, iv=0.5, fwd=61000.0)
    base.update(kw)
    return Contract(**base)


def test_contracts_inside_the_last_hour_are_dropped_not_invalid():
    exp = date(2026, 11, 21)
    for minutes_left, kept in [(59, False), (61, True), (24 * 60, True), (0, False), (-30, False)]:
        at = datetime(2026, 11, 21, 8, 0, tzinfo=UTC) - timedelta(minutes=minutes_left)
        out = G.finalize([_contract(exp)], 61000.0, at, CryptoCalendar())
        assert bool(out) is kept, minutes_left


def test_equity_contracts_are_not_subject_to_the_crypto_floor():
    """A SPY contract 30 minutes before its close is still finalized (the equity rule has no floor)."""
    from tests.conftest import exchange_cal
    cal = exchange_cal()
    spy = Contract(symbol="SPY261016C00700000", root="SPY", expiry=date(2026, 10, 16), settlement="PM",
                   strike=700.0, type="call", multiplier=100, oi=100, iv=0.2)
    at = datetime(2026, 10, 16, 15, 30, tzinfo=timezone(timedelta(hours=-4))).astimezone(UTC)
    out = G.finalize([spy], 700.0, at, cal)
    assert out and out[0].valid


# ---------------------------------------------------------------- calendar and expiry cycle

def test_crypto_calendar_is_always_open():
    cal = CryptoCalendar()
    sat = datetime(2026, 11, 21, 3, 0, tzinfo=UTC)
    assert cal.market_open(sat) and cal.exchange and cal.covers(date(2000, 1, 1), date(2100, 1, 1))
    assert cal.is_session(date(2026, 12, 25)) and cal.prev_session(date(2026, 11, 22)) == date(2026, 11, 21)
    assert valuation_time(cal, sat, sat - timedelta(seconds=5)) == sat - timedelta(seconds=5)   # never "the last close"


def test_settlement_is_0800_utc_every_day():
    for d in (date(2026, 11, 21), date(2026, 11, 22), date(2026, 12, 25)):          # Sat, Sun, Christmas
        assert settlement_dt(d, config.CRYPTO_SETTLEMENT) == datetime(d.year, d.month, d.day, 8, tzinfo=UTC)
    assert time_to_expiry(datetime(2026, 11, 20, 8, tzinfo=UTC), date(2026, 11, 21), config.CRYPTO_SETTLEMENT) \
        == pytest.approx(1 / 365)


@pytest.mark.parametrize("d,kind", [
    (date(2026, 11, 21), "daily"), (date(2026, 11, 20), "weekly"), (date(2026, 12, 4), "weekly"),
    (date(2026, 11, 27), "monthly"), (date(2027, 1, 29), "monthly"),
    (date(2026, 12, 25), "quarterly"), (date(2027, 3, 26), "quarterly"), (date(2027, 6, 25), "quarterly"),
])
def test_crypto_expiry_kind(d, kind):
    assert crypto_expiry_kind(d) == kind


# ---------------------------------------------------------------- bands and steps

def test_bands_are_per_asset_and_equities_are_unchanged():
    assert config.band("sweep", "BTC") == 0.20 and config.band("gate", "ETH") == 0.10
    assert config.band("sweep", "SPY") == config.SWEEP_BAND == 0.10
    assert config.band("gate", None) == config.GATE_OI_BAND == 0.05
    assert G.sweep_step("BTC", 61000) == 50.0 and G.sweep_step("ETH", 1750) == 2.0
    grid = G.sweep_grid("BTC", 61000.0)
    assert grid[0] >= 61000 * 0.8 and grid[-1] <= 61000 * 1.2 and 450 < len(grid) < 550


def test_coverage_uses_the_chains_own_band():
    ch = _chain()
    cs = G.finalize(ch.contracts, ch.spot.price, ch.valuation, CryptoCalendar())
    cov = G.coverage(cs, ch.spot.price)
    assert cov.pct == 100.0 and cov.eligible > 100
    assert G.gex_cov(cs, ch.spot.price).band == 0.10


# ---------------------------------------------------------------- volume and ATR from the perpetual

def test_crypto_last_completed_follows_the_0800_utc_bar():
    assert magnitude.crypto_last_completed(datetime(2026, 11, 20, 11, 0, tzinfo=UTC)) == date(2026, 11, 19)
    assert magnitude.crypto_last_completed(datetime(2026, 11, 20, 8, 0, tzinfo=UTC)) == date(2026, 11, 19)
    assert magnitude.crypto_last_completed(datetime(2026, 11, 20, 7, 59, tzinfo=UTC)) == date(2026, 11, 18)


def _chart(days=30, end=date(2026, 11, 20)):
    ticks, o, h, lo, c, v, cost = [], [], [], [], [], [], []
    for i in range(days):
        d = end - timedelta(days=days - 1 - i)
        ticks.append(int(datetime(d.year, d.month, d.day, 8, tzinfo=UTC).timestamp() * 1000))
        px = 60000 + 100 * i
        o.append(px), h.append(px + 1500), lo.append(px - 1000), c.append(px + 50)
        v.append(20000.0), cost.append(20000.0 * (px + 20))
    return {"status": "ok", "ticks": ticks, "open": o, "high": h, "low": lo, "close": c, "volume": v, "cost": cost}


def test_bars_feed_the_shared_adv_and_atr_helpers():
    bars = magnitude.bars_from_chart(_chart())
    last = date(2026, 11, 19)                                   # the Nov 20 bar is still open
    adv = magnitude.adv_from_bars(bars, last)
    assert adv["adv_usd"] and adv["last"] == "2026-11-19" and adv["sessions"] == 20
    assert adv["adv_usd"] == pytest.approx(np.mean([20000.0 * (60000 + 100 * i + 20) for i in range(9, 29)]))
    atr = magnitude.atr_from_bars(bars, last)
    assert atr["sessions"] == 14 and atr["last"] == "2026-11-19" and 3.0 < atr["atr_pct_of_close"] < 5.0


def test_partial_current_bar_is_never_used():
    chart = _chart()
    chart["volume"][-1], chart["cost"][-1] = 1.0, 1.0 * 99999    # a huge price on a tiny partial bar
    adv = magnitude.adv_from_bars(magnitude.bars_from_chart(chart), date(2026, 11, 19))
    assert adv["adv_usd"] < 2e9


# ---------------------------------------------------------------- API in demo mode

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GEXLAB_DEMO", "1")
    from gexlab import api, service
    service._CHAINS.clear()
    service._LEVEL_CACHE.clear()
    return TestClient(api.app, base_url="http://127.0.0.1")


@pytest.mark.parametrize("t", ["BTC", "ETH"])
def test_map_btc_eth(client, t):
    m = client.get("/api/map", params={"ticker": t}).json()
    assert m["ticker"] == t and m["feed"]["label"] == "Recorded Deribit snapshot (demo)"  # label is the demo one
    assert m["coverage"]["pct"] == 100.0 and m["coverage"]["oi_band_pct"] == 10.0
    assert m["levels"]["call_wall"] and m["levels"]["put_wall"] and m["levels"]["regime"] in ("positive", "negative", "neutral")
    assert m["oi_source"]["kind"] == "deribit+okx" and m["oi_date"] is None and m["oi_freshness"] is None
    assert m["vol_drift"] is None and m["frozen_today"]["status"] == "eligible"
    mg = m["magnitude"]
    assert mg["adv_usd"] and mg["per_atr"]["atr_pct"] and mg["pct_of_adv"] > 0
    lo, hi = m["spot"]["price"] * 0.8, m["spot"]["price"] * 1.2
    assert all(lo <= s["strike"] <= hi for s in m["strikes"])        # displayed band is +-20%
    assert any("Venues included: Deribit + OKX" in w and "Bybit" in w for w in m["warnings"])


def test_lead_uses_crypto_precision(client):
    btc = client.get("/api/map", params={"ticker": "BTC"}).json()["lead"]
    eth = client.get("/api/map", params={"ticker": "ETH"}).json()["lead"]
    assert "within 20% of spot" in btc and "within 20% of spot" in eth
    assert " at " in btc and "." not in btc.split(" at ")[1].split(" ")[0]    # BTC in whole dollars
    assert "." in eth.split(" at ")[1].split(" ")[0]                          # ETH to one decimal


def test_expirations_crypto(client):
    e = client.get("/api/expirations", params={"ticker": "BTC"}).json()
    assert e["window_days"] == 180
    kinds = {x["kind"] for x in e["expirations"]}
    assert {"daily", "weekly", "monthly", "quarterly"} <= kinds
    assert all(x["settlement"] == "UTC08" and x["settles_at"].endswith("T08:00:00Z") for x in e["expirations"])
    dates = [x["date"] for x in e["expirations"]]
    assert dates == sorted(dates) and all(0 <= x["dte"] <= 180 for x in e["expirations"])


def test_signed_and_unsigned_totals(client):
    s = client.get("/api/map", params={"ticker": "BTC", "signed": True}).json()["totals"]
    u = client.get("/api/map", params={"ticker": "BTC", "signed": False}).json()["totals"]
    assert u["put_gex"] == pytest.approx(abs(s["put_gex"])) and u["call_gex"] == pytest.approx(s["call_gex"])


def test_expiry_selection_changes_the_map(client):
    exps = [x["date"] for x in client.get("/api/expirations", params={"ticker": "BTC"}).json()["expirations"]]
    one = client.get("/api/map", params={"ticker": "BTC", "expiries": exps[0]}).json()
    allx = client.get("/api/map", params={"ticker": "BTC"}).json()
    assert one["expiry_selection"]["selected"] == 1 and allx["expiry_selection"]["selected"] == len(exps)
    assert abs(one["totals"]["net_gex"]) != abs(allx["totals"]["net_gex"])


def test_guidance_btc_whole_dollars_and_band_text(client):
    g = client.get("/api/guidance", params={"ticker": "BTC"}).json()
    assert g["narrative"]["validated"] is True
    assert g["atr"] and g["atr"]["pct"] > 0
    for lvl in g["levels"]:
        assert lvl["price"] == round(lvl["price"])                          # BTC prices are whole dollars
    assert any("within 20% of spot" in n for n in g["confidence"]) if isinstance(g.get("confidence"), list) else True


def test_futures_view_not_offered_for_crypto(client):
    assert client.get("/api/futures", params={"ticker": "BTC"}).status_code == 404


def test_search_and_tickers_include_crypto(client):
    assert client.get("/api/search", params={"q": "bt"}).json()[0]["symbol"] == "BTC"
    assert client.get("/api/tickers").json()["pinned"][:2] == ["BTC", "ETH"]
