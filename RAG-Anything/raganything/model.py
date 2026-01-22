"""
Unified RAG-Anything model for lab reports Q&A.

This module consolidates all helper routing functions and answer logic into a
single class, RagAnything. It supports three intents:
 - 'sql'     : Generate and execute Postgres SELECT queries using patients/reports
 - 'md'      : Answer from parsed report content (markdown preferred, PDF fallback)
 - 'general' : Short, professional general medical information without retrieval

Key capabilities:
 - Robust patient resolution from free-form questions (handles swapped names, minor typos)
 - Test-type selection (e.g., "T3" -> "Thyroid") to narrow relevant reports
 - Preference for using already-parsed markdown files; falls back to PDF text extraction
 - Friendly, concise answers for SQL and GENERAL intents
 - Markdown audit artifacts for both SQL and MD flows
"""
from __future__ import annotations

import os
import re
import csv
import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import subprocess
import sys

from dotenv import load_dotenv
from pypdf import PdfReader
from openai import OpenAI
import psycopg



# Resolve project root relative to this file: RAG-Anything/
ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

NAME_INDEX_PATH = ROOT / "data" / "name_index.json"
PDF_ROOT = Path(os.getenv("REPORTS_SRC", ROOT / "lab_reports_final"))
PARSE_DIR = Path(os.getenv("PARSE_OUTPUT_DIR", ROOT / "data" / "parsed"))
ANSWERS_DIR = ROOT / "data" / "answers"
ANSWERS_DIR.mkdir(parents=True, exist_ok=True)

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12
}

DDL_SCHEMA_DOC = """
Tables:

patients(patient_id TEXT PRIMARY KEY, full_name TEXT, first_seen DATE, last_seen DATE)
reports(report_id TEXT PRIMARY KEY, patient_id TEXT NOT NULL, test_type TEXT, report_date DATE, src_path TEXT, pages INT)

Rules:
- Generate ONE safe SELECT query only (no DDL/insert/update/delete).
- Use only the above columns; GROUP BY/ORDER BY/LIMIT are ok.
- For counts, ALWAYS use COUNT(DISTINCT <primary_key>).
"""


class RagAnything:
    """
    Unified RAG orchestrator for lab reports Q&A.

    Typical usage:
        rag = RagAnything()
        print(rag.ask("How many patients are in total?"))
        print(rag.ask("Tell me the T3 value of Mark Lewis in the recent test"))
        print(rag.ask("I have high blood pressure, what should I do?"))
    """

    def __init__(self) -> None:
        # LLM binding via .env
        # Prefer LLM_* variables; fall back to OPENAI_* for backward compatibility
        self.llm_binding = (os.getenv("LLM_BINDING", "openai") or "openai").lower()
        self.model_name = (
            os.getenv("LLM_MODEL")
            or os.getenv("OPENAI_MODEL")
            or "gpt-4o-mini"
        )
        self.llm_api_key = os.getenv("LLM_BINDING_API_KEY") or os.getenv("OPENAI_API_KEY")
        self.llm_host = os.getenv("LLM_BINDING_HOST")  # optional base_url for OpenAI-compatible endpoints

    def _get_client(self) -> OpenAI:
        """
        Construct an OpenAI client honoring LLM_* .env variables.
        - If LLM_BINDING_HOST is set, use it as base_url (for OpenAI-compatible hosts).
        - If LLM_BINDING_API_KEY is set, pass it explicitly; otherwise rely on environment.
        """
        kwargs = {}
        if self.llm_host:
            kwargs["base_url"] = self.llm_host
        if self.llm_api_key:
            kwargs["api_key"] = self.llm_api_key
        return OpenAI(**kwargs)  # type: ignore[arg-type]

    # -------------------------
    # Name and question parsing
    # -------------------------
    @staticmethod
    def normalize_person_name(s: str) -> str:
        s = s.strip()
        if "," in s:
            parts = [p.strip() for p in s.split(",", 1)]
            if len(parts) == 2:
                last = parts[0]
                first_and_more = parts[1]
                s = f"{first_and_more} {last}"
        s = re.sub(r"\s+", " ", s)
        return s.lower()

    @staticmethod
    def load_name_index() -> Dict[str, str]:
        if not NAME_INDEX_PATH.exists():
            raise FileNotFoundError(f"Name index not found: {NAME_INDEX_PATH}")
        data = json.loads(NAME_INDEX_PATH.read_text(encoding="utf-8"))
        name_to_pid: Dict[str, str] = {}
        for pid, name in data.items():
            nm = RagAnything.normalize_person_name(name)
            name_to_pid.setdefault(nm, pid)
            if " " in name:
                first, *mid_and_last = name.split()
                last = mid_and_last[-1] if mid_and_last else ""
                last_first = f"{last}, {first}"
                name_to_pid.setdefault(RagAnything.normalize_person_name(last_first), pid)
        return name_to_pid

    @staticmethod
    def load_pid_to_name() -> Dict[str, str]:
        if not NAME_INDEX_PATH.exists():
            return {}
        return json.loads(NAME_INDEX_PATH.read_text(encoding="utf-8"))

    @staticmethod
    def parse_question(q: str) -> Tuple[Optional[str], Optional[int], Optional[int]]:
        qlow = q.lower()
        analyte_key = None
        month = None
        for mname, mnum in MONTHS.items():
            if mname in qlow:
                month = mnum
                break
        year = None
        m = re.search(r"(20\d{2})", qlow)
        if m:
            year = int(m.group(1))
        return analyte_key, month, year

    @staticmethod
    def extract_name_from_question(q: str) -> Optional[str]:
        m = re.search(r"for\s+([A-Za-z][A-Za-z\.\-\'\s,]+?)(?:\s+in\s+\w+|\s+on\s+\w+|\s+for\s+\w+|$)", q, flags=re.I)
        if m:
            return m.group(1).strip()
        m = re.search(r"(?:of|about)\s+([A-Za-z][A-Za-z\.\-\'\s,]+?)(?:\s+in\s+\w+|\s+on\s+\w+|$)", q, flags=re.I)
        if m:
            return m.group(1).strip()
        return None

    @staticmethod
    def find_patient_id(name_to_pid: Dict[str, str], raw_name: str) -> Optional[str]:
        nm = RagAnything.normalize_person_name(raw_name)
        if nm in name_to_pid:
            return name_to_pid[nm]
        toks = nm.split()
        if len(toks) >= 2:
            first = toks[0]
            last = toks[-1]
            trial = f"{first} {last}"
            if trial in name_to_pid:
                return name_to_pid[trial]
        return None

    # -------------------------
    # File/filename utilities
    # -------------------------
    @staticmethod
    def parse_date_from_filename(fname: str) -> Optional[Tuple[int, int, int]]:
        m = re.search(r"_(\d{8})(?:_|\.pdf$|\.md$)", fname)
        if not m:
            return None
        dt8 = m.group(1)
        y, mth, d = int(dt8[:4]), int(dt8[4:6]), int(dt8[6:8])
        return (y, mth, d)

    @staticmethod
    def parse_test_type_from_filename(fname: str) -> Optional[str]:
        m = re.match(r"^P\d+_([^_]+(?:_[^_]+)*)_(\d{8})", fname)
        if not m:
            return None
        return m.group(1)

    @staticmethod
    def list_patient_files(pid: str) -> List[Tuple[Path, str, Optional[str], Optional[Tuple[int, int, int]]]]:
        out: List[Tuple[Path, str, Optional[str], Optional[Tuple[int, int, int]]]] = []
        if PARSE_DIR.exists():
            for p in PARSE_DIR.glob(f"{pid}_*.md"):
                tt = RagAnything.parse_test_type_from_filename(p.name)
                trip = RagAnything.parse_date_from_filename(p.name)
                out.append((p, "md", tt, trip))
        for p in PDF_ROOT.glob(f"{pid}_*.pdf"):
            tt = RagAnything.parse_test_type_from_filename(p.name)
            trip = RagAnything.parse_date_from_filename(p.name)
            out.append((p, "pdf", tt, trip))
        return out

    # -------------------------
    # LLM helpers (routing)
    # -------------------------
    def llm_classify_intent(self, question: str) -> Optional[str]:
        try:
            client = self._get_client()
            system = (
                "You are an intent classifier for lab-report Q&A. Choose one of: sql, md, general.\n"
                "- sql: answerable using ONLY the given Postgres tables (patients, reports) with a simple SELECT.\n"
                "- md: needs parsed report (PDF/markdown) content.\n"
                "- general: general medical information or definitions not tied to DB or a specific report.\n"
                "Return only one token: sql, md, or general."
            )
            user = f"DDL:\n{DDL_SCHEMA_DOC}\n\nQuestion:\n{question}\n\nReturn exactly one token: sql or md or general."
            resp = client.chat.completions.create(
                model=self.model_name,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                temperature=0,
            )
            label = (resp.choices[0].message.content or "").strip().lower()
            if "sql" in label and "md" not in label:
                return "sql"
            if "md" in label and "sql" not in label:
                return "md"
            if "general" in label:
                return "general"
        except Exception:
            pass
        return None

    def llm_resolve_patient(self, question: str, pid_to_name: Dict[str, str]) -> Tuple[Optional[str], Optional[str]]:
        if not pid_to_name:
            return None, None
        client = self._get_client()
        system = (
            "You are a patient name resolver. Given a user question and a mapping of patient_id to full_name, "
            "identify the single best matching patient for the question. "
            "The patient name might be in order of lastname firstname also in the question"
            "There might be typo/spelling mistake in he patient name in the question"
            "Return a strict JSON object: {\"patient_id\": <id or null>, \"full_name\": <name or null>}. "
            "Only return a patient_id that exists in the provided mapping. If ambiguous or none, return nulls."
        )
        user = (
            f"Question:\n{question}\n\n"
            f"name_index (JSON of {{patient_id: full_name}}):\n{pid_to_name}\n\n"
            "Output only the json with patient id with correct name of the patient."
            "Expected format: {\"patient_id\": <id>, \"full_name\": <name>}"
        )
        try:
            resp = client.chat.completions.create(
                model=self.model_name,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                temperature=0,
            )
            raw = (resp.choices[0].message.content or "").strip()
            data = json.loads(raw)
            pid = data.get("patient_id")
            full_name = data.get("full_name")
            if pid in pid_to_name:
                return pid, pid_to_name.get(pid, full_name)
        except Exception:
            pass
        return None, None

    def llm_choose_test_types(self, question: str, available_types: List[str]) -> List[str]:
        if not available_types:
            return []
        client = self._get_client()
        system = (
            "You are a lab test router. Given a user question (which may include conversation history) and a list of test types "
            "(e.g., LFT, CBC, Thyroid, Lipid_Profile, Iron_Studies, CMP, RFT, Vitamin, Inflammation), "
            "select which test types are RELEVANT to answer the question. "
            "Key analyte mappings:\n"
            "- Thyroid: T3, T4, TSH\n"
            "- Lipid_Profile: Cholesterol, LDL, HDL, Triglycerides\n"
            "- LFT (Liver): ALT, AST, ALP, Bilirubin, GGT, Albumin\n"
            "- RFT (Renal/Kidney): Creatinine, BUN, Urea, eGFR, Uric Acid\n"
            "- CBC (Blood): Hemoglobin, WBC, RBC, Platelets, Hematocrit\n"
            "- CMP/Diabetes: Glucose, HbA1c (Note: Creatinine may also be in CMP but prefer RFT)\n"
            "- Inflammation: CRP, ESR\n"
            "\n"
            "IMPORTANT: If the question references values mentioned in [Previous conversation], "
            "use the conversation context to determine which test type contains that value. "
            "For follow-up questions like 'is it normal?', 'is that high?', look at what test/value "
            "was discussed in the previous conversation.\n"
            "\n"
            "Return only a JSON array of matching test type strings from the provided list; "
            "if none, return an empty JSON array."
        )
        user = (
            f"Question:\n{question}\n\n"
            f"Available test types:\n{json.dumps(available_types)}\n\n"
            "Your answer must be a pure JSON array, e.g., [\"Thyroid\"] or [\"RFT\"]."
        )
        try:
            resp = client.chat.completions.create(
                model=self.model_name,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                temperature=0,
            )
            raw = (resp.choices[0].message.content or "").strip()
            data = json.loads(raw)
            if isinstance(data, list):
                s = set(available_types)
                return [t for t in data if t in s]
        except Exception:
            pass
        return []

    def llm_select_report(self, question: str, name_index: Dict[str, str], md_files: List[str]) -> Tuple[Optional[str], Optional[str], Optional[int], Optional[int]]:
        client = self._get_client()
        system = (
            "You are a routing assistant. Given a user question, a mapping of patient_id to full_name, "
            "and a list of available parsed markdown report filenames, you must: "
            "1) Identify the patient's name from the question, allowing swapped order and minor misspellings. "
            "2) Resolve the patient_id using the provided name_index JSON; if uncertain, return null. "
            "3) Extract target month and year from the question if present (month in 1-12). "
            "4) Choose the best report filename from the list that starts with patient_id and matches the month/year token YYYYMMDD; "
            "   if month/year are missing, pick the most relevant/latest. "
            "Return a strict JSON object with keys: patient_id, report_md, month, year."
        )
        content = (
            "Question:\n"
            f"{question}\n\n"
            "name_index (JSON of {patient_id: full_name}):\n"
            f"{json.dumps(name_index)[:60000]}\n\n"
            "parsed md files (one per line):\n"
            + "\n".join(md_files[:])
        )
        resp = client.chat.completions.create(
            model=self.model_name,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": content}],
            temperature=0,
        )
        raw = resp.choices[0].message.content or "{}"
        try:
            data = json.loads(raw)
            pid = data.get("patient_id")
            rmd = data.get("report_md")
            month = data.get("month")
            year = data.get("year")
            return pid, rmd, int(month) if month else None, int(year) if year else None
        except Exception:
            return None, None, None, None

    # -------------------------
    # PDF/MD reading and LLM answering
    # -------------------------
    @staticmethod
    def choose_pdf_for_date(pid: str, month: Optional[int], year: Optional[int]) -> Optional[Path]:
        cands = []
        for p in PDF_ROOT.glob(f"{pid}_*.pdf"):
            trip = RagAnything.parse_date_from_filename(p.name)
            if not trip:
                continue
            y, mth, d = trip
            if year and y != year:
                continue
            if month and mth != month:
                continue
            cands.append((y, mth, d, p))
        if not cands and (month is None and year is None):
            for p in PDF_ROOT.glob(f"{pid}_*.pdf"):
                trip = RagAnything.parse_date_from_filename(p.name)
                if trip:
                    y, mth, d = trip
                    cands.append((y, mth, d, p))
        if not cands:
            return None
        cands.sort()
        return cands[-1][3]

    @staticmethod
    def extract_text_from_pdf(pdf_path: Path, max_chars: int = 20000) -> str:
        try:
            reader = PdfReader(str(pdf_path))
            buf = []
            for page in reader.pages:
                t = page.extract_text() or ""
                buf.append(t)
                if sum(len(x) for x in buf) > max_chars:
                    break
            text = "\n".join(buf)
            if len(text) > max_chars:
                text = text[:max_chars]
            return text
        except Exception as e:
            return f"(Failed to extract text from PDF: {e})"

    def ask_openai(self, question: str, context: str) -> str:
        client = self._get_client()
        system_prompt = (
            "You are a careful medical report reader. "
            "Use ONLY the provided report text to answer the user's question. "
            "Extract the requested lab analyte value(s) with units and date. "
            "If multiple values exist for the timeframe, pick the closest match. "
            "If not present, say 'Not found in report text.' "
            "Then provide a short, friendly narrative summary (2-4 sentences) instead of bullets: "
            "1) If the report includes a reference range, classify the result as below/within/above range. "
            "2) If no range is present, use common adult reference ranges from general medical knowledge and state that these are general references. "
            "3) Add one brief, non-diagnostic guidance sentence (if normal: 'Within typical range; no immediate action needed.'; "
            "if abnormal: suggest brief next steps). "
            "Do not include file paths, headings, or markdown formatting; reply as fluent sentences."
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Question: {question}\n\nReport text:\n{context}"},
        ]
        resp = client.chat.completions.create(
            model=self.model_name,
            messages=messages,
            temperature=0,
        )
        return resp.choices[0].message.content.strip()

    def ask_openai_from_md_files(self, question: str, file_to_text: List[Tuple[str, str]]) -> str:
        client = self._get_client()
        system_prompt = (
            "You are a careful medical report reader. You may receive multiple report excerpts. "
            "Use ONLY the provided text to answer. Include values, units, and dates. "
            "If multiple reports match the timeframe, summarize across them in a short, friendly narrative (2-4 sentences). "
            "Then provide a concise interpretation: "
            "1) Prefer report-provided reference ranges to label results as below/within/above range. "
            "2) If no ranges exist in the text, use general adult reference ranges and clearly state they are general references. "
            "3) Add one brief, non-diagnostic guidance sentence (normal vs abnormal). "
            "Be succinct and cautious; do not diagnose. Do not include file paths, headings, or markdown formatting."
        )
        parts = []
        for fname, txt in file_to_text:
            parts.append(f"--- File: {fname} ---\n{txt}\n")
        context = "\n".join(parts)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Question: {question}\n\nReports:\n{context}"},
        ]
        resp = client.chat.completions.create(
            model=self.model_name,
            messages=messages,
            temperature=0,
        )
        return resp.choices[0].message.content.strip()

    @staticmethod
    def build_parsed_md_listing() -> List[str]:
        files = []
        if PARSE_DIR.exists():
            for p in sorted(PARSE_DIR.glob("*.md")):
                files.append(p.name)
        return files

    @staticmethod
    def write_md_answer(question: str, patient_id: str, chosen_file: str, answer_text: str) -> Path:
        ANSWERS_DIR.mkdir(parents=True, exist_ok=True)
        safe_q = re.sub(r"[^a-zA-Z0-9]+", "_", question)[:80].strip("_")
        out = ANSWERS_DIR / f"answer_{safe_q or 'query'}.md"
        md = [
            f"# Answer\n",
            f"Question: {question}\n\n",
            f"- Patient ID: {patient_id or 'unknown'}\n",
            f"- Report file: {chosen_file or 'unknown'}\n\n",
            f"---\n\n",
            answer_text.strip(),
            "\n",
        ]
        out.write_text("".join(md), encoding="utf-8")
        return out

    # -------------------------
    # SQL generation and execution
    # -------------------------
    @staticmethod
    def open_pg_conn():
        dsn = (
            f"host={os.getenv('POSTGRES_HOST','localhost')} "
            f"port={os.getenv('POSTGRES_PORT','5432')} "
            f"dbname={os.getenv('POSTGRES_DATABASE')} "
            f"user={os.getenv('POSTGRES_USER')} "
            f"password={os.getenv('POSTGRES_PASSWORD')}"
        )
        return psycopg.connect(dsn)

    def llm_generate_sql(self, question: str, resolved_pid: Optional[str] = None, resolved_name: Optional[str] = None) -> Optional[str]:
        client = self._get_client()
        hint = ""
        if resolved_pid:
            hint = (
                f"\nResolved patient context:\n"
                f"- resolved_patient_id: {resolved_pid}\n"
                f"- resolved_full_name: {resolved_name or ''}\n"
                "When resolved_patient_id is provided, you MUST filter by patients.patient_id = '<id>' "
                "instead of fuzzy name matching.\n"
            )
        # Timeframe hint: interpret "showed up in YEAR" as any report in that calendar year
        year_hint = ""
        m_year = re.search(r"\b(20\d{2})\b", question)
        if m_year:
            y = int(m_year.group(1))
            y1 = y + 1
            year_hint = (
                "\nTimeframe hint:\n"
                f"- If the question says 'showed up in {y}', interpret as patients having at least one report in that year.\n"
                f"- Implement with reports.report_date >= DATE '{y}-01-01' AND reports.report_date < DATE '{y1}-01-01'.\n"
                "- When counting patients across reports/timeframes, use COUNT(DISTINCT patients.patient_id) (or DISTINCT reports.patient_id).\n"
                "- Join reports to patients on reports.patient_id = patients.patient_id when needed.\n"
            )
        prompt = (
            "You are a SQL generator for Postgres. Produce ONE safe SELECT query for the following question.\n"
            f"{DDL_SCHEMA_DOC}\n"
            f"{hint}{year_hint}\n"
            f"Question:\n{question}\n"
            "Output only the SQL, nothing else."
        )
        resp = client.chat.completions.create(
            model=self.model_name,
            messages=[
                {"role": "system", "content": "Return only a single SQL SELECT statement for Postgres."},
                {"role": "user", "content": prompt},
            ],
            temperature=0,
        )
        raw = (resp.choices[0].message.content or "").strip()
        sql = self._clean_sql_text(raw)
        if not re.match(r"(?is)^\s*select\b", sql):
            return None
        if ";" in sql.strip().rstrip(";"):
            return None
        if re.search(r"(?i)\b(drop|alter|insert|update|delete|grant|revoke|call|copy)\b", sql):
            return None
        return sql

    @staticmethod
    def _clean_sql_text(text: str) -> str:
        """
        Normalize LLM output to raw SQL:
        - Strip markdown code fences and 'sql' language tags
        - Extract from first 'select' token to end
        - Trim whitespace
        """
        t = text.strip()
        # Remove triple backtick fences if present
        if t.startswith("```"):
            # Remove starting fence
            t = re.sub(r"^```(?:sql)?\s*", "", t, flags=re.I)
            # Remove ending fence
            t = re.sub(r"\s*```$", "", t)
        # Extract from the first 'select'
        m = re.search(r"(?is)\bselect\b", t)
        if m:
            t = t[m.start():]
        return t.strip()

    @staticmethod
    def run_sql(sql: str) -> Tuple[List[str], List[tuple]]:
        with RagAnything.open_pg_conn() as conn, conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
            headers = [desc[0] for desc in cur.description] if cur.description else []
        return headers, rows

    @staticmethod
    def save_csv(question: str, headers: List[str], rows: List[tuple]) -> Path:
        safe_q = re.sub(r"[^a-zA-Z0-9]+", "_", question)[:80].strip("_")
        out = ANSWERS_DIR / f"sql_{safe_q or 'query'}.csv"
        with out.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if headers:
                w.writerow(headers)
            for r in rows:
                w.writerow(list(r))
        return out

    @staticmethod
    def _normalize_plain_text(text: str, max_sentences: int = 5) -> str:
        import re as _re
        t = text.replace("`", " ").replace("*", " ").replace("_", " ")
        lines = []
        for ln in t.splitlines():
            ln = _re.sub(r"^\s*[-•]\s+", "", ln)
            ln = _re.sub(r"^\s*\d+\.\s+", "", ln)
            if ln.strip():
                lines.append(ln.strip())
        t = " ".join(lines)
        t = _re.sub(r"\s+", " ", t).strip()
        sentences = _re.split(r"(?<=[\.!?])\s+", t)
        sentences = [s.strip() for s in sentences if s.strip()]
        if max_sentences and len(sentences) > max_sentences:
            sentences = sentences[:max_sentences]
        return " ".join(sentences)

    def llm_general_answer(self, question: str) -> str:
        client = self._get_client()
        system = (
            "You provide general medical information and education. "
            "Answer ONLY from your own knowledge; do not reference any files or databases. "
            "Style requirements:\n"
            "- 3 to 5 short sentences total\n"
            "- Professional, neutral tone\n"
            "- No markdown, no bullets, no numbered lists, no headings, no asterisks\n"
            "- Include a brief non‑diagnostic disclaimer if appropriate"
        )
        resp = client.chat.completions.create(
            model=self.model_name,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": question}],
            temperature=0.1,
        )
        raw = resp.choices[0].message.content.strip()
        return self._normalize_plain_text(raw, max_sentences=5)

    # -------------------------
    # Top-level answer flows
    # -------------------------
    def answer_md(self, question: str) -> str:
        pid_to_name = self.load_pid_to_name()
        name_to_pid = self.load_name_index()
        qlow = question.lower()

        pid: Optional[str] = None
        detected_name: Optional[str] = None
        llm_pid, llm_name = self.llm_resolve_patient(question, pid_to_name)
        if llm_pid:
            pid = llm_pid
            detected_name = llm_name or pid_to_name.get(llm_pid)
        else:
            for nm, p in name_to_pid.items():
                if nm in qlow:
                    pid = p
                    detected_name = pid_to_name.get(p, None)
                    break
                parts = nm.split()
                if len(parts) >= 2:
                    first = parts[0]; last = parts[-1]
                    lf = f"{last}, {first}".lower()
                    if lf in qlow:
                        pid = p
                        detected_name = pid_to_name.get(p, None)
                        break
            if not pid:
                raw_name = self.extract_name_from_question(question) or ""
                pid = self.find_patient_id(name_to_pid, raw_name) if raw_name else None
                if pid and not detected_name:
                    detected_name = pid_to_name.get(pid, raw_name or None)
        print(f"Patient name - {detected_name or 'None'}")
        print(f"Patient id - {pid or 'None'}")

        analyte_key, month, year = self.parse_question(question)
        if not pid:
            return "I'm not able to identify the patient name or ID. Please include the full name or patient ID and try again."

        def md_matches_for(_pid: str, _month: Optional[int], _year: Optional[int]) -> List[Path]:
            matches = []
            if _pid and PARSE_DIR.exists():
                for p in PARSE_DIR.glob(f"{_pid}_*.md"):
                    trip = RagAnything.parse_date_from_filename(p.name)
                    if not trip:
                        continue
                    y, mth, d = trip
                    if _year and y != _year:
                        continue
                    if _month and mth != _month:
                        continue
                    matches.append(p)
            return matches

        all_files = self.list_patient_files(pid)
        available_types = sorted({tt for _, _, tt, _ in all_files if tt})
        if available_types:
            print(f"Available test types - {available_types}")
        chosen_types = self.llm_choose_test_types(question, available_types) if available_types else []
        
        # Fallback: If no test types chosen and question contains conversation history,
        # try to extract test type from previous conversation
        if not chosen_types and "[Previous conversation]" in question:
            try:
                # Look for common test type names in the conversation history
                question_lower = question.lower()
                for test_type in available_types:
                    # Match test type names (e.g., "RFT", "LFT", "CBC")
                    if test_type.lower() in question_lower:
                        chosen_types.append(test_type)
                        print(f"Inferred test type from conversation history - {test_type}")
                        break
                    # Match common analytes to test types
                    analyte_mappings = {
                        "creatinine": "RFT", "bun": "RFT", "urea": "RFT", "egfr": "RFT",
                        "alt": "LFT", "ast": "LFT", "bilirubin": "LFT", "albumin": "LFT",
                        "t3": "Thyroid", "t4": "Thyroid", "tsh": "Thyroid",
                        "ldl": "Lipid_Profile", "hdl": "Lipid_Profile", "cholesterol": "Lipid_Profile",
                        "hemoglobin": "CBC", "wbc": "CBC", "platelet": "CBC",
                        "glucose": "Diabetes", "hba1c": "Diabetes"
                    }
                    for analyte, test_type_map in analyte_mappings.items():
                        if analyte in question_lower and test_type_map in available_types:
                            chosen_types.append(test_type_map)
                            print(f"Inferred test type from analyte '{analyte}' in conversation history - {test_type_map}")
                            break
                    if chosen_types:
                        break
            except Exception as e:
                print(f"Warning: Could not infer test type from conversation history: {e}")
        
        if chosen_types:
            print(f"Chosen test types - {chosen_types}")

        md_files = md_matches_for(pid, month, year)
        if chosen_types:
            md_files = [p for p in md_files if (RagAnything.parse_test_type_from_filename(p.name) or "") in chosen_types]
        
        # Traditional file-based retrieval
        if md_files:
            print(f"MD files matched - {len(md_files)}")
            file_to_text = []
            for p in sorted(md_files):
                txt = p.read_text(encoding="utf-8", errors="ignore")[:8000]
                file_to_text.append((p.name, txt))
            ans = self.ask_openai_from_md_files(question, file_to_text)
            self.write_md_answer(question, pid, ", ".join(p.name for p in md_files), ans)
            return ans

        pdf_path = None
        if pid:
            if chosen_types:
                candidates = []
                for p in PDF_ROOT.glob(f"{pid}_*.pdf"):
                    tt = RagAnything.parse_test_type_from_filename(p.name) or ""
                    if tt not in chosen_types:
                        continue
                    trip = RagAnything.parse_date_from_filename(p.name)
                    if not trip:
                        continue
                    y, mth, d = trip
                    if year and y != year:
                        continue
                    if month and mth != month:
                        continue
                    candidates.append((y, mth, d, p))
                if candidates:
                    candidates.sort()
                    pdf_path = candidates[-1][3]
            if not pdf_path:
                pdf_path = self.choose_pdf_for_date(pid, month, year)
        if pid and pdf_path:
            print(f"Using PDF - {pdf_path.name}")
            report_text = self.extract_text_from_pdf(pdf_path)
            q = f"{question}\n\nPatient ID: {pid}\nReport file: {pdf_path.name}"
            ans = self.ask_openai(q, report_text)
            self.write_md_answer(question, pid, pdf_path.name, ans)
            return ans

        md_files_listing = self.build_parsed_md_listing()
        pid_to_name = self.load_pid_to_name()
        llm_pid, rmd, mth, yr = self.llm_select_report(question, pid_to_name, md_files_listing)
        if not llm_pid or not rmd:
            return self.ask_router(question)

        target_month = mth or month
        target_year = yr or year
        md_list = []
        for p in PARSE_DIR.glob(f"{llm_pid}_*.md"):
            trip = RagAnything.parse_date_from_filename(p.name)
            if not trip:
                continue
            y, mth2, d = trip
            if target_year and y != target_year:
                continue
            if target_month and mth2 != target_month:
                continue
            md_list.append(p)
        if not md_list:
            md_list = [PARSE_DIR / rmd]
        print(f"MD files matched (routed) - {len(md_list)}")
        file_to_text = []
        for p in sorted(md_list):
            if not p.exists():
                continue
            txt = p.read_text(encoding="utf-8", errors="ignore")[:8000]
            file_to_text.append((p.name, txt))
        ans = self.ask_openai_from_md_files(question, file_to_text)
        self.write_md_answer(question, llm_pid, ", ".join(p.name for p in md_list), ans)
        return ans

    def ask_router(self, question: str) -> str:
        """
        SQL/general router. Kept separately to allow fallback calls from MD path.
        """
        intent = self.llm_classify_intent(question) or "md"
        print(f"Intent classified -- {intent}")
        if intent == "md":
            return self.answer_md(question)
        if intent == "general":
            return self.llm_general_answer(question)
        pid_to_name = self.load_pid_to_name()
        resolved_pid = None
        resolved_name = None
        if pid_to_name:
            try:
                rp, rn = self.llm_resolve_patient(question, pid_to_name)
                if rp:
                    resolved_pid, resolved_name = rp, rn
                    print(f"Resolved patient for SQL -- {resolved_pid} ({resolved_name})")
            except Exception:
                pass
        sql = self.llm_generate_sql(question, resolved_pid=resolved_pid, resolved_name=resolved_name)
        # Heuristic fallback for common year+patients phrasing (e.g., "showed up in 2024")
        if not sql:
            m_year = re.search(r"\b(20\d{2})\b", question)
            if m_year and re.search(r"(?i)\bpatients?\b", question):
                y = int(m_year.group(1)); y1 = y + 1
                sql = (
                    "SELECT COUNT(DISTINCT reports.patient_id) AS total_patients\n"
                    "FROM reports\n"
                    f"WHERE reports.report_date >= DATE '{y}-01-01'\n"
                    f"  AND reports.report_date < DATE '{y1}-01-01'"
                )
        if not sql:
            return "Could not generate a safe SQL query for the question."
        print("Generated SQL:")
        print(sql)
        headers, rows = self.run_sql(sql)
        try:
            csv_path = self.save_csv(question, headers, rows)
            md_out = ANSWERS_DIR / (csv_path.stem + ".md")
            md = [
                "# SQL Answer\n",
                f"Question: {question}\n\n",
                "```sql\n", sql, "\n```\n\n",
                f"Wrote CSV: {csv_path}\n",
                f"Rows: {len(rows)}\n"
            ]
            md_out.write_text("".join(md), encoding="utf-8")
        except Exception:
            pass
        if rows:
            first = rows[0]
            if len(first) == 1:
                val = first[0]
                qlow = question.lower()
                m = re.search(r"how\s+many\s+([a-z][a-z\s_]+)", qlow) or re.search(r"count\s+of\s+([a-z][a-z\s_]+)", qlow)
                if m:
                    subject = m.group(1).strip().rstrip("?.")
                    subject = re.sub(r"\s+", " ", subject)
                    return f"There are total of {val} {subject}."
                if headers and headers[0]:
                    return f"{headers[0]}: {val}"
                return str(val)
            if headers:
                return ", ".join(f"{h}: {v}" for h, v in zip(headers, first))
            return ", ".join(str(v) for v in first)
        return "No results"

    def ask(self, question: str) -> str:
        """
        Main entrypoint for end-users. Classifies intent (sql/md/general) and routes.
        """
        intent = self.llm_classify_intent(question) or "md"
        print(f"Intent classified -- {intent}")
        if intent == "general":
            return self.llm_general_answer(question)
        if intent == "sql":
            return self.ask_router(question)
        return self.answer_md(question)


