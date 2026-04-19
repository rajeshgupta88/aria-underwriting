from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from aria.models import CompositeScore, SubmissionEvent, UWDecision

_console = Console()
_SLA_HOURS = int(os.getenv("SLA_HOURS", "4"))

_ROUTING_COLOR = {
    "auto_pass": "green",
    "referral": "yellow",
    "auto_decline": "red",
}


def _render_card(score: CompositeScore, event: SubmissionEvent) -> None:
    color = _ROUTING_COLOR.get(score.routing, "white")
    tiv_display = f"${event.tiv:,.0f}" if event.tiv is not None else "— (missing)"

    # ── Score breakdown table ─────────────────────────────────────────────────
    t = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    t.add_column("Component", style="dim", min_width=32)
    t.add_column("Points", justify="right", min_width=8)

    t.add_row(
        f"SIC {score.sic.sic_code}  {event.sic_description}  [Tier {score.sic.tier}]",
        str(score.sic.base_score),
    )
    state_label = (
        f"State: {score.state.state} (coastal)"
        if score.state.is_coastal
        else f"State: {score.state.state}"
    )
    mod_color = "red" if score.state.modifier < 0 else "green"
    t.add_row(
        state_label,
        f"[{mod_color}]{score.state.modifier:+d}[/{mod_color}]",
    )
    tiv_mod_color = "red" if score.tiv.modifier < 0 else ("dim" if score.tiv.modifier == 0 else "green")
    t.add_row(
        f"TIV: {score.tiv.band_label}" + (" [missing]" if score.tiv.missing else ""),
        f"[{tiv_mod_color}]{score.tiv.modifier:+d}[/{tiv_mod_color}]",
    )
    t.add_section()
    t.add_row(
        "[bold]COMPOSITE SCORE[/bold]",
        f"[bold yellow]{score.total}[/bold yellow]",
    )
    t.add_row(
        f"[italic dim]{score.routing_reason}[/italic dim]",
        "",
    )

    # ── Submission detail ─────────────────────────────────────────────────────
    d = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    d.add_column("Field", style="dim", min_width=18)
    d.add_column("Value")
    d.add_row("Named Insured", f"[bold]{event.named_insured}[/bold]")
    d.add_row("Writing State", event.writing_state)
    d.add_row("TIV", tiv_display)
    d.add_row("PC Account", event.pc_account_id)
    d.add_row(
        "SLA Deadline",
        f"[bold red]{_SLA_HOURS}h from {score.scored_at.strftime('%Y-%m-%d %H:%M UTC')}[/bold red]",
    )

    _console.print()
    _console.print(
        Panel(
            t,
            title=f"[bold]Aria — Score Card · Referral Required[/bold]  "
                  f"[{color}]({score.routing.upper().replace('_', ' ')})[/{color}]",
            border_style=color,
            subtitle=f"[dim]{event.submission_id}[/dim]",
        )
    )
    _console.print(Panel(d, title="[bold]Submission Detail[/bold]", border_style="blue"))


def render_and_prompt(
    score: CompositeScore,
    event: SubmissionEvent,
    audit,   # AuditLogger — avoid circular at import time
    db,      # SubmissionDB
) -> UWDecision:
    """Render the HITL card and collect underwriter decision synchronously."""
    _render_card(score, event)

    _console.print("[bold cyan]Underwriter Action Required:[/bold cyan]")
    _console.print("  [A] Approve  — advance to W3 enrichment")
    _console.print("  [O] Override — approve with adjusted score + reason code")
    _console.print("  [D] Decline  — decline with reason code")
    _console.print()

    choice = None
    for attempt in range(2):
        raw = input("  Choice [A/O/D]: ").strip().upper()
        if raw in ("A", "O", "D"):
            choice = raw
            break
        if attempt == 0:
            _console.print("[red]Invalid input — please enter A, O, or D.[/red]")
    if choice is None:
        raise TimeoutError("No valid HITL choice after 2 attempts")

    reviewer_id = input("  Reviewer ID [uw_terminal]: ").strip() or "uw_terminal"
    reason_code: str | None = None
    override_score: int | None = None
    notes: str | None = None

    if choice == "A":
        uw_choice = "approve"
        notes = input("  Notes (optional): ").strip() or None

    elif choice == "O":
        uw_choice = "override"
        reason_code = input("  Reason code: ").strip() or "OVERRIDE_MANUAL"
        raw_score = input(f"  Override score [current {score.total}]: ").strip()
        try:
            override_score = int(raw_score)
        except ValueError:
            override_score = score.total
        notes = input("  Notes (optional): ").strip() or None

    else:  # D
        uw_choice = "decline"
        reason_code = input("  Reason code: ").strip() or "DECLINE_MANUAL"
        notes = input("  Notes (optional): ").strip() or None

    decision = UWDecision(
        submission_id=event.submission_id,
        reviewer_id=reviewer_id,
        choice=uw_choice,
        reason_code=reason_code,
        override_score=override_score,
        notes=notes,
        decided_at=datetime.now(timezone.utc),
    )

    audit.log_decision(decision, score)
    new_status = "w3_triggered" if uw_choice in ("approve", "override") else "declined"
    db.update_status(event.submission_id, new_status)

    _console.print()
    _console.print(
        f"[bold green]Decision recorded:[/bold green] {uw_choice.upper()} "
        f"by {reviewer_id} — status → {new_status}"
    )
    _console.print()

    return decision
