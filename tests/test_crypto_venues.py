"""Second venue (OKX), the merge with Deribit, degraded modes and the new coins (SOL, TRX, SUI, EGLD). Offline."""
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from gexlab import config
from gexlab.engine import gex as G
from gexlab.engine.calendar import CryptoCalendar
from gexlab.guidance import build as GB
from gexlab.sources import crypto, deribit, okx
from gexlab.sources.base import NoOptions, SourceError

UTC = timezone.utc
FX = config.FIXTURES_DIR


def _j(name):
    return json.loads((FX / name).read_text("utf-8"))


def _okx(t="BTC"):
    f = [_j(f"okx_{t}_{k}.json") for k in ("instruments", "summary", "oi", "tickers", "index")]
    now = okx._ms(f[4]["data"][0]["ts"])
    return f, now


def _dpart(t="BTC"):
    sp, ip = _j(f"deribit_{t}_summary.json"), _j(f"deribit_{t}_index.json")
    return deribit.parse_part(t, sp, ip, deribit.server_time(ip))


def test_okx_instrument_parse():
    assert okx.parse_instrument("BTC-USD-261121-54000-C")[1:] == (datetime(2026, 11, 21).date(), 54000.0, "call")
    assert okx.parse_instrument("ETH-USD-261225-1750-P")[0] == "ETH"
    with pytest.raises(ValueError):
        okx.parse_instrument("BTC-USDT-SWAP")


def test_okx_oi_is_normalised_to_coin_units():
    f, now = _okx("BTC")
    part = okx.parse_part("BTC", *f, now)
    oi = {r["instId"]: r for r in f[2]}
    spec = {r["instId"]: r for r in f[0]}
    c = next(c for c in part.contracts if c.oi)
    mult = float(spec[c.symbol]["ctVal"]) * float(spec[c.symbol]["ctMult"])
    assert c.oi == pytest.approx(float(oi[c.symbol]["oi"]) * mult) and c.multiplier == 1 and c.venue == "okx"
    # OKX reports oiCcy in coin: it must agree with our conversion
    assert c.oi == pytest.approx(float(oi[c.symbol]["oiCcy"]), rel=1e-6)


def test_okx_iv_is_a_fraction_and_forward_is_kept():
    f, now = _okx("ETH")
    part = okx.parse_part("ETH", *f, now)
    assert all(0.05 < c.iv < 5 for c in part.contracts if c.iv)
    assert all(c.fwd and 0.5 * part.spot.price < c.fwd < 2 * part.spot.price for c in part.contracts if c.fwd)


def test_okx_no_options_for_other_coins():
    with pytest.raises(NoOptions):
        okx.parse_part("SOL", [], [], [], [], {"data": []}, datetime.now(UTC))
    with pytest.raises(NoOptions):
        okx.fetch_part("SOL", datetime.now(UTC))        # family None: no request is made


def test_merge_total_gex_is_sum_of_venues():
    now = datetime(2026, 11, 20, tzinfo=UTC)
    d, (f, onow) = _dpart("BTC"), _okx("BTC")
    o = okx.parse_part("BTC", *f, onow)
    both = crypto.merge("BTC", now, [("deribit", d, None), ("okx", o, None)])
    cal = CryptoCalendar()
    def total(ch):
        cs = G.finalize(ch.contracts, ch.spot.price, ch.valuation, cal)
        return sum(c.gex for c in cs if c.valid)
    only_d = crypto.merge("BTC", now, [("deribit", d, None), ("okx", None, ("unavailable", "x"))])
    only_o = crypto.merge("BTC", now, [("deribit", None, ("unavailable", "x")), ("okx", o, None)])
    # same spot for all three so the sum is exact
    for ch in (only_d, only_o):
        ch.spot, ch.valuation_ts = both.spot, both.valuation_ts
    assert total(both) == pytest.approx(total(only_d) + total(only_o), rel=1e-9)
    assert both.feed == "deribit+okx" and {c.venue for c in both.contracts} == {"deribit", "okx"}
    assert [v["status"] for v in both.venues] == ["ok", "ok"]


def test_degraded_mode_warns_and_keeps_the_other_venue():
    now = datetime(2026, 11, 20, tzinfo=UTC)
    ch = crypto.merge("BTC", now, [("deribit", _dpart("BTC"), None), ("okx", None, ("unavailable", "HTTP 503"))])
    assert ch.feed == "deribit" and ch.complete is False
    assert any("Partial map" in w and "OKX" in w for w in ch.warnings)
    assert next(v for v in ch.venues if v["name"] == "okx")["status"] == "unavailable"


def test_all_venues_down_raises_and_none_listing_is_no_options():
    now = datetime(2026, 11, 20, tzinfo=UTC)
    with pytest.raises(SourceError) as e:
        crypto.merge("BTC", now, [("deribit", None, ("unavailable", "a")), ("okx", None, ("unavailable", "b"))])
    assert not isinstance(e.value, NoOptions)
    with pytest.raises(NoOptions):
        crypto.merge("SUI", now, [("deribit", None, ("no_options", "a")), ("okx", None, ("no_options", "b"))])


def test_spot_disagreement_warning():
    now = datetime(2026, 11, 20, tzinfo=UTC)
    d, (f, onow) = _dpart("ETH"), _okx("ETH")
    o = okx.parse_part("ETH", *f, onow)
    o.spot.price = d.spot.price * 1.02
    ch = crypto.merge("ETH", now, [("deribit", d, None), ("okx", o, None)])
    assert any("index prices differ" in w for w in ch.warnings) and ch.spot.price == d.spot.price


def test_linear_usdc_names_and_oi_units():
    assert deribit.parse_instrument("TRX_USDC-21NOV26-0d28-C")[2] == 0.28
    p = _dpart("SOL")
    assert p.contracts and all(c.root == "SOL" and c.venue == "deribit" for c in p.contracts)


def test_nice_sweep_step_for_tiny_prices():
    for t, s in (("TRX", 0.3057), ("SUI", 1.0), ("SOL", 70.9), ("EGLD", 5.0)):
        step = G.sweep_step(t, s)
        assert step > 0 and 150 < len(G.sweep_grid(t, s)) < 600
    assert G.sweep_step("TRX", 0.3057) == pytest.approx(0.0005)
    assert G.sweep_step("BTC", 61000) == 50.0          # fixed steps untouched


def test_guidance_keeps_decimals_for_low_priced_coins():
    tok = GB._DP.set(GB._dp_for("TRX"))
    try:
        assert GB.rp(0.305639) == 0.30564 and GB.fp(0.30564) == "0.30564"
    finally:
        GB._DP.reset(tok)
    assert GB.rp(0.305639) == 0.31 and GB._dp_for("SOL") is None and GB._dp_for("SPY") is None


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GEXLAB_DEMO", "1")
    from gexlab import api, service
    service._CHAINS.clear()
    service._LEVEL_CACHE.clear()
    return TestClient(api.app, base_url="http://127.0.0.1")


@pytest.mark.parametrize("t", ["SOL", "TRX"])
def test_thin_coins_map_with_warning(client, t):
    m = client.get("/api/map", params={"ticker": t}).json()
    assert m["levels"]["call_wall"] and any("Thin market" in w for w in m["warnings"])
    assert [v["name"] for v in m["venues"] if v["status"] == "ok"] == ["deribit"]


@pytest.mark.parametrize("t", ["SUI", "EGLD"])
def test_coins_without_options_are_a_clear_404(client, t):
    r = client.get("/api/map", params={"ticker": t})
    assert r.status_code == 404


def test_btc_map_lists_both_venues_with_shares(client):
    m = client.get("/api/map", params={"ticker": "BTC"}).json()
    v = {x["name"]: x for x in m["venues"]}
    assert set(v) == {"deribit", "okx"} and abs(sum(x["gex_share_pct"] for x in v.values()) - 100) < 0.2


def test_search_flags_coins_without_options(client):
    rows = {r["symbol"]: r for r in client.get("/api/search", params={"q": "SUI"}).json()}
    assert "no listed options" in rows["SUI"]["name"]
