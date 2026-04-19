# Aria — Appetite & Risk Intelligence Agent

Aria is a small commercial insurance underwriting agent that scores incoming submissions across three dimensions — class of business, geography, and total insured value — using deterministic YAML-driven rules. An LLM generates a plain-English rationale for each decision. Edge cases are flagged for underwriter review through a human-in-the-loop (HITL) layer before any decision is finalised.

Built as a prototype for the BOP / GL line of business, running entirely on localhost.

---

## What it does

1. **Ingests** a submission event (SIC code, writing state, ZIP, TIV, named insured)
2. **Scores** it across three independent dimensions (0–100 composite)
3. **Routes** the result automatically — pass, referral, or decline
4. **Flags** low-confidence SIC classifications as HITLRequired before scoring
5. **Queues** referrals and HITLRequired cases for underwriter review in the browser
6. **Allows** underwriters to override any automated decision (pass or decline) with a documented reason code
7. **Logs** every score and decision to a tamper-evident append-only audit trail

---

## High-level architecture

```
                          ┌──────────────────────────────────────────┐
                          │              FastAPI server               │
                          │            (aria/server.py)               │
                          │                                           │
  POST /score ───────────▶│  SubmissionRouter  ──▶  Scorer           │
  POST /test/submit/{n}   │  (aria/router.py)        (aria/scorer.py) │
                          │         │                                  │
                          │         ├─ HITLRequired ▶ manual review   │
                          │         ├─ auto_pass   ──▶ W3 enrichment  │
                          │         ├─ referral    ──▶ HITL review    │
                          │         └─ auto_decline ▶ decline record  │
                          │                                           │
                          │  AuditLogger  ◀─── every score/decision   │
                          │  (aria/audit.py)                          │
                          └──────────────────────────────────────────┘
                                        │
              ┌─────────────────────────┼─────────────────────────┐
              │                         │                         │
         GET /              GET /review/{id}          GET /audit, /insights
    Submission queue      UW review card            Audit log + analytics
    (queue.html)          (review.html)             (Jinja2 templates)
```

### Layer breakdown

| Layer | Files | Responsibility |
|---|---|---|
| **Scoring engine** | `aria/scorer.py` | Deterministic SIC → tier lookup, state modifier, TIV band, composite 0–100 |
| **Config** | `config/*.yaml` | Appetite tiers, state modifiers, TIV bands — editable without code changes |
| **Router** | `aria/router.py` | Dispatches scored events downstream; raises HITLRequired before scoring when confidence is too low |
| **Audit** | `aria/audit.py` | Append-only JSONL with per-record SHA-256 hash for tamper detection; records composite snapshot on every decision including overrides |
| **Database** | `aria/db.py` | SQLite submission store (no ORM); tracks lifecycle status |
| **LLM** | `aria/llm.py` | OpenAI / Anthropic client; generates narrative rationale (swap via config) |
| **HITL** | `hitl/review.py`, `hitl/card.py` | Browser review page with decision forms and override capability; terminal Rich card |
| **UI** | `templates/`, `aria/server.py` | Four-screen Jinja2 UI: queue, UW review, audit log, insights |

---

## Scoring model

Each submission receives a composite score (0–100) from three components:

```
Composite = clamp(SIC base score + state modifier + TIV modifier, 0, 100)
```

**SIC base score** (from `config/appetite_config.yaml`)

| Tier | Base score | Examples |
|---|---|---|
| A — Preferred | 90 | Restaurants (5812), Grocery (5411), Hotels (7011) |
| B — Acceptable | 70 | Hardware (5251), Drug stores (5912), Car rental (7514) |
| C — Marginal | 40 | Bars/taverns (5813), Motorcycle dealers (5571), Electrical contractors (1731) |
| X — Hard decline | 0 | Liquor stores (5921), Gambling (7993), Plumbing contractors (1711) |

**State modifier** (from `config/state_mods.yaml`)

| State | Standard | Coastal |
|---|---|---|
| FL | −25 | −30 |
| CA | −15 | −20 |
| NY | −10 | — |
| TX | +5 | −20 |
| GA, PA, OH, NC | +5 | varies |

**TIV band** (from `config/tiv_bands.yaml`)

| Band | Range | Modifier |
|---|---|---|
| Micro | < $250K | +15 |
| Small | $250K – $500K | +10 |
| Mid | $500K – $2M | 0 |
| Large | $2M – $5M | −15 |
| Jumbo | > $5M | −25 |

**Routing thresholds**

| Score | Routing |
|---|---|
| ≥ 65 | `auto_pass` — advance to W3 enrichment |
| 35 – 64 | `referral` — queue for underwriter review |
| < 35 | `auto_decline` — reject and write decline record |

**HITLRequired** — raised *before* scoring if SIC classifier confidence < 0.80, or < 0.90 for ambiguous SIC codes (7389, 7999, 1521, 7011). No composite score is computed; the submission goes straight to manual review.

---

## Underwriter decision flows

The browser review card (`/review/{id}`) presents a different form depending on the submission state:

Both HITLRequired and scored referrals show the same **"Pending Review"** badge in the queue — they both need the same UW action. The distinction (why it's pending) is visible only on the review page itself.

| State | Queue badge | What the UW sees on the review page |
|---|---|---|
| **HITLRequired** | Pending Review (amber) | Amber warning banner explaining the confidence shortfall; manual approve (with score) or decline form |
| **Referral** | Pending Review (amber) | Full Approve / Override (adjusted score) / Decline panel |
| **Auto-passed** | Auto-passed (green) | Read-only score card + collapsible "Override — Decline" form with mandatory notes |
| **Auto-declined** | Auto-declined (red) | Read-only score card + collapsible "Override — Approve" form with mandatory override score and notes |
| **UW override applied** | ↑ Override · Approved or ↓ Override · Declined (bold border badge) | Read-only outcome panel showing reviewer, reason code, and notes |
| **Decision recorded** | ✓ UW Approved / ✗ UW Declined (green/red) | Read-only outcome panel |

Every decision — including automated overrides — is logged to `data/decisions.jsonl` with a SHA-256 integrity hash and a snapshot of the composite score at the time of decision.

### Reason codes

All decision forms require the underwriter to select a structured reason code. Codes are grouped into three categories:

| Category | Codes |
|---|---|
| **Approval** | `APPROVE_STANDARD`, `APPROVE_CONDITIONS`, `APPROVE_EXCEPTION`, `APPROVE_RELATIONSHIP` |
| **Override / Escalation** | `OVERRIDE_INFO_RESOLVED`, `OVERRIDE_PORTFOLIO`, `OVERRIDE_PRICING`, `OVERRIDE_MANUAL` |
| **Decline** | `DECLINE_CAT_EXPOSURE`, `DECLINE_LOSS_HISTORY`, `DECLINE_OUTSIDE_APPETITE`, `DECLINE_SIC_INELIGIBLE`, `DECLINE_TIV_LIMIT`, `DECLINE_COMPLIANCE`, `DECLINE_MANUAL` |

---

## Demo screens

| Screen | URL | What it shows |
|---|---|---|
| Submission queue | `localhost:8001/` | Five metric cards (total, approved, pending, declined, overrides), filter bar, reset button, sortable table |
| UW review | `localhost:8001/review/{id}` | Score tiles, composite bar, context-aware decision form with structured reason codes |
| Audit log | `localhost:8001/audit` | Tamper-evident log, per-record SHA-256 integrity status, JSONL export |
| Insights | `localhost:8001/insights` | Straight-through rate, average score, UW time saved, override rate, decline drivers, SIC mix, SLA clock, governance health |

The **Reset demo** button on the queue page truncates all audit files and re-seeds the six sample submissions in one click.

### Queue metric cards

| Card | What it counts |
|---|---|
| **Total** | All submissions in the database |
| **Approved** | Submissions currently in `w3_triggered` or `w3_pending_retry` status — includes auto-passes and UW-approved referrals/overrides |
| **Pending review** | Submissions waiting for an underwriter decision (`referral_pending`) |
| **Declined** | Submissions with `declined` status — includes auto-declines and UW-declined overrides |
| **UW overrides** | Count of decisions where an underwriter reversed an automated outcome (auto-pass → declined, or auto-decline → approved) |

### Insights page explained

| Metric | What it means |
|---|---|
| **Straight-through rate** | % of submissions auto-approved or auto-declined with no human review (higher = more automation) |
| **Average risk score** | Mean composite score (0–100) across scored submissions; Aria auto-passes at ≥ 65 |
| **Estimated UW time saved** | Automated submissions × 8 min/submission vs. full manual review |
| **Override rate** | % of all decisions that reversed an automated outcome — high values warrant governance attention |
| **Why did Aria decline?** | Breakdown of root causes (SIC Tier X, state modifier, jumbo TIV) across auto-declined submissions |
| **Submission mix by SIC** | Volume per SIC code; bar colour shows Aria's risk tier (green=Preferred, amber=Acceptable/Marginal, red=Hard decline) |
| **Pending review — SLA clock** | Open referrals and HITL items with time remaining on the 24-hour SLA; red = under 1 hour or overdue |
| **Governance & system health** | Audit trail integrity check, SLA on-track count, total decisions logged, LLM provider and API key status |

---

## Project structure

```
aria-underwriting/
├── aria/
│   ├── models.py       # Pydantic v2 data models (SubmissionEvent, CompositeScore, UWDecision…)
│   ├── scorer.py       # Deterministic scoring engine
│   ├── router.py       # Submission routing + downstream dispatch
│   ├── audit.py        # Tamper-evident JSONL audit logger
│   ├── db.py           # SQLite submission store + 6 seed samples
│   ├── llm.py          # LLM client (OpenAI / Anthropic)
│   └── server.py       # FastAPI app, UI routes, POST /reset
├── hitl/
│   ├── review.py       # Browser HITL router — all decision forms and override logic
│   └── card.py         # Terminal Rich card with interactive prompt
├── config/
│   ├── appetite_config.yaml   # SIC tiers and ambiguous SIC list
│   ├── state_mods.yaml        # State modifiers and coastal overrides
│   ├── tiv_bands.yaml         # TIV band definitions
│   └── llm_config.yaml        # LLM provider and model selection
├── templates/
│   ├── base.html       # Shared sidebar layout, CSS, JS countdown
│   ├── queue.html      # Submission queue with reset button
│   ├── review.html     # UW review card (all five decision states)
│   ├── audit.html      # Audit log + integrity banner
│   └── insights.html   # Analytics and governance
├── tests/
│   ├── test_scorer.py  # 20 scoring engine unit tests
│   └── test_audit.py   # 12 audit logger unit tests
├── data/               # Runtime data (SQLite, JSONL logs, decline records)
├── run_demo.py         # Exec demo launcher
├── pyproject.toml
└── requirements.txt
```

---

## Running the demo

```bash
# First time setup
cp .env.example .env          # add your API key
~/.pyenv/versions/3.12.4/bin/python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -e .

# Launch (resets data, submits 6 samples, opens browser)
.venv/bin/python run_demo.py --demo
```

The demo submits six representative submissions covering every routing path:

| Submission | SIC | State | TIV | Score | Routing |
|---|---|---|---|---|---|
| Rossi's Italian Kitchen | 5812 (A) | CA coastal | $800K | 70 | auto_pass |
| Patel Food Markets | 5411 (A) | TX inland | $300K | 100 | auto_pass |
| Harbor View Lounge | 5813 (C) | FL coastal | $1.5M | 10 | auto_decline |
| Apex Cycle & Powersports | 5571 (C) | GA | $400K | 55 | referral |
| Apex Business Services | 7389 ambiguous | IL | — | — | **hitl_required** |
| Greenberg Builders | 1521 (C) | NY | $4.0M | 15 | auto_decline |

Apex Business Services uses SIC 7389 (miscellaneous business services — an ambiguous code) with 82% classifier confidence, which is below the 90% threshold required for ambiguous codes. Scoring is skipped; the submission goes directly to the manual review queue.

---

## Configuration

**Switch LLM provider** — edit `config/llm_config.yaml`:

```yaml
provider: anthropic   # was: openai
```

**HITL mode** — edit `.env`:

```
HITL_MODE=browser     # browser (default) | terminal
```

Only the API key for the active provider needs to be set.

---

## Run tests

```bash
.venv/bin/pytest tests/ -v
# 32 passed
```

## Run server manually

```bash
.venv/bin/uvicorn aria.server:app --port 8001 --reload
```
