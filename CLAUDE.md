\# Learning Coach (personal, local only)

Multi-agent app that teaches any subject with verified lessons.

Full spec: docs/SPEC.md (read it before planning any phase).



\## Commands

\- Run app: streamlit run ui/app.py

\- Tests: pytest

\- Install: pip install -r requirements.txt



\## Structure

agents/        one prompt + Pydantic schema per agent

orchestrator.py  Coach loop and pipeline

db/            SQLite schema and helpers

ui/            Streamlit app



\## Rules

\- Ask (AskUserQuestion) when anything is ambiguous; never assume.

\- Check docs.claude.com for current model IDs and API params.

\- Run tests and show output before saying something works.

\- One phase at a time; stop for my review after each.

\- Keep code simple; I'm an EE student, not a full-time dev.

\- Never commit .env or API keys.



\## Don't touch

\- .env, db/\*.sqlite (local data)

