from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from aria.models import CompositeScore, UWDecision


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _serialize(obj) -> dict:
    """Convert a Pydantic model to a plain dict with ISO datetime strings."""
    return json.loads(obj.model_dump_json())


class AuditLogger:
    def __init__(
        self,
        score_log_path: str | None = None,
        decisions_path: str | None = None,
    ):
        self._score_log = Path(
            score_log_path or os.getenv("SCORE_LOG_PATH", "data/score_log.jsonl")
        )
        self._decisions = Path(
            decisions_path or os.getenv("DECISIONS_PATH", "data/decisions.jsonl")
        )
        self._score_log.parent.mkdir(parents=True, exist_ok=True)
        self._decisions.parent.mkdir(parents=True, exist_ok=True)

    # ── Write ─────────────────────────────────────────────────────────────────

    def log_score(self, score: CompositeScore) -> None:
        record = _serialize(score)
        record["log_hash"] = _sha256(json.dumps(record, sort_keys=True))
        with open(self._score_log, "a") as f:
            f.write(json.dumps(record) + "\n")

    def log_decision(self, decision: UWDecision, score: CompositeScore) -> None:
        record: dict = {}
        record.update(_serialize(decision))
        record["composite_snapshot"] = _serialize(score)
        record["log_hash"] = _sha256(json.dumps(record, sort_keys=True))
        with open(self._decisions, "a") as f:
            f.write(json.dumps(record) + "\n")

    # ── Read ──────────────────────────────────────────────────────────────────

    def _read_jsonl(self, path: Path, submission_id: str | None) -> list[dict]:
        if not path.exists():
            return []
        records = []
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                if submission_id is None or record.get("submission_id") == submission_id:
                    records.append(record)
        return records

    def read_scores(self, submission_id: str | None = None) -> list[dict]:
        return self._read_jsonl(self._score_log, submission_id)

    def read_decisions(self, submission_id: str | None = None) -> list[dict]:
        return self._read_jsonl(self._decisions, submission_id)

    # ── Integrity ─────────────────────────────────────────────────────────────

    def _verify_file(self, path: Path) -> dict:
        total = 0
        tampered = []
        if not path.exists():
            return {"total": 0, "tampered": []}
        with open(path) as f:
            for lineno, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                total += 1
                record = json.loads(line)
                stored_hash = record.pop("log_hash", None)
                recomputed = _sha256(json.dumps(record, sort_keys=True))
                if stored_hash != recomputed:
                    tampered.append({
                        "line": lineno,
                        "submission_id": record.get("submission_id"),
                        "stored": stored_hash,
                        "expected": recomputed,
                    })
        return {"total": total, "tampered": tampered}

    def verify_integrity(self) -> dict:
        return {
            "score_log": self._verify_file(self._score_log),
            "decisions": self._verify_file(self._decisions),
        }
