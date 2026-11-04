# Learning Coach

A personal, local app that teaches any subject with a milestone plan.
Phase 1: Scoper → Planner → Teacher, a simple Coach, Streamlit UI, SQLite and cost logging.
Phase 2: Curator with web search, PDF uploads, and a Teacher that cites its sources.

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

## How it works

1. **New subject** → the Scoper asks up to 5 questions in chat.
2. You check and edit the profile, then confirm it.
3. The Planner writes 4–8 milestones, each with a testable objective.
4. **Open lesson** → the Curator searches the web for 1–2 resources, the app downloads them,
   picks the best-matching pages of your PDFs, and the Teacher writes a cited lesson.
   Lessons stay **Unverified** until the checkers arrive in Phase 3.
5. Tick **Mark done** to unlock the next milestone.

## Sources (Phase 2)

- **PDFs** (sidebar → *PDF sources*): text is extracted on upload. Each lesson uses the pages
  that best match the objective (at most `PDF_MAX_PAGES`, default 15). Optionally give a page
  range to limit the search. Scanned PDFs (no text layer) can't be used; the upload warns you.
- **Web**: the Curator (Haiku + web search) picks 1–2 primary resources and up to 2 backups.
  Videos are shown under *Recommended resources* but not taught from. The app downloads
  pages itself (15 s timeout, max 5 MB, text capped at 8K tokens) and caches them for 30 days.
  A page that fails is replaced by a backup; *What this lesson used* lists skipped URLs and why.
- **Priority**: PDF beats web, web beats model knowledge. Every section is labeled with the
  sources it actually cites, e.g. `[Source: pdf, web]`, and the numbers `[1]` point to the
  Sources list at the end of the lesson.
- **Regenerate**: re-runs Curator + Teacher (estimated cost on the button). The old lesson stays
  until the new one is ready. Lessons written before Phase 2 or before your newest PDF upload
  show an **Outdated** badge.
- **Cost**: web search is $10 per 1,000 searches (max 5 per lesson), counted in the monthly
  spend. All limits are in `config.py`.
- When the database needs new columns, a backup copy is saved first as
  `db/coach.backup-<date-time>.sqlite`.

## Settings

Everything is in `config.py`: models per agent, effort, prices, the $10 warning and $15 stop
limits, chat history length (20), and server-side fallbacks (off).

The cost of every API call is in the `usage_log` table of `db/coach.sqlite`.
The sidebar shows this month's spend and the all-time total.
