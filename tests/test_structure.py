"""Chain structure and second-order exposures: vanna, charm, max pain, put/call, expected move, 25-delta RR."""
import math
from datetime import date, datetime, timezone

import pytest

from gexlab.engine import bsm, structure as ST
from gexlab.engine import gex as G
from gexlab.models import Contract

AS_OF = datetime(2026, 10, 16, 14, 0, tzinfo=timezone.utc)
EXP = date(2026, 10, 21)


def _c(strike, typ, oi, iv=0.2, expiry=EXP, valid=True):
    c = Contract(symbol=f"X{expiry}{strike}{typ}", root="X", expiry=expiry, settlement="PM",
                 strike=float(strike), type=typ, oi=oi, iv=iv)
    return c


def _final(cs, spot=100.0):
    return G.finalize(cs, spot, AS_OF, None)


# -- greeks against finite differences -------------------------------------------------

@pytest.mark.parametrize("K", [90.0, 100.0, 108.0])
def test_vanna_matches_finite_difference(K):
    S, T, sg, h = 100.0, 30 / 365, 0.3, 1e-5
    fd = (bsm.delta(S, K, T, 0, sg + h, "call") - bsm.delta(S, K, T, 0, sg - h, "call")) / (2 * h)
    assert bsm.vanna(S, K, T, 0, sg) == pytest.approx(fd, rel=1e-6)
    # the same for puts: delta differs by a constant, so its vol derivative is identical
    fdp = (bsm.delta(S, K, T, 0, sg + h, "put") - bsm.delta(S, K, T, 0, sg - h, "put")) / (2 * h)
    assert bsm.vanna(S, K, T, 0, sg) == pytest.approx(fdp, rel=1e-6)


@pytest.mark.parametrize("typ", ["call", "put"])
def test_charm_matches_finite_difference(typ):
    S, K, T, sg = 100.0, 102.0, 30 / 365, 0.3
    dt = 1 / 365 / 1000
    fd = (bsm.delta(S, K, T - dt, 0, sg, typ) - bsm.delta(S, K, T, 0, sg, typ)) / dt / 365
    assert bsm.charm(S, K, T, 0, sg, typ) == pytest.approx(fd, rel=1e-4)


def test_second_order_zero_once_expired():
    assert bsm.vanna(100, 100, 0, 0, 0.2) == 0.0
    assert bsm.charm(100, 100, 0, 0, 0.2, "call") == 0.0


def test_vex_golden_units():
    # one call, S=K=100, T=1, sigma=0.2: d1=0.1, d2=-0.1, vanna = -n(0.1) * (-0.1) / 0.2
    v = bsm.vanna(100, 100, 1.0, 0, 0.2)
    assert v == pytest.approx(0.39695254747701 * 0.1 / 0.2, rel=1e-9)
    assert ST.contract_vex(v, 1000, 100, 100.0, +1) == pytest.approx(v * 0.01 * 1000 * 100 * 100.0)
    assert ST.contract_vex(v, 1000, 100, 100.0, -1) == pytest.approx(-v * 0.01 * 1000 * 100 * 100.0)


def test_vex_cex_skip_invalid_and_zero_oi():
    cs = _final([_c(100, "call", 10), _c(100, "put", None), _c(101, "call", 0), _c(102, "call", 5, iv=None)])
    vnet, vabs, cnet, cabs, n = ST.vex_cex(cs, 100.0)
    assert n == 1                                   # only the valid contract with OI
    assert vabs == pytest.approx(abs(vnet)) and cabs == pytest.approx(abs(cnet))


# -- max pain, put/call, points of control -----------------------------------------------

def test_max_pain_hand_computed():
    # calls 100: 10 OI; puts 100: 10 OI; calls 110: 5 OI. Settling at 100: calls 110 pay 0, nothing else pays -> pain 0.
    cs = [_c(100, "call", 10), _c(100, "put", 10), _c(110, "call", 5)]
    assert ST.max_pain_for(cs, 100.0) == 100.0


def test_max_pain_none_without_open_interest():
    assert ST.max_pain_for([_c(100, "call", 0), _c(110, "put", None)], 100.0) is None


def test_max_pain_tie_goes_to_strike_nearest_spot():
    # symmetric book: strikes 90 and 110 tie; spot 108 makes 110 the pick
    cs = [_c(90, "call", 1), _c(110, "put", 1)]
    assert ST.max_pain_for(cs, 108.0) == 110.0
    assert ST.max_pain_for(cs, 92.0) == 90.0


def test_put_call_ratio_and_poc():
    cs = _final([_c(100, "call", 100), _c(100, "put", 50), _c(105, "call", 400), _c(95, "put", 30)])
    calls, puts, pcr = ST.put_call_oi(cs)
    assert (calls, puts) == (500.0, 80.0) and pcr == pytest.approx(0.16)
    gex_poc, oi_poc = ST.points_of_control(cs)
    assert oi_poc == 105.0
    assert gex_poc in {100.0, 105.0}


def test_put_call_ratio_none_without_calls():
    assert ST.put_call_oi([_c(100, "put", 5)])[2] is None


def test_cumulative_zero_interpolates_between_strikes():
    cs = _final([_c(95, "put", 200), _c(105, "call", 200)])      # puts below (negative), calls above (positive)
    z = ST.cumulative_zero(cs, 100.0)
    assert z is not None and 95.0 < z < 105.0


def test_cumulative_zero_none_when_it_never_crosses():
    assert ST.cumulative_zero(_final([_c(100, "call", 10), _c(105, "call", 10)]), 100.0) is None


# -- volatility structure ------------------------------------------------------------------

def _smile(expiry=EXP, base=0.2, slope=-0.002, strikes=range(90, 111)):
    out = []
    for k in strikes:
        iv = base + slope * (k - 100)
        out += [_c(k, "call", 100, iv, expiry), _c(k, "put", 100, iv, expiry)]
    return _final(out)


def test_atm_iv_interpolates_to_spot():
    cs = _smile()
    assert ST.atm_iv(cs, 100.0) == pytest.approx(0.2, abs=1e-9)
    assert ST.atm_iv(cs, 100.5) == pytest.approx(0.2 - 0.002 * 0.5, abs=1e-9)


def test_atm_iv_none_with_too_few_strikes():
    assert ST.atm_iv(_smile(strikes=[100, 101]), 100.0) is None


def test_expected_move_is_one_sigma_of_atm_iv():
    cs = _smile()
    em, rr, exp = ST.expected_move_for(cs, 100.0)
    T = cs[0].T
    assert em is not None and exp == EXP
    assert em.sigma_abs == pytest.approx(100.0 * 0.2 * math.sqrt(T))
    assert em.low == pytest.approx(100.0 - em.sigma_abs) and em.high == pytest.approx(100.0 + em.sigma_abs)
    assert em.days == pytest.approx(T * 365.0)


def test_expected_move_skips_expiries_under_a_day():
    soon = date(2026, 10, 16)                       # same day as AS_OF: T well under one day
    cs = _smile(expiry=soon)
    em, _, _ = ST.expected_move_for(cs, 100.0)
    assert em is None


def test_expected_move_falls_through_to_next_expiry():
    cs = _smile(expiry=date(2026, 10, 16)) + _smile(expiry=EXP)
    em, _, exp = ST.expected_move_for(cs, 100.0)
    assert exp == EXP and em is not None


def test_risk_reversal_negative_for_put_skew_and_positive_for_call_skew():
    put_skew = _smile(slope=-0.004)                 # IV higher at low strikes
    call_skew = _smile(slope=+0.004)
    assert ST.risk_reversal(put_skew, 100.0) < 0
    assert ST.risk_reversal(call_skew, 100.0) > 0
    assert ST.risk_reversal(_smile(slope=0.0), 100.0) == pytest.approx(0.0, abs=1e-9)


def test_risk_reversal_none_when_delta_not_bracketed():
    assert ST.risk_reversal(_smile(strikes=[99, 100, 101]), 100.0) is None


def test_compute_empty_is_safe():
    s = ST.compute([], 100.0)
    assert s.vex_net is None and s.max_pain == [] and s.expected_move is None


# -- through the API (demo fixtures, no network) ----------------------------------------------

@pytest.mark.parametrize("ticker", ["SPY", "BTC"])
def test_map_carries_structure(monkeypatch, ticker):
    monkeypatch.setenv("GEXLAB_DEMO", "1")
    from fastapi.testclient import TestClient
    from gexlab import api, service
    service._CHAINS.clear()
    service._LEVEL_CACHE.clear()
    m = TestClient(api.app, base_url="http://127.0.0.1").get("/api/map", params={"ticker": ticker}).json()
    st = m["structure"]
    assert st["available"] is True
    assert st["pcr_oi"] and st["pcr_oi"] > 0
    assert st["max_pain"] and all(r["strike"] > 0 for r in st["max_pain"])
    em = st["expected_move"]
    assert em and em["low"] < m["spot"]["price"] < em["high"]
    assert st["vex_abs_usd"] >= abs(st["vex_net_usd"]) and st["cex_abs_usd_day"] >= abs(st["cex_net_usd_day"])
