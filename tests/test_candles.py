"""Terminal screen backend: OKX candle parsing and the /api/candles endpoint (demo fixtures, offline)."""
import pytest
from fastapi.testclient import TestClient

from gexlab import candles
from gexlab.sources.base import SourceError


def test_parse_orders_oldest_first_and_flags_the_forming_bar():
    rows = [["1795183200000", "10", "12", "9", "11", "5", "50", "500", "0"],
            ["1795182300000", "9", "10", "8", "10", "4", "40", "400", "1"],
            ["bad"]]
    out = candles.parse(rows)
    assert [c["t"] for c in out] == [1795182300, 1795183200]
    assert out[0]["confirmed"] is True and out[1]["confirmed"] is False
    assert out[1]["o"] == 10 and out[1]["h"] == 12 and out[1]["l"] == 9 and out[1]["c"] == 11 and out[1]["v"] == 50


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GEXLAB_DEMO", "1")
    from gexlab import api
    return TestClient(api.app, base_url="http://127.0.0.1")


def test_endpoint_returns_recorded_candles(client):
    j = client.get("/api/candles", params={"ticker": "BTC", "bar": "1H", "limit": 50}).json()
    assert j["ticker"] == "BTC" and j["bar"] == "1H" and len(j["candles"]) == 50
    ts = [c["t"] for c in j["candles"]]
    assert ts == sorted(ts) and all(c["l"] <= min(c["o"], c["c"]) and c["h"] >= max(c["o"], c["c"]) for c in j["candles"])


def test_endpoint_rejects_equities_and_bad_bars(client):
    assert client.get("/api/candles", params={"ticker": "SPY"}).status_code == 404
    assert client.get("/api/candles", params={"ticker": "BTC", "bar": "7m"}).status_code == 400


def test_low_priced_coin_keeps_its_decimals(client):
    c = client.get("/api/candles", params={"ticker": "TRX", "bar": "15m", "limit": 10}).json()["candles"][-1]
    assert 0.05 < c["c"] < 2 and round(c["c"], 5) == c["c"]


def test_unknown_coin_is_a_source_error():
    with pytest.raises(SourceError):
        candles.get_candles("DOGE", "15m")
