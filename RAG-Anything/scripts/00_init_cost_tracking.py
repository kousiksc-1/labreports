"""
Initialize cost tracking database schema

Run this once to set up the model usage logging table.
"""
from model_cost_logger import init_cost_logging_table

if __name__ == "__main__":
    print("Initializing cost tracking schema...")
    init_cost_logging_table()
    print("✅ Cost tracking ready!")
    print("\nYou can now:")
    print("  - Track API costs automatically")
    print("  - View cost reports via /api/costs endpoint")
    print("  - Monitor daily usage and spending")

