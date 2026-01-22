"""
Threaded wrapper around the docling CLI to parse all PDFs under REPORTS_SRC.

What this script does
- Lists PDFs under REPORTS_SRC (from .env), and runs the external "docling" CLI.
- Writes results under PARSE_OUTPUT_DIR (from .env), one directory/file per report_id.
- Appends a CSV log (data/docling_process_log.csv) with status, timing, and stderr tail.
- Uses per-thread TEMP/TMP directories to reduce Windows file-lock conflicts.
- Retries each docling invocation once if it fails the first time.

What this script does NOT do
- It does not normalize/flatten docling's nested outputs (that's handled elsewhere).
- It does not update the database (hydration occurs in other scripts).

Environment variables (see .env)
- REPORTS_SRC:           Source folder containing *.pdf lab reports
- PARSE_OUTPUT_DIR:      Destination folder for parsed outputs
- DOC_PROCESS_WORKERS:   Number of threads to run concurrently (fallback: PARSE_MAX_WORKERS or 2)

Typical usage
    python RAG-Anything/scripts/02_parser_docling.py
"""

import os
import csv
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Tuple
import time
import datetime as dt
import threading
import os
import math

from dotenv import load_dotenv
from tqdm import tqdm


# Resolve project root and load .env
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
load_dotenv(ROOT / ".env")

# Paths and settings
SRC_DIR = Path(os.getenv("REPORTS_SRC", "lab_reports_final")).resolve()
PARSE_DIR = (ROOT / os.getenv("PARSE_OUTPUT_DIR", "data/parsed")).resolve()
LOG_CSV = ROOT / "data" / "docling_process_log.csv"
TMP_BASE = ROOT / "data" / "tmp" / "docling"

# Concurrency for docling CLI
WORKERS = int(os.getenv("DOC_PROCESS_WORKERS", os.getenv("PARSE_MAX_WORKERS", "2")))


def is_processed(report_id: str) -> bool:
    """
    Return True if a report already appears parsed, so we can skip reprocessing.
    Consider processed if either:
    - normalized file exists: data/parsed/{report_id}.json or .md
    - nested docling layout exists:
      data/parsed/{report_id}/{report_id}/docling/{report_id}.json
    """
    # top-level normalized md/json (after flattening)
    normalized_json = PARSE_DIR / f"{report_id}.json"
    normalized_md = PARSE_DIR / f"{report_id}.md"
    if normalized_json.exists() or normalized_md.exists():
        return True

    nested = PARSE_DIR / report_id / report_id / "docling" / f"{report_id}.json"
    if nested.exists():
        return True

    # Some docling versions output md only; also accept .md as signal
    nested_md = PARSE_DIR / report_id / report_id / "docling" / f"{report_id}.md"
    return nested_md.exists()


def scan_source_pdfs(src_dir: Path) -> List[Path]:
    """
    Return all immediate *.pdf paths within src_dir, sorted for reproducibility.
    """
    return sorted([p for p in src_dir.iterdir() if p.suffix.lower() == ".pdf"])




def run_docling(pdf_path: Path) -> Tuple[str, int, str, str, str, float, str, int, str]:
    """
    Run docling CLI for a single PDF and capture telemetry.

    Returns a tuple:
        report_id, returncode, stderr_snippet_tail,
        start_iso, end_iso, duration_sec,
        thread_name, thread_id, pdf_path_str
    )
    """
    report_id = pdf_path.stem
    thread_name = threading.current_thread().name
    thread_id = threading.get_ident()
    start_ts = time.time()
    start_iso = dt.datetime.fromtimestamp(start_ts).isoformat(timespec="seconds")

    # Build docling CLI command. We intentionally pass absolute paths and do not
    # assume docling's working directory to avoid path issues on Windows.
    cmd = [
        "docling",
        str(pdf_path),
        "--output",
        str(PARSE_DIR),
    ]
    # Isolate temp folder per-thread to avoid Windows file locks and TMP contention
    thread_tmp = TMP_BASE / f"t{thread_id}"
    thread_tmp.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["TMP"] = str(thread_tmp)
    env["TEMP"] = str(thread_tmp)
    env.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")

    try:
        # Execute docling; simple retry once on failure (common transient issues: cache/locks)
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
        if proc.returncode != 0:
            time.sleep(0.5)
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
        stderr_tail = (proc.stderr or "")[-400:]
        end_ts = time.time()
        end_iso = dt.datetime.fromtimestamp(end_ts).isoformat(timespec="seconds")
        return (
            report_id, proc.returncode, stderr_tail,
            start_iso, end_iso, end_ts - start_ts,
            thread_name, thread_id, str(pdf_path)
        )
    except Exception as e:
        end_ts = time.time()
        end_iso = dt.datetime.fromtimestamp(end_ts).isoformat(timespec="seconds")
        return (
            report_id, 1, str(e),
            start_iso, end_iso, end_ts - start_ts,
            thread_name, thread_id, str(pdf_path)
        )


def main():
    """
    Entry point:
    - Validates input/output directories
    - Scans PDFs from REPORTS_SRC
    - Processes them with a thread pool and logs per-file results
    """
    assert SRC_DIR.exists(), f"Missing folder: {SRC_DIR}"
    PARSE_DIR.mkdir(parents=True, exist_ok=True)

    # Enumerate all PDFs. We intentionally parse ALL rather than "only missing"
    # because downstream flattening or hydration scripts may rely on docling layout.
    all_pdfs = scan_source_pdfs(SRC_DIR)
    targets: List[Tuple[str, str]] = [(pdf.stem, str(pdf)) for pdf in all_pdfs]
    print(f"Found {len(all_pdfs)} PDFs; processing all with workers={WORKERS}")

    # Process the targets with a thread pool. If you wish to skip already-processed
    # items, filter here using is_processed(report_id).
    if targets:
        print(f"Processing {len(targets)} PDFs with docling (workers={WORKERS})...")

        # Prepare log file (append; create with header if not exists)
        write_header = not LOG_CSV.exists()
        LOG_CSV.parent.mkdir(parents=True, exist_ok=True)
        log_f = LOG_CSV.open("a", newline="", encoding="utf-8")
        log_w = csv.writer(log_f)
        if write_header:
            log_w.writerow([
                "report_id", "pdf_path", "status", "return_code",
                "start_time", "end_time", "duration_sec",
                "thread_name", "thread_id", "stderr_tail"
            ])

        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            # Submit one future per PDF and keep a simple {future: report_id} map
            futs = {ex.submit(run_docling, Path(path)): rid for rid, path in targets}
            for fut in tqdm(as_completed(futs), total=len(futs), desc="Docling parsing"):
                rid = futs[fut]
                try:
                    rep_id, rc, err, start_iso, end_iso, dur, tname, tid, ppath = fut.result()
                    status = "ok" if rc == 0 else "fail"
                    log_w.writerow([rep_id, ppath, status, rc, start_iso, end_iso, f"{dur:.2f}", tname, tid, err])
                    log_f.flush()
                    if rc != 0:
                        print(f"[docling] FAIL {rep_id}: rc={rc} ... {err}")
                except Exception as e:
                    # We keep going even if a single file fails; the CSV log captures per-file status
                    print(f"[docling] EXC {rid}: {e}")
        log_f.close()
    else:
        print("No PDFs to process.")

    # ------------------------------------------------------------------
    # Optional: Process ALL files (commented). Uncomment to reparse all.
    # ------------------------------------------------------------------
    # print(f"Reprocessing ALL {len(all_pdfs)} PDFs with docling (workers={WORKERS})...")
    # with ThreadPoolExecutor(max_workers=WORKERS) as ex:
    #     futs = {ex.submit(run_docling, pdf): pdf.stem for pdf in all_pdfs}
    #     for fut in tqdm(as_completed(futs), total=len(futs), desc="Docling parsing (ALL)"):
    #         rid = futs[fut]
    #         try:
    #             rep_id, rc, err = fut.result()
    #             if rc != 0:
    #                 print(f"[docling] FAIL {rep_id}: rc={rc} ... {err}")
    #         except Exception as e:
    #             print(f"[docling] EXC {rid}: {e}")


if __name__ == "__main__":
    main()

