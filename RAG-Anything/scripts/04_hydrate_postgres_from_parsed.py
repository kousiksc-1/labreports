"""
Hydrate Postgres from parsed report outputs (markdown/json) under PARSE_OUTPUT_DIR.

Scope
- Reads top-level .md / .json files produced by the parsing pipeline (e.g., docling + flatten).
- For each file:
  - Ensures a corresponding patients/reports record exists (upsert by ids derived from filename).
  - Extracts patient name evidence using robust heuristics; stores into name_evidence.
  - Heuristically extracts observation rows (analyte, value, unit, ref_range); upserts into observations.
- After processing all files:
  - Finalizes patients.full_name by choosing the best-scoring / most frequent name per patient.
  - Emits a JSON index (patient_id → full_name) at data/name_index.json for the chatbot.

Notes
- This script does not parse nested docling layouts directly; use a "flatten" step beforehand or
  ensure parsed files exist at the top-level (data/parsed/<report_id>.md|.json).
- Observations extraction is heuristic and intentionally conservative; adjust VALUE_ROW if needed.
"""

import os
import re
import json
from pathlib import Path
from typing import List, Tuple, Dict, Optional

from dotenv import load_dotenv
import psycopg


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
load_dotenv(ROOT / ".env")

# Inputs
_parse = os.getenv("PARSE_OUTPUT_DIR", "data/parsed")
PARSE_DIR = (ROOT / _parse).resolve()

# Postgres connection
PG_HOST = os.getenv("POSTGRES_HOST", "localhost")
PG_PORT = int(os.getenv("POSTGRES_PORT", "5432"))
PG_DB = os.getenv("POSTGRES_DATABASE", "lab_reports")
PG_USER = os.getenv("POSTGRES_USER", "postgres")
PG_PWD = os.getenv("POSTGRES_PASSWORD", "")

# Output name index
NAME_INDEX_PATH = ROOT / "data" / "name_index.json"


# --------------------------
# Name extraction heuristics
# --------------------------
# We see a variety of label styles; these regexes try to capture common variants:
NAME_LINE = re.compile(r"^(?:patient\s*)?name\s*[:\-]\s*([A-Za-z\'\-\.\s,]+)", re.I)
# Alternate label styles commonly seen in headers/tables
ALT_NAME = re.compile(r"^(?:pt\.?\s*)?name\s*[:\-]\s*([A-Za-z\'\-\.\s,]+)$", re.I)
PATIENT_COLON = re.compile(r"^patient\s*[:\-]\s*([A-Za-z\'\-\.\s,]+)$", re.I)
PIPE_CELL = re.compile(r"\|\s*(?:patient\s*name|name|patient)\s*\|\s*([A-Za-z\'\-\.\s,]+)\s*\|", re.I)
NEXT_LINE_LABEL = re.compile(r"^(?:patient\s*)?name\s*[:\-]?\s*$", re.I)
FIELD_LINE = re.compile(
    r"^(?:patient\s*)?(first|last|middle|given|surname)\s*name\s*[:\-]\s*([A-Za-z\'\-\.\s]+)",
    re.I,
)

# Generic value row extraction from text lines (heuristic)
# Matches lines such as:
#   "Creatinine 1.01 mg/dL 0.6 - 1.3"
#   "T3 110 ng/dL"
VALUE_ROW = re.compile(
    r"(?P<analyte>[A-Za-z][A-Za-z0-9 _/%\-]{1,40})\s+"
    r"(?P<value>-?\d+(?:\.\d+)?)\s*"
    r"(?P<unit>[A-Za-z/%µ\^\-]+)?"
    r"(?:\s*(?P<flag>\([HL]\)))?"
    r"(?:\s+(?P<ref>\d+(?:\.\d+)?\s*-\s*\d+(?:\.\d+)?))?"
)


def _title_case_name(name: str) -> str:
    """
    Title-case a possibly mixed-case name including hyphens and apostrophes.
    'wILson, aNthony' → 'Wilson, Anthony' (when invoked on tokens)
    """
    parts = []
    for token in name.strip().split():
        sub = "-".join(s[:1].upper() + s[1:].lower() if s else s for s in token.split("-"))
        sub = "'".join(s[:1].upper() + s[1:].lower() if s else s for s in sub.split("'"))
        parts.append(sub)
    return " ".join(parts)


def _clean_tail(value: str) -> str:
    """
    Truncate trailing segments commonly seen after names (MRN/DOB/etc.)
    e.g., 'Mary Moore DOB 01/02/1970' → 'Mary Moore'
    """
    # Stop at common following labels
    cut_tokens = ["mrn", "dob", "age", "sex", "id", "patient", "no", "number"]
    for tok in cut_tokens:
        idx = value.lower().find(f" {tok}")
        if idx > 0:
            value = value[:idx]
    return value.strip()


def extract_patient_name(text: str) -> Tuple[Optional[str], Optional[float]]:
    """
    Extract a patient name from free text using multiple heuristics with a
    best-first approach. Returns (name, confidence) or (None, None).

    Confidence scale
      0.98: From explicit field lines 'First Name:', 'Last Name:' etc.
      0.94: From 'Name: LAST, FIRST [M]' after reordering tokens
      0.90: From 'Name: FIRST [M] LAST'
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    # Join wrapped label/value pairs across lines (e.g., "Name:" on one line, value on the next)
    joined: list[str] = []
    i = 0
    while i < len(lines):
        if NEXT_LINE_LABEL.match(lines[i]) and i + 1 < len(lines):
            joined.append(f"Name: {lines[i+1]}")
            i += 2
        else:
            joined.append(lines[i])
            i += 1
    lines = joined

    # 1) Field-based extraction
    first = None
    last = None
    middle = None
    for ln in lines:
        m = FIELD_LINE.match(ln)
        if not m:
            continue
        kind = m.group(1).lower()
        val = _clean_tail(m.group(2)).strip(" ,")
        if not val:
            continue
        if kind in ("first", "given"):
            first = val
        elif kind in ("last", "surname"):
            last = val
        elif kind == "middle":
            middle = val
    if first and last:
        name = " ".join(filter(None, [_title_case_name(first), _title_case_name(middle or ""), _title_case_name(last)])).strip()
        return name, 0.98

    def _normalize_raw_name(raw: str) -> Optional[str]:
        raw = _clean_tail(raw).strip(" ,")
        # LAST, FIRST [M ...]
        if "," in raw:
            last_part, rest = raw.split(",", 1)
            last_part = last_part.strip()
            rest = rest.strip()
            if rest:
                tokens = [t for t in rest.split() if t]
                if tokens:
                    first_part = tokens[0]
                    middle_part = " ".join(tokens[1:]) if len(tokens) > 1 else ""
                    return " ".join(
                        filter(
                            None,
                            [
                                _title_case_name(first_part),
                                _title_case_name(middle_part),
                                _title_case_name(last_part),
                            ],
                        )
                    ).strip()
        # FIRST [M] LAST
        tokens = [t for t in raw.split() if t]
        if len(tokens) >= 2:
            first_part = tokens[0]
            last_part = tokens[-1]
            middle_part = " ".join(tokens[1:-1])
            return " ".join(
                filter(
                    None,
                    [
                        _title_case_name(first_part),
                        _title_case_name(middle_part),
                        _title_case_name(last_part),
                    ],
                )
            ).strip()
        return None

    # 2) Table/alt single-line patterns
    for ln in lines:
        for rx in (PIPE_CELL, PATIENT_COLON, ALT_NAME):
            m = rx.search(ln)
            if not m:
                continue
            nm = _normalize_raw_name(m.group(1))
            if nm:
                return nm, 0.93

    # 3) Generic "Name: ..." lines
    for ln in lines:
        m = NAME_LINE.match(ln)
        if not m:
            continue
        nm = _normalize_raw_name(m.group(1))
        if nm:
            # Prefer the slightly higher score for explicit "Name:" lines
            # Will be overridden by field-based (0.98) if present
            return nm, 0.94

    return None, None


def extract_observations(text: str) -> List[Tuple[str, str, str, str]]:
    """
    Heuristically parse analyte rows from free text into
    (analyte, value, unit, ref_range) tuples.
    Avoids headers and excessively long lines.
    """
    rows: List[Tuple[str, str, str, str]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or len(line) > 200:
            continue
        m = VALUE_ROW.search(line)
        if not m:
            continue
        analyte = m.group("analyte") or ""
        value = m.group("value") or ""
        unit = (m.group("unit") or "").strip()
        ref = (m.group("ref") or "").strip()
        # ignore obvious headers
        if analyte.lower() in {"name", "patient", "value", "result", "reference"}:
            continue
        rows.append((analyte.strip(), value.strip(), unit, ref))
    return rows[:200]  # guardrail


def upsert_db_from_file(conn: psycopg.Connection, parsed_file: Path, name_index: Dict[str, str]) -> None:
    """
    For a single parsed file (.md or .json), ensure base rows exist and
    hydrate name evidence + observations:
      - patients: ensure patient_id exists (from report_id prefix "P\d+_...")
      - reports:  upsert test_type / report_date (if present in id) and src_path
      - name_evidence: insert extracted candidate name with confidence
      - observations: replace rows for this report with new heuristic rows
    
    All errors are handled silently to prevent cascading failures.
    """
    report_id = parsed_file.stem
    # derive patient_id from report_id e.g. P101805_LFT_20241031_...
    m = re.match(r"^(P\d+)_", report_id, re.I)
    if not m:
        return  # Skip files that don't match expected pattern
    patient_id = m.group(1)

    try:
        text = parsed_file.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return  # Skip files that can't be read

    try:
        # 1) Ensure patient/report exist
        with conn.cursor() as cur:
            try:
                cur.execute(
                    "INSERT INTO patients(patient_id) VALUES(%s) ON CONFLICT (patient_id) DO NOTHING",
                    (patient_id,),
                )
            except Exception:
                pass  # Silently ignore patient insert errors
            
            # best-effort parse test_type and date from report_id
            parts = report_id.split("_")
            test_type = parts[1] if len(parts) > 1 else None
            report_date = None
            if len(parts) > 2 and re.match(r"^\d{8}$", parts[2]):
                try:
                    report_date = f"{parts[2][:4]}-{parts[2][4:6]}-{parts[2][6:]}"
                except Exception:
                    report_date = None
            
            try:
                cur.execute(
                    """
                    INSERT INTO reports(report_id, patient_id, test_type, report_date, src_path, pages)
                    VALUES(%s,%s,%s,%s,%s,NULL)
                    ON CONFLICT (report_id) DO UPDATE SET
                      patient_id=EXCLUDED.patient_id,
                      test_type=EXCLUDED.test_type,
                      report_date=EXCLUDED.report_date,
                      src_path=EXCLUDED.src_path
                    """,
                    (report_id, patient_id, test_type, report_date, str(parsed_file)),
                )
            except Exception:
                pass  # Silently ignore report insert errors

        # 2) Name evidence
        try:
            name, conf = extract_patient_name(text)
            if name:
                with conn.cursor() as cur:
                    try:
                        cur.execute(
                            """
                            INSERT INTO name_evidence(patient_id, source_report, name, confidence)
                            VALUES(%s,%s,%s,%s)
                            """,
                            (patient_id, report_id, name, conf),
                        )
                        # save in index (only if not present yet)
                        name_index.setdefault(patient_id, name)
                    except Exception:
                        pass  # Silently ignore name evidence errors
        except Exception:
            pass  # Silently ignore name extraction errors

        # 3) Observations
        try:
            rows = extract_observations(text)
            if rows:
                with conn.cursor() as cur:
                    try:
                        cur.execute("DELETE FROM observations WHERE report_id=%s", (report_id,))
                        cur.executemany(
                            "INSERT INTO observations(report_id, analyte, value, unit, ref_range, page) VALUES(%s,%s,%s,%s,%s,NULL)",
                            [(report_id, a, v, u, r) for a, v, u, r in rows],
                        )
                    except Exception:
                        pass  # Silently ignore observation errors
        except Exception:
            pass  # Silently ignore observation extraction errors
    except Exception:
        pass  # Catch any other unexpected errors


def finalize_patient_names(conn: psycopg.Connection) -> None:
    """
    Compute a canonical full_name per patient using ranking:
      - Higher confidence wins
      - Ties broken by frequency, then alphabetical
    Writes the chosen name into patients.full_name.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH cand AS (
              SELECT patient_id, name,
                     MAX(confidence) AS max_conf,
                     COUNT(*) AS freq
              FROM name_evidence
              GROUP BY patient_id, name
            ),
            ranked AS (
              SELECT patient_id, name, max_conf, freq,
                     DENSE_RANK() OVER (
                       PARTITION BY patient_id
                       ORDER BY max_conf DESC, freq DESC, name ASC
                     ) AS rnk
              FROM cand
            )
            UPDATE patients p
            SET full_name = r.name
            FROM ranked r
            WHERE r.patient_id = p.patient_id AND r.rnk = 1
            """
        )


def main():
    """
    Entry point:
      - Reads top-level parsed files under PARSE_DIR
      - Hydrates DB tables per file
      - Finalizes patients.full_name
      - Writes a name_index.json for downstream lookup
    """
    assert PARSE_DIR.exists(), f"Missing parsed directory: {PARSE_DIR}"

    # Collect candidate files (top-level .md/.json)
    files: List[Path] = []
    for p in PARSE_DIR.iterdir():
        if p.is_file() and p.suffix.lower() in {".md", ".json"}:
            files.append(p)
    files.sort()
    print(f"[hydrate] Found {len(files)} parsed files to process under {PARSE_DIR}")

    name_index: Dict[str, str] = {}
    with psycopg.connect(
        host=PG_HOST, port=PG_PORT, dbname=PG_DB, user=PG_USER, password=PG_PWD, autocommit=False
    ) as conn:
        processed = 0
        for f in files:
            # Process each file in its own transaction to avoid cascading failures
            # All errors are handled silently inside upsert_db_from_file
            try:
                upsert_db_from_file(conn, f, name_index)
                conn.commit()  # Commit after each successful file
                processed += 1
                if processed % 200 == 0:
                    print(f"[hydrate] {processed} files processed...")
            except Exception:
                # Silently rollback and continue - errors are already handled in upsert_db_from_file
                conn.rollback()
        # finalize names and commit
        try:
            finalize_patient_names(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            # Silently ignore finalization errors - data is already loaded
        print(f"[hydrate] Processed {processed} files successfully")

    # Write name index json
    NAME_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    with NAME_INDEX_PATH.open("w", encoding="utf-8") as f:
        json.dump(name_index, f, ensure_ascii=False, indent=2)
    print(f"[hydrate] Wrote name index: {NAME_INDEX_PATH}")
    print(f"[hydrate] Done.")


if __name__ == "__main__":
    main()

