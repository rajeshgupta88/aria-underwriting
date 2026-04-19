from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from aria.models import CompositeScore, SICScore, StateScore, SubmissionEvent, TIVScore, UWDecision

router = APIRouter(prefix="/review", tags=["hitl"])
templates = Jinja2Templates(directory="templates")

_SLA_HOURS = int(os.getenv("SLA_HOURS", "4"))


def _load_event_and_score(submission_id: str, request: Request):
    """Reconstruct SubmissionEvent and latest CompositeScore from DB + audit log."""
    db = request.app.state.db
    audit = request.app.state.audit

    row = db.get_submission(submission_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Submission {submission_id!r} not found")

    if row["status"] not in ("referral_pending", "aria_scored"):
        raise HTTPException(
            status_code=409,
            detail=f"Submission {submission_id!r} is not pending review (status: {row['status']})",
        )

    payload = json.loads(row["raw_payload"])
    payload["created_at"] = datetime.fromisoformat(payload["created_at"])
    event = SubmissionEvent(**payload)

    score_records = audit.read_scores(submission_id=submission_id)
    if not score_records:
        raise HTTPException(status_code=404, detail="No score record found — submission may not have been scored yet")

    raw = score_records[-1]
    raw.pop("log_hash", None)

    # Reconstruct nested models
    score = CompositeScore(
        submission_id=raw["submission_id"],
        sic=SICScore(**raw["sic"]),
        state=StateScore(**raw["state"]),
        tiv=TIVScore(**raw["tiv"]),
        total=raw["total"],
        routing=raw["routing"],
        routing_reason=raw["routing_reason"],
        scored_at=datetime.fromisoformat(raw["scored_at"]),
    )

    return event, score


@router.get("/{submission_id}", response_class=HTMLResponse)
async def review_page(submission_id: str, request: Request):
    event, score = _load_event_and_score(submission_id, request)
    tiv_display = f"${event.tiv:,.0f}" if event.tiv is not None else "Missing"
    return templates.TemplateResponse(
        "review.html",
        {
            "request": request,
            "event": event,
            "score": score,
            "tiv_display": tiv_display,
            "sla_hours": _SLA_HOURS,
            "scored_at": score.scored_at.strftime("%Y-%m-%d %H:%M UTC"),
        },
    )


@router.post("/{submission_id}/decide")
async def decide(
    submission_id: str,
    request: Request,
    choice: str = Form(...),
    reviewer_id: str = Form(...),
    reason_code: str = Form(default=""),
    notes: str = Form(default=""),
    override_score: str = Form(default=""),
):
    db = request.app.state.db
    audit = request.app.state.audit
    sub_router = request.app.state.router

    event, score = _load_event_and_score(submission_id, request)

    if choice not in ("approve", "override", "decline"):
        raise HTTPException(status_code=422, detail="Invalid choice")

    override_score_int: int | None = None
    if choice == "override" and override_score.strip():
        try:
            override_score_int = int(override_score.strip())
        except ValueError:
            override_score_int = score.total

    decision = UWDecision(
        submission_id=submission_id,
        reviewer_id=reviewer_id.strip() or "uw_browser",
        choice=choice,
        reason_code=reason_code.strip() or None,
        override_score=override_score_int,
        notes=notes.strip() or None,
        decided_at=datetime.now(timezone.utc),
    )

    audit.log_decision(decision, score)

    if choice in ("approve", "override"):
        await sub_router.advance_to_w3(event, score)
    else:
        db.update_status(submission_id, "declined")

    return RedirectResponse(
        url=f"/review/{submission_id}/result", status_code=303
    )


@router.get("/{submission_id}/result", response_class=HTMLResponse)
async def result_page(submission_id: str, request: Request):
    db = request.app.state.db
    audit = request.app.state.audit

    row = db.get_submission(submission_id)
    if not row:
        raise HTTPException(status_code=404, detail="Submission not found")

    decisions = audit.read_decisions(submission_id=submission_id)
    latest = decisions[-1] if decisions else None

    choice = latest.get("choice", "—") if latest else "—"
    reviewer = latest.get("reviewer_id", "—") if latest else "—"
    status = row["status"]

    status_color = {
        "w3_triggered": "#1a7f37",
        "w3_pending_retry": "#9a6700",
        "declined": "#cf222e",
    }.get(status, "#57606a")

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Aria — Decision Result</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 680px; margin: 48px auto; padding: 0 24px; color: #24292f; }}
    h1 {{ font-size: 1.25rem; color: #0969da; margin-bottom: 4px; }}
    .badge {{ display: inline-block; padding: 4px 12px; border-radius: 12px; font-weight: 600;
              background: {status_color}22; color: {status_color}; border: 1px solid {status_color}44; }}
    .card {{ background: #f6f8fa; border: 1px solid #d0d7de; border-radius: 8px; padding: 20px 24px; margin-top: 24px; }}
    dl {{ display: grid; grid-template-columns: 160px 1fr; gap: 8px 12px; margin: 0; }}
    dt {{ font-weight: 600; color: #57606a; }}
    a {{ color: #0969da; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
  </style>
</head>
<body>
  <h1>Aria — Decision Recorded</h1>
  <p>Submission <strong>{submission_id}</strong></p>
  <span class="badge">{status.upper().replace('_', ' ')}</span>
  <div class="card">
    <dl>
      <dt>Choice</dt><dd>{choice.upper()}</dd>
      <dt>Reviewer</dt><dd>{reviewer}</dd>
      <dt>Named Insured</dt><dd>{row['named_insured']}</dd>
      <dt>Final Status</dt><dd>{status}</dd>
    </dl>
  </div>
  <p style="margin-top: 24px;">
    <a href="/submissions/{submission_id}">View full submission record</a> &nbsp;·&nbsp;
    <a href="/submissions">All submissions</a>
  </p>
</body>
</html>"""
    return HTMLResponse(content=html)
