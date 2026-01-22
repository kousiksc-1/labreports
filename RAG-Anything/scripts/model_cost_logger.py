"""
Model Cost Logging System

Tracks LLM API usage, token consumption, and costs for monitoring and optimization.
Supports multiple models (OpenAI, Anthropic, etc.) with configurable pricing.
"""
import os
import json
import psycopg
from datetime import datetime
from typing import Optional, Dict, Any
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
load_dotenv(ROOT / ".env")

# Model pricing (USD per 1K tokens)
# Updated as of 2024 - adjust based on actual pricing
MODEL_PRICING = {
    # OpenAI GPT-4
    "gpt-4": {"input": 0.03, "output": 0.06},
    "gpt-4-turbo": {"input": 0.01, "output": 0.03},
    "gpt-4-turbo-preview": {"input": 0.01, "output": 0.03},
    "gpt-4o": {"input": 0.005, "output": 0.015},
    "gpt-4o-mini": {"input": 0.00015, "output": 0.0006},
    
    # OpenAI GPT-3.5
    "gpt-3.5-turbo": {"input": 0.0005, "output": 0.0015},
    "gpt-3.5-turbo-16k": {"input": 0.003, "output": 0.004},
    
    # Anthropic Claude
    "claude-3-opus": {"input": 0.015, "output": 0.075},
    "claude-3-sonnet": {"input": 0.003, "output": 0.015},
    "claude-3-haiku": {"input": 0.00025, "output": 0.00125},
    "claude-3-5-sonnet": {"input": 0.003, "output": 0.015},
    
    # Default fallback
    "default": {"input": 0.001, "output": 0.002}
}


def pg_conn():
    """Create PostgreSQL connection"""
    dsn = f"host={os.getenv('POSTGRES_HOST','localhost')} port={os.getenv('POSTGRES_PORT','5432')} dbname={os.getenv('POSTGRES_DATABASE')} user={os.getenv('POSTGRES_USER')} password={os.getenv('POSTGRES_PASSWORD')}"
    return psycopg.connect(dsn)


def init_cost_logging_table():
    """
    Initialize the cost logging table in PostgreSQL
    Run this once to create the table
    """
    with pg_conn() as conn, conn.cursor() as cur:
        cur.execute("""
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
            
            CREATE INDEX IF NOT EXISTS idx_model_usage_logs_user_id ON model_usage_logs(user_id);
            CREATE INDEX IF NOT EXISTS idx_model_usage_logs_session_id ON model_usage_logs(session_id);
            CREATE INDEX IF NOT EXISTS idx_model_usage_logs_timestamp ON model_usage_logs(timestamp);
            CREATE INDEX IF NOT EXISTS idx_model_usage_logs_model_name ON model_usage_logs(model_name);
        """)
    print("✅ Model usage logging table initialized")


def calculate_cost(model_name: str, input_tokens: int, output_tokens: int) -> Dict[str, float]:
    """
    Calculate cost based on model and token usage
    
    Args:
        model_name: Name of the model used
        input_tokens: Number of input tokens
        output_tokens: Number of output tokens
        
    Returns:
        Dictionary with input_cost, output_cost, and total_cost
    """
    # Get pricing for model (or use default)
    pricing = MODEL_PRICING.get(model_name.lower(), MODEL_PRICING["default"])
    
    # Calculate costs (pricing is per 1K tokens)
    input_cost = (input_tokens / 1000) * pricing["input"]
    output_cost = (output_tokens / 1000) * pricing["output"]
    total_cost = input_cost + output_cost
    
    return {
        "input_cost": round(input_cost, 6),
        "output_cost": round(output_cost, 6),
        "total_cost": round(total_cost, 6)
    }


def log_model_usage(
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    operation_type: str = "chat",
    model_name: str = "gpt-4o-mini",
    input_tokens: int = 0,
    output_tokens: int = 0,
    metadata: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Log model usage and cost to database
    
    Args:
        user_id: User identifier
        session_id: Chat session identifier
        operation_type: Type of operation (chat, patient_detection, etc.)
        model_name: Model used
        input_tokens: Number of input tokens
        output_tokens: Number of output tokens
        metadata: Additional context (patient_id, query, etc.)
        
    Returns:
        Dictionary with logged information including costs
    """
    # Calculate costs
    costs = calculate_cost(model_name, input_tokens, output_tokens)
    total_tokens = input_tokens + output_tokens
    
    # Prepare metadata
    metadata_json = json.dumps(metadata or {})
    
    # Log to database
    try:
        with pg_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO model_usage_logs (
                    user_id, session_id, operation_type, model_name,
                    input_tokens, output_tokens, total_tokens,
                    input_cost, output_cost, total_cost, metadata
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, timestamp
            """, (
                user_id, session_id, operation_type, model_name,
                input_tokens, output_tokens, total_tokens,
                costs["input_cost"], costs["output_cost"], costs["total_cost"],
                metadata_json
            ))
            log_id, timestamp = cur.fetchone()
        
        # Also log to file for backup
        log_to_file({
            "id": log_id,
            "timestamp": timestamp.isoformat(),
            "user_id": user_id,
            "session_id": session_id,
            "operation_type": operation_type,
            "model_name": model_name,
            "tokens": {
                "input": input_tokens,
                "output": output_tokens,
                "total": total_tokens
            },
            "costs": costs
        })
        
        return {
            "log_id": log_id,
            "timestamp": timestamp,
            "costs": costs,
            "tokens": {
                "input": input_tokens,
                "output": output_tokens,
                "total": total_tokens
            }
        }
    except Exception as e:
        print(f"⚠️  Failed to log model usage to database: {e}")
        # Still log to file even if DB fails
        log_to_file({
            "error": str(e),
            "user_id": user_id,
            "session_id": session_id,
            "model_name": model_name,
            "tokens": {"input": input_tokens, "output": output_tokens},
            "costs": costs
        })
        return {"costs": costs, "error": str(e)}


def log_to_file(log_entry: Dict[str, Any]):
    """
    Backup logging to file
    """
    log_dir = ROOT / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    
    # Daily log file
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = log_dir / f"model_costs_{today}.jsonl"
    
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(log_entry) + "\n")


def get_cost_summary(
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    model_name: Optional[str] = None
) -> Dict[str, Any]:
    """
    Get cost summary with various filters
    
    Args:
        user_id: Filter by user
        session_id: Filter by session
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)
        model_name: Filter by model
        
    Returns:
        Summary statistics
    """
    conditions = []
    params = []
    
    if user_id:
        conditions.append("user_id = %s")
        params.append(user_id)
    if session_id:
        conditions.append("session_id = %s")
        params.append(session_id)
    if start_date:
        conditions.append("timestamp >= %s")
        params.append(start_date)
    if end_date:
        conditions.append("timestamp <= %s")
        params.append(end_date)
    if model_name:
        conditions.append("model_name = %s")
        params.append(model_name)
    
    where_clause = " AND ".join(conditions) if conditions else "1=1"
    
    with pg_conn() as conn, conn.cursor() as cur:
        # Overall summary
        cur.execute(f"""
            SELECT 
                COUNT(*) as total_requests,
                SUM(input_tokens) as total_input_tokens,
                SUM(output_tokens) as total_output_tokens,
                SUM(total_tokens) as total_tokens,
                SUM(total_cost) as total_cost,
                AVG(total_cost) as avg_cost_per_request,
                MIN(timestamp) as first_request,
                MAX(timestamp) as last_request
            FROM model_usage_logs
            WHERE {where_clause}
        """, params)
        
        summary = cur.fetchone()
        
        # By model breakdown
        cur.execute(f"""
            SELECT 
                model_name,
                COUNT(*) as requests,
                SUM(total_tokens) as tokens,
                SUM(total_cost) as cost
            FROM model_usage_logs
            WHERE {where_clause}
            GROUP BY model_name
            ORDER BY cost DESC
        """, params)
        
        by_model = cur.fetchall()
        
        # By operation type
        cur.execute(f"""
            SELECT 
                operation_type,
                COUNT(*) as requests,
                SUM(total_cost) as cost
            FROM model_usage_logs
            WHERE {where_clause}
            GROUP BY operation_type
            ORDER BY cost DESC
        """, params)
        
        by_operation = cur.fetchall()
    
    return {
        "summary": {
            "total_requests": summary[0] or 0,
            "total_input_tokens": summary[1] or 0,
            "total_output_tokens": summary[2] or 0,
            "total_tokens": summary[3] or 0,
            "total_cost_usd": float(summary[4] or 0),
            "avg_cost_per_request": float(summary[5] or 0),
            "first_request": summary[6].isoformat() if summary[6] else None,
            "last_request": summary[7].isoformat() if summary[7] else None
        },
        "by_model": [
            {
                "model": row[0],
                "requests": row[1],
                "tokens": row[2],
                "cost_usd": float(row[3])
            }
            for row in by_model
        ],
        "by_operation": [
            {
                "operation": row[0],
                "requests": row[1],
                "cost_usd": float(row[2])
            }
            for row in by_operation
        ]
    }


def get_daily_costs(days: int = 30) -> list:
    """
    Get daily cost breakdown for last N days
    """
    with pg_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT 
                DATE(timestamp) as date,
                COUNT(*) as requests,
                SUM(total_tokens) as tokens,
                SUM(total_cost) as cost
            FROM model_usage_logs
            WHERE timestamp >= CURRENT_DATE - INTERVAL '%s days'
            GROUP BY DATE(timestamp)
            ORDER BY date DESC
        """, (days,))
        
        return [
            {
                "date": row[0].isoformat(),
                "requests": row[1],
                "tokens": row[2],
                "cost_usd": float(row[3])
            }
            for row in cur.fetchall()
        ]


if __name__ == "__main__":
    # Initialize table if running directly
    init_cost_logging_table()
    print("Cost logging system initialized!")

