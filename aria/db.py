from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from aria.models import SubmissionEvent

_VALID_STATUSES = {
    "aria_pending",
    "aria_scored",
    "referral_pending",
    "w3_triggered",
    "w3_pending_retry",
    "declined",
}

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS submissions (
    id              TEXT PRIMARY KEY,
    named_insured   TEXT NOT NULL,
    sic_code        TEXT NOT NULL,
    writing_state   TEXT NOT NULL,
    tiv             REAL,
    pc_account_id   TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'aria_pending',
    raw_payload     TEXT NOT NULL,
    created_at      TEXT NOT NULL
)
"""

_SAMPLE_SUBMISSIONS = [
    {
        "submission_id": "SUB-2024-0001",
        "acord_fields": {"policy_type": "BOP", "years_in_business": 8},
        "pc_account_id": "PC-10001",
        "sic_code": "5812",
        "sic_description": "Eating places",
        "sic_confidence": 0.97,
        "writing_state": "CA",
        "premises_zip": "90210",      # coastal → modifier -20
        "tiv": 800_000.0,             # mid band → 0; total 90-20+0=70 → auto_pass
        "named_insured": "Rossi's Italian Kitchen LLC",
        "created_at": "2024-03-01T09:00:00+00:00",
    },
    {
        "submission_id": "SUB-2024-0002",
        "acord_fields": {"policy_type": "BOP", "years_in_business": 15},
        "pc_account_id": "PC-10002",
        "sic_code": "5411",
        "sic_description": "Grocery stores",
        "sic_confidence": 0.99,
        "writing_state": "TX",
        "premises_zip": "78701",      # inland TX +5; micro +15; total 90+5+15=110→100
        "tiv": 300_000.0,
        "named_insured": "Patel Food Markets Inc",
        "created_at": "2024-03-02T10:30:00+00:00",
    },
    {
        "submission_id": "SUB-2024-0003",
        "acord_fields": {"policy_type": "GL", "years_in_business": 3},
        "pc_account_id": "PC-10003",
        "sic_code": "5813",
        "sic_description": "Drinking places (bars, taverns, nightclubs)",
        "sic_confidence": 0.94,
        "writing_state": "FL",
        "premises_zip": "33149",      # FL coastal -30; large -15; total 40-30-15=-5→0
        "tiv": 1_500_000.0,
        "named_insured": "Harbor View Lounge LLC",
        "created_at": "2024-03-03T11:15:00+00:00",
    },
    {
        "submission_id": "SUB-2024-0004",
        "acord_fields": {"policy_type": "BOP", "years_in_business": 5},
        "pc_account_id": "PC-10004",
        "sic_code": "5571",
        "sic_description": "Motorcycle dealers",
        "sic_confidence": 0.93,       # Tier C (40) + GA (+5) + small (+10) = 55 → referral
        "writing_state": "GA",
        "premises_zip": "30301",
        "tiv": 400_000.0,
        "named_insured": "Apex Cycle & Powersports LLC",
        "created_at": "2024-03-04T08:45:00+00:00",
    },
    {
        "submission_id": "SUB-2024-0005",
        "acord_fields": {"policy_type": "CPP", "years_in_business": 22},
        "pc_account_id": "PC-10005",
        "sic_code": "1521",
        "sic_description": "General building contractors - residential",
        "sic_confidence": 0.91,
        "writing_state": "NY",
        "premises_zip": "10001",      # NY -10; large -15; total 40-10-15=15 → auto_decline
        "tiv": 4_000_000.0,
        "named_insured": "Greenberg Builders Inc",
        "created_at": "2024-03-05T14:00:00+00:00",
    },
]


class SubmissionDB:
    def __init__(self, db_path: str = "data/submissions.db"):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(_CREATE_TABLE)
        self._conn.commit()

    # ── Write ─────────────────────────────────────────────────────────────────

    def insert_submission(self, event: SubmissionEvent) -> None:
        self._conn.execute(
            """
            INSERT OR IGNORE INTO submissions
                (id, named_insured, sic_code, writing_state, tiv,
                 pc_account_id, status, raw_payload, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'aria_pending', ?, ?)
            """,
            (
                event.submission_id,
                event.named_insured,
                event.sic_code,
                event.writing_state,
                event.tiv,
                event.pc_account_id,
                event.model_dump_json(),
                event.created_at.isoformat(),
            ),
        )
        self._conn.commit()

    def update_status(self, submission_id: str, status: str) -> None:
        if status not in _VALID_STATUSES:
            raise ValueError(
                f"Invalid status '{status}'. Must be one of: {sorted(_VALID_STATUSES)}"
            )
        self._conn.execute(
            "UPDATE submissions SET status = ? WHERE id = ?",
            (status, submission_id),
        )
        self._conn.commit()

    # ── Read ──────────────────────────────────────────────────────────────────

    def get_submission(self, submission_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM submissions WHERE id = ?", (submission_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_submissions(
        self, status: str | None = None, limit: int = 20
    ) -> list[dict]:
        if status:
            rows = self._conn.execute(
                "SELECT * FROM submissions WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM submissions ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Seed ──────────────────────────────────────────────────────────────────

    def seed_sample_data(self) -> None:
        for raw in _SAMPLE_SUBMISSIONS:
            event = SubmissionEvent(**{
                **raw,
                "created_at": datetime.fromisoformat(raw["created_at"]),
            })
            self.insert_submission(event)
