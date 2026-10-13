"""Shared plumbing for ROTD data sources.

Owned fork: ROTD does not import the ``penny`` package. The patterns here
(rate limiting, TTL cache, tolerant CSV/number parsing, a base HTTP client that
scrubs secrets) are adapted from the earlier system but every adapter in this
package is built against ROTD's own fixtures (``sources/fixtures/``) captured by
``probes/probe_sources.py``, and reads configuration through ROTD's ``Config``.

Design rules (build prompt §4.4/§5):
- A source never decides anything. It returns raw records plus a fetch timestamp.
- A failure is ``SourceError`` and callers fail soft (fail closed downstream).
- Secrets never appear in logs, messages or exceptions.
"""

from __future__ import annotations

import csv
import io
import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional

from ..util import is_trading_day, scrub

log = logging.getLogger(__name__)


class SourceError(RuntimeError):
    """A recoverable source failure; callers fail soft (never a pass)."""


class RateLimiter:
    """Thread-safe limiter: at most ``rate`` calls per ``per_seconds``."""

    def __init__(self, rate: int, per_seconds: float = 60.0):
        self.rate = max(1, int(rate))
        self.per = float(per_seconds)
        self._times: list[float] = []
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._times = [t for t in self._times if now - t < self.per]
                if len(self._times) < self.rate:
                    self._times.append(now)
                    return
                wait = self.per - (now - self._times[0]) + 0.01
            time.sleep(max(0.01, wait))


class TTLCache:
    def __init__(self) -> None:
        self._data: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str, ttl: float) -> Optional[Any]:
        with self._lock:
            item = self._data.get(key)
        if item is None:
            return None
        ts, val = item
        return None if time.monotonic() - ts > ttl else val

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = (time.monotonic(), value)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


def parse_csv(text: str) -> list[dict[str, str]]:
    """Parse CSV text into dicts, tolerating a BOM and blank lines."""
    if text is None:
        return []
    s = text.lstrip("\ufeff")
    if not s.strip():
        return []
    rows = []
    for row in csv.DictReader(io.StringIO(s)):
        if row is None:
            continue
        clean = {(k.strip() if k else k): (v.strip() if isinstance(v, str) else v)
                 for k, v in row.items()}
        if any(v for v in clean.values()):
            rows.append(clean)
    return rows


def looks_like_html(text: str) -> bool:
    head = (text or "").lstrip()[:200].lower()
    return head.startswith("<!doctype html") or head.startswith("<html") or "<html" in head


def fnum(value: Any) -> Optional[float]:
    """Tolerant number parse: strips %, $, commas and K/M/B/T multipliers."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", "").replace("%", "").replace("$", "")
    if not s or s in {"-", "n/a", "N/A", "NA", "--", "None", "nan", "null"}:
        return None
    mult = 1.0
    if s and s[-1].upper() in {"K", "M", "B", "T"}:
        mult = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}[s[-1].upper()]
        s = s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return None


def finviz_avg_volume_shares(row: dict[str, str]) -> Optional[float]:
    """Finviz's ``Average Volume`` column is in thousands; other feeds are shares."""
    thousands = fnum(row.get("Average Volume"))
    if thousands is not None:
        return thousands * 1000.0
    return fnum(row.get("Avg Volume"))


def fixture_dir() -> Path:
    return Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> bytes:
    """Read a saved raw response (bytes; SEC fixtures are ISO-8859-1)."""
    return (fixture_dir() / name).read_bytes()


def fixture_text(name: str, encoding: str = "utf-8") -> str:
    return load_fixture(name).decode(encoding, errors="replace")


class BaseClient:
    """Common HTTP behaviour: timeouts, retries, secret scrubbing, limiter."""

    name = "base"

    def __init__(self, cfg, secrets: Optional[list[str]] = None,
                 rate: Optional[tuple[int, float]] = None):
        self.cfg = cfg
        self._secrets = [s for s in (secrets or []) if s]
        self._session = None
        self._cache = TTLCache()
        self._api_calls = 0
        self._lock = threading.Lock()
        self._limiter = RateLimiter(*rate) if rate else None

    @property
    def secrets(self) -> list[str]:
        return list(self._secrets)

    def session(self):
        if self._session is None:
            import requests
            self._session = requests.Session()
            self._session.headers.update({"User-Agent": "big-whale-penny-tracker/0.1"})
        return self._session

    def count_call(self, n: int = 1) -> None:
        with self._lock:
            self._api_calls += n

    @property
    def api_calls(self) -> int:
        with self._lock:
            return self._api_calls

    def reset_calls(self) -> None:
        with self._lock:
            self._api_calls = 0

    def _request(self, method: str, url: str, accept: str = "", **kwargs):
        if self._limiter is not None:
            self._limiter.acquire()
        self.count_call()
        headers = dict(kwargs.pop("headers", {}) or {})
        if accept:
            headers.setdefault("Accept", accept)
        kwargs.setdefault("timeout", 20.0)
        try:
            resp = self.session().request(method, url, headers=headers, **kwargs)
        except Exception as exc:  # noqa: BLE001 - network failure is data absence
            raise SourceError(self.error_text(exc)) from exc
        return resp

    def get_text(self, url: str, encoding: str = "utf-8", **kwargs) -> str:
        resp = self._request("GET", url, **kwargs)
        if resp.status_code in (401, 403):
            raise SourceError(f"{self.name}: rejected (HTTP {resp.status_code})")
        if resp.status_code >= 400:
            raise SourceError(f"{self.name}: HTTP {resp.status_code}")
        return resp.content.decode(encoding, errors="replace")

    def get_json(self, url: str, **kwargs) -> Any:
        resp = self._request("GET", url, accept="application/json", **kwargs)
        if resp.status_code in (401, 403):
            raise SourceError(f"{self.name}: rejected (HTTP {resp.status_code})")
        if resp.status_code >= 400:
            raise SourceError(f"{self.name}: HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise SourceError(f"{self.name}: invalid JSON") from exc

    def health(self) -> tuple[bool, str]:
        return True, "ok"

    def error_text(self, exc: Exception) -> str:
        return scrub(str(exc), self.secrets)

    def is_market_day(self) -> bool:
        return is_trading_day()
