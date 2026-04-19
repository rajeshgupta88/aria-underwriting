from __future__ import annotations

from datetime import datetime, timezone

import pytest

from aria.models import HITLRequired, SubmissionEvent
from aria.scorer import compute_composite, is_coastal_zip, score_sic, score_state, score_tiv


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_submission(**overrides) -> SubmissionEvent:
    defaults = dict(
        submission_id="TEST-001",
        acord_fields={},
        pc_account_id="ACC-001",
        sic_code="5812",
        sic_description="Eating places",
        sic_confidence=0.95,
        writing_state="IL",
        premises_zip="60601",
        tiv=800_000.0,
        named_insured="Test Bistro LLC",
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return SubmissionEvent(**defaults)


# ── SIC scoring ───────────────────────────────────────────────────────────────

def test_tier_a_sic_base_score():
    result = score_sic("5812", sic_confidence=0.95)
    assert result.tier == "A"
    assert result.base_score == 90
    assert result.flagged is False


def test_tier_x_sic_base_score():
    result = score_sic("5921", sic_confidence=0.95)
    assert result.tier == "X"
    assert result.base_score == 0


def test_unknown_sic_defaults_to_tier_c_flagged():
    result = score_sic("0000", sic_confidence=0.95)
    assert result.tier == "C"
    assert result.base_score == 40
    assert result.flagged is True


def test_low_confidence_raises_hitl():
    with pytest.raises(HITLRequired) as exc_info:
        score_sic("5812", sic_confidence=0.75)
    assert exc_info.value.field == "sic_confidence"
    assert "0.75" in exc_info.value.reason


def test_ambiguous_sic_below_090_raises_hitl():
    # 7389 is in ambiguous_sics — needs confidence >= 0.90
    with pytest.raises(HITLRequired) as exc_info:
        score_sic("7389", sic_confidence=0.85)
    assert exc_info.value.field == "sic_confidence"
    assert "ambiguous" in exc_info.value.reason


def test_ambiguous_sic_above_090_passes():
    result = score_sic("7389", sic_confidence=0.92)
    assert result.tier == "A"
    assert result.base_score == 90


# ── State scoring ─────────────────────────────────────────────────────────────

def test_ca_coastal_zip_modifier():
    result = score_state("CA", "90210")  # 902xx prefix — coastal
    assert result.is_coastal is True
    assert result.modifier == -20


def test_ca_inland_zip_modifier():
    result = score_state("CA", "95814")  # Sacramento — not coastal
    assert result.is_coastal is False
    assert result.modifier == -15


def test_fl_coastal_modifier():
    result = score_state("FL", "33149")  # Key Biscayne — 331xx coastal
    assert result.is_coastal is True
    assert result.modifier == -30


def test_tx_standard_modifier():
    result = score_state("TX", "78701")  # Austin — not coastal
    assert result.is_coastal is False
    assert result.modifier == 5


def test_default_state_modifier():
    result = score_state("MN", "55101")
    assert result.modifier == 0


# ── TIV scoring ───────────────────────────────────────────────────────────────

def test_tiv_mid_band():
    result = score_tiv(800_000.0)
    assert "Mid" in result.band_label
    assert result.modifier == 0
    assert result.missing is False


def test_tiv_micro_band():
    result = score_tiv(100_000.0)
    assert "Micro" in result.band_label
    assert result.modifier == 15


def test_tiv_jumbo_band():
    result = score_tiv(6_000_000.0)
    assert "Jumbo" in result.band_label
    assert result.modifier == -25


def test_missing_tiv_neutral():
    result = score_tiv(None)
    assert result.missing is True
    assert result.modifier == 0
    assert result.tiv is None


# ── Composite scoring ─────────────────────────────────────────────────────────

def test_composite_tier_b_ca_coastal_mid_referral():
    # SIC B (70) + CA coastal (-20) + mid (0) = 50 → referral
    sub = make_submission(
        sic_code="7514",   # Tier B
        sic_confidence=0.95,
        writing_state="CA",
        premises_zip="90210",  # coastal
        tiv=800_000.0,         # mid band
    )
    result = compute_composite(sub)
    assert result.sic.base_score == 70
    assert result.state.modifier == -20
    assert result.tiv.modifier == 0
    assert result.total == 50
    assert result.routing == "referral"


def test_composite_tier_a_tx_micro_clamped_auto_pass():
    # SIC A (90) + TX (+5) + micro (+15) = 110 → clamped to 100 → auto_pass
    sub = make_submission(
        sic_code="5812",   # Tier A
        sic_confidence=0.95,
        writing_state="TX",
        premises_zip="78701",  # inland
        tiv=50_000.0,          # micro band
    )
    result = compute_composite(sub)
    assert result.sic.base_score == 90
    assert result.state.modifier == 5
    assert result.tiv.modifier == 15
    assert result.total == 100  # clamped from 110
    assert result.routing == "auto_pass"


def test_composite_tier_x_auto_decline():
    # SIC X (0) → total <= 34 in any realistic scenario → auto_decline
    sub = make_submission(
        sic_code="5921",   # Tier X
        sic_confidence=0.95,
        writing_state="IL",
        premises_zip="60601",
        tiv=800_000.0,
    )
    result = compute_composite(sub)
    assert result.sic.base_score == 0
    assert result.routing == "auto_decline"


def test_routing_reason_cites_largest_negative():
    # CA coastal (-20) is the biggest drag vs. SIC B base deviation and neutral TIV
    sub = make_submission(
        sic_code="7514",   # Tier B
        sic_confidence=0.95,
        writing_state="CA",
        premises_zip="90210",
        tiv=800_000.0,
    )
    result = compute_composite(sub)
    # state modifier (-20) is the worst component
    assert "CA" in result.routing_reason or "state" in result.routing_reason.lower()


def test_composite_missing_tiv_neutral_modifier():
    sub = make_submission(
        sic_code="5812",
        sic_confidence=0.95,
        writing_state="IL",
        premises_zip="60601",
        tiv=None,
    )
    result = compute_composite(sub)
    assert result.tiv.missing is True
    assert result.tiv.modifier == 0
    # SIC A (90) + IL (0) + missing TIV (0) = 90 → auto_pass
    assert result.total == 90
    assert result.routing == "auto_pass"
