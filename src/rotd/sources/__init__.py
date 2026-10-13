"""ROTD data sources.

An owned fork: ROTD does **not** import the ``penny`` package. Each adapter was
built against raw fixtures captured by ``probes/probe_sources.py`` and reads
configuration through :class:`rotd.config.Config`.

Source roles (docs/VERIFIED.md §G, docs/DECISIONS.md):
- **finviz** — primary: universe, movers, quote rows, news, filings, and the only
  extended-hours price source on these plans.
- **twelvedata** — regular-session 1-min bars + freshness quote.
- **finnhub** — quote, news and the session detector (no candles: 403).
- **alpha_vantage** — free ``GLOBAL_QUOTE`` only.
- **yahoo** — last-resort chart/bars/regime.
- **sec** — filings + ticker->CIK map.
- **finra** — short-sale volume and short interest.
- **alpaca** — optional candle fallback, **BLOCKED** (key rejected).
"""

from __future__ import annotations

from typing import Optional

from .base import BaseClient, SourceError, fnum, parse_csv
from .alpha_vantage import AlphaVantageClient
from .alpaca import AlpacaBlocked, AlpacaClient
from .finnhub import FinnhubClient
from .finra import FinraClient
from .finviz import FinvizClient, FinvizTokenRejected
from .sec import SecClient
from .twelvedata import TwelveDataClient
from .yahoo import YahooClient

__all__ = [
    "BaseClient", "SourceError", "fnum", "parse_csv",
    "AlphaVantageClient", "AlpacaClient", "AlpacaBlocked",
    "FinnhubClient", "FinraClient", "FinvizClient", "FinvizTokenRejected",
    "SecClient", "TwelveDataClient", "YahooClient",
    "build_clients", "SOURCE_CLASSES",
]

SOURCE_CLASSES = {
    "finviz": FinvizClient,
    "twelvedata": TwelveDataClient,
    "finnhub": FinnhubClient,
    "alpha_vantage": AlphaVantageClient,
    "yahoo": YahooClient,
    "sec": SecClient,
    "finra": FinraClient,
    "alpaca": AlpacaClient,
}


def build_clients(cfg, only: Optional[list[str]] = None) -> dict[str, BaseClient]:
    """Instantiate the requested clients (all by default)."""
    names = only or list(SOURCE_CLASSES)
    return {n: SOURCE_CLASSES[n](cfg) for n in names if n in SOURCE_CLASSES}
