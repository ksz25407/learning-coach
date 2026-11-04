# Learning Coach

A personal, local app that teaches any subject with a milestone plan.
Phase 1: Scoper → Planner → Teacher, a simple Coach, Streamlit UI, SQLite and cost logging.
Phase 2: Curator with web search, PDF uploads, and a Teacher that cites its sources.
Phase 3: Fact-checker, Pedagogy reviewer, a minimal Examiner and a blind Quiz solver.

## Setup (Windows, PowerShell)

Needs Python 3.11 or newer (`python --version`).

```powershell
cd C:\Users\<you>\learning-coach
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install -r requirements.txt
copy .env.example .env               # then edit .env and paste your ANTHROPIC_API_KEY
```

## Run

```powershell
.\.venv\Scripts\Activate.ps1
streamlit run ui/app.py
```

Your browser opens at http://localhost:8501.

## Test

```powershell
pytest
```

Tests use a fake API client, so they cost nothing.

### Fact-checker eval (real API, costs money)

```powershell
python evals/fact_check_eval.py --model opus     # 10 tuning lessons with planted errors
python evals/fact_check_eval.py --model sonnet   # compare models
python evals/fact_check_eval.py --holdout        # 3 held-out lessons: only after tuning
python evals/fact_check_eval.py --list-errors    # rewrite evals/PLANTED_ERRORS.md (free)
```

Prints caught / missed / false alarms per lesson and saves the run to `evals/results/`.
It won't start if this month's eval spend plus the run's worst case would pass
`EVAL_MONTHLY_CAP_USD` ($3). The planted errors are listed in `evals/PLANTED_ERRORS.md`.

## How it works

1. **New subject** → the Scoper asks up to 5 questions in chat.
2. You check and edit the profile, then confirm it.
3. The Planner writes 4–8 milestones, each with a testable objective.
4. **Open lesson** → the Curator searches the web for 1–2 resources, the app downloads them,
   picks the best-matching pages of your PDFs, and the Teacher writes a cited lesson.
   Then the checkers run (Phase 3, below).
5. Tick **Mark done** to unlock the next milestone.

## Sources (Phase 2)

- **PDFs** (sidebar → *PDF sources*): text is extracted on upload. Each lesson uses the pages
  that best match the objective (at most `PDF_MAX_PAGES`, default 15). Optionally give a page
  range to limit the search. Scanned PDFs (no text layer) can't be used; the upload warns you.
- **Web**: the Curator (Haiku + web search) picks 1–2 primary resources and up to 2 backups.
  Videos are shown under *Recommended resources* but not taught from. The app downloads
  pages itself (15 s timeout, max 5 MB, text capped at 8K tokens) and caches them for 30 days.
  A page that fails is replaced by a backup; *What this lesson used* lists skipped URLs and why.
  Sites that refuse downloads go in `BLOCKED_DOMAINS` in `config.py` (e.g. `autodesk.com/learn`):
  the Curator prefers other sources and the app never tries to download them.
- **Priority**: PDF beats web, web beats model knowledge. Every section is labeled with the
  sources it actually cites, e.g. `[Source: pdf, web]`, and the numbers `[1]` point to the
  Sources list at the end of the lesson.
- **Regenerate**: re-runs Curator + Teacher. The cost on the button comes from your real average
  lesson cost × 1.25 once 3 lessons exist (a rough upper formula before that). The old lesson stays
  until the new one is ready. Lessons written before Phase 2 or before your newest PDF upload
  show an **Outdated** badge.
- **Cost**: web search is $10 per 1,000 searches (max 5 per lesson), counted in the monthly
  spend. All limits are in `config.py`.
- When the database needs new columns, a backup copy is saved first as
  `db/coach.backup-<date-time>.sqlite`.

## Checkers (Phase 3)

- **Fact-checker** (Opus) and **Pedagogy reviewer** (Sonnet) run in parallel on every new lesson.
  Every flag quotes the lesson and gives evidence; a flag whose quote isn't in the lesson only
  counts as minor. Any **major** flag sends the lesson back to the Teacher (same sources), at
  most 2 times. Minor flags are listed under *Review details*.
- **Status** above each lesson: **Verified** (every section passed a source check),
  **Checked (partly unsourced)** (passed, but `[Source: model]` sections were only checked against
  the Fact-checker's own knowledge), **Flagged** (still has major problems after 2 rewrites; they
  are listed at the top), or **Unchecked** (older lesson, or the checks couldn't finish).
- **Run checks** on an unchecked lesson runs the checkers + quiz without a new web search.
- **Quiz**: the Examiner (Haiku) writes 5 questions (multiple choice, numeric with unit, or exact
  term). The Quiz solver (Sonnet) answers them from the lesson without seeing the key. Python
  compares (units converted: 2.2 kΩ = 2200 Ω). Questions it answers differently are dropped and
  listed under *Dropped questions*. Fewer than 3 left → the Examiner writes replacements (max 2
  times). Grading your own answers comes in Phase 4.
- The cost on **Regenerate** and **Run checks** shows the typical case (passes first time) and the
  maximum (2 rewrites + 2 quiz redrafts).

## Settings

Everything is in `config.py`: models per agent, effort, prices, the $10 warning and $15 stop
limits, chat history length (20), and server-side fallbacks (off).

The cost of every API call is in the `usage_log` table of `db/coach.sqlite`.
The sidebar shows this month's spend and the all-time total.
