from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, computed_field


class SubmissionEvent(BaseModel):
    submission_id: str
    acord_fields: dict
    pc_account_id: str
    sic_code: str
    sic_description: str
    sic_confidence: float
    writing_state: str
    premises_zip: str
    tiv: float | None
    named_insured: str
    created_at: datetime


class SICScore(BaseModel):
    sic_code: str
    tier: str
    base_score: int
    confidence: float
    flagged: bool


class StateScore(BaseModel):
    state: str
    zip: str
    is_coastal: bool
    modifier: int


class TIVScore(BaseModel):
    tiv: float | None
    band_label: str
    modifier: int
    missing: bool


class CompositeScore(BaseModel):
    submission_id: str
    sic: SICScore
    state: StateScore
    tiv: TIVScore
    total: int
    routing: Literal["auto_pass", "referral", "auto_decline"]
    routing_reason: str
    scored_at: datetime


class UWDecision(BaseModel):
    submission_id: str
    reviewer_id: str
    choice: Literal["approve", "override", "decline"]
    reason_code: str | None
    override_score: int | None
    notes: str | None
    decided_at: datetime


class HITLRequired(Exception):
    def __init__(self, reason: str, field: str):
        self.reason = reason
        self.field = field
        super().__init__(f"HITL required — {field}: {reason}")
