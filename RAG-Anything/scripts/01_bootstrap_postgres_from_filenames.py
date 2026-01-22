import os, re, hashlib, datetime as dt
from pathlib import Path
from dotenv import load_dotenv
import psycopg
import sys

# Bootstrap structural metadata (patients/reports/ingested_files) into Postgres
# by reading ONLY PDF filenames. No OCR or content parsing happens here.
# Idempotent: uses ON CONFLICT and SHA-256 dedupe tracking.
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(ROOT))  # allow importing project-root modules if needed

load_dotenv(ROOT / ".env")


PG_HOST = os.getenv("POSTGRES_HOST", "localhost")
PG_PORT = int(os.getenv("POSTGRES_PORT", "5432"))
PG_DB   = os.getenv("POSTGRES_DATABASE", "lab_reports")
PG_USER = os.getenv("POSTGRES_USER", "postgres")
PG_PWD  = os.getenv("POSTGRES_PASSWORD", "")
MODE    = os.getenv("POSTGRES_CONFIG", "append").strip().lower()  # 'init' or 'append'
_src = os.getenv("REPORTS_SRC", "lab_reports_final")
SRC_DIR = Path(_src if Path(_src).is_absolute() else (ROOT / _src)).resolve()  # absolute path to PDFs

REQUIRED_TABLES = {"patients","reports","observations","name_evidence","ingested_files"}  # minimal schema present in append mode

PATTERN = re.compile(r"^(P\d+)_(?P<test>[^_]+)_(?P<ymd>\d{8})_.*\.pdf$", re.IGNORECASE)
# Example: P101805_LFT_20241031_borderless.pdf → patient=P101805, test=LFT, ymd=20241031

DDL = """
CREATE TABLE IF NOT EXISTS patients(
  patient_id TEXT PRIMARY KEY,
  full_name  TEXT,
  first_seen DATE,
  last_seen  DATE
);
CREATE TABLE IF NOT EXISTS reports(
  report_id  TEXT PRIMARY KEY,
  patient_id TEXT NOT NULL,
  test_type  TEXT,
  report_date DATE,
  src_path   TEXT,
  pages      INT,
  CONSTRAINT fk_reports_patient
    FOREIGN KEY(patient_id) REFERENCES patients(patient_id)
    ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS observations(
  obs_id     SERIAL PRIMARY KEY,
  report_id  TEXT NOT NULL,
  analyte    TEXT,
  value      TEXT,
  unit       TEXT,
  ref_range  TEXT,
  page       INT,
  CONSTRAINT fk_obs_report
    FOREIGN KEY(report_id) REFERENCES reports(report_id)
    ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS name_evidence(
  id            SERIAL PRIMARY KEY,
  patient_id    TEXT NOT NULL,
  source_report TEXT NOT NULL,
  name          TEXT,
  confidence    REAL,
  CONSTRAINT fk_name_patient
    FOREIGN KEY(patient_id) REFERENCES patients(patient_id)
    ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS ingested_files(
  sha256      TEXT PRIMARY KEY,
  report_id   TEXT,
  patient_id  TEXT,
  src_path    TEXT,
  mtime       BIGINT,
  size_bytes  BIGINT,
  ingested_at TIMESTAMP DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_reports_patient_date ON reports(patient_id, report_date);
CREATE INDEX IF NOT EXISTS idx_obs_report ON observations(report_id);
"""

def parse_filename(name: str):
    """
    Decode filename into minimum report metadata; return None if not matched.
    """
    m = PATTERN.match(name)
    if not m: return None
    pid = m.group(1)
    test_type = m.group("test")
    date = dt.datetime.strptime(m.group("ymd"), "%Y%m%d").date()
    report_id = name[:-4]
    return {"patient_id": pid, "test_type": test_type, "report_date": date, "report_id": report_id}


def sha256_file(p: Path) -> str:
    """
    Compute SHA-256 digest of a file by streaming in 1MB chunks.
    Used to deduplicate entries in ingested_files.
    """
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024*1024), b""):
            h.update(chunk)
    return h.hexdigest()

def ensure_tables_exist(conn: psycopg.Connection):
    """
    Fail fast in append mode if required tables are not present.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
        have = {r[0] for r in cur.fetchall()}
    missing = REQUIRED_TABLES - have
    if missing:
        raise SystemExit(f"Missing tables: {sorted(missing)}. Set POSTGRES_CONFIG=init once, then re-run.")



def main():
    print(f"[bootstrap] MODE={MODE} SRC_DIR={SRC_DIR}")
    assert SRC_DIR.exists(), f"Missing folder: {SRC_DIR}"

    with psycopg.connect(host=PG_HOST, port=PG_PORT, dbname=PG_DB, user=PG_USER, password=PG_PWD, autocommit=False) as conn:
        # Optionally create schema, otherwise ensure it exists
        if MODE == "init":
            conn.execute(DDL)
            conn.commit()
            print("[bootstrap] Schema ensured (init). Proceeding to ingest...")
        else:
            ensure_tables_exist(conn)

        new_reports = 0
        skipped = 0
        total_scanned = 0
        total_matched = 0
        with conn.cursor() as cur:
            # Walk the source folder; ingest files that match the expected naming pattern
            for fn in os.listdir(SRC_DIR):
                total_scanned += 1
                if not fn.lower().endswith(".pdf"):
                    continue
                meta = parse_filename(fn)
                if not meta:
                    continue
                total_matched += 1

                full_path = SRC_DIR / fn
                pid = meta["patient_id"]
                rid = meta["report_id"]
                test = meta["test_type"]
                rdate = meta["report_date"]
                srcp = str(full_path)
                mtime = int(full_path.stat().st_mtime)
                sizeb = full_path.stat().st_size

                # 1) Ensure patient exists and upsert report metadata
                cur.execute("INSERT INTO patients(patient_id) VALUES(%s) ON CONFLICT (patient_id) DO NOTHING", (pid,))
                cur.execute("""
                INSERT INTO reports(report_id, patient_id, test_type, report_date, src_path, pages)
                VALUES(%s,%s,%s,%s,%s,NULL)
                ON CONFLICT (report_id) DO UPDATE SET
                    patient_id=EXCLUDED.patient_id,
                    test_type=EXCLUDED.test_type,
                    report_date=EXCLUDED.report_date,
                    src_path=EXCLUDED.src_path
                """, (rid, pid, test, rdate, srcp))

                # 2) Dedupe tracking for ingestion bookkeeping (no-op if already seen)
                digest = sha256_file(full_path)
                cur.execute("""
                INSERT INTO ingested_files(sha256, report_id, patient_id, src_path, mtime, size_bytes)
                VALUES(%s,%s,%s,%s,%s,%s)
                ON CONFLICT (sha256) DO NOTHING
                """, (digest, rid, pid, srcp, mtime, sizeb))

                new_reports += 1

                if cur.rowcount != 1:
                    skipped += 1  # indicates SHA-256 already present → file was previously tracked

            # 3) Backfill first_seen / last_seen from report dates
            cur.execute("""
              UPDATE patients p
              SET first_seen = s.min_date,
                  last_seen  = s.max_date
              FROM (
                SELECT patient_id, MIN(report_date) AS min_date, MAX(report_date) AS max_date
                FROM reports GROUP BY patient_id
              ) s
              WHERE p.patient_id = s.patient_id
            """)
        conn.commit()

    print(f"[bootstrap] Scan summary: total_files={total_scanned} matched_pdf_pattern={total_matched}")
    print(f"[bootstrap] Ingest complete. Processed={new_reports} already_tracked={skipped}")


if __name__ == "__main__":
    main()