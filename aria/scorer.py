from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from aria.models import (
    CompositeScore,
    HITLRequired,
    SICScore,
    StateScore,
    SubmissionEvent,
    TIVScore,
)

# ── Config loading (once at import time) ──────────────────────────────────────

_CONFIG_DIR = Path(__file__).parent.parent / "config"


def _load(name: str) -> dict:
    with open(_CONFIG_DIR / name) as f:
        return yaml.safe_load(f)


_APPETITE = _load("appetite_config.yaml")
_STATE = _load("state_mods.yaml")
_TIV = _load("tiv_bands.yaml")

_TIER_BASES: dict[str, int] = {
    tier: defn["base_score"]
    for tier, defn in _APPETITE["tier_definitions"].items()
}

# ── Coastal ZIP prefix table ──────────────────────────────────────────────────
# Three-digit prefixes that indicate coastal exposure by state.

_COASTAL_PREFIXES: set[str] = {
    # Florida — Atlantic and Gulf coasts
    "331", "332", "333", "334",  # Miami, Fort Lauderdale, Palm Beach
    "322", "323",                 # Jacksonville, Daytona Beach coast
    "339", "341",                 # Naples, Fort Myers Gulf
    "342",                        # Sarasota
    # California — Pacific coast
    "900", "902", "904", "939",  # LA, Santa Monica, Ventura, Santa Cruz
    "941", "949",                 # San Francisco, Newport Beach
    # North Carolina — Outer Banks and coast
    "279", "280", "285",
    # South Carolina — Grand Strand and Lowcountry
    "295", "299",
    # Texas — Gulf Coast
    "775", "776", "777",         # Houston metro / Galveston area
    # New York — Long Island / coastal
    "117", "119",
}


def is_coastal_zip(zip5: str) -> bool:
    return zip5[:3] in _COASTAL_PREFIXES


# ── SIC scorer ────────────────────────────────────────────────────────────────

def score_sic(
    sic_code: str,
    sic_confidence: float,
    ambiguous_sics: list[str] | None = None,
    tier_table: dict[str, str] | None = None,
) -> SICScore:
    if ambiguous_sics is None:
        ambiguous_sics = [str(s) for s in _APPETITE["ambiguous_sics"]]
    if tier_table is None:
        tier_table = {str(k): str(v) for k, v in _APPETITE["sic_tiers"].items()}

    # Confidence gate — below 0.80 always requires human review
    if sic_confidence < 0.80:
        raise HITLRequired(
            reason=f"SIC confidence {sic_confidence:.2f} is below minimum threshold 0.80",
            field="sic_confidence",
        )

    # Ambiguous SIC gate — requires higher confidence
    if sic_code in ambiguous_sics and sic_confidence < 0.90:
        raise HITLRequired(
            reason=(
                f"SIC {sic_code} is ambiguous and confidence {sic_confidence:.2f} "
                "is below required 0.90"
            ),
            field="sic_confidence",
        )

    tier = tier_table.get(sic_code, "C")
    flagged = sic_code not in tier_table  # unknown SIC defaults to C but is flagged

    return SICScore(
        sic_code=sic_code,
        tier=tier,
        base_score=_TIER_BASES[tier],
        confidence=sic_confidence,
        flagged=flagged,
    )


# ── State scorer ──────────────────────────────────────────────────────────────

def score_state(
    writing_state: str,
    premises_zip: str,
    config: dict | None = None,
) -> StateScore:
    if config is None:
        config = _STATE

    coastal = is_coastal_zip(premises_zip)
    state_cfg = config["modifiers"].get(writing_state, config["modifiers"]["DEFAULT"])

    if coastal and "coastal_modifier" in state_cfg:
        modifier = state_cfg["coastal_modifier"]
    else:
        modifier = state_cfg["standard"]

    return StateScore(
        state=writing_state,
        zip=premises_zip,
        is_coastal=coastal,
        modifier=modifier,
    )


# ── TIV scorer ────────────────────────────────────────────────────────────────

def score_tiv(tiv: float | None, config: dict | None = None) -> TIVScore:
    if config is None:
        config = _TIV

    if tiv is None:
        missing_cfg = config["missing_tiv_behavior"]
        return TIVScore(
            tiv=None,
            band_label="Unknown",
            modifier=missing_cfg["modifier"],
            missing=True,
        )

    for band in config["bands"]:
        lo = band["min"]  # None means no lower bound
        hi = band["max"]  # None means no upper bound
        below_hi = (hi is None) or (tiv < hi)
        above_lo = (lo is None) or (tiv >= lo)
        if above_lo and below_hi:
            return TIVScore(
                tiv=tiv,
                band_label=band["label"],
                modifier=band["modifier"],
                missing=False,
            )

    # Fallback — should not be reached with a well-formed config
    return TIVScore(tiv=tiv, band_label="Unknown", modifier=0, missing=False)


# ── Composite scorer ──────────────────────────────────────────────────────────

def _routing_reason(sic: SICScore, state: StateScore, tiv: TIVScore) -> str:
    components = {
        "sic_base": sic.base_score - 70,   # deviation from neutral tier B
        "state_modifier": state.modifier,
        "tiv_modifier": tiv.modifier,
    }
    # Find the largest negative drag
    worst_key = min(components, key=lambda k: components[k])
    worst_val = components[worst_key]

    labels = {
        "sic_base": f"SIC tier {sic.tier} (base {sic.base_score})",
        "state_modifier": f"state modifier for {state.state} ({'coastal' if state.is_coastal else 'standard'}: {state.modifier:+d})",
        "tiv_modifier": f"TIV band '{tiv.band_label}' (modifier {tiv.modifier:+d})",
    }

    if worst_val < 0:
        return f"Largest negative component: {labels[worst_key]}"
    return "No significant negative components — score driven by positive signals"


def compute_composite(sub: SubmissionEvent) -> CompositeScore:
    sic_score = score_sic(sub.sic_code, sub.sic_confidence)
    state_score = score_state(sub.writing_state, sub.premises_zip)
    tiv_score = score_tiv(sub.tiv)

    raw = sic_score.base_score + state_score.modifier + tiv_score.modifier
    total = max(0, min(100, raw))

    if total >= 65:
        routing = "auto_pass"
    elif total >= 35:
        routing = "referral"
    else:
        routing = "auto_decline"

    return CompositeScore(
        submission_id=sub.submission_id,
        sic=sic_score,
        state=state_score,
        tiv=tiv_score,
        total=total,
        routing=routing,
        routing_reason=_routing_reason(sic_score, state_score, tiv_score),
        scored_at=datetime.now(timezone.utc),
    )
