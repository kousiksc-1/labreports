# SQL Answer
Question: show me the trend of C-reactive protein for Elizabeth Perez

```sql
SELECT report_date, COUNT(DISTINCT report_id) AS report_count
FROM reports
WHERE patient_id = 'P100864' AND test_type = 'C-reactive protein'
GROUP BY report_date
ORDER BY report_date;
```

Wrote CSV: C:\Users\kousik.senthilkumar\Downloads\rag_anything\RAG-Anything\data\answers\sql_show_me_the_trend_of_C_reactive_protein_for_Elizabeth_Perez.csv
Rows: 0
