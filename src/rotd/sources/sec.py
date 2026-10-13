"""SEC EDGAR client — filings, ticker->CIK map and the daily index.

Verified (docs/VERIFIED.md G-12): all three endpoints work with a descriptive
``User-Agent`` and ``Accept-Encoding: identity``. The Atom submissions feed is
**ISO-8859-1**, not UTF-8, so it must be decoded as such; the daily-index landing
page is HTML and its *contents* page is the machine-readable one.

SEC requires a real contact in the User-Agent. ``SEC_USER_AGENT`` supplies it;
requests without one get 403.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from typing import Any, Optional

from .base import BaseClient, SourceError, fnum

log = logging.getLogger(__name__)

BASE = "https://www.sec.gov"
_DEFAULT_UA = "big-whale-penny-tracker research contact@example.com"
_ATOM_NS = "{http://www.w3.org/2005/Atom}"


class SecClient(BaseClient):
    name = "sec"

    def __init__(self, cfg, rate: Optional[tuple[int, float]] = (8, 1.0)):
        # SEC asks for <=10 requests/second.
        super().__init__(cfg, secrets=[], rate=rate)

    def _headers(self) -> dict[str, str]:
        ua = self.cfg.str("SEC_USER_AGENT", _DEFAULT_UA)
        # SEC requires a contact address in the UA.
        if "@" not in ua:
            ua = f"{ua} contact@example.com"
        return {"User-Agent": ua, "Accept-Encoding": "identity",
                "Host": "www.sec.gov"}

    def _get_text(self, url: str, encoding: str = "utf-8") -> str:
        resp = self._request("GET", url, headers=self._headers(),
                             accept="application/json, application/xml, text/html")
        if resp.status_code in (401, 403):
            raise SourceError(f"sec: rejected (HTTP {resp.status_code}); check User-Agent")
        if resp.status_code >= 400:
            raise SourceError(f"sec: HTTP {resp.status_code}")
        return resp.content.decode(encoding, errors="replace")

    # -- endpoints ----------------------------------------------------------
    def company_tickers(self) -> dict[str, dict[str, Any]]:
        """CIK/ticker/name map. Keyed by upper-case ticker."""
        text = self._get_text(f"{BASE}/files/company_tickers.json")
        import json
        try:
            rows = json.loads(text)
        except ValueError as exc:
            raise SourceError("sec: bad company_tickers JSON") from exc
        out: dict[str, dict[str, Any]] = {}
        for r in rows.values() if isinstance(rows, dict) else rows:
            t = (r.get("ticker") or "").upper()
            if t:
                out[t] = {"cik": int(r["cik_str"]), "title": r.get("title", "")}
        return out

    def submissions(self, cik: int) -> dict[str, Any]:
        """An issuer's recent filings (Atom, ISO-8859-1)."""
        text = self._get_text(f"{BASE}/cgi-bin/browse-edgar?action=getcompany"
                              f"&CIK={cik:010d}&type=&dateb=&owner=include"
                              f"&count=40&output=atom", encoding="iso-8859-1")
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            raise SourceError("sec: bad submissions Atom") from exc
        filings = []
        for entry in root.findall(f"{_ATOM_NS}entry"):
            def _txt(tag: str) -> str:
                el = entry.find(f"{_ATOM_NS}{tag}")
                return (el.text or "").strip() if el is not None else ""
            filings.append({
                "title": _txt("title"),
                "form": _form_from_title(_txt("title")),
                "updated": _txt("updated"),
                "summary": _txt("summary"),
                "link": _link(entry),
            })
        return {"cik": cik, "company": _company_name(root), "filings": filings}

    def daily_index(self, date: str) -> str:
        """Raw daily index for ``YYYY-MM-DD`` (pipe-delimited .idx)."""
        y, m, d = date.split("-")
        quarter = (int(m) - 1) // 3 + 1
        url = f"{BASE}/Archives/edgar/daily-index/{y}/QTR{quarter}/form.{y}{m}{d}.idx"
        return self._get_text(url)

    def fetch_url(self, url: str, encoding: str = "utf-8") -> str:
        """Fetch an absolute SEC URL (a filing document) as text."""
        if not url.startswith(BASE):
            raise SourceError("sec: refusing a non-SEC URL")
        return self._get_text(url, encoding=encoding)

    def health(self) -> tuple[bool, str]:
        try:
            self._get_text(f"{BASE}/files/company_tickers.json")
            return True, "ok"
        except Exception as exc:  # noqa: BLE001
            return False, self.error_text(exc)


def _form_from_title(title: str) -> str:
    m = re.search(r"\((\w[\w/\- ]*)\)\s*$", title)
    return m.group(1).strip() if m else ""


def _link(entry) -> str:
    for link in entry.findall(f"{_ATOM_NS}link"):
        if link.get("rel") == "alternate" and link.get("href"):
            return link.get("href")
    return ""
