## Lab Reports Chatbot — Pipeline Flow

This document explains how a user question flows through the unified RagAnything model and the FastAPI backend, with one concrete MD/PDF example.

### High-level flow (ASCII)

```
+---------------------+
|     User Question   |
+----------+----------+
           |
           v
+-------------------------------+
| Intent Classifier (LLM)       |
| returns: sql | md | general   |
+-----+------------+------------+
      |            |
      |            |
      |            +-------------------------------+
      |                                            |
      v                                            v
+-----------------------------+        +------------------------------+
| SQL Path                    |        | MD/PDF Path                  |
| - Generate ONE safe SELECT  |        | - Resolve patient (LLM)      |
|   using DDL rules           |        |   (stop if not found)        |
| - Run on Postgres           |        | - Choose test types (LLM)    |
| - Format concise answer     |        | - Prefer parsed .md by date/ |
| - Save CSV + audit .md      |        |   type; else fallback to PDF |
+-------------+---------------+        | - Ask LLM on report text     |
              |                        | - Save audit .md             |
              |                        +---------------+--------------+
              |                                        |
              +--------------------+                   |
                                   |                   |
                                   v                   v
                             +----------------------------------+
                             |    Final Answer to the User      |
                             +----------------------------------+

General intent:
    +------------------------------+
    | GENERAL Path                 |
    | - Short 3–5 sentence reply   |
    |   from LLM knowledge only    |
    |   (no DB/files, no markdown) |
    +---------------+--------------+
                    |
                    v
            +-------------------------+
            | Final Answer to the User|
            +-------------------------+

Storage/Artifacts:
    [Postgres] users, chat_sessions, chat_messages,
               patients, reports, observations, name_evidence
    [Files]    lab_reports_final/*.pdf, data/parsed/*.md,
               data/answers/*.md, *.csv
```

### Components

- Intent classification: LLM prompt chooses exactly one of `sql`, `md`, or `general` using the DDL context.
- SQL path:
  - Generate ONE safe `SELECT` (no DDL/DML). Prefer `COUNT(DISTINCT ...)` for counts.
  - Execute against Postgres, format a short friendly sentence; save CSV and an audit `.md` under `data/answers`.
- MD/PDF path:
  - Resolve patient from question (LLM) using `data/name_index.json`. If unresolved, stop with a clear message.
  - Optionally pick test types (LLM) based on the question (e.g., T3 → Thyroid).
  - Prefer parsed Markdown files in `data/parsed` scoped by patient and optional month/year. If none match, fall back to PDF text extraction.
  - Ask LLM on the extracted report text, summarizing values/units/dates and adding brief guidance; write an audit `.md`.
- General path:
  - Provide a short 3–5 sentence explanation from LLM knowledge only (no DB/files), plain text (no markdown).
- Chat layer (FastAPI):
  - JWT signup/login, chat sessions/messages persisted to Postgres.
  - Uses a sliding context window (CHAT_CONTEXT_WINDOW) to help patient resolution across turns.
  - UI shows step-by-step metadata (intent, patient detected).

### Example (MD/PDF path)

- Question: “Explain the recent inflammation report of Mary Moo”

1) Intent classify → md
2) Patient resolution → resolves “Mary Moo” → `P10xxxx` (from `data/name_index.json`)
3) Test-type selection (LLM) → “Inflammation”
4) File selection:
   - Find `data/parsed/P10xxxx_Inflammation_YYYYMMDD_*.md` for the latest date
   - If none exist, pick the latest `lab_reports_final/P10xxxx_Inflammation_YYYYMMDD_*.pdf`
5) Answer:
   - Read the chosen `.md` (or extract text from `.pdf`), truncate to a safe size, ask LLM to:
     - Extract relevant values with units and dates
     - Compare against provided reference ranges (or general references if missing)
     - Produce a short, friendly paragraph with non‑diagnostic guidance
   - Save `data/answers/answer_<question>.md` for auditing
6) Response to user (UI): a concise narrative (no markdown) and step-by-step info:
   - Patient detected: Mary Moo
   - Intent: MD

### Example (SQL path)

- Question: “How many total patients showed up in 2024”
  - Intent classify → sql
  - SQL generated (or fallback): count distinct `reports.patient_id` where `report_date` is within 2024
  - Run query → format: “There are total of N patients.”
  - Save CSV and a small audit `.md` (SQL + row count) under `data/answers`.

### Example (GENERAL path)

- Question: “What is high blood pressure and how is it managed?”
  - Intent classify → general
  - Return a short 3–5 sentence plain-text answer (no markdown, no lists).

### Environment and configuration

- LLM config via `.env`:
  - `LLM_BINDING`, `LLM_MODEL`, `LLM_BINDING_API_KEY`, `LLM_BINDING_HOST`
  - Falls back to `OPENAI_MODEL` / `OPENAI_API_KEY` if provided
- Data locations:
  - PDFs: `lab_reports_final/*.pdf`
  - Parsed: `data/parsed/*.md`
  - Answers: `data/answers/*.md` and `*.csv`
- Chat config:
  - `JWT_SECRET`, `JWT_EXPIRES_MIN`, `CHAT_CONTEXT_WINDOW`

### Entry points

- CLI quick test:
  - `python scripts/ask.py`
- FastAPI (full chat):
  - `python -m uvicorn scripts.07_chat_api:app --host 127.0.0.1 --port 8000 --reload`


