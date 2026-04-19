from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import httpx

from aria.audit import AuditLogger
from aria.db import SubmissionDB
from aria.models import CompositeScore, HITLRequired, SubmissionEvent
from aria.scorer import compute_composite

logger = logging.getLogger("aria.router")

_W3_ENDPOINT = os.getenv("W3_ENDPOINT", "http://localhost:8002/enrich")
_W3_PENDING_DIR = Path("data/w3_pending")
_DECLINE_DIR = Path("data")


class SubmissionRouter:
    def __init__(self, db: SubmissionDB, audit: AuditLogger, hitl_mode: str = "terminal"):
        self._db = db
        self._audit = audit
        self._hitl_mode = hitl_mode

    async def route(self, event: SubmissionEvent) -> dict:
        # Insert submission if not already present
        self._db.insert_submission(event)

        try:
            score = compute_composite(event)
        except HITLRequired as exc:
            self._db.update_status(event.submission_id, "referral_pending")
            logger.warning(
                "HITLRequired for %s — field=%s reason=%s",
                event.submission_id, exc.field, exc.reason,
            )
            return {
                "outcome": "hitl_required",
                "score": None,
                "routing_reason": exc.reason,
                "field": exc.field,
            }

        self._audit.log_score(score)
        self._db.update_status(event.submission_id, "aria_scored")

        if score.routing == "auto_pass":
            await self._advance_to_w3(event, score)
        elif score.routing == "referral":
            await self._fire_hitl(event, score)
        else:
            await self._send_decline(event, score)

        return {
            "outcome": score.routing,
            "score": json.loads(score.model_dump_json()),
            "routing_reason": score.routing_reason,
        }

    # ── Downstream handlers ───────────────────────────────────────────────────

    async def _advance_to_w3(self, event: SubmissionEvent, score: CompositeScore) -> None:
        self._db.update_status(event.submission_id, "w3_triggered")
        payload = {
            "submission_id": event.submission_id,
            "pc_account_id": event.pc_account_id,
            "score": score.total,
            "sic_code": event.sic_code,
            "writing_state": event.writing_state,
        }
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(_W3_ENDPOINT, json=payload, timeout=5.0)
                resp.raise_for_status()
            logger.info("W3 enrich triggered for %s", event.submission_id)
        except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError) as exc:
            logger.warning(
                "W3 endpoint unavailable for %s (%s) — queuing for retry",
                event.submission_id, exc,
            )
            self._db.update_status(event.submission_id, "w3_pending_retry")
            _W3_PENDING_DIR.mkdir(parents=True, exist_ok=True)
            pending_file = _W3_PENDING_DIR / f"{event.submission_id}.json"
            pending_file.write_text(json.dumps(payload, indent=2))

    async def _fire_hitl(self, event: SubmissionEvent, score: CompositeScore) -> None:
        self._db.update_status(event.submission_id, "referral_pending")
        if self._hitl_mode == "terminal":
            from hitl.card import render_hitl_card
            render_hitl_card(event, score)
        else:
            # browser mode: card data is served via GET /submissions/{id}
            logger.info(
                "HITL browser card queued for %s (score=%d)",
                event.submission_id, score.total,
            )

    async def _send_decline(self, event: SubmissionEvent, score: CompositeScore) -> None:
        self._db.update_status(event.submission_id, "declined")
        decline_record = {
            "submission_id": event.submission_id,
            "named_insured": event.named_insured,
            "reason_code": "SCORE_BELOW_THRESHOLD",
            "score_total": score.total,
            "score_breakdown": {
                "sic_tier": score.sic.tier,
                "sic_base": score.sic.base_score,
                "state_modifier": score.state.modifier,
                "tiv_modifier": score.tiv.modifier,
            },
            "routing_reason": score.routing_reason,
            "scored_at": score.scored_at.isoformat(),
        }
        decline_path = _DECLINE_DIR / f"decline_{event.submission_id}.json"
        decline_path.parent.mkdir(parents=True, exist_ok=True)
        decline_path.write_text(json.dumps(decline_record, indent=2))
        logger.info(
            "Decline written for %s (score=%d)",
            event.submission_id, score.total,
        )
