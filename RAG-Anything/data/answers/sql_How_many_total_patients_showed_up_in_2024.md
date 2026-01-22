# SQL Answer
Question: How many total patients showed up in 2024

```sql
SELECT COUNT(DISTINCT patients.patient_id) AS total_patients
FROM patients
JOIN reports ON patients.patient_id = reports.patient_id
WHERE reports.report_date >= DATE '2024-01-01' AND reports.report_date < DATE '2025-01-01';
```

Wrote CSV: /app/data/answers/sql_How_many_total_patients_showed_up_in_2024.csv
Rows: 1
