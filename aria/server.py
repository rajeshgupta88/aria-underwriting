from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

load_dotenv()

from aria.audit import AuditLogger
from aria.db import SubmissionDB, _SAMPLE_SUBMISSIONS
from aria.llm import llm_status
from aria.models import SubmissionEvent
from aria.router import SubmissionRouter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
)
logger = logging.getLogger("aria.server")

# ── App state (populated in lifespan) ────────────────────────────────────────

_state: dict = {}

VERSION = "0.1.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    port = os.getenv("ARIA_PORT", "8001")
    hitl_mode = os.getenv("HITL_MODE", "terminal")

    db = SubmissionDB(os.getenv("SCORE_LOG_PATH", "data/submissions.db").replace("score_log.jsonl", "submissions.db"))
    audit = AuditLogger(
        score_log_path=os.getenv("SCORE_LOG_PATH", "data/score_log.jsonl"),
        decisions_path=os.getenv("DECISIONS_PATH", "data/decisions.jsonl"),
    )
    router = SubmissionRouter(db=db, audit=audit, hitl_mode=hitl_mode)

    _state["db"] = db
    _state["audit"] = audit
    _state["router"] = router
    _state["hitl_mode"] = hitl_mode

    llm_info = llm_status()
    logger.info("Aria — Appetite & Risk Intelligence Agent running on port %s", port)
    logger.info("LLM provider: %s  model: %s  key_set: %s",
                llm_info["provider"], llm_info["model"], llm_info["api_key_set"])

    yield

    logger.info("Aria shutting down")


app = FastAPI(title="Aria", version=VERSION, lifespan=lifespan)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post("/score")
async def score_submission(event: SubmissionEvent):
    result = await _state["router"].route(event)
    return result


@app.get("/submissions")
async def list_submissions(
    status: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
):
    rows = _state["db"].list_submissions(status=status, limit=limit)
    return {"count": len(rows), "submissions": rows}


@app.get("/submissions/{submission_id}")
async def get_submission(submission_id: str):
    row = _state["db"].get_submission(submission_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Submission {submission_id!r} not found")
    scores = _state["audit"].read_scores(submission_id=submission_id)
    latest_score = scores[-1] if scores else None
    return {"submission": row, "latest_score": latest_score}


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": VERSION,
        "hitl_mode": _state.get("hitl_mode", "terminal"),
        "llm": llm_status(),
    }


@app.post("/test/submit/{n}")
async def test_submit(n: int):
    if n < 0 or n >= len(_SAMPLE_SUBMISSIONS):
        raise HTTPException(
            status_code=400,
            detail=f"n must be 0–{len(_SAMPLE_SUBMISSIONS) - 1}",
        )
    raw = _SAMPLE_SUBMISSIONS[n]
    event = SubmissionEvent(
        **{**raw, "created_at": datetime.now(timezone.utc)},
    )
    result = await _state["router"].route(event)
    return {"sample_index": n, "named_insured": event.named_insured, **result}
