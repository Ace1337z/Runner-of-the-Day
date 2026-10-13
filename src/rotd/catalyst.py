"""Catalyst service (build prompt §10).

The language model **only classifies** news and filings — it never ranks, scores
or times anything. It runs **asynchronously**, off the decision path: a cycle
never blocks on a model call. Verdicts are cached by ``(symbol, item_id)`` for
``CATALYST_CACHE_TTL_H``.

A verdict is one of ``MAJOR`` / ``MINOR`` / ``NONE`` / ``UNKNOWN`` with a
confidence 0..1, plus the headline and a one-line rationale. On timeout, missing
credentials or a bad response the verdict is ``UNKNOWN`` — fail closed, never a
false positive.

The model endpoint is OpenAI-compatible (``AI_BASE_URL``/``AI_KEY``/``AI_MODEL``).
When unset, the service returns ``UNKNOWN`` and logs once; the system runs fully
without it.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from .config import Config
from .store import Store
from .util import now_utc, stable_json

log = logging.getLogger(__name__)

VERDICT_MAJOR = "MAJOR"
VERDICT_MINOR = "MINOR"
VERDICT_NONE = "NONE"
VERDICT_UNKNOWN = "UNKNOWN"

# A one-shot, low-temperature classification prompt. The model returns JSON only.
_SYSTEM = (
    "You are a strict equity-catalyst classifier for US penny stocks. You are "
    "given one news item or SEC filing about a sub-$10 US-listed stock. Decide "
    "whether it is a MATERIAL catalyst that could drive a large intraday move. "
    "Answer with JSON only: "
    '{"verdict":"MAJOR|MINOR|NONE","confidence":0.0-1.0,"reason":"<=20 words"}. '
    "MAJOR = a definitive, company-specific event (FDA approval, major contract, "
    "merger, massive earnings beat, offering priced and cleared). MINOR = mildly "
    "relevant (analyst note, routine PR). NONE = irrelevant, promotional or "
    "unclear. Never speculate beyond the given text."
)


@dataclass
class CatalystVerdict:
    symbol: str
    item_id: str
    kind: str
    verdict: str
    confidence: Optional[float]
    headline: str = ""
    reason: str = ""
    model: str = ""
    cached: bool = False
    at: Optional[datetime] = None

    @property
    def score(self) -> Optional[float]:
        """Map a verdict to a 0..100 catalyst score the scorer can consume."""
        base = {VERDICT_MAJOR: 90.0, VERDICT_MINOR: 50.0, VERDICT_NONE: 10.0}.get(self.verdict)
        if base is None:
            return None
        conf = self.confidence if self.confidence is not None else 0.5
        return base * (0.5 + 0.5 * max(0.0, min(1.0, conf)))

    def as_dict(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "item_id": self.item_id, "kind": self.kind,
                "verdict": self.verdict, "confidence": self.confidence,
                "reason": self.reason, "cached": self.cached}


class CatalystService:
    """Classifies catalysts asynchronously and caches the verdicts."""

    def __init__(self, cfg: Config, store: Optional[Store] = None):
        self.cfg = cfg
        self.store = store
        self._inflight: set[tuple[str, str]] = set()
        self._lock = threading.Lock()
        self._warned = False

    def configured(self) -> bool:
        return bool(self.cfg.raw("AI_BASE_URL") and self.cfg.raw("AI_KEY"))

    # -- cache --------------------------------------------------------------
    def cached(self, symbol: str, item_id: str) -> Optional[CatalystVerdict]:
        if not self.store:
            return None
        row = self.store.get_catalyst(symbol, item_id)
        if not row:
            return None
        ttl = self.cfg.float("CATALYST_CACHE_TTL_H", 24)
        try:
            decided = datetime.fromisoformat(row["decided_at"])
        except (ValueError, TypeError):
            return None
        if now_utc() - decided > timedelta(hours=ttl):
            return None
        return CatalystVerdict(symbol=symbol, item_id=item_id, kind=row["kind"],
                               verdict=row["verdict"], confidence=row["confidence"],
                               headline=row["headline"], model=row["model"],
                               cached=True, at=decided)

    def _store_verdict(self, v: CatalystVerdict) -> None:
        if self.store:
            self.store.save_catalyst(v.symbol, v.item_id, v.kind, v.verdict,
                                     v.confidence, v.headline, v.reason, v.model)

    # -- the call -----------------------------------------------------------
    def classify(self, symbol: str, item_id: str, kind: str, headline: str,
                 body: str = "") -> CatalystVerdict:
        """Classify one item (synchronously). Returns UNKNOWN when it cannot."""
        hit = self.cached(symbol, item_id)
        if hit:
            return hit
        if not self.configured():
            if not self._warned:
                log.info("catalyst: no model configured; verdicts are UNKNOWN")
                self._warned = True
            return CatalystVerdict(symbol, item_id, kind, VERDICT_UNKNOWN, None,
                                   headline=headline, reason="no model configured")
        try:
            raw = self._call_model(headline, body)
            v = self._parse(symbol, item_id, kind, headline, raw)
        except Exception as exc:  # noqa: BLE001 - model failure is UNKNOWN, not a pass
            log.warning("catalyst: %s failed for %s/%s", type(exc).__name__, symbol, item_id)
            v = CatalystVerdict(symbol, item_id, kind, VERDICT_UNKNOWN, None,
                                headline=headline, reason=f"error: {type(exc).__name__}")
        self._store_verdict(v)
        return v

    def _call_model(self, headline: str, body: str) -> str:
        import requests
        base = self.cfg.raw("AI_BASE_URL").rstrip("/")
        model = self.cfg.str("AI_MODEL", "deepseek-v4.1-flash")
        timeout = self.cfg.float("CATALYST_TIMEOUT_S", 45)
        payload = {
            "model": model,
            "temperature": 0.0,
            "messages": [{"role": "system", "content": _SYSTEM},
                         {"role": "user", "content": f"HEADLINE: {headline}\n\n{body[:2000]}"}],
            "response_format": {"type": "json_object"},
        }
        resp = requests.post(f"{base}/chat/completions", json=payload, timeout=timeout,
                             headers={"Authorization": f"Bearer {self.cfg.raw('AI_KEY')}"})
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    @staticmethod
    def _parse(symbol: str, item_id: str, kind: str, headline: str,
               raw: str) -> CatalystVerdict:
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`")
            text = text[text.find("{"):text.rfind("}") + 1]
        try:
            obj = json.loads(text)
        except ValueError:
            return CatalystVerdict(symbol, item_id, kind, VERDICT_UNKNOWN, None,
                                   headline=headline, reason="unparseable model output")
        verdict = str(obj.get("verdict", "")).upper()
        if verdict not in (VERDICT_MAJOR, VERDICT_MINOR, VERDICT_NONE):
            verdict = VERDICT_UNKNOWN
        try:
            conf = float(obj.get("confidence"))
        except (TypeError, ValueError):
            conf = None
        return CatalystVerdict(symbol, item_id, kind, verdict, conf, headline=headline,
                               reason=str(obj.get("reason", ""))[:200])

    # -- async --------------------------------------------------------------
    def classify_async(self, symbol: str, item_id: str, kind: str, headline: str,
                       body: str = "", on_done=None) -> None:
        """Classify without blocking the caller (a background daemon thread)."""
        key = (symbol, item_id)
        with self._lock:
            if key in self._inflight:
                return
            self._inflight.add(key)

        def _run() -> None:
            try:
                v = self.classify(symbol, item_id, kind, headline, body)
                if on_done:
                    on_done(v)
            finally:
                with self._lock:
                    self._inflight.discard(key)

        threading.Thread(target=_run, name=f"catalyst-{symbol}-{item_id}", daemon=True).start()

    def best_score(self, symbol: str, items: list[dict[str, Any]]) -> Optional[float]:
        """The best (highest) catalyst score among already-cached verdicts."""
        best: Optional[float] = None
        for item in items:
            v = self.cached(symbol, str(item.get("id", "")))
            if v and v.score is not None:
                best = v.score if best is None else max(best, v.score)
        return best

    def health(self) -> tuple[bool, str]:
        if not self.configured():
            return False, "no model configured (verdicts UNKNOWN)"
        return True, f"model {self.cfg.str('AI_MODEL', '?')}"
