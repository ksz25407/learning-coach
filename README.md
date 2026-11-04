# Learning Coach

A personal, local app that teaches any subject with a milestone plan.
Phase 1: Scoper → Planner → Teacher, a simple Coach, Streamlit UI, SQLite and cost logging.

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
4. **Open lesson** → the Teacher writes the lesson once and saves it.
   Phase 1 lessons are marked **Unverified**: they come from the model's own knowledge.
5. Tick **Mark done** to unlock the next milestone.

## Settings

Everything is in `config.py`: models per agent, effort, prices, the $10 warning and $15 stop
limits, chat history length (20), and server-side fallbacks (off).

The cost of every API call is in the `usage_log` table of `db/coach.sqlite`.
The sidebar shows this month's spend and the all-time total.
