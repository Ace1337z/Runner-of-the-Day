"""TwelveData 429 handling: quote cache, local pacing, and source cooldown.

No network. The HTTP session is replaced with a scripted fake so each test pins
one behaviour: a cache hit costs no call, a second fetch inside the minimum gap is
refused locally, and a 429 puts the source on cooldown without a second request.
"""
from __future__ import annotations

import pytest

from rotd.config import Config
from rotd.sources.base import SourceError
from rotd.sources.twelvedata import TwelveDataClient


class _Resp:
    def __init__(self, status: int, body=None, headers=None):
        self.status_code = status
        self._body = body or {}
        self.headers = headers or {}
        self.content = b"{}"

    def json(self):
        return self._body


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def request(self, method, url, **kwargs):
        self.calls += 1
        return self.responses.pop(0)


QUOTE = {"symbol": "ABCD", "close": "3.42", "previous_close": "3.22",
         "timestamp": 1760000000}


def _client(tmp_path, responses, **settings):
    path = tmp_path / "rotd.env"
    lines = ["TWELVEDATA_KEY=testkey"] + [f"{k}={v}" for k, v in settings.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    cfg = Config(path=path)
    client = TwelveDataClient(cfg, rate=None)
    client._session = _Session(responses)
    return client


def test_cache_hit_costs_no_call(tmp_path):
    c = _client(tmp_path, [_Resp(200, QUOTE)], TWELVEDATA_MIN_INTERVAL_S=0,
                QUOTE_CACHE_TTL_S=60)
    first = c.quote("ABCD")
    second = c.quote("ABCD")
    assert first["price"] == 3.42 and second["price"] == 3.42
    assert c._session.calls == 1


def test_fetch_inside_min_gap_is_refused_locally(tmp_path):
    c = _client(tmp_path, [_Resp(200, QUOTE)], TWELVEDATA_MIN_INTERVAL_S=600,
                QUOTE_CACHE_TTL_S=0)
    c.quote("ABCD")
    with pytest.raises(SourceError, match="throttled locally"):
        c.quote("EFGH")
    assert c._session.calls == 1


def test_429_sets_cooldown_and_skips_network(tmp_path):
    c = _client(tmp_path, [_Resp(429, {}, {"Retry-After": "120"})],
                TWELVEDATA_MIN_INTERVAL_S=0, QUOTE_CACHE_TTL_S=0)
    with pytest.raises(SourceError, match="429"):
        c.quote("ABCD")
    # During cooldown the next call fails fast with no second HTTP request.
    with pytest.raises(SourceError, match="rate limited"):
        c.quote("EFGH")
    assert c._session.calls == 1


def test_429_without_retry_after_uses_named_cooldown(tmp_path):
    c = _client(tmp_path, [_Resp(429)], TWELVEDATA_MIN_INTERVAL_S=0,
                QUOTE_CACHE_TTL_S=0, SOURCE_COOLDOWN_S=90)
    with pytest.raises(SourceError):
        c.quote("ABCD")
    assert c._cooldown_until > 0
    import time
    remaining = c._cooldown_until - time.monotonic()
    assert 80 < remaining <= 91
