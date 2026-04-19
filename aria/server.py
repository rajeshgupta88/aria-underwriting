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

VERSION = "0.1.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    port = os.getenv("ARIA_PORT", "8001")
    hitl_mode = os.getenv("HITL_MODE", "terminal")

    db = SubmissionDB("data/submissions.db")
    audit = AuditLogger(
        score_log_path=os.getenv("SCORE_LOG_PATH", "data/score_log.jsonl"),
        decisions_path=os.getenv("DECISIONS_PATH", "data/decisions.jsonl"),
    )
    sub_router = SubmissionRouter(db=db, audit=audit, hitl_mode=hitl_mode)

    # Store on app.state so the review router can access without circular imports
    app.state.db = db
    app.state.audit = audit
    app.state.router = sub_router
    app.state.hitl_mode = hitl_mode

    llm_info = llm_status()
    logger.info("Aria — Appetite & Risk Intelligence Agent running on port %s", port)
    logger.info(
        "LLM provider: %s  model: %s  key_set: %s",
        llm_info["provider"], llm_info["model"], llm_info["api_key_set"],
    )
    logger.info("HITL mode: %s", hitl_mode)

    yield

    logger.info("Aria shutting down")


app = FastAPI(title="Aria", version=VERSION, lifespan=lifespan)

# Mount the browser HITL review router
from hitl.review import router as review_router  # noqa: E402 — after app creation
app.include_router(review_router)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post("/score")
async def score_submission(event: SubmissionEvent):
    result = await app.state.router.route(event)
    return result


@app.get("/submissions")
async def list_submissions(
    status: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
):
    rows = app.state.db.list_submissions(status=status, limit=limit)
    return {"count": len(rows), "submissions": rows}


@app.get("/submissions/{submission_id}")
async def get_submission(submission_id: str):
    row = app.state.db.get_submission(submission_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Submission {submission_id!r} not found")
    scores = app.state.audit.read_scores(submission_id=submission_id)
    latest_score = scores[-1] if scores else None
    return {"submission": row, "latest_score": latest_score}


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": VERSION,
        "hitl_mode": app.state.hitl_mode,
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
    result = await app.state.router.route(event)
    return {"sample_index": n, "named_insured": event.named_insured, **result}
