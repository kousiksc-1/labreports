# DDL Documentation - Patients and Reports

This document explains the core tables in Postgres, with their columns and three example rows per table.

## Table: patients
- `patient_id` (TEXT, PK): Stable patient identifier (e.g., P100027). Derived from file names and used as the primary key.
- `full_name` (TEXT, nullable): Canonical resolved patient name. Populated from parsed reports via `name_evidence`.
- `first_seen` (DATE, nullable): Earliest report date seen for the patient (backfilled from `reports`).
- `last_seen` (DATE, nullable): Latest report date seen for the patient (backfilled from `reports`).

Example rows:

| patient_id | full_name        | first_seen | last_seen  |
|------------|------------------|------------|------------|
| P100027    | Mary Moore       | 2025-11-04 | 2025-11-04 |
| P100000    | Mark Lewis       | 2024-12-10 | 2025-10-31 |
| P100011    | Elizabeth Perez  | 2024-07-07 | 2025-09-03 |

## Table: reports
- `report_id` (TEXT, PK): Unique report key (e.g., P100027_Lipid_Profile_20251104_alternative).
- `patient_id` (TEXT, FK patients.patient_id): Patient owner for the report.
- `test_type` (TEXT, nullable): Parsed test type token (e.g., LFT, Lipid, CBC, CMP).
- `report_date` (DATE, nullable): Backfilled from report_id token `_YYYYMMDD_` if present.
- `src_path` (TEXT, nullable): Canonical file path. For parsed .md this points to data/parsed; for PDFs it points to lab_reports_final.
- `pages` (INT, nullable): Optional page count if known (not required).

Example rows:

| report_id                                  | patient_id | test_type | report_date | src_path                                                                                 | pages |
|--------------------------------------------|------------|-----------|-------------|------------------------------------------------------------------------------------------|-------|
| P100027_Lipid_Profile_20251104_alternative | P100027    | Lipid     | 2025-11-04  | data/parsed/P100027_Lipid_Profile_20251104_alternative.md                                | null  |
| P100000_LFT_20241210_minimal_bars          | P100000    | LFT       | 2024-12-10  | data/parsed/P100000_LFT_20241210_minimal_bars.md                                         | null  |
| P100011_CMP_20250903_bordered_chart        | P100011    | CMP       | 2025-09-03  | data/parsed/P100011_CMP_20250903_bordered_chart.md                                       | null  |

Notes:
- The `report_id` usually contains three main tokens: `patient_id` + `test_type` + `_YYYYMMDD_`, plus an optional style suffix (e.g., `alternative`, `borderless`). The date segment is used to backfill `report_date`.
- `src_path` for PDFs will look like `lab_reports_final/P100027_Lipid_Profile_20251104_alternative.pdf`.


