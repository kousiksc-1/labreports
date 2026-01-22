import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


DDL = """
CREATE TABLE IF NOT EXISTS users (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email        TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  created_at   TIMESTAMP NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chat_sessions (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      UUID NOT NULL,
  title        TEXT,
  created_at   TIMESTAMP NOT NULL DEFAULT now(),
  updated_at   TIMESTAMP NOT NULL DEFAULT now(),
  CONSTRAINT fk_chat_user FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS chat_messages (
  id           BIGSERIAL PRIMARY KEY,
  session_id   UUID NOT NULL,
  role         TEXT NOT NULL, -- 'user' | 'assistant' | 'system'
  content      TEXT NOT NULL,
  created_at   TIMESTAMP NOT NULL DEFAULT now(),
  CONSTRAINT fk_chat_session FOREIGN KEY(session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages(session_id, created_at);
"""


def pg_conn():
    dsn = f"host={os.getenv('POSTGRES_HOST','localhost')} port={os.getenv('POSTGRES_PORT','5432')} dbname={os.getenv('POSTGRES_DATABASE')} user={os.getenv('POSTGRES_USER')} password={os.getenv('POSTGRES_PASSWORD')}"
    return psycopg.connect(dsn, autocommit=True)


def main():
    with pg_conn() as conn, conn.cursor() as cur:
        # enable pgcrypto for gen_random_uuid if needed
        try:
            cur.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto;")
        except Exception:
            pass
        cur.execute(DDL)
    print("Chat schema initialized (users, chat_sessions, chat_messages).")


if __name__ == "__main__":
    main()


