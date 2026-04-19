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

# Reason codes grouped by category — shown as <optgroup> in the decision form
REASON_CODES = [
    ("Approval", [
        ("APPROVE_STANDARD",      "Standard risk — within appetite guidelines"),
        ("APPROVE_CONDITIONS",    "Approved with underwriting conditions attached"),
        ("APPROVE_EXCEPTION",     "Exception granted — requires home-office sign-off"),
        ("APPROVE_RELATIONSHIP",  "Strategic account — relationship exception"),
    ]),
    ("Override / Escalation", [
        ("OVERRIDE_INFO_RESOLVED", "Additional information resolves flagged concern"),
        ("OVERRIDE_PORTFOLIO",     "Portfolio fit — accept for class diversification"),
        ("OVERRIDE_PRICING",       "Acceptable risk at adjusted premium level"),
        ("OVERRIDE_MANUAL",        "Manual review completed — within appetite"),
    ]),
    ("Decline", [
        ("DECLINE_CAT_EXPOSURE",     "Catastrophe exposure exceeds carrier threshold"),
        ("DECLINE_LOSS_HISTORY",     "Adverse prior loss history"),
        ("DECLINE_OUTSIDE_APPETITE", "Class of business outside current appetite"),
        ("DECLINE_SIC_INELIGIBLE",   "SIC code ineligible for this program"),
        ("DECLINE_TIV_LIMIT",        "TIV exceeds maximum per-risk limit"),
        ("DECLINE_COMPLIANCE",       "Compliance or regulatory restriction"),
        ("DECLINE_MANUAL",           "Manual underwriter decline — see notes"),
    ]),
]


def _routing_badge(status: str, routing: str | None, decision: dict | None) -> dict:
    """Mirrors aria/server.py — same logic, kept here to avoid circular imports."""
    if decision:
        choice = decision.get("choice", "")
        notes = decision.get("notes") or ""
        if "Override of auto_pass" in notes:
            return {"cls": "badge-override-red",   "icon": "↓", "label": "Override · Declined"}
        if "Override of auto_decline" in notes:
            return {"cls": "badge-override-green", "icon": "↑", "label": "Override · Approved"}
        if routing is None:
            if choice == "decline":
                return {"cls": "badge-red",   "icon": "✗", "label": "Manual Decline"}
            return {"cls": "badge-green", "icon": "✓", "label": "Manual Approve"}
        if choice == "decline":
            return {"cls": "badge-red",   "icon": "✗", "label": "UW Declined"}
        return {"cls": "badge-green", "icon": "✓", "label": "UW Approved"}

    if routing == "auto_pass":
        return {"cls": "badge-green",  "icon": "", "label": "Auto-passed"}
    if routing == "auto_decline":
        return {"cls": "badge-red",    "icon": "", "label": "Auto-declined"}
    if routing == "referral":
        if status in ("w3_triggered", "w3_pending_retry"):
            return {"cls": "badge-green", "icon": "✓", "label": "UW Approved"}
        return {"cls": "badge-amber", "icon": "", "label": "Pending Review"}
    if status == "referral_pending":   # HITLRequired — no score computed
        return {"cls": "badge-amber", "icon": "", "label": "Pending Review"}
    return {"cls": "badge-blue", "icon": "", "label": status}


def _load_event(submission_id: str, request: Request):
    db = request.app.state.db
    row = db.get_submission(submission_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Submission {submission_id!r} not found")
    payload = json.loads(row["raw_payload"])
    payload["created_at"] = datetime.fromisoformat(payload["created_at"])
    event = SubmissionEvent(**payload)
    return row, event


def _load_score(submission_id: str, request: Request) -> CompositeScore | None:
    audit = request.app.state.audit
    score_records = audit.read_scores(submission_id=submission_id)
    if not score_records:
        return None
    raw = {k: v for k, v in score_records[-1].items() if k != "log_hash"}
    return CompositeScore(
        submission_id=raw["submission_id"],
        sic=SICScore(**raw["sic"]),
        state=StateScore(**raw["state"]),
        tiv=TIVScore(**raw["tiv"]),
        total=raw["total"],
        routing=raw["routing"],
        routing_reason=raw["routing_reason"],
        scored_at=datetime.fromisoformat(raw["scored_at"]),
    )


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

    row, event = _load_event(submission_id, request)
    score = _load_score(submission_id, request)
    status = row["status"]

    hitl_required = score is None

    tiv_display = f"${event.tiv:,.0f}" if event.tiv is not None else "Missing"
    nav_count = len(db.list_submissions(status="referral_pending", limit=200))

    decisions = audit.read_decisions(submission_id=submission_id)
    latest_decision = decisions[-1] if decisions else None
    routing = score.routing if score else None
    badge = _routing_badge(status, routing, latest_decision)

    # SLA — use score.scored_at when available, else submission created_at
    if score is not None:
        sla_label, sla_css = _sla_info(score.scored_at)
        scored_at_fmt = score.scored_at.strftime("%Y-%m-%d %H:%M UTC")
    else:
        sla_label, sla_css = _sla_info(event.created_at.replace(tzinfo=timezone.utc)
                                         if event.created_at.tzinfo is None
                                         else event.created_at)
        scored_at_fmt = "—"

    # Can UW override an already-automated decision?
    can_override_auto = (
        not latest_decision
        and score is not None
        and status in ("w3_triggered", "w3_pending_retry", "declined")
    )

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
        "hitl_required": hitl_required,
        "can_override_auto": can_override_auto,
        "reason_codes": REASON_CODES,
        "routing_badge": badge,
        # score colors (safe to call even when score is None via template checks)
        "sic_color":       _score_color(score.sic.base_score) if score else "#6b6a65",
        "state_color":     _sign_color(score.state.modifier)  if score else "#6b6a65",
        "tiv_color":       _sign_color(score.tiv.modifier)    if score else "#6b6a65",
        "composite_color": _score_color(score.total)           if score else "#6b6a65",
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

    row, event = _load_event(submission_id, request)
    score = _load_score(submission_id, request)

    if choice not in ("approve", "override", "decline"):
        raise HTTPException(status_code=422, detail="Invalid choice")

    override_score_int: int | None = None
    if override_score.strip():
        try:
            override_score_int = int(override_score.strip())
        except ValueError:
            override_score_int = score.total if score else None

    # For override of an auto decision, prepend original routing to notes for clarity
    original_routing = score.routing if score else "hitl_required"
    if row["status"] in ("w3_triggered", "w3_pending_retry", "declined"):
        prefix = f"[Override of {original_routing}] "
        notes = prefix + notes if notes else prefix.strip()

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
        if score is not None:
            await sub_router.advance_to_w3(event, score)
        else:
            # HITLRequired — manual approval, no composite score available
            db.update_status(submission_id, "w3_triggered")
    else:
        db.update_status(submission_id, "declined")

    return RedirectResponse(url=f"/review/{submission_id}/result", status_code=303)


@router.get("/{submission_id}/result", response_class=HTMLResponse)
async def result_page(submission_id: str, request: Request):
    return RedirectResponse(url=f"/review/{submission_id}", status_code=303)
