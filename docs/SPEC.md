\# Project: Personal AI Learning Coach (local, personal use only)



\## Goal

Build a local app that helps me learn ANY new subject with clear objectives, minimal wasted time, and verified material. It runs only on my Windows laptop. No commercial use, no multi-user, no auth, no cloud hosting. My first test subject: Revit, for an off-grid house I'm designing with a friend.



\## Before you write any code

1\. Use AskUserQuestion (or Plan mode) to clarify anything ambiguous. Do not assume. Ask one question at a time, multiple choice when possible.

2\. Check the current Claude API docs (https://docs.claude.com) for exact model IDs, parameters, and feature names. Do not rely on memory.

3\. Propose the folder structure and a Phase 1 plan, then wait for my approval.



\## Architecture (5 layers)

1\. UI: Streamlit on localhost. Chat panel + milestone checklist with progress. Sidebar to switch subjects, start a new one, and delete the selected one (delete needs a second "Are you sure?" click).

2\. Coach (orchestrator): the only agent I talk to. Routes tasks to other agents, keeps the plan, and only unlocks the next milestone after I pass the Examiner.

3\. Builder agents: Scoper, Planner, Curator, Teacher, Examiner.

4\. Checker agents: Fact-checker, Pedagogy reviewer, Quiz solver.

5\. Infrastructure: SQLite, verified-lesson cache, web search tool.



\## Agents (each = one system prompt + fixed JSON output, except Teacher)

\- Scoper: asks me 3–5 questions (goal, current level, hours/week, deadline, units/tools) and writes a subject profile:

&#x20; {subject, goal, level, hours\_per\_week, deadline, practice\_type: "software"|"math"|"language"|"hands-on", proof\_of\_skill: "file"|"solved\_problem"|"explain\_back"}

\- Planner: 4–8 milestones, each with a TESTABLE objective (e.g., "Model ground-floor walls from a sketch in 20 min"), never vague ("understand walls").

\- Curator: web search; max 1–2 resources per objective; each must include URL, exact section/timestamps, and why it beats alternatives. Flag anything >30 min.

\- Teacher: writes the lesson using ONLY Curator sources, with citations (Citations API). Returns prose, not JSON.

\- Examiner: writes a short quiz/task matching practice\_type.

\- Fact-checker: gets the lesson + sources (not the Teacher's reasoning); flags unsupported or wrong claims.

\- Pedagogy reviewer: checks alignment with the objective, fit for my level, and length.

\- Quiz solver: solves the Examiner's quiz blind and compares with the answer key; questions where they disagree are thrown out.



\## Pipeline per objective

Curator → Teacher → Fact-checker → Pedagogy reviewer → Examiner → Quiz solver → shown to me.

If a check fails, it goes back to the builder. Max 2 revision rounds, then flag it to me instead of looping.



\## Must stay subject-agnostic

No subject is hard-coded. Every agent reads the subject profile. Revit is just the first test; circuit analysis is the second.



\## API, cost and accuracy requirements

\- Use the Claude API with ANTHROPIC\_API\_KEY from .env (never hard-code or commit it).

\- Model tiers (confirm current IDs in docs): Haiku for Curator and Examiner drafting; Sonnet for Coach, Teacher, Pedagogy reviewer; Opus or Sonnet for Fact-checker and Quiz solver (make this a config setting).

\- Prompt caching on each agent's system prompt + subject profile (stable prefix first).

\- Structured outputs (JSON schema, Pydantic models) for all agents except the Teacher. Note: Citations and Structured Outputs are incompatible, so the Teacher returns cited prose.

\- Batch API for pre-generating and pre-checking upcoming lessons (non-urgent work).

\- Token counting before large requests; context editing for the Coach's long sessions.

\- Files API for uploaded PDFs/manuals so they aren't resent to every agent.

\- Shared state lives in SQLite so agents don't redo each other's work.

\- Verified-lesson cache with local vector search (Chroma) so a topic is never generated twice.

\- Log tokens and cost per agent per call to SQLite; show running total and this month's spend in the UI; warn me at $10 and stop new lessons at $15 (configurable).



\## Data (SQLite)

profiles, milestones, objectives, lessons (verified flag, sources), quiz\_results, review\_schedule (spaced repetition via py-fsrs), usage\_log.

Deleting a subject removes its profile, milestones, objectives, lessons, quiz results, review schedule and chat messages, but keeps its usage\_log rows so monthly and all-time spend stay accurate.



\## Tech stack

Windows 11, PowerShell. Python 3.11+, anthropic SDK, Pydantic, SQLite, Chroma, py-fsrs, Streamlit. Use a virtual environment (venv). requirements.txt + README with Windows setup steps.



\## Build order (stop after each phase for my review)

\- Phase 1: Scoper → Planner → Teacher + simple Coach loop + Streamlit UI + SQLite + cost logging.

\- Phase 2: Curator with web search; Teacher must cite.

\- Phase 3: Three checker agents with the 2-round retry limit.

\- Phase 4: Examiner gate, progress tracking, spaced review.

\- Phase 5: Test with a second subject to prove it's generic.



\## Testing

Keep 10 known-correct lessons and plant errors in some. The Fact-checker must catch the planted errors; if it misses any, tighten its prompt. Add pytest tests for the pipeline logic and JSON schemas.



\## Working rules for you (Claude)

\- Ask, don't assume, when requirements are unclear.

\- Don't claim something works without running it and showing the evidence.

\- Keep the code simple and readable; I'm an EE student, not a full-time software developer.

\- Explain each phase briefly when done: what was built, what to test, what's next.

