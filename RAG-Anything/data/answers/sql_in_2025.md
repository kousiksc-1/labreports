# SQL Answer
Question: in 2025?

```sql
SELECT COUNT(DISTINCT patients.patient_id) AS patient_count
FROM patients
JOIN reports ON patients.patient_id = reports.patient_id
WHERE reports.report_date >= DATE '2025-01-01' AND reports.report_date < DATE '2026-01-01';
```

Wrote CSV: C:\Users\kousik.senthilkumar\Documents\rag_anything\RAG-Anything\data\answers\sql_in_2025.csv
Rows: 1
