# Aria — Appetite & Risk Intelligence Agent

Aria is a small commercial insurance underwriting agent that scores incoming submissions across three dimensions — class of business, geography, and total insured value — using deterministic YAML-driven rules. An LLM generates a plain-English rationale for each decision. Edge cases are flagged for underwriter review through a human-in-the-loop (HITL) layer before any decision is finalised.

Built as a prototype for the BOP / GL line of business, running entirely on localhost.

---

## What it does

1. **Ingests** a submission event (SIC code, writing state, ZIP, TIV, named insured)
2. **Scores** it across three independent dimensions (0–100 composite)
3. **Routes** the result automatically — pass, referral, or decline
4. **Flags** edge cases for a human underwriter via browser review card
5. **Logs** every score and decision to a tamper-evident append-only audit trail

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
                          │         ├─ auto_pass ──▶ W3 enrichment    │
                          │         ├─ referral  ──▶ HITL review      │
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
| **Router** | `aria/router.py` | Dispatches scored events downstream; handles HITLRequired exceptions |
| **Audit** | `aria/audit.py` | Append-only JSONL with per-record SHA-256 hash for tamper detection |
| **Database** | `aria/db.py` | SQLite submission store (no ORM); tracks lifecycle status |
| **LLM** | `aria/llm.py` | OpenAI / Anthropic client; generates narrative rationale (swap via config) |
| **HITL** | `hitl/review.py`, `hitl/card.py` | Browser review page (POST /review/{id}/decide) and terminal Rich card |
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

**HITLRequired** is raised before scoring if SIC classifier confidence < 0.80, or < 0.90 for ambiguous SIC codes (7389, 7999, 1521, 7011).

---

## Demo screens

| Screen | URL | What it shows |
|---|---|---|
| Submission queue | `localhost:8001/` | All submissions, scores, routing badges, filter bar |
| UW review | `localhost:8001/review/{id}` | Score tiles, composite bar, HITL decision form |
| Audit log | `localhost:8001/audit` | Tamper-evident log, integrity status, JSONL export |
| Insights | `localhost:8001/insights` | STP rate, decline drivers, SIC volume, referral SLA |

---

## Project structure

```
aria-underwriting/
├── aria/
│   ├── models.py       # Pydantic v2 data models
│   ├── scorer.py       # Deterministic scoring engine
│   ├── router.py       # Submission routing + downstream dispatch
│   ├── audit.py        # Tamper-evident JSONL audit logger
│   ├── db.py           # SQLite submission store
│   ├── llm.py          # LLM client (OpenAI / Anthropic)
│   └── server.py       # FastAPI app, UI routes, data helpers
├── hitl/
│   ├── review.py       # Browser HITL router (GET/POST /review/{id})
│   └── card.py         # Terminal Rich card with interactive prompt
├── config/
│   ├── appetite_config.yaml   # SIC tiers and ambiguous SIC list
│   ├── state_mods.yaml        # State modifiers and coastal overrides
│   ├── tiv_bands.yaml         # TIV band definitions
│   └── llm_config.yaml        # LLM provider and model selection
├── templates/
│   ├── base.html       # Shared sidebar layout, CSS, JS countdown
│   ├── queue.html      # Submission queue
│   ├── review.html     # UW review card
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

# Launch (resets data, submits 5 samples, opens browser)
.venv/bin/python run_demo.py --demo
```

The demo submits five representative submissions covering each routing path:

| Submission | SIC | State | TIV | Score | Routing |
|---|---|---|---|---|---|
| Rossi's Italian Kitchen | 5812 (A) | CA coastal | $800K | 70 | auto_pass |
| Patel Food Markets | 5411 (A) | TX inland | $300K | 100 | auto_pass |
| Harbor View Lounge | 5813 (C) | FL coastal | $1.5M | 10 | auto_decline |
| Apex Cycle & Powersports | 5571 (C) | GA | $400K | 55 | referral |
| Greenberg Builders | 1521 (C) | NY | $4.0M | 15 | auto_decline |

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
