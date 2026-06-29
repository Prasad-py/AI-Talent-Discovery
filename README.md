# Talent Scout

An **agentic, two-stage talent-discovery engine**. You describe *who* you want and
*where*; it finds undiscovered builders across the open web, builds a 360° profile of
the best, and scores each against a transparent rubric — driven from a good-looking
localhost UI with live progress.

Built on free public signals (GitHub + OpenRank, Codeforces, Hugging Face, Semantic
Scholar, Stack Overflow) with **Claude as the reasoning brain** for planning,
classification, 360° research, scoring, and outreach.

## Principles
- **Agentic & adaptive.** Claude reads your brief and derives the search plan —
  geography, sources, languages, repos, keywords, and which metrics matter. Nothing is
  hard-coded; the same engine works for "AI engineers in Bengaluru" or "Rust systems
  engineers in London".
- **Everything is a 0–1 score with evidence.** Humans review a ranked shortlist; the
  model recommends, it never auto-decides.
- **Free-first data.** Official APIs + public data only. Paid enrichment is an optional
  drop-in later.
- **Human-in-the-loop by design** (recruitment AI is high-risk under the EU AI Act; the
  UI is the decision point, not the model).

## The two-step process
1. **Discover (breadth).** From the derived plan, discover candidates from many public
   signals — GitHub (by city/language), Codeforces (country-rated), Hugging Face (model
   authors), and recent papers (Semantic Scholar) — whichever the agent selects for the
   brief. The pool is pre-ranked so only the most promising slice is deeply scored.
2. **360° profile & score (depth).** For the top candidates, investigate everywhere —
   LinkedIn, GitHub, Kaggle, Hugging Face, Stack Overflow, X, Reddit, personal sites,
   talks, papers — with recursive "dig deeper" follow-ups. Collect every public link +
   contact, then score on **12 metrics across 5 pillars** with evidence, confidence, and
   a Strong-Yes/Yes/Maybe/No recommendation.

## Setup

```bash
git clone https://github.com/Prasad-py/AI-Talent-Discovery.git
cd AI-Talent-Discovery
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

API keys are read from a `.env` in this folder (or its parent):

```
ANTHROPIC_API_KEY=...        # required (the reasoning brain)
GITHUB_TOKEN=...             # strongly recommended: GraphQL + 5000 req/hr (vs 60 unauth)
OPENAI_API_KEY=...           # optional fallback / search grounding
GEMINI_API_KEY=...           # optional fallback / search grounding
```

`.env`, `data/`, `.cache/`, and `reports/` are gitignored.

## Web UI (recommended)

```bash
./.venv/bin/python -m scout.cli serve        # → http://127.0.0.1:8000
```

Enter a plain-language **description** + **location**, optionally tweak the **model** and
advanced params (top N, research depth, pool cap, sources), and run. You'll watch it work
live — *"Understanding your brief"*, *"Searching GitHub: Bengaluru / Python"*,
*"Monitoring location specificity"*, *"Fetching from Hugging Face"*, *"Building 360
profile…"*, *"Scoring…"* — and get a ranked list with **why to choose them**, all public
data + links, contacts, and a per-metric breakdown with evidence.

By default the agent **chooses the sources** for your brief; you can force-include
specific sources or pin parameters in the Advanced panel.

## CLI (headless / power use)

```bash
python -m scout.cli check                       # validate config + API keys
python -m scout.cli init-db                      # create tables

python -m scout.cli deep-run --top 6             # full two-stage run + HTML report
python -m scout.cli list                         # show the ranked shortlist
python -m scout.cli report --top 25              # (re)generate the HTML report

# individual stages
python -m scout.cli discover-geo                 # GitHub user-search by city/language
python -m scout.cli codeforces-india             # country-rated competitive programmers
python -m scout.cli discover --limit-repos 3     # mine contributors of high-OpenRank repos
python -m scout.cli score                        # 12-metric scorecard on the pool
python -m scout.cli deep-dive --top 6            # 360° deep view on the top candidates
python -m scout.cli outreach --top 10            # personalized outreach drafts

# Stage 7 (optional): AI interview + feedback loop
python -m scout.cli interview-questions --candidate 1
python -m scout.cli interview-score --candidate 1 --transcript ./transcript.txt
python -m scout.cli outcome --candidate 1 --label hired     # advanced|hired|rejected
python -m scout.cli retrain                                 # relearn scorecard weights from outcomes
```

## Scoring rubric (12 metrics · 5 pillars)
Modeled on how talent-intelligence platforms (SeekOut, Eightfold) score — weighted,
evidence-backed factors with a recommendation:

- **Technical Craft** — engineering depth, code-quality judgment, open-source impact
- **AI Specialization** — AI/ML domain depth, ahead-of-curve adoption
- **Output & Momentum** — shipping velocity, consistency/momentum
- **Knowledge & Communication** — research depth, communication/writing, community standing
- **Fit** — authenticity (real builder vs bot/influencer), hireability (hungry, reachable,
  in-geo; arrived founders/CTOs/FAANG score low)

Each metric carries a 0–1 score, a confidence, and a one-line evidence citation. As you
record real outcomes, `retrain` relearns the weights from what actually predicted success
(written to `config/learned_weights.json`, preferred by the scorer when present).

## Configuration
Edit `config/icp.yaml` for defaults: target geography/areas, GitHub languages + target
repos, Codeforces thresholds, rubric weights, and `seeds` (known-good / known-bad GitHub
logins that calibrate cold-start scoring). The UI's brief overrides these per run.

## Layout
- `scout/intake.py` — brief → structured search plan (the agentic planner)
- `scout/sources/` — `github`, `openrank`, `codeforces`, `huggingface`, `scholar`,
  `stackexchange`, `discover` (GitHub repo + India/geo discovery)
- `scout/authenticity/` — real-builder vs bot/influencer/recruiter classifier
- `scout/enrich/profile360.py` — multi-source 360° research → cited dossier
- `scout/resolve/` — cross-platform identity resolution
- `scout/contact/` — free contact discovery
- `scout/scoring/scorecard.py` — 12-metric rubric + composite
- `scout/outreach/` — personalized message drafting
- `scout/interview/` + `scout/feedback/` — AI interview + outcome-driven weight learning
- `scout/report.py` — professional HTML report
- `scout/webjob.py` + `webapp/` — web UI server, job runner, live progress (SSE)
- `scout/pipeline.py` — headless two-stage orchestrator · `scout/cli.py` — command line

## Security & compliance
Public data via official APIs only; respects rate limits. Keep `.env` private (gitignored)
and rotate any key that was shared. Recruitment AI carries obligations (EU AI Act high-risk,
India DPDP) — the human-in-the-loop design and evidence-linked, explainable scores are there
to keep it defensible.
