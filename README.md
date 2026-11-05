# Learning Coach

A personal, local app that teaches any subject with a milestone plan.
Phase 1: Scoper → Planner → Teacher, a simple Coach, Streamlit UI, SQLite and cost logging.
Phase 2: Curator with web search, PDF uploads, and a Teacher that cites its sources.
Phase 3: Fact-checker, Pedagogy reviewer, a minimal Examiner and a blind Quiz solver.
Phase 4: Examiner gate (quiz + practical task), progress tracking and spaced review (py-fsrs).
Phase 5: a second subject (circuit analysis) to prove it's generic: LaTeX equations, SPICE
netlists, course-PDF pages sent as images, verified answer keys, and grading photos of
handwritten working.

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
python evals/fact_check_eval.py --model opus     # 12 tuning lessons with planted errors
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
5. Pass the **Exam** under the lesson to unlock the next milestone (Phase 4, below).

## Sources (Phase 2)

- **PDFs** (sidebar → *PDF sources*): text is extracted on upload. Each lesson uses the pages
  that best match the objective (at most `PDF_MAX_PAGES`, default 15). Optionally give a page
  range to limit the search. Pages with equations, figures or no text are sent as images
  (Phase 5, below).
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
  `db/coach.backup-<date-time>.sqlite`. Only the newest 5 backups are kept (`MAX_BACKUPS`).

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
  times). You answer the quiz in the Exam (Phase 4).
- The cost on **Regenerate** and **Run checks** shows the typical case (passes first time) and the
  maximum (2 rewrites + 2 quiz redrafts).

## Exam gate, progress and review (Phase 4)

- **Exam** (under each lesson). Both parts must pass:
  1. **Quiz**: at least 80% right (`QUIZ_PASS_PCT`). Graded by Python, free. Answers are shown
     only after you submit. For each miss you see the correct answer, why, and a link to the
     lesson section. A failed quiz needs **Get a new quiz** (fresh questions, checked by the
     Quiz solver again, a few cents). If no question survived the solver, the exam is the task alone.
  2. **Practical task**, matching your *proof of skill*: screenshots of your work (file),
     a solved problem with working, or an explanation in your own words. The rubric and the
     screenshots to take are shown up front. The **Grader** (Sonnet) gives each rubric item
     *pass / fail / can't tell* with a one-line reason. A *pass* must name the specific evidence
     it saw; otherwise it becomes *can't tell*, which asks you for another screenshot instead of
     failing. You can **override** a grade with a reason (logged and shown on the milestone).
  - A part you passed stays passed. The task stays the same between tries.
  - Last resort: after 2 submissions that couldn't be graded (`MANUAL_PASS_AFTER`), you can
    pass with a written reason.
  - The exam is disabled while the lesson is *Unchecked*. Lessons from before Phase 4 show
    **Prepare exam** (tags the quiz questions with concepts and writes the task).
- **Spaced review** (*Due today* at the top): one card per concept of a milestone. Concepts you
  miss in an exam go in right away; on a pass, all its concepts go in. Each review shows the
  question for that concept you saw least recently. Wrong = Again (back tomorrow), right = Good;
  after a right answer you can switch to Hard or Easy (only that day, only the latest review).
  At most 15 cards per day per subject (`REVIEW_DAILY_LIMIT`). Milestones ticked *done* before
  Phase 4 have an **Add to review** button.
- **Progress**: milestones passed, hours done (from the plan's estimates), projected finish at
  your hours/week, and on track or behind your **Deadline** date. Each milestone shows how it
  was passed, quiz tries and best score, task result, overrides, and time you logged with
  **Log time** (shown only; not used for the on-track check). *Attempt history* lists every try.
- Times are stored in UTC. "Today" uses your time zone (`TIMEZONE`, default America/New_York).

## Second subject: circuit analysis (Phase 5)

Nothing is hard-coded per subject; these features turn on when a subject needs them.

- **Equations** are written in LaTeX (`$V_{GS}$`, `$$...$$`) and rendered in lessons, quizzes,
  tasks, grades and chat. Dollar amounts are escaped, so cost labels stay plain text.
- **Circuits** are SPICE netlists in `spice` code blocks (you can paste them into a simulator).
  Python checks every netlist before you see it (`netlist.py`): element syntax, values (it
  catches `1M`, which SPICE reads as 1 *milli*), dependent sources, `.model` lines, ground.
  A broken netlist sends a lesson back to the Teacher, drops a quiz question, or redrafts a task.
- **Course PDFs**: on upload each page is sorted into text / equations / figure / scanned. Non-text
  pages are sent to Claude as page images (Claude reads the page's text and a picture of it),
  uploaded once with the Files API, at most `VISUAL_PDF_MAX_PAGES` (8) per lesson, about
  4,500 tokens each. Pages are still chosen by their text; a scanned page scores like the
  pages around it. Source documents are cached, so rewrites and re-checks reuse them cheaply.
  PDFs uploaded before Phase 5 are text only (re-upload to get images). Removing a PDF or a
  subject deletes its pages from the Files API too.
- **Solved-problem tasks** (proof of skill *solved_problem*): drafted by Sonnet from the lesson
  and its sources, so your course's conventions are used. The rubric has 1-2 *final value*
  items (the value is hidden from you until you pass) and 2-4 *method* items that accept any
  valid approach. A blind **Task solver** (Sonnet) works the problem without the rubric; if its
  values don't match the key, the Examiner redrafts (max `TASK_MAX_REDRAFTS`, 2). The task shows
  **Answer key verified** or **unverified**.
- **Handwritten working**: upload photos (JPG, PNG or iPhone HEIC; they're turned upright
  automatically), one per page. The Grader reads your working and quotes the lines it judged;
  illegible parts become *can't tell* with the part to re-photograph. The Grader only *reads*
  your final value; Python checks it against the key with the tolerance, like a quiz answer.
- **Quiz units** include compound units (`µA/V²`, `V/V`, `1/s`), and a bare `m` means milli
  unless metres are expected.
- **Test on a scratch database**: set `COACH_DB_PATH` to use another database file, so your
  real subjects and spend are untouched:

  ```powershell
  $env:COACH_DB_PATH = "C:\temp\coach-test.sqlite"
  streamlit run ui/app.py
  Remove-Item Env:COACH_DB_PATH     # back to db/coach.sqlite
  ```

## Settings

Everything is in `config.py`: models per agent, effort, prices, the $10 warning and $15 stop
limits, chat history length (20), and server-side fallbacks (off).

The cost of every API call is in the `usage_log` table of `db/coach.sqlite`.
The sidebar shows this month's spend and the all-time total.
