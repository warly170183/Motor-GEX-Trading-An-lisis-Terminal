"""Crypto GEX history: snapshots, per-candle modeled flow, replay endpoints. Offline (demo fixtures, temp DB)."""
import time

import pytest
from fastapi.testclient import TestClient

from gexlab import history
from gexlab.engine import gex as G


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("GEXLAB_DEMO", "1")
    monkeypatch.setenv("GEXLAB_DEMO_DB", str(tmp_path / "demo.db"))
    from gexlab import api, service
    service._CHAINS.clear(); service._LEVEL_CACHE.clear()
    return TestClient(api.app, base_url="http://127.0.0.1")


def test_move_flow_signs_and_flat_value():
    prof = [{"spot": 100 * (1 + i / 1000), "net_gex": 4e6} for i in range(-150, 151)]          # long gamma
    up = G.hedge_flow_move(prof, 100, 101)
    down = G.hedge_flow_move(prof, 100, 99)
    assert up == pytest.approx(-4e6 * 0.995033, rel=0.002) and down > 0          # sells a rise, buys a fall
    neg = [{"spot": p["spot"], "net_gex": -4e6} for p in prof]
    assert G.hedge_flow_move(neg, 100, 101) > 0 > G.hedge_flow_move(neg, 100, 99)  # short gamma chases
    assert G.hedge_flow_move(prof, 100, 100) == 0.0
    assert G.hedge_flow_move(prof, 100, 101) == pytest.approx(-G.hedge_flow_move(prof, 101, 100))   # path reversal


def test_move_flow_clamps_and_rejects_short_curves():
    assert G.hedge_flow_move([{"spot": 1, "net_gex": 1}], 1, 2) is None
    prof = [{"spot": 100 * (1 + i / 100), "net_gex": 1e6} for i in range(-5, 6)]
    assert G.hedge_flow_move(prof, 100, 200) == G.hedge_flow_move(prof, 100, 105)         # clamped to the curve


def test_record_and_read_back(env):
    now = int(time.time())
    assert history.record("BTC", now - 600) and history.record("BTC", now - 300) and history.record("BTC", now)
    assert history.record("SUI", now) is False                                            # no options: nothing stored
    ts = history.timestamps("BTC", now - 3600)
    assert ts == [now - 600, now - 300, now]
    s = history.snapshot("BTC", now - 301)                                               # at or just before
    assert s["ts"] == now - 600
    assert s["levels"]["call_wall"] and s["strikes"] and s["hedge_flow"]["bins"]


def test_candle_flows_only_where_a_snapshot_exists(env):
    base = 1_800_000_000 // 900 * 900
    history.record("BTC", base + 450)                   # inside candle 0 only
    s = history.snapshot("BTC", base + 450)
    sp = s["spot"]
    candles = [{"t": base, "o": sp, "h": sp * 1.01, "l": sp * 0.99, "c": sp * 1.005, "v": 1, "confirmed": True},
               {"t": base + 900 * 40, "o": sp, "h": sp, "l": sp, "c": sp * 1.01, "v": 1, "confirmed": True}]
    out = history.candle_flows("BTC", candles, 900)
    assert [r["t"] for r in out["rows"]] == [base] and out["coverage"]["snaps"] == 1
    assert out["rows"][0]["flow_usd"] == pytest.approx(G.hedge_flow_move(s["profile"], sp, sp * 1.005))


def test_candle_flows_without_history_is_empty(env):
    out = history.candle_flows("ETH", [{"t": 1_800_000_000, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1, "confirmed": True}], 900)
    assert out["rows"] == [] and out["coverage"]["snaps"] == 0


def test_endpoints(env):
    now = int(time.time())
    history.record("ETH", now)
    j = env.get("/api/history/snapshots", params={"ticker": "ETH", "hours": 1}).json()
    assert j["timestamps"] == [now]
    snap = env.get("/api/history/snapshot", params={"ticker": "ETH", "ts": now}).json()
    assert snap["ticker"] == "ETH" and snap["profile"]
    assert env.get("/api/history/snapshot", params={"ticker": "ETH", "ts": now - 10_000}).status_code == 404
    assert env.get("/api/history/snapshots", params={"ticker": "SPY"}).status_code == 404
    f = env.get("/api/history/flow", params={"ticker": "ETH", "bar": "15m", "limit": 50}).json()
    assert f["ticker"] == "ETH" and "coverage" in f and isinstance(f["rows"], list)


def test_prune_keeps_recent_rows(env):
    now = int(time.time())
    history.record("BTC", now - 20 * 86400)
    history.record("BTC", now)
    assert history.prune(14) == 1 and history.timestamps("BTC") == [now]


def test_recorder_is_off_in_demo(env):
    assert history.start_recorder() is False
