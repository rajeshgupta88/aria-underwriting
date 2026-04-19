from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

from aria.models import CompositeScore, SubmissionEvent

_console = Console()

_ROUTING_COLOR = {
    "auto_pass": "green",
    "referral": "yellow",
    "auto_decline": "red",
}


def render_hitl_card(event: SubmissionEvent, score: CompositeScore) -> None:
    """Print a Rich HITL review card to the terminal."""
    color = _ROUTING_COLOR.get(score.routing, "white")

    # ── Score breakdown table ─────────────────────────────────────────────────
    breakdown = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan")
    breakdown.add_column("Component", style="dim")
    breakdown.add_column("Value", justify="right")
    breakdown.add_column("Modifier", justify="right")

    breakdown.add_row(
        f"SIC {score.sic.sic_code} — Tier {score.sic.tier}",
        str(score.sic.base_score),
        "",
    )
    breakdown.add_row(
        f"State: {score.state.state}"
        + (" (coastal)" if score.state.is_coastal else ""),
        "",
        f"{score.state.modifier:+d}",
    )
    breakdown.add_row(
        f"TIV: {score.tiv.band_label}"
        + (" [missing]" if score.tiv.missing else ""),
        "",
        f"{score.tiv.modifier:+d}",
    )
    breakdown.add_row(
        "[bold]TOTAL[/bold]",
        f"[bold {color}]{score.total}[/bold {color}]",
        "",
    )

    # ── Submission detail table ───────────────────────────────────────────────
    detail = Table(box=box.SIMPLE, show_header=False)
    detail.add_column("Field", style="dim", width=20)
    detail.add_column("Value")

    tiv_display = f"${event.tiv:,.0f}" if event.tiv is not None else "— (missing)"
    detail.add_row("Submission ID", event.submission_id)
    detail.add_row("Named Insured", event.named_insured)
    detail.add_row("SIC Code", f"{event.sic_code} — {event.sic_description}")
    detail.add_row("SIC Confidence", f"{event.sic_confidence:.0%}")
    detail.add_row("Writing State", event.writing_state)
    detail.add_row("Premises ZIP", event.premises_zip)
    detail.add_row("TIV", tiv_display)
    detail.add_row("PC Account", event.pc_account_id)

    panel_title = (
        f"[bold {color}] HITL REVIEW — {score.routing.upper().replace('_', ' ')} "
        f"(score {score.total}) [/bold {color}]"
    )

    _console.print()
    _console.print(Panel(detail, title="[bold]Submission[/bold]", border_style="blue"))
    _console.print(Panel(breakdown, title="[bold]Score Breakdown[/bold]", border_style=color))
    _console.print(
        Panel(
            f"[italic]{score.routing_reason}[/italic]",
            title=panel_title,
            border_style=color,
        )
    )
    _console.print()
