# SQL Answer
Question: [Previous conversation]
Assistant: There are total of 1020 total patients showed up in.
User: How many total patients showed up in 2024


[Current Question]
in 2025?

```sql
SELECT COUNT(DISTINCT patients.patient_id) AS total_patients_2025
FROM patients
JOIN reports ON patients.patient_id = reports.patient_id
WHERE reports.report_date >= DATE '2025-01-01' AND reports.report_date < DATE '2026-01-01';
```

Wrote CSV: /app/data/answers/sql_Previous_conversation_Assistant_There_are_total_of_1020_total_patients_showed_u.csv
Rows: 1
