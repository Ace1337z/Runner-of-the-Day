"""Config-registry tests: the "no magic numbers" rule must be mechanically true.

Every setting must carry a unit-or-blank and a rationale, keys must be unique,
and the typed getters must round-trip values with the documented fallbacks.
"""

from __future__ import annotations

from rotd.config import BY_KEY, SETTINGS, Config, rationale, unit_of


def test_at_least_one_setting():
    assert len(SETTINGS) >= 50


def test_keys_unique():
    keys = [s.key for s in SETTINGS]
    assert len(keys) == len(set(keys)), "duplicate setting keys"


def test_every_setting_has_a_rationale():
    missing = [s.key for s in SETTINGS if not s.rationale or len(s.rationale.strip()) < 5]
    assert not missing, f"settings without a rationale: {missing}"


def test_every_setting_has_a_unit_or_is_unitless_by_design():
    # Unitless text/bool/model/url keys are allowed; numeric keys must declare a unit.
    unitless_ok = {"", "bool", "file", "url", "model", "path", "text"}
    bad = []
    for s in SETTINGS:
        if s.unit in unitless_ok:
            continue
        # a declared unit is present and short
        if len(s.unit) > 20:
            bad.append((s.key, s.unit))
    assert not bad, f"odd units: {bad}"


def test_registry_lookup_helpers():
    assert rationale("MAX_PRICE")
    assert unit_of("MAX_PRICE") == "USD"
    assert BY_KEY["BOARD_SIZE"].key == "BOARD_SIZE"


def test_typed_getters_and_defaults(cfg: Config):
    # a set value round-trips
    cfg._values["MAX_PRICE"] = "7.50"
    assert cfg.float("MAX_PRICE") == 7.5
    # an unset key falls back to the caller default (never a magic literal)
    assert cfg.int("ALERT_BUDGET_PER_DAY", 3) == 30  # Candle-1 release default
    assert cfg.str("AI_PROMPT_FILE", "methodology_v3.md") == "methodology_v3.md"
    # an override wins over the file
    c2 = Config(path=cfg.path, overrides={"MAX_PRICE": "4.0"})
    assert c2.float("MAX_PRICE") == 4.0


def test_registry_default_has_no_blank_numeric_unit():
    # guard the "no magic numbers" contract for the most decision-critical keys
    for key in ("MAX_PRICE", "ALERT_BUDGET_PER_DAY", "ROTD_MIN_SCORE",
                "CHASE_CAP_PCT", "RUNNER_LIST_PCT"):
        assert BY_KEY[key].unit, f"{key} must declare a unit"


def test_catalyst_weight_default_stays_light_on_the_80_point_scale(cfg: Config):
    """W_CATALYST must default to 5 so a future deploy cannot re-anchor the score.

    At 25 the noisy LLM catalyst number acted as a 25% anchor, pinning pure
    momentum runners below ARM_MIN_SCORE until the move had topped out. The five
    weights now sum to an 80-point raw scale that scoring.py rescales to 0..100,
    so this change needs no threshold edits — which is exactly why a silent
    revert would be easy to miss.
    """
    assert cfg.float("W_CATALYST") == 5.0
    weights = {k: cfg.float(f"W_{k}") for k in
               ("TAPE", "CATALYST", "FUEL", "STRUCTURE", "CONTEXT")}
    assert weights == {"TAPE": 30.0, "CATALYST": 5.0, "FUEL": 20.0,
                       "STRUCTURE": 15.0, "CONTEXT": 10.0}
    assert sum(weights.values()) == 80.0


def test_shipped_env_template_pins_the_catalyst_weight():
    """config.env.example is the deployment template; it must not reintroduce 25."""
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "config.env.example").read_text(
        encoding="utf-8")
    active = [ln.strip() for ln in text.splitlines()
              if ln.strip().startswith("W_CATALYST=")]
    assert active == ["W_CATALYST=5"], f"template pins {active!r}"
