"""Chain structure and second-order exposures. Display only, never gates a capture or a guidance scenario.

Adds what the GEX map did not show, each piece taken from a reference tool and re-derived on this engine's conventions
(r = q = 0, forward of the expiry for crypto, the same valid-contract rule and the same valuation clock as GEX):

  * VEX  vanna exposure   = vanna * 0.01 * OI * multiplier * S      dollars of dealer delta per +1 volatility point
  * CEX  charm exposure   = charm * OI * multiplier * S             dollars of dealer delta lost per calendar day
    Both carry the GEX sign assumption (+1 call, -1 put: dealers long calls, short puts), shown as such.
  * Max pain: the strike where option holders collect least at settlement, per expiry. No open interest means no answer
    (never the lowest strike).
  * Put/call ratio on open interest, GEX and OI points of control (the strike holding the most net |GEX| and the most OI).
  * Expected move: 1 sigma = F * IV * sqrt(T) from the at-the-money IV of the nearest expiry with a day or more left.
  * 25-delta risk reversal: IV of the 25-delta call minus IV of the 25-delta put on that same expiry.
  * Cumulative-GEX zero cross: per-strike net GEX summed from the lowest strike up, zero interpolated. A cruder cousin
    of the swept gamma flip, shown next to it as a cross-check, never in its place.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

from gexlab.engine import bsm
from gexlab.engine.gex import iv_ok, sign_of
from gexlab.models import Contract

EXPECTED_MOVE_MIN_DAYS = 1.0     # an expiry with less than a day left is skipped: its IV is a few hours of variance annualized
ATM_BAND = 0.05                  # both ATM-bracketing strikes must lie within 5% of spot
MIN_ATM_IVS = 4                  # with at least this many usable IVs inside the band
RR_DELTA = 0.25


@dataclass
class MaxPain:
    expiry: date
    strike: float
    distance_pct: float          # (strike / spot - 1) * 100
    oi: float                    # total open interest of that expiry (contracts)


@dataclass
class ExpectedMove:
    expiry: date
    days: float
    iv: float                    # at-the-money, decimal
    sigma_abs: float
    sigma_pct: float
    low: float
    high: float


@dataclass
class Structure:
    vex_net: float | None = None         # USD per +1 vol point, signed
    vex_abs: float | None = None         # sum |VEX|
    cex_net: float | None = None         # USD per calendar day, signed
    cex_abs: float | None = None
    contracts: int = 0                   # valid contracts counted in VEX / CEX
    pcr_oi: float | None = None
    call_oi: float = 0.0
    put_oi: float = 0.0
    gex_poc: float | None = None
    oi_poc: float | None = None
    cum_zero: float | None = None
    max_pain: list[MaxPain] = field(default_factory=list)
    expected_move: ExpectedMove | None = None
    rr25: float | None = None            # call IV minus put IV at 25 delta, decimal (negative = put skew)
    rr25_expiry: date | None = None


# -- per-contract exposures -----------------------------------------------------------

def contract_vex(vanna: float, oi: float, multiplier: float, spot: float, sign: int) -> float:
    return sign * vanna * 0.01 * oi * multiplier * spot


def contract_cex(charm_day: float, oi: float, multiplier: float, spot: float, sign: int) -> float:
    return sign * charm_day * oi * multiplier * spot


def vex_cex(contracts: list[Contract], spot: float) -> tuple[float, float, float, float, int]:
    vnet = vabs = cnet = cabs = 0.0
    n = 0
    for c in contracts:
        if not c.valid or not c.oi or c.T is None or c.T <= 0 or not iv_ok(c.iv):
            continue
        u = c.fwd or spot
        sg = sign_of(c.type)
        v = contract_vex(bsm.vanna(u, c.strike, c.T, 0.0, c.iv), c.oi, c.multiplier, spot, sg)
        k = contract_cex(bsm.charm(u, c.strike, c.T, 0.0, c.iv, c.type), c.oi, c.multiplier, spot, sg)
        vnet += v
        vabs += abs(v)
        cnet += k
        cabs += abs(k)
        n += 1
    return vnet, vabs, cnet, cabs, n


# -- open-interest structure ---------------------------------------------------------

def max_pain_for(contracts: list[Contract], spot: float) -> float | None:
    """Strike minimising total holder payout at settlement. Ties go to the strike nearest spot. None if no open interest."""
    rows = [c for c in contracts if c.oi and c.oi > 0]
    if not rows:
        return None
    strikes = sorted({c.strike for c in rows})
    best, best_pay = None, math.inf
    for k in strikes:
        pay = 0.0
        for c in rows:
            pay += c.oi * c.multiplier * (max(k - c.strike, 0.0) if c.type == "call" else max(c.strike - k, 0.0))
        if pay < best_pay - 1e-9 or (abs(pay - best_pay) <= 1e-9 and best is not None
                                     and abs(k - spot) < abs(best - spot)):
            best, best_pay = k, pay
    return best


def max_pain_by_expiry(contracts: list[Contract], spot: float) -> list[MaxPain]:
    by: dict[date, list[Contract]] = {}
    for c in contracts:
        by.setdefault(c.expiry, []).append(c)
    out = []
    for exp in sorted(by):
        k = max_pain_for(by[exp], spot)
        if k is None:
            continue
        oi = float(sum(c.oi or 0 for c in by[exp]))
        out.append(MaxPain(exp, k, round((k / spot - 1.0) * 100.0, 2), oi))
    return out


def put_call_oi(contracts: list[Contract]) -> tuple[float, float, float | None]:
    calls = float(sum(c.oi or 0 for c in contracts if c.type == "call"))
    puts = float(sum(c.oi or 0 for c in contracts if c.type == "put"))
    return calls, puts, (puts / calls if calls > 0 else None)


def points_of_control(contracts: list[Contract]) -> tuple[float | None, float | None]:
    """(strike with the largest |net GEX|, strike with the most open interest). Valid contracts only for GEX."""
    gex: dict[float, float] = {}
    oi: dict[float, float] = {}
    for c in contracts:
        if c.valid and c.gex is not None:
            gex[c.strike] = gex.get(c.strike, 0.0) + c.gex
        if c.oi:
            oi[c.strike] = oi.get(c.strike, 0.0) + c.oi
    g = max(gex, key=lambda k: (abs(gex[k]), -k)) if gex and any(gex.values()) else None
    o = max(oi, key=lambda k: (oi[k], -k)) if oi else None
    return g, o


def cumulative_zero(contracts: list[Contract], spot: float) -> float | None:
    """Zero crossing of the strike-ordered cumulative net GEX nearest spot, linearly interpolated. None if it never crosses."""
    by: dict[float, float] = {}
    for c in contracts:
        if c.valid and c.gex is not None:
            by[c.strike] = by.get(c.strike, 0.0) + c.gex
    if len(by) < 2:
        return None
    ks = sorted(by)
    cum, run = [], 0.0
    for k in ks:
        run += by[k]
        cum.append(run)
    crossings = []
    for i in range(1, len(ks)):
        a, b = cum[i - 1], cum[i]
        if a == 0.0:
            crossings.append(ks[i - 1])
        elif a * b < 0:
            crossings.append(ks[i - 1] + (ks[i] - ks[i - 1]) * (-a) / (b - a))
    return min(crossings, key=lambda x: abs(x - spot)) if crossings else None


# -- volatility structure --------------------------------------------------------------

def _strike_iv(group: list[Contract]) -> dict[float, float]:
    """IV per strike: the average of the call's and the put's own usable IVs when both exist, else the one that does."""
    acc: dict[float, list[float]] = {}
    for c in group:
        if c.valid and iv_ok(c.iv):
            acc.setdefault(c.strike, []).append(c.iv)
    return {k: sum(v) / len(v) for k, v in acc.items()}


def atm_iv(group: list[Contract], spot: float) -> float | None:
    """IV at spot: the two strikes bracketing spot (each inside ATM_BAND), interpolated linearly. None when it cannot be made."""
    iv = _strike_iv(group)
    near = {k: v for k, v in iv.items() if abs(k / spot - 1.0) <= ATM_BAND}
    if len(near) < MIN_ATM_IVS:
        return None
    below = [k for k in near if k <= spot]
    above = [k for k in near if k >= spot]
    if not below or not above:
        return None
    lo, hi = max(below), min(above)
    if lo == hi:
        return near[lo]
    w = (spot - lo) / (hi - lo)
    return near[lo] * (1.0 - w) + near[hi] * w


def _iv_at_delta(points: list[tuple[float, float]], target: float) -> float | None:
    """Linear interpolation of IV at |delta| = target. points = (|delta|, iv). Needs a point on each side."""
    pts = sorted(points)
    for (d0, v0), (d1, v1) in zip(pts, pts[1:]):
        if d0 <= target <= d1 and d1 > d0:
            return v0 + (v1 - v0) * (target - d0) / (d1 - d0)
    return None


def risk_reversal(group: list[Contract], spot: float, target: float = RR_DELTA) -> float | None:
    calls, puts = [], []
    for c in group:
        if not (c.valid and iv_ok(c.iv) and c.T and c.T > 0):
            continue
        u = c.fwd or spot
        d = bsm.delta(u, c.strike, c.T, 0.0, c.iv, c.type)
        (calls if c.type == "call" else puts).append((abs(d), c.iv))
    cv, pv = _iv_at_delta(calls, target), _iv_at_delta(puts, target)
    return None if cv is None or pv is None else cv - pv


def expected_move_for(contracts: list[Contract], spot: float) -> tuple[ExpectedMove | None, float | None, date | None]:
    """Expected move and 25-delta RR from the nearest expiry with at least EXPECTED_MOVE_MIN_DAYS left and a usable ATM IV."""
    by: dict[date, list[Contract]] = {}
    for c in contracts:
        if c.T and c.T * 365.0 >= EXPECTED_MOVE_MIN_DAYS:
            by.setdefault(c.expiry, []).append(c)
    for exp in sorted(by):
        grp = by[exp]
        iv = atm_iv(grp, spot)
        if iv is None:
            continue
        T = grp[0].T
        fwd = next((c.fwd for c in grp if c.fwd), None) or spot
        sig = fwd * iv * math.sqrt(T)
        em = ExpectedMove(exp, T * 365.0, iv, sig, sig / spot * 100.0, spot - sig, spot + sig)
        return em, risk_reversal(grp, spot), exp
    return None, None, None


# -- entry point -----------------------------------------------------------------------

def compute(contracts: list[Contract], spot: float) -> Structure:
    s = Structure()
    if not contracts or spot <= 0:
        return s
    vnet, vabs, cnet, cabs, n = vex_cex(contracts, spot)
    if n:
        s.vex_net, s.vex_abs, s.cex_net, s.cex_abs, s.contracts = vnet, vabs, cnet, cabs, n
    s.call_oi, s.put_oi, s.pcr_oi = put_call_oi(contracts)
    s.gex_poc, s.oi_poc = points_of_control(contracts)
    s.cum_zero = cumulative_zero(contracts, spot)
    s.max_pain = max_pain_by_expiry(contracts, spot)
    s.expected_move, s.rr25, s.rr25_expiry = expected_move_for(contracts, spot)
    return s
