"""Valuation clock (model v1.0): T is measured from one explicit valuation time per map or capture.

In regular hours that is the option quote time (never later than now). With the market closed it
is the last regular-session close, and the spot is that session's official close, never an
extended-hours print. Measuring T from a Saturday wall clock against Friday's IVs overstates near-the-money gamma, so the
rule is tested here on the exchange-calendar engine itself.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from gexlab.engine.calendar import NY, TradingCalendar, valuation_label, valuation_time
from conftest import exchange_cal

UTC = timezone.utc
CAL = exchange_cal()
FRI_CLOSE = datetime(2026, 10, 16, 20, 0, tzinfo=UTC)            # Fri Oct 16 16:00 ET
FRI_1615 = datetime(2026, 10, 16, 20, 15, tzinfo=UTC)            # last quote of the session
SAT = datetime(2026, 10, 17, 18, 48, tzinfo=UTC)                 # Sat 14:48 ET, when the bug was found


def nyt(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=NY).astimezone(UTC)


# ----------------------------------------------------------------------------- the rule

def test_closed_market_valuation_is_the_last_session_close():
    assert valuation_time(CAL, SAT) == FRI_CLOSE                          # weekend
    assert valuation_time(CAL, nyt(2026, 10, 16, 20)) == FRI_CLOSE        # after hours, same day
    assert valuation_time(CAL, nyt(2026, 10, 19, 8)) == FRI_CLOSE         # Monday pre-market
    assert valuation_time(CAL, SAT, FRI_1615) == FRI_CLOSE                # a 16:15 payload is valued at 16:00
    older = nyt(2026, 10, 16, 15, 40)
    assert valuation_time(CAL, SAT, older) == older                       # data older than the close: its own time


def test_in_session_valuation_is_the_quote_time_never_later_than_now():
    now = nyt(2026, 10, 16, 11, 0)
    q = now - timedelta(minutes=15)
    assert valuation_time(CAL, now, q) == q                               # a 15 min delayed quote
    assert valuation_time(CAL, now, now + timedelta(seconds=5)) == now    # a future vendor stamp is capped
    assert valuation_time(CAL, now) == now
    assert valuation_time(None, now, q) == q                              # no calendar: in-session clock


def test_holiday_and_early_close():
    rows = [{"date": "2026-11-25", "open": "09:30", "close": "16:00"},     # Wed
            {"date": "2026-11-27", "open": "09:30", "close": "13:00"},     # Fri after Thanksgiving: early close
            {"date": "2026-11-30", "open": "09:30", "close": "16:00"}]     # Thanksgiving (Thu 26) absent
    cal = TradingCalendar.from_rows(rows)
    wed_close = nyt(2026, 11, 25, 16)
    assert valuation_time(cal, nyt(2026, 11, 26, 12)) == wed_close        # holiday: prior session close
    assert cal.market_open(nyt(2026, 11, 27, 12, 30))
    assert valuation_time(cal, nyt(2026, 11, 27, 12, 30), nyt(2026, 11, 27, 12, 29)) == nyt(2026, 11, 27, 12, 29)
    fri_early = nyt(2026, 11, 27, 13)
    assert valuation_time(cal, nyt(2026, 11, 27, 14)) == fri_early        # after an early close
    assert valuation_time(cal, nyt(2026, 11, 28, 10)) == fri_early        # the weekend after it
    assert valuation_label(fri_early) == "Valued at Fri Nov 27 close"
    assert valuation_label(nyt(2026, 11, 27, 12, 45), at_close=False) == "Valued at Fri Nov 27 12:45 ET"
