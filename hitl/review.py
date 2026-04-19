from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from aria.models import CompositeScore, SICScore, StateScore, SubmissionEvent, TIVScore, UWDecision

router = APIRouter(prefix="/review", tags=["hitl"])
templates = Jinja2Templates(directory="templates")

_SLA_HOURS = int(os.getenv("SLA_HOURS", "4"))


def _load_event_and_score(submission_id: str, request: Request):
    db = request.app.state.db
    audit = request.app.state.audit

    row = db.get_submission(submission_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Submission {submission_id!r} not found")

    payload = json.loads(row["raw_payload"])
    payload["created_at"] = datetime.fromisoformat(payload["created_at"])
    event = SubmissionEvent(**payload)

    score_records = audit.read_scores(submission_id=submission_id)
    if not score_records:
        raise HTTPException(status_code=404,
                            detail="No score record found — submission may not have been scored yet")

    raw = {k: v for k, v in score_records[-1].items() if k != "log_hash"}
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
    return row, event, score


def _sla_info(scored_at: datetime) -> tuple[str, str]:
    deadline = scored_at + timedelta(hours=_SLA_HOURS)
    remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
    if remaining <= 0:
        return "OVERDUE", "sla-red"
    h, m = int(remaining // 3600), int((remaining % 3600) // 60)
    label = f"{h}h {m}m remaining"
    if remaining < 3600:
        return label, "sla-red"
    if remaining < 7200:
        return label, "sla-amber"
    return label, "sla-green"


def _score_color(total: int | None) -> str:
    if total is None:
        return "#6b6a65"
    if total >= 65:
        return "#1D9E75"
    if total >= 35:
        return "#EF9F27"
    return "#E24B4A"


def _sign_color(modifier: int) -> str:
    if modifier < 0:
        return "#E24B4A"
    if modifier > 0:
        return "#1D9E75"
    return "#6b6a65"


@router.get("/{submission_id}", response_class=HTMLResponse)
async def review_page(submission_id: str, request: Request):
    from aria.llm import llm_status

    db = request.app.state.db
    audit = request.app.state.audit

    row, event, score = _load_event_and_score(submission_id, request)
    status = row["status"]

    tiv_display = f"${event.tiv:,.0f}" if event.tiv is not None else "Missing"
    sla_label, sla_css = _sla_info(score.scored_at)
    scored_at_fmt = score.scored_at.strftime("%Y-%m-%d %H:%M UTC")

    # Latest decision (if any)
    decisions = audit.read_decisions(submission_id=submission_id)
    latest_decision = decisions[-1] if decisions else None

    # Nav counts for base template
    nav_count = len(db.list_submissions(status="referral_pending", limit=200))

    return templates.TemplateResponse("review.html", {
        "request": request,
        "page_title": "UW review",
        "current_route": "/review",
        "nav_counts": {"referral_pending": nav_count},
        "llm": llm_status(),
        "event": event,
        "score": score,
        "status": status,
        "tiv_display": tiv_display,
        "sla_label": sla_label,
        "sla_css": sla_css,
        "scored_at_fmt": scored_at_fmt,
        "decision": latest_decision,
        "sic_color": _score_color(score.sic.base_score),
        "state_color": _sign_color(score.state.modifier),
        "tiv_color": _sign_color(score.tiv.modifier),
        "composite_color": _score_color(score.total),
    })


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

    row, event, score = _load_event_and_score(submission_id, request)

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

    return RedirectResponse(url=f"/review/{submission_id}/result", status_code=303)


@router.get("/{submission_id}/result", response_class=HTMLResponse)
async def result_page(submission_id: str, request: Request):
    # Just redirect to the review page — it now shows the outcome panel
    return RedirectResponse(url=f"/review/{submission_id}", status_code=303)
