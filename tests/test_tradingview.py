"""TradingView scanner adapter + runtime wiring. No network: sessions are faked.

Pins the conservative behaviour the design requires of an unverified feed:
only price/volume level fields, malformed rows dropped, HTTP failures raise
SourceError (fail soft), and the runtime keeps it out of the decision path
outside the regular session and beyond the deviation cap.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from rotd.config import Config
from rotd.sources.base import SourceError
from rotd.sources.tradingview import SCANNER_URL, TradingViewClient
from rotd.store import Store
from rotd.runtime import Runtime


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body or {}
        self.headers = {}

    def json(self):
        return self._body


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs.get("json")))
        return self.responses.pop(0)


def _client(tmp_path, responses, **settings):
    path = tmp_path / "rotd.env"
    lines = [f"{k}={v}" for k, v in settings.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    cfg = Config(path=path)
    client = TradingViewClient(cfg)
    client._session = _Session(responses)
    return client


def test_batch_parses_minimal_columns(tmp_path):
    body = {"data": [
        {"s": "NASDAQ:AAPL", "d": ["AAPL", 3.42, 5.2, 12345]},
        {"s": "NYSE:ZXYZ", "d": ["ZXYZ", 0.88, 3.1, 500]},
    ]}
    c = _client(tmp_path, [_Resp(200, body)])
    out = c.fetch_batch_metrics(["aapl", "ZXYZ"])
    assert out["AAPL"]["close"] == 3.42
    assert out["AAPL"]["change_pct"] == 5.2
    assert out["ZXYZ"]["volume"] == 500
    # keyless: one POST, correct endpoint, minimal columns
    method, url, payload = c._session.calls[0]
    assert method == "POST" and url == SCANNER_URL
    assert payload["columns"] == ["name", "close", "change", "volume"]


def test_http_error_raises_source_error(tmp_path):
    c = _client(tmp_path, [_Resp(500)])
    with pytest.raises(SourceError, match="HTTP 500"):
        c.fetch_batch_metrics(["AAPL"])


def test_malformed_row_dropped(tmp_path):
    body = {"data": [
        {"s": "NASDAQ:AAPL", "d": ["AAPL", 3.42, 5.2, 12345]},  # good
        {"s": "NYSE:BAD", "d": ["BAD", 1.0]},                    # column-count mismatch
        {"d": ["NO", 2.0, 1.0, 10]},                             # no symbol
    ]}
    c = _client(tmp_path, [_Resp(200, body)])
    out = c.fetch_batch_metrics(["AAPL", "BAD"])
    assert list(out) == ["AAPL"]


def test_cache_hit_costs_no_call(tmp_path):
    body = {"data": [{"s": "NASDAQ:AAPL", "d": ["AAPL", 3.42, 5.2, 12345]}]}
    c = _client(tmp_path, [_Resp(200, body)], TRADINGVIEW_CACHE_TTL_S=60)
    c.fetch_batch_metrics(["AAPL"])
    c.fetch_batch_metrics(["AAPL"])
    assert len(c._session.calls) == 1


class _FakeTV:
    def __init__(self, batch):
        self.batch = batch
        self.calls = 0

    def fetch_batch_metrics(self, symbols):
        self.calls += 1
        return self.batch


class _FakeTD:
    """Stands in for the paced TwelveData fallback."""

    def __init__(self):
        self.calls = 0
        self.api_calls = 0

    def quote(self, sym):
        self.calls += 1
        self.api_calls += 1  # runtime breaks after the first fresh network fetch
        return {"symbol": sym.upper(), "price": 3.41, "ts": None,
                "volume": 100.0, "prev_close": 3.20}


def _runtime(tmp_path, tv, td):
    path = tmp_path / "rt.env"
    path.write_text("TWELVEDATA_KEY=testkey\nTRADINGVIEW_ENABLED=1\n",
                    encoding="utf-8")
    cfg = Config(path=path)
    store = Store(tmp_path / "rt.db")
    clients = {"tradingview": tv, "twelvedata": td}
    return Runtime(cfg, store, clients,
                   notifier=SimpleNamespace(), catalyst=SimpleNamespace())


NOW = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)


def test_runtime_prefers_batch_and_skips_twelvedata(tmp_path):
    tv = _FakeTV({"AA": {"close": 3.42, "change_pct": 5.0, "volume": 1000},
                  "BB": {"close": 1.05, "change_pct": 2.0, "volume": 500}})
    td = _FakeTD()
    rt = _runtime(tmp_path, tv, td)
    out = rt._fresh_points(["AA", "BB"], NOW, reference={"AA": 3.40, "BB": 1.05},
                           mode="OPEN")
    assert set(out) == {"AA", "BB"}
    assert out["AA"][0].source == "tradingview"
    assert td.calls == 0, "TwelveData must be skipped when one batch covers the pool"


def test_runtime_drops_deviant_price_then_falls_back(tmp_path):
    tv = _FakeTV({"AA": {"close": 10.0, "change_pct": 5.0, "volume": 1000}})
    td = _FakeTD()
    rt = _runtime(tmp_path, tv, td)
    out = rt._fresh_points(["AA"], NOW, reference={"AA": 3.40}, mode="OPEN")
    # 10.0 is >20% off the Finviz anchor: rejected; falls back to TwelveData.
    assert out["AA"][0].source == "twelvedata"
    assert td.calls == 1


def test_runtime_ignores_batch_outside_regular_session(tmp_path):
    tv = _FakeTV({"AA": {"close": 3.42, "change_pct": 5.0, "volume": 1000}})
    td = _FakeTD()
    rt = _runtime(tmp_path, tv, td)
    out = rt._fresh_points(["AA"], NOW, reference={"AA": 3.40}, mode="POST")
    assert out["AA"][0].source == "twelvedata", "extended hours stay with verified feeds"
    assert tv.calls == 1 and td.calls == 1


def test_runtime_survives_batch_failure(tmp_path):
    class _BoomTV:
        def fetch_batch_metrics(self, symbols):
            raise SourceError("tradingview: HTTP 500")

    td = _FakeTD()
    rt = _runtime(tmp_path, _BoomTV(), td)
    out = rt._fresh_points(["AA"], NOW, reference={"AA": 3.40}, mode="OPEN")
    assert out["AA"][0].source == "twelvedata"
