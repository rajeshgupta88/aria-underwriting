from __future__ import annotations

import json
import logging
import os
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

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
_SLA_HOURS = int(os.getenv("SLA_HOURS", "4"))

templates = Jinja2Templates(directory="templates")


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    hitl_mode = os.getenv("HITL_MODE", "terminal")
    port = os.getenv("ARIA_PORT", "8001")

    # Ensure data dirs exist
    Path("data").mkdir(exist_ok=True)
    Path("data/w3_pending").mkdir(exist_ok=True)
    for f in ("data/score_log.jsonl", "data/decisions.jsonl"):
        Path(f).touch()

    db = SubmissionDB("data/submissions.db")
    audit = AuditLogger(
        score_log_path=os.getenv("SCORE_LOG_PATH", "data/score_log.jsonl"),
        decisions_path=os.getenv("DECISIONS_PATH", "data/decisions.jsonl"),
    )
    sub_router = SubmissionRouter(db=db, audit=audit, hitl_mode=hitl_mode)

    app.state.db = db
    app.state.audit = audit
    app.state.router = sub_router
    app.state.hitl_mode = hitl_mode

    llm = llm_status()
    logger.info("Aria — Appetite & Risk Intelligence Agent")
    logger.info("Provider: %s · Model: %s", llm["provider"], llm["model"])
    logger.info("Exec demo: http://localhost:%s", port)

    yield
    logger.info("Aria shutting down")


app = FastAPI(title="Aria", version=VERSION, lifespan=lifespan)

# Mount HITL review router (POST /review/{id}/decide + GET /review/{id}/result)
from hitl.review import router as _review_router  # noqa: E402
app.include_router(_review_router)


# ── Data helpers ──────────────────────────────────────────────────────────────

def _routing_badge(status: str, routing: str | None, decision: dict | None) -> dict:
    """Return display info dict {cls, icon, label} for a submission's effective state.

    Overridden submissions get a distinct badge class so the UW can see at a glance
    that the final outcome differs from Aria's automated decision.
    """
    if decision:
        choice = decision.get("choice", "")
        notes = decision.get("notes") or ""
        if "Override of auto_pass" in notes:
            return {"cls": "badge-override-red",   "icon": "↓", "label": "Override · Declined"}
        if "Override of auto_decline" in notes:
            return {"cls": "badge-override-green", "icon": "↑", "label": "Override · Approved"}
        # HITLRequired — no original routing, fully manual
        if routing is None:
            if choice == "decline":
                return {"cls": "badge-red",   "icon": "✗", "label": "Manual Decline"}
            return {"cls": "badge-green", "icon": "✓", "label": "Manual Approve"}
        # Referral decided by UW
        if choice == "decline":
            return {"cls": "badge-red",   "icon": "✗", "label": "UW Declined"}
        return {"cls": "badge-green", "icon": "✓", "label": "UW Approved"}

    # No decision yet — show automated routing
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


def _nav_counts(db: SubmissionDB) -> dict:
    count = len(db.list_submissions(status="referral_pending", limit=200))
    return {"referral_pending": count}


def _score_color(total: int | None) -> str:
    if total is None:
        return "#6b6a65"
    if total >= 65:
        return "#1D9E75"
    if total >= 35:
        return "#EF9F27"
    return "#E24B4A"


def _base_ctx(request: Request, page_title: str, current_route: str) -> dict:
    db: SubmissionDB = request.app.state.db
    return {
        "request": request,
        "page_title": page_title,
        "current_route": current_route,
        "nav_counts": _nav_counts(db),
        "llm": llm_status(),
    }


def _build_queue_rows(db: SubmissionDB, audit: AuditLogger, filter_val: str) -> list[dict]:
    subs = db.list_submissions(limit=200)
    all_scores = audit.read_scores()
    all_decisions = audit.read_decisions()

    score_by_id: dict = {}
    for s in all_scores:
        sid = s.get("submission_id")
        if sid:
            score_by_id[sid] = s

    # Latest decision per submission
    decision_by_id: dict = {}
    for d in all_decisions:
        sid = d.get("submission_id")
        if sid:
            decision_by_id[sid] = d

    rows = []
    for sub in subs:
        score = score_by_id.get(sub["id"])
        decision = decision_by_id.get(sub["id"])
        payload = json.loads(sub["raw_payload"])
        total = score["total"] if score else None
        routing = score["routing"] if score else None
        status = sub["status"]

        badge = _routing_badge(status, routing, decision)

        # Filter logic — overridden auto_pass submissions count as "declined" for the filter
        effective_declined = (
            status == "declined"
            or routing == "auto_decline"
            or (decision and "Override of auto_pass" in (decision.get("notes") or ""))
        )
        effective_passed = (
            routing == "auto_pass"
            and not (decision and "Override of auto_pass" in (decision.get("notes") or ""))
        )
        if filter_val == "referral" and not (routing == "referral" or status == "referral_pending"):
            continue
        if filter_val == "pass" and not effective_passed:
            continue
        if filter_val == "declined" and not effective_declined:
            continue

        scored_at_raw = score["scored_at"] if score else None
        rows.append({
            "id": sub["id"],
            "named_insured": sub["named_insured"],
            "sic_code": sub["sic_code"],
            "sic_description": payload.get("sic_description", ""),
            "writing_state": sub["writing_state"],
            "tiv": sub["tiv"],
            "status": status,
            "routing": routing,
            "score_total": total,
            "score_color": _score_color(total),
            "scored_at": scored_at_raw[:16].replace("T", " ") if scored_at_raw else None,
            "badge": badge,
        })
    return rows


def _build_metrics(db: SubmissionDB, audit: AuditLogger) -> dict:
    """Effective outcome metrics — reflect final state after any UW overrides."""
    subs = db.list_submissions(limit=200)
    all_decisions = audit.read_decisions()

    total    = len(subs)
    approved = sum(1 for s in subs if s["status"] in ("w3_triggered", "w3_pending_retry"))
    pending  = sum(1 for s in subs if s["status"] == "referral_pending")
    declined = sum(1 for s in subs if s["status"] == "declined")
    overrides = sum(1 for d in all_decisions
                    if "Override of auto_" in (d.get("notes") or ""))

    return {
        "total": total,
        "approved": approved,
        "pending": pending,
        "declined": declined,
        "overrides": overrides,
        # kept for any legacy template references
        "referral_pending": pending,
    }


def _build_audit_rows(audit: AuditLogger) -> tuple[list[dict], list[dict], dict]:
    integrity = audit.verify_integrity()
    tampered_score = {t["line"] - 1 for t in integrity["score_log"]["tampered"]}
    tampered_dec   = {t["line"] - 1 for t in integrity["decisions"]["tampered"]}

    raw_scores = audit.read_scores()
    score_rows = []
    for i, r in enumerate(raw_scores):
        sid = r.get("submission_id", "")
        score_rows.append({
            "hash_short": (r.get("log_hash") or "")[:8],
            "submission_id": sid,
            "named_insured": r.get("sic", {}).get("sic_code", ""),  # placeholder
            "sic_code": r.get("sic", {}).get("sic_code", ""),
            "writing_state": r.get("state", {}).get("state", ""),
            "score_total": r.get("total"),
            "score_color": _score_color(r.get("total")),
            "routing": r.get("routing", ""),
            "scored_at_fmt": (r.get("scored_at") or "")[:16].replace("T", " "),
            "tampered": i in tampered_score,
            # named_insured is not in score log — pull from submission_id for display
            "named_insured": sid,
        })

    raw_decs = audit.read_decisions()
    decision_rows = []
    for i, r in enumerate(raw_decs):
        decision_rows.append({
            "hash_short": (r.get("log_hash") or "")[:8],
            "submission_id": r.get("submission_id", ""),
            "choice": r.get("choice", ""),
            "reviewer_id": r.get("reviewer_id", ""),
            "reason_code": r.get("reason_code"),
            "decided_at_fmt": (r.get("decided_at") or "")[:16].replace("T", " "),
            "tampered": i in tampered_dec,
        })

    totals = {"scores": len(score_rows), "decisions": len(decision_rows)}
    return score_rows, decision_rows, integrity, totals


def _sla_info(scored_at_iso: str) -> tuple[str, str]:
    """Return (label, css_class) for SLA countdown."""
    try:
        scored_at = datetime.fromisoformat(scored_at_iso)
        if scored_at.tzinfo is None:
            scored_at = scored_at.replace(tzinfo=timezone.utc)
        deadline = scored_at + timedelta(hours=_SLA_HOURS)
        remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            return "OVERDUE", "sla-red"
        h = int(remaining // 3600)
        m = int((remaining % 3600) // 60)
        label = f"{h}h {m}m remaining"
        if remaining < 3600:
            return label, "sla-red"
        if remaining < 7200:
            return label, "sla-amber"
        return label, "sla-green"
    except Exception:
        return f"SLA {_SLA_HOURS}h", "sla-amber"


def _build_insights(db: SubmissionDB, audit: AuditLogger) -> dict:
    all_scores = audit.read_scores()
    all_decisions = audit.read_decisions()
    now = datetime.now(timezone.utc)

    total = len(all_scores)
    auto_pass_count = sum(1 for s in all_scores if s.get("routing") == "auto_pass")
    auto_decline_count = sum(1 for s in all_scores if s.get("routing") == "auto_decline")
    referral_count = sum(1 for s in all_scores if s.get("routing") == "referral")

    stp_rate = round(auto_pass_count / total * 100, 1) if total else 0.0
    avg_score = round(sum(s.get("total", 0) for s in all_scores) / total, 1) if total else 0.0
    uw_saved_mins = (auto_pass_count + auto_decline_count) * 8
    uw_time_saved = f"{uw_saved_mins // 60}h {uw_saved_mins % 60}m"
    # Override rate = automated decisions reversed by UW / total decisions made
    auto_overrides = sum(1 for d in all_decisions
                         if "Override of auto_" in (d.get("notes") or ""))
    total_decisions = len(all_decisions)
    override_rate = f"{round(auto_overrides / total_decisions * 100)}%" if total_decisions else "—"

    # Decline drivers
    decline_scores = [s for s in all_scores if s.get("routing") == "auto_decline"]
    tier_x = sum(1 for s in decline_scores if s.get("sic", {}).get("tier") == "X")
    state_drag = sum(1 for s in decline_scores if s.get("state", {}).get("modifier", 0) < 0)
    tiv_jumbo = sum(1 for s in decline_scores if s.get("tiv", {}).get("modifier", 0) == -25)
    decline_drivers = [
        {"label": "SIC Tier X", "count": tier_x},
        {"label": "State modifier", "count": state_drag},
        {"label": "TIV jumbo band", "count": tiv_jumbo},
    ]
    max_decline = max((d["count"] for d in decline_drivers), default=1) or 1

    # SIC volume top 5
    sic_counter = Counter(s.get("sic", {}).get("sic_code", "?") for s in all_scores)
    tier_by_sic = {s.get("sic", {}).get("sic_code", ""): s.get("sic", {}).get("tier", "?")
                   for s in all_scores}
    tier_colors = {"A": "#1D9E75", "B": "#EF9F27", "C": "#EF9F27", "X": "#E24B4A"}
    sic_volume = [
        {"sic_code": sic, "count": cnt,
         "tier": tier_by_sic.get(sic, "?"),
         "bar_color": tier_colors.get(tier_by_sic.get(sic, "?"), "#6b6a65")}
        for sic, cnt in sic_counter.most_common(5)
    ]
    max_sic = max((s["count"] for s in sic_volume), default=1) or 1

    # Pending review SLA — includes both referrals and HITLRequired (no score)
    referral_subs = db.list_submissions(status="referral_pending", limit=50)
    referral_sla = []
    for sub in referral_subs:
        scores = audit.read_scores(submission_id=sub["id"])
        # Use scored_at if available, else submission created_at (HITLRequired)
        if scores:
            ref_time = scores[-1].get("scored_at", "")
        else:
            ref_time = sub["created_at"]
        sla_label, sla_color_cls = _sla_info(ref_time)
        color = {"sla-red": "#E24B4A", "sla-amber": "#EF9F27", "sla-green": "#1D9E75"}.get(sla_color_cls, "#6b6a65")
        is_hitl = len(scores) == 0
        referral_sla.append({
            "id": sub["id"],
            "named_insured": sub["named_insured"],
            "sla_label": sla_label,
            "sla_color": color,
            "is_hitl": is_hitl,
        })

    # Governance
    integrity = audit.verify_integrity()
    total_tampered = len(integrity["score_log"]["tampered"]) + len(integrity["decisions"]["tampered"])
    sla_total = len(referral_sla)
    sla_on_track = sum(1 for s in referral_sla if "OVERDUE" not in s["sla_label"])
    llm = llm_status()
    override_rate_pct = round(auto_overrides / total_decisions * 100) if total_decisions else None

    governance = {
        "audit_clean": total_tampered == 0,
        "sla_on_track": sla_on_track,
        "sla_total": sla_total,
        "override_rate": override_rate_pct,
        "decisions_logged": len(all_decisions),
        "llm_provider": llm["provider"],
        "llm_model": llm["model"],
        "api_key_set": llm["api_key_set"],
    }

    return {
        "metrics": {
            "stp_rate": stp_rate,
            "avg_score": avg_score,
            "uw_time_saved": uw_time_saved,
            "uw_saved_mins": uw_saved_mins,
            "override_rate": override_rate,
            "override_context": f"{auto_overrides} of {total_decisions} decision{'s' if total_decisions != 1 else ''}" if total_decisions else "no decisions yet",
            "total_scored": total,
            "auto_pass_count": auto_pass_count,
            "auto_decline_count": auto_decline_count,
            "referral_count": referral_count,
        },
        "decline_drivers": decline_drivers,
        "max_decline_count": max_decline,
        "sic_volume": sic_volume,
        "max_sic_count": max_sic,
        "referral_sla": referral_sla,
        "sla_hours": _SLA_HOURS,
        "governance": governance,
    }


# ── UI routes ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def queue_page(request: Request, filter: str = Query(default="all")):
    db: SubmissionDB = request.app.state.db
    audit: AuditLogger = request.app.state.audit
    ctx = _base_ctx(request, "Submission queue", "/")
    ctx["rows"] = _build_queue_rows(db, audit, filter)
    ctx["metrics"] = _build_metrics(db, audit)
    ctx["filter"] = filter
    return templates.TemplateResponse("queue.html", ctx)


@app.get("/audit", response_class=HTMLResponse)
async def audit_page(request: Request):
    audit: AuditLogger = request.app.state.audit
    score_rows, decision_rows, integrity, totals = _build_audit_rows(audit)
    ctx = _base_ctx(request, "Audit log", "/audit")
    ctx.update({
        "score_rows": score_rows,
        "decision_rows": decision_rows,
        "integrity": integrity,
        "totals": totals,
    })
    return templates.TemplateResponse("audit.html", ctx)


@app.get("/audit/export")
async def audit_export():
    path = os.getenv("SCORE_LOG_PATH", "data/score_log.jsonl")
    return FileResponse(
        path,
        media_type="application/x-ndjson",
        filename="aria_score_log.jsonl",
        headers={"Content-Disposition": "attachment; filename=aria_score_log.jsonl"},
    )


@app.get("/insights", response_class=HTMLResponse)
async def insights_page(request: Request):
    db: SubmissionDB = request.app.state.db
    audit: AuditLogger = request.app.state.audit
    ctx = _base_ctx(request, "Insights", "/insights")
    ctx.update(_build_insights(db, audit))
    return templates.TemplateResponse("insights.html", ctx)


@app.post("/reset", response_class=RedirectResponse)
async def reset_demo():
    """Truncate audit files and re-seed all demo submissions."""
    for p in ("data/score_log.jsonl", "data/decisions.jsonl"):
        Path(p).write_text("")
    db: SubmissionDB = app.state.db
    db._conn.execute("DELETE FROM submissions")
    db._conn.commit()
    db.seed_sample_data()
    return RedirectResponse(url="/", status_code=303)


@app.get("/review", response_class=RedirectResponse)
async def review_nav(request: Request):
    db: SubmissionDB = request.app.state.db
    rows = db.list_submissions(status="referral_pending", limit=1)
    if rows:
        return RedirectResponse(url=f"/review/{rows[0]['id']}")
    return RedirectResponse(url="/?filter=referral")


# ── API routes ────────────────────────────────────────────────────────────────

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
    return {"submission": row, "latest_score": scores[-1] if scores else None}


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
        raise HTTPException(status_code=400,
                            detail=f"n must be 0–{len(_SAMPLE_SUBMISSIONS) - 1}")
    raw = _SAMPLE_SUBMISSIONS[n]
    event = SubmissionEvent(**{**raw, "created_at": datetime.now(timezone.utc)})
    result = await app.state.router.route(event)
    return {"sample_index": n, "named_insured": event.named_insured, **result}
