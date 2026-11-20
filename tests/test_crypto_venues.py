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


# ---------------------------------------------------------------- Bybit (documented payload shape, not verified live)

from gexlab.sources import bybit  # noqa: E402


def _bybit_rows(spot=61000.0):
    mk = lambda sym, oi, iv: {"symbol": sym, "markIv": iv, "openInterest": oi, "underlyingPrice": str(spot * 1.001),  # noqa: E731
                              "indexPrice": str(spot), "bid1Price": "100", "ask1Price": "120"}
    return [mk("BTC-25DEC26-60000-C-USDT", "12.5", "0.55"), mk("BTC-25DEC26-60000-P-USDT", "30", "0.57"),
            mk("BTC-25DEC26-62000-C", "8", "0.5"), mk("BTC-25DEC26-61000-C-USDT", "5", "55"),   # IV 5500%: implausible
            mk("garbage", "1", "0.5")]


def test_bybit_symbols_with_and_without_settle_suffix():
    assert bybit.parse_instrument("BTC-25DEC26-60000-C-USDT")[1:] == (date(2026, 12, 25), 60000.0, "call")
    assert bybit.parse_instrument("SOL-3JAN27-80-P")[1] == date(2027, 1, 3)
    with pytest.raises(ValueError):
        bybit.parse_instrument("BTCUSDT")


from datetime import date  # noqa: E402


def test_bybit_parse_units_and_guards():
    p = bybit.parse_part("BTC", _bybit_rows(), datetime(2026, 11, 20, tzinfo=UTC))
    assert p.venue == "bybit" and len(p.contracts) == 4 and p.spot.price == 61000.0
    c = next(c for c in p.contracts if c.symbol.endswith("60000-C-USDT"))
    assert c.oi == 12.5 and c.multiplier == 1 and c.iv == 0.55 and c.venue == "bybit" and c.fwd == pytest.approx(61061.0)
    assert next(c for c in p.contracts if "61000" in c.symbol).iv is None          # implausible IV dropped, not guessed
    assert any("not been verified" in w for w in p.warnings) and any("implausible" in w for w in p.warnings)


def test_bybit_empty_is_no_options():
    with pytest.raises(NoOptions):
        bybit.parse_part("SUI", [], datetime(2026, 11, 20, tzinfo=UTC))


def test_three_venue_merge_and_bybit_down_degrades():
    now = datetime(2026, 11, 20, tzinfo=UTC)
    d, (f, onow) = _dpart("BTC"), _okx("BTC")
    o, b = okx.parse_part("BTC", *f, onow), bybit.parse_part("BTC", _bybit_rows(d.spot.price), now)
    ch = crypto.merge("BTC", now, [("bybit", b, None), ("okx", o, None), ("deribit", d, None)])
    assert ch.feed == "deribit+okx+bybit" and [v["name"] for v in ch.venues] == ["deribit", "okx", "bybit"]
    down = crypto.merge("BTC", now, [("deribit", d, None), ("okx", o, None), ("bybit", None, ("unavailable", "HTTP 403"))])
    assert down.feed == "deribit+okx" and any("Partial map" in w and "Bybit" in w for w in down.warnings)


def test_fetch_403_is_reported_as_unavailable_not_crash(monkeypatch):
    def boom(*a, **k):
        raise SourceError("GET failed with HTTP 403", status=403)
    monkeypatch.setattr(bybit, "get_with_backoff", boom)
    with pytest.raises(SourceError) as e:
        bybit.fetch_part("BTC", datetime.now(UTC))
    assert "country block" in str(e.value)


# ---------------------------------------------------------------- hedge flow strip

def test_hedge_flow_flat_profile_matches_net_gex_per_percent():
    spot = 100.0
    prof = [{"spot": spot * (1 + i / 1000), "net_gex": 5e6} for i in range(-200, 201)]     # flat +5M per 1%
    hf = G.hedge_flow_bins(prof, spot, band=0.06)
    # moving up +1% -> dealers (long gamma) SELL about 5M x ln(1.01)/0.01 = 4.975M
    up1 = sum(b["flow_usd"] for b in hf["bins"] if b["lo"] >= spot and b["hi"] <= spot * 1.01 + 1e-9)
    assert up1 == pytest.approx(-5e6 * 0.995033, rel=0.001)
    assert hf["to_up_usd"] < 0 < hf["to_down_usd"]          # sells rallies, buys dips


def test_hedge_flow_negative_gamma_chases():
    prof = [{"spot": 100 * (1 + i / 1000), "net_gex": -2e6} for i in range(-100, 101)]
    hf = G.hedge_flow_bins(prof, 100.0, band=0.05)
    assert hf["to_up_usd"] > 0 > hf["to_down_usd"]          # buys rallies, sells dips


def test_hedge_flow_is_in_the_crypto_map_only(monkeypatch):
    monkeypatch.setenv("GEXLAB_DEMO", "1")
    from gexlab import api, service
    service._CHAINS.clear(); service._LEVEL_CACHE.clear()
    c = TestClient(api.app, base_url="http://127.0.0.1")
    m = c.get("/api/map", params={"ticker": "BTC"}).json()
    hf = m["hedge_flow"]
    assert hf and len(hf["bins"]) > 10 and hf["bins"][0]["lo"] >= m["spot"]["price"] * 0.94 - 1
    assert c.get("/api/map", params={"ticker": "SPY"}).json().get("hedge_flow") is None


def test_hedge_flow_bins_are_signed_away_from_spot():
    prof = [{"spot": 100 * (1 + i / 1000), "net_gex": -2e6} for i in range(-100, 101)]    # negative gamma: chase
    hf = G.hedge_flow_bins(prof, 100.0, band=0.05)
    assert all(b["flow_usd"] > 0 for b in hf["bins"] if b["lo"] >= 100.0)   # above spot: buy as it rises
    assert all(b["flow_usd"] < 0 for b in hf["bins"] if b["hi"] <= 100.0)   # below spot: sell as it falls
