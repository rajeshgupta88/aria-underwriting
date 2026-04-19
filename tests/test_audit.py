from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aria.audit import AuditLogger, _sha256
from aria.models import CompositeScore, SICScore, StateScore, TIVScore, UWDecision


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_score(submission_id: str = "SUB-TEST-001") -> CompositeScore:
    return CompositeScore(
        submission_id=submission_id,
        sic=SICScore(sic_code="5812", tier="A", base_score=90, confidence=0.97, flagged=False),
        state=StateScore(state="IL", zip="60601", is_coastal=False, modifier=0),
        tiv=TIVScore(tiv=800_000.0, band_label="Mid ($500K–$2M)", modifier=0, missing=False),
        total=90,
        routing="auto_pass",
        routing_reason="No significant negative components — score driven by positive signals",
        scored_at=datetime.now(timezone.utc),
    )


def _make_decision(submission_id: str = "SUB-TEST-001") -> UWDecision:
    return UWDecision(
        submission_id=submission_id,
        reviewer_id="uw-001",
        choice="approve",
        reason_code=None,
        override_score=None,
        notes="Looks clean.",
        decided_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def logger(tmp_path):
    return AuditLogger(
        score_log_path=str(tmp_path / "score_log.jsonl"),
        decisions_path=str(tmp_path / "decisions.jsonl"),
    )


# ── Append tests ──────────────────────────────────────────────────────────────

def test_log_score_appends_one_line(logger, tmp_path):
    logger.log_score(_make_score())
    lines = (tmp_path / "score_log.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1


def test_log_score_multiple_appends(logger, tmp_path):
    logger.log_score(_make_score("SUB-001"))
    logger.log_score(_make_score("SUB-002"))
    logger.log_score(_make_score("SUB-003"))
    lines = (tmp_path / "score_log.jsonl").read_text().strip().splitlines()
    assert len(lines) == 3


def test_log_decision_appends_one_line(logger, tmp_path):
    score = _make_score()
    logger.log_decision(_make_decision(), score)
    lines = (tmp_path / "decisions.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1


def test_log_decision_includes_composite_snapshot(logger, tmp_path):
    score = _make_score()
    logger.log_decision(_make_decision(), score)
    record = json.loads((tmp_path / "decisions.jsonl").read_text().strip())
    assert "composite_snapshot" in record
    assert record["composite_snapshot"]["total"] == 90


# ── Hash integrity ────────────────────────────────────────────────────────────

def test_log_score_record_has_hash(logger, tmp_path):
    logger.log_score(_make_score())
    record = json.loads((tmp_path / "score_log.jsonl").read_text().strip())
    assert "log_hash" in record
    assert len(record["log_hash"]) == 64  # sha256 hex digest


def test_verify_integrity_clean_file(logger):
    logger.log_score(_make_score("SUB-A"))
    logger.log_score(_make_score("SUB-B"))
    result = logger.verify_integrity()
    assert result["score_log"]["total"] == 2
    assert result["score_log"]["tampered"] == []


def test_verify_integrity_empty_files(logger):
    result = logger.verify_integrity()
    assert result["score_log"]["total"] == 0
    assert result["decisions"]["total"] == 0


# ── Tamper detection ──────────────────────────────────────────────────────────

def test_verify_integrity_detects_tampered_score(logger, tmp_path):
    logger.log_score(_make_score("SUB-TAMPER"))

    # Corrupt the total field in the written line
    log_path = tmp_path / "score_log.jsonl"
    record = json.loads(log_path.read_text().strip())
    record["total"] = 42          # tamper: change score without updating hash
    log_path.write_text(json.dumps(record) + "\n")

    result = logger.verify_integrity()
    assert len(result["score_log"]["tampered"]) == 1
    assert result["score_log"]["tampered"][0]["submission_id"] == "SUB-TAMPER"


def test_verify_integrity_detects_tampered_decision(logger, tmp_path):
    score = _make_score("SUB-DEC")
    dec = _make_decision("SUB-DEC")
    logger.log_decision(dec, score)

    dec_path = tmp_path / "decisions.jsonl"
    record = json.loads(dec_path.read_text().strip())
    record["choice"] = "decline"  # tamper: flip the decision
    dec_path.write_text(json.dumps(record) + "\n")

    result = logger.verify_integrity()
    assert len(result["decisions"]["tampered"]) == 1


# ── Read / filter ─────────────────────────────────────────────────────────────

def test_read_scores_all(logger):
    logger.log_score(_make_score("SUB-X"))
    logger.log_score(_make_score("SUB-Y"))
    records = logger.read_scores()
    assert len(records) == 2


def test_read_scores_filtered(logger):
    logger.log_score(_make_score("SUB-X"))
    logger.log_score(_make_score("SUB-Y"))
    records = logger.read_scores(submission_id="SUB-X")
    assert len(records) == 1
    assert records[0]["submission_id"] == "SUB-X"


def test_read_decisions_filtered(logger):
    score_a = _make_score("SUB-A")
    score_b = _make_score("SUB-B")
    logger.log_decision(_make_decision("SUB-A"), score_a)
    logger.log_decision(_make_decision("SUB-B"), score_b)
    records = logger.read_decisions(submission_id="SUB-B")
    assert len(records) == 1
    assert records[0]["submission_id"] == "SUB-B"
