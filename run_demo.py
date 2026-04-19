#!/usr/bin/env python
"""
Aria executive demo launcher.

Usage:
  .venv/bin/python run_demo.py          # submit samples + print summary
  .venv/bin/python run_demo.py --demo   # submit samples + open browser
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

CONSOLE = Console()
BASE = "http://localhost:8001"


def _health_check() -> bool:
    try:
        r = httpx.get(f"{BASE}/health", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def _ensure_server() -> bool:
    if _health_check():
        return True
    CONSOLE.print("[yellow]Server not running — launching now…[/yellow]")
    subprocess.Popen(
        [".venv/bin/uvicorn", "aria.server:app", "--port", "8001"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(2)
    if _health_check():
        CONSOLE.print("[green]Server started.[/green]")
        return True
    CONSOLE.print("[red]Server failed to start. Run manually:[/red]")
    CONSOLE.print("  .venv/bin/uvicorn aria.server:app --port 8001 --reload")
    return False


def _reset_demo_data() -> None:
    # Truncate audit files
    for path in ("data/score_log.jsonl", "data/decisions.jsonl"):
        Path(path).write_text("")

    # Clear and re-seed submissions DB
    conn = sqlite3.connect("data/submissions.db")
    conn.execute("DELETE FROM submissions")
    conn.commit()
    conn.close()

    # Re-seed via the DB class
    from aria.db import SubmissionDB
    db = SubmissionDB("data/submissions.db")
    db.seed_sample_data()
    CONSOLE.print("[dim]Demo data reset — 5 sample submissions loaded[/dim]")


def _submit_samples() -> list[dict]:
    results = []
    with httpx.Client(base_url=BASE, timeout=15) as client:
        for n in range(5):
            try:
                r = client.post(f"/test/submit/{n}")
                r.raise_for_status()
                data = r.json()
                results.append(data)
            except Exception as exc:
                CONSOLE.print(f"[red]Sample {n} failed: {exc}[/red]")
                results.append({"error": str(exc), "sample_index": n})
            time.sleep(0.8)
    return results


def _print_results(results: list[dict]) -> None:
    t = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style="bold cyan",
              title="Aria — Submission results")
    t.add_column("Named insured", min_width=24)
    t.add_column("SIC", justify="center", width=6)
    t.add_column("State", justify="center", width=6)
    t.add_column("TIV", justify="right", width=8)
    t.add_column("Score", justify="center", width=6)
    t.add_column("Routing", width=14)
    t.add_column("Outcome", width=14)

    routing_style = {
        "auto_pass":    "[green]",
        "referral":     "[yellow]",
        "auto_decline": "[red]",
        "hitl_required": "[magenta]",
    }

    for r in results:
        if "error" in r:
            t.add_row(f"[red]Error: {r['error']}[/red]", *["—"] * 6)
            continue
        score = r.get("score") or {}
        sic = score.get("sic", {}) if score else {}
        state = score.get("state", {}) if score else {}
        tiv = score.get("tiv", {}) if score else {}

        sic_code = sic.get("sic_code", "—")
        state_name = state.get("state", "—")
        tiv_val = tiv.get("tiv")
        tiv_fmt = f"${tiv_val/1000:.0f}K" if tiv_val and tiv_val < 1e6 else (f"${tiv_val/1e6:.1f}M" if tiv_val else "—")
        total = score.get("total", "—") if score else "—"
        routing = r.get("outcome", "—")
        style = routing_style.get(routing, "")
        close = "[/]" if style else ""

        t.add_row(
            r.get("named_insured", "—"),
            sic_code,
            state_name,
            tiv_fmt,
            str(total),
            f"{style}{routing}{close}",
            f"{style}{r.get('outcome','—')}{close}",
        )

    CONSOLE.print(t)


def _print_summary(results: list[dict]) -> None:
    auto_pass   = sum(1 for r in results if r.get("outcome") == "auto_pass")
    referrals   = sum(1 for r in results if r.get("outcome") in ("referral", "hitl_required"))
    auto_decline = sum(1 for r in results if r.get("outcome") == "auto_decline")

    try:
        from aria.audit import AuditLogger
        audit = AuditLogger()
        n_audit = len(audit.read_scores())
    except Exception:
        n_audit = "?"

    first_referral = next(
        (r.get("score", {}).get("submission_id") for r in results
         if r.get("outcome") in ("referral", "hitl_required") and r.get("score")),
        None,
    )

    lines = [
        f"[green]Auto-passed:    {auto_pass}[/green]",
        f"[yellow]Referrals:      {referrals}[/yellow]"
        + (f"  → [cyan]http://localhost:8001/review/{first_referral}[/cyan]" if first_referral else ""),
        f"[red]Auto-declined:  {auto_decline}[/red]",
        f"[dim]Audit entries:  {n_audit}[/dim]",
    ]
    CONSOLE.print(Panel("\n".join(lines), title="[bold]Summary[/bold]", border_style="blue"))
    CONSOLE.print("[bold]Exec demo ready at:[/bold] [cyan]http://localhost:8001[/cyan]")


def main() -> None:
    demo_mode = "--demo" in sys.argv

    # Header
    CONSOLE.print(Panel(
        f"[bold white]Aria — Appetite & Risk Intelligence Agent[/bold white]\n"
        f"[dim]Executive demo · {datetime.now().strftime('%A %d %B %Y')}[/dim]",
        border_style="purple",
    ))

    # 1. Ensure server is running
    if not _ensure_server():
        sys.exit(1)

    # 2. Reset demo data
    _reset_demo_data()

    # 3. Submit all 5 samples
    CONSOLE.print("[dim]Submitting 5 sample submissions…[/dim]")
    results = _submit_samples()

    # 4. Print per-submission table
    _print_results(results)

    # 5. Summary panel
    _print_summary(results)

    # 6. Open browser if --demo
    if demo_mode:
        time.sleep(1)
        webbrowser.open("http://localhost:8001")


if __name__ == "__main__":
    main()
