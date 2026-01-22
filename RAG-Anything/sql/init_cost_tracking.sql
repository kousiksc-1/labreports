-- Initialize cost tracking table for API usage monitoring
-- Run this SQL in your PostgreSQL database

CREATE TABLE IF NOT EXISTS model_usage_logs (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    user_id TEXT,
    session_id TEXT,
    operation_type TEXT,  -- e.g., 'chat', 'patient_detection', 'intent_classification'
    model_name TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    total_tokens INTEGER,
    input_cost DECIMAL(10, 6),
    output_cost DECIMAL(10, 6),
    total_cost DECIMAL(10, 6),
    metadata JSONB,  -- Additional context (e.g., patient_id, query_text)
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Create indexes for fast querying
CREATE INDEX IF NOT EXISTS idx_model_usage_logs_user_id ON model_usage_logs(user_id);
CREATE INDEX IF NOT EXISTS idx_model_usage_logs_session_id ON model_usage_logs(session_id);
CREATE INDEX IF NOT EXISTS idx_model_usage_logs_timestamp ON model_usage_logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_model_usage_logs_model_name ON model_usage_logs(model_name);

-- Verify table was created
SELECT 'Cost tracking table created successfully!' as status;

