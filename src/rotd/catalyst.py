"""Catalyst service (build prompt §10).

Two layers, both off the decision path:

1. :mod:`rotd.auditor` — the **methodology_v3 red-team auditor**. It builds one
   FACT BUNDLE per alert and asks the model for a ``decision_v3`` DECISION
   REPORT, then validates it (§10 A4). This is the primary §10 contract.
2. :class:`CatalystService` — a thin, cached facade the runtime calls:
   ``audit_report`` (the v3 report) and ``best_score`` (a 0..100 catalyst number
   for the Stage-1 score, derived from the report's ``catalyst_quality``).

The model **only classifies and audits** — it never ranks, scores or times
anything. Verdicts are cached by ``(symbol, bundle-hash)`` for
``CATALYST_CACHE_TTL_H``. On timeout, missing credentials or a bad response the
result is the rules-only block — fail closed, never a false positive.

The model endpoint is OpenAI-compatible (``AI_BASE_URL``/``AI_KEY``/``AI_MODEL``).
When unset, the service returns the rules-only block and logs once; the system
runs fully without it.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from .auditor import AuditResult, AuditorService, BundleInputs, calibrated_confidence
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
    """Classifies catalysts asynchronously and caches the verdicts.

    Wraps :class:`~rotd.auditor.AuditorService` for the methodology_v3 report and
    exposes a 0..100 catalyst score for the Stage-1 scorer."""

    def __init__(self, cfg: Config, store: Optional[Store] = None,
                 auditor: Optional[AuditorService] = None):
        self.cfg = cfg
        self.store = store
        self.auditor = auditor or AuditorService(cfg)
        self._inflight: set[tuple[str, str]] = set()
        self._lock = threading.Lock()
        self._warned = False
        self._reports: dict[str, AuditResult] = {}

    def configured(self) -> bool:
        return self.auditor.configured()

    # -- methodology_v3 audit (§10) ----------------------------------------
    def audit_report(self, inp: BundleInputs) -> AuditResult:
        """Run (and cache) the full red-team audit for one candidate."""
        res = self.auditor.audit(inp)
        self._reports[inp.symbol.upper()] = res
        return res

    def audit_report_async(self, inp: BundleInputs, on_done=None) -> None:
        """Audit without blocking the caller (a background daemon thread)."""
        key = (inp.symbol.upper(), "audit")
        with self._lock:
            if key in self._inflight:
                return
            self._inflight.add(key)

        def _run() -> None:
            try:
                res = self.audit_report(inp)
                if on_done:
                    on_done(res)
            finally:
                with self._lock:
                    self._inflight.discard(key)

        threading.Thread(target=_run, name=f"audit-{inp.symbol}", daemon=True).start()

    def report(self, symbol: str) -> Optional[AuditResult]:
        return self._reports.get(symbol.upper())

    # -- score bridge -------------------------------------------------------
    # catalyst_quality -> a 0..100 number for the Stage-1 CATALYST component.
    # These are policy choices (a named setting would be overkill for six
    # constants); the rationale is: a REAL credited cause is the strongest
    # signal, financing/promotional are actively negative.
    _QUALITY_SCORE = {
        "REAL": 85.0, "WEAK": 40.0, "PROMOTIONAL": 15.0,
        "DILUTIVE": 10.0, "NONE": 20.0, "UNKNOWN": None,
    }

    def report_score(self, symbol: str) -> Optional[float]:
        """The 0..100 catalyst score from the cached audit report, or None."""
        res = self._reports.get(symbol.upper())
        if not res:
            return None
        base = self._QUALITY_SCORE.get(res.report.catalyst_quality)
        if base is None:
            return None
        if res.report.verdict in ("REJECT", "NO_TRADE"):
            base *= 0.5
        return base * (0.5 + 0.5 * calibrated_confidence(res.report.confidence_band, self.cfg))

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
        """The best catalyst score: the v3 audit report if present, else the
        cached per-item verdict scores."""
        rep_score = self.report_score(symbol)
        if rep_score is not None:
            return rep_score
        best: Optional[float] = None
        for item in items:
            v = self.cached(symbol, str(item.get("id", "")))
            if v and v.score is not None:
                best = v.score if best is None else max(best, v.score)
        return best

    def health(self) -> tuple[bool, str]:
        if not self.configured():
            return False, "no model configured (rules-only verdicts)"
        return True, f"model {self.cfg.str('AI_MODEL', '?')}"
