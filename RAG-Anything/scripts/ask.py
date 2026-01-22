"""
Simple CLI entrypoint for the unified RagAnything model.

Usage:
    python scripts/ask.py
Then type your question when prompted.
"""
from pathlib import Path
import sys

# Allow running even if executed from a different CWD
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(ROOT))

from raganything import RagAnything  # noqa: E402


if __name__ == "__main__":
    q = input("Enter your question: ").strip()
    if not q:
        print("No question provided.")
        sys.exit(0)
    rag = RagAnything()
    print(rag.ask(q))


