"""Local price history: paged backfill, idempotency, confirmed-only storage, status endpoint. Offline (fake pager, temp DB)."""
import threading

import pytest
from fastapi.testclient import TestClient

from gexlab import config, ohlcv

NOW = 1_800_000_000


@pytest.fixture(autouse=True)
def db(monkeypatch, tmp_path):
    monkeypatch.setenv("GEXLAB_DB", str(tmp_path / "t.db"))
    monkeypatch.delenv("GEXLAB_DEMO", raising=False)


def fake_page(step_s, calls=None, forming=True, now=NOW):
    """OKX-like pager: bars exist at every multiple of step_s up to NOW; newest first, 100 per page, `after` = strictly older."""
    def page(inst, bar, after_ms):
        if calls is not None:
            calls.append(after_ms)
        top = (now // step_s) * step_s
        if after_ms is not None:
            top = min(top, (after_ms // 1000) - step_s)
        rows = []
        for i in range(100):
            t = top - i * step_s
            if t < NOW - 4000 * 86400:
                break
            rows.append([str(t * 1000), "10", "12", "9", "11", "5", "50", "500", "1" if not (forming and t == (now // step_s) * step_s) else "0"])
        return rows
    return page


def test_first_sync_reaches_target_depth_and_skips_forming_bar():
    n = ohlcv.sync_series("BTC", "1H", fake_page(3600), now=NOW)
    lo, hi, cnt = ohlcv.span("BTC", "1H")
    assert cnt == n and hi == (NOW // 3600 - 1) * 3600          # the forming bar is not stored
    assert lo <= NOW - 365 * 86400 and lo > NOW - 366 * 86400 - 100 * 3600
    assert cnt >= 365 * 24 - 2


def test_second_sync_only_fetches_the_gap_and_is_idempotent():
    ohlcv.sync_series("ETH", "1D", fake_page(86400), now=NOW)
    _, _, before = ohlcv.span("ETH", "1D")
    calls = []
    n = ohlcv.sync_series("ETH", "1D", fake_page(86400, calls), now=NOW)
    assert n == 0 and len(calls) == 1 and ohlcv.span("ETH", "1D")[2] == before      # one request, nothing new


def test_new_bars_extend_the_series_and_deeper_target_extends_backwards():
    ohlcv.sync_series("SOL", "4H", fake_page(14400, now=NOW - 20 * 86400), now=NOW - 20 * 86400)
    _, hi0, _ = ohlcv.span("SOL", "4H")
    ohlcv.sync_series("SOL", "4H", fake_page(14400), now=NOW)
    lo, hi, _ = ohlcv.span("SOL", "4H")
    assert hi > hi0 and lo <= NOW - 730 * 86400


def test_young_coin_with_short_history_is_just_shorter():
    def young(inst, bar, after_ms):
        full = fake_page(86400, forming=False)(inst, bar, after_ms)
        return [r for r in full if int(r[0]) // 1000 > NOW - 30 * 86400]
    ohlcv.sync_series("TRX", "1D", young, now=NOW)
    lo, _, cnt = ohlcv.span("TRX", "1D")
    assert 25 <= cnt <= 31 and lo > NOW - 31 * 86400


def test_read_returns_oldest_first_and_before_filter():
    ohlcv.sync_series("BTC", "1D", fake_page(86400), now=NOW)
    rows = ohlcv.read("BTC", "1D", 50)
    ts = [r["t"] for r in rows]
    assert len(rows) == 50 and ts == sorted(ts)
    older = ohlcv.read("BTC", "1D", 10, before_t=ts[0])
    assert len(older) == 10 and older[-1]["t"] < ts[0]


def test_sync_all_survives_one_failing_series_and_reports_progress():
    from gexlab.sources.base import SourceError
    steps = {"5m": 300, "15m": 900, "1H": 3600, "4H": 14400, "1Dutc": 86400}

    def page(inst, bar, after_ms):
        if inst == config.CRYPTO_OKX_SWAP["TRX"]:
            raise SourceError("boom")
        return fake_page(steps[bar])(inst, bar, after_ms)
    import time as _t
    real = _t.time; _t.time = lambda: NOW
    try:
        out = ohlcv.sync_all(page=page, pace_s=0)
    finally:
        _t.time = real
    assert out["TRX:5m"] == -1 and out["BTC:5m"] > 0
    s = ohlcv.status()
    assert s["done"] == s["total"] == len(config.CRYPTO_TICKERS) * 5 and s["finished"] and not s["running"] and s["error"]


def test_stop_event_halts_the_download():
    ev = threading.Event(); ev.set()
    assert ohlcv.sync_all(page=fake_page(300), pace_s=0, stop=ev) == {} and not ohlcv.status()["finished"]


def test_start_is_a_noop_in_demo_or_when_disabled(monkeypatch):
    monkeypatch.setenv("GEXLAB_DEMO", "1")
    assert ohlcv.start() is False
    monkeypatch.delenv("GEXLAB_DEMO"); monkeypatch.setenv("GEXLAB_HISTORY", "0")
    assert ohlcv.start() is False


def test_backfill_endpoint(monkeypatch):
    monkeypatch.setenv("GEXLAB_DEMO", "1")
    monkeypatch.setenv("GEXLAB_DEMO_DB", str(__import__("pathlib").Path(config.ROOT) / "data" / "gexlab_demo.db"))
    from gexlab import api
    j = TestClient(api.app, base_url="http://127.0.0.1").get("/api/history/backfill").json()
    assert {"running", "done", "total", "stored_bars", "targets_days"} <= set(j) and j["targets_days"]["1D"] == 1095
