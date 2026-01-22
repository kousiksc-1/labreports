"""FastAPI service for the lab chatbot.

Endpoints
- POST /auth/signup, /auth/login: JWT auth using pbkdf2_sha256 password hashes.
- POST /chat/start: create a chat session (title auto-populates from first user msg).
- GET  /chat/sessions, /chat/messages: list sessions and fetch message history.
- POST /chat/send: persist user message, resolve intent/patient, and return answer.

Implementation notes
- Context window: last N messages (CHAT_CONTEXT_WINDOW) are used to try resolving
  the patient when not explicitly mentioned in the latest turn.
- For MD intent, if the patient cannot be identified, we stop early with a clear
  message instead of producing misleading answers.
"""
import os
import time
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, List, Tuple

import sys
import psycopg
import jwt
from fastapi import FastAPI, Depends, HTTPException, status, Header, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from passlib.context import CryptContext
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
load_dotenv(ROOT / ".env")

# Ensure local project root is importable before any site-packages shadowing
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Import the chat RAG model directly to avoid triggering optional LightRAG imports
# in `raganything/__init__.py` during Docker runs.
from raganything.model import RagAnything  # noqa: E402

# Import Redis cache module
try:
    from redis_cache import (
        get_cached_session_messages,
        cache_session_messages,
        invalidate_session_cache,
        health_check as redis_health_check,
        get_cache_stats,
        cache_uploaded_doc,
        get_cached_uploaded_doc,
    )
    REDIS_ENABLED = True
except ImportError:
    REDIS_ENABLED = False
    print("⚠️  Redis cache module not available, using PostgreSQL only")

# Import cost logging module
try:
    from model_cost_logger import log_model_usage, get_cost_summary, get_daily_costs, init_cost_logging_table
    COST_LOGGING_ENABLED = True
except ImportError:
    COST_LOGGING_ENABLED = False
    print("⚠️  Cost logging module not available")

# Import smart upload parser
try:
    from smart_upload_parser import get_parser
    SMART_PARSER_ENABLED = True
except ImportError:
    SMART_PARSER_ENABLED = False
    print("⚠️  Smart upload parser not available")


JWT_SECRET = os.getenv("JWT_SECRET", "dev-secret-change-me")
JWT_EXPIRES_MIN = int(os.getenv("JWT_EXPIRES_MIN", "120"))
CONTEXT_WINDOW = int(os.getenv("CHAT_CONTEXT_WINDOW", os.getenv("CONTEXT_WINDOW", "20")))
ENABLE_COST_LOGGING = os.getenv("ENABLE_COST_LOGGING", "true").lower() == "true"
DEFAULT_MODEL_NAME = os.getenv("LLM_MODEL_NAME", "gpt-4o-mini")

# Use pbkdf2_sha256 to avoid bcrypt backend/length issues
pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")

app = FastAPI(title="RAG Lab Chat API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup_event():
    """Run on application startup"""
    # Initialize cost tracking table if enabled
    if COST_LOGGING_ENABLED and ENABLE_COST_LOGGING:
        try:
            init_cost_logging_table()
            print("✅ Cost tracking initialized")
        except Exception as e:
            print(f"⚠️  Cost tracking initialization failed: {e}")
    
    # Check Redis health if enabled
    if REDIS_ENABLED:
        try:
            is_healthy = redis_health_check()
            if is_healthy:
                print("✅ Redis cache is healthy")
            else:
                print("⚠️  Redis health check failed")
        except Exception as e:
            print(f"⚠️  Redis error: {e}")


@app.get("/health")
def health_check():
    """Health check endpoint for Docker and load balancers"""
    try:
        # Quick database connectivity check
        with pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        
        return {
            "status": "healthy",
            "database": "connected",
            "redis": "available" if REDIS_ENABLED and redis_health_check() else "unavailable"
        }
    except Exception as e:
        return {
            "status": "unhealthy",
            "error": str(e)
        }


def pg_conn():
    """
    Open a short-lived Postgres connection using .env DSN parameters.
    Handlers use context managers so connections are always closed promptly.
    """
    dsn = f"host={os.getenv('POSTGRES_HOST','localhost')} port={os.getenv('POSTGRES_PORT','5432')} dbname={os.getenv('POSTGRES_DATABASE')} user={os.getenv('POSTGRES_USER')} password={os.getenv('POSTGRES_PASSWORD')}"
    return psycopg.connect(dsn)


# Unified RAG model (handles sql/md/general and helpers)
rag = RagAnything()


def create_token(user_id: str, email: str) -> str:
    """
    Create a short-lived JWT with user id and email as claims.
    The token's TTL is controlled by JWT_EXPIRES_MIN in .env.
    """
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "email": email,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=JWT_EXPIRES_MIN)).timestamp()),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def verify_token(auth_header: Optional[str]) -> Tuple[str, str]:
    """
    Parse and verify a Bearer token from the Authorization header.
    Returns (user_id, email) on success, otherwise raises 401.
    """
    if not auth_header or not auth_header.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing or invalid Authorization header")
    token = auth_header.split(" ", 1)[1]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return payload["sub"], payload.get("email", "")
    except jwt.PyJWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


class SignupBody(BaseModel):
    email: EmailStr
    password: str


class LoginBody(BaseModel):
    email: EmailStr
    password: str


class StartChatBody(BaseModel):
    title: Optional[str] = None


class SendBody(BaseModel):
    session_id: str
    message: str
    file_id: Optional[str] = None


class UploadResponse(BaseModel):
    file_id: str
    filename: str
    markdown_content: str
    content_type: str


@app.post("/auth/signup")
def signup(body: SignupBody):
    """
    Register a new user:
    - Hashes password with pbkdf2_sha256
    - Inserts into users table (email unique)
    - Returns a JWT for immediate use
    """
    password_hash = pwd_context.hash(body.password)
    with pg_conn() as conn, conn.cursor() as cur:
        # try insert user
        try:
            cur.execute(
                "INSERT INTO users (id, email, password_hash) VALUES (gen_random_uuid(), %s, %s) RETURNING id",
                (body.email, password_hash),
            )
            user_id = cur.fetchone()[0]
        except Exception as e:
            # maybe email exists
            raise HTTPException(status_code=400, detail="User already exists")
    token = create_token(str(user_id), body.email)
    return {"user_id": str(user_id), "token": token}


@app.post("/auth/login")
def login(body: LoginBody):
    """
    Authenticate a user:
    - Verifies email/password against stored hash
    - Returns a fresh JWT
    """
    with pg_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, password_hash FROM users WHERE email=%s", (body.email,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        user_id, password_hash = row
        if not pwd_context.verify(body.password, password_hash):
            raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_token(str(user_id), body.email)
    return {"user_id": str(user_id), "token": token}


@app.post("/chat/start")
def start_chat(body: StartChatBody, authorization: Optional[str] = Header(None)):
    """
    Start a new chat session for the authenticated user.
    The frontend can optionally supply a title; otherwise it is auto-set
    from the first user message when sent.
    """
    user_id, _ = verify_token(authorization)
    with pg_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO chat_sessions (id, user_id, title) VALUES (gen_random_uuid(), %s, %s) RETURNING id",
            (user_id, body.title or None),
        )
        session_id = cur.fetchone()[0]
    return {"session_id": str(session_id)}

@app.get("/chat/sessions")
def list_sessions(authorization: Optional[str] = Header(None)):
    """
    List current user's chat sessions, newest first.
    Useful to populate the collapsible left sidebar in the UI.
    """
    user_id, _ = verify_token(authorization)
    with pg_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, COALESCE(title, 'New chat'), updated_at FROM chat_sessions WHERE user_id=%s ORDER BY updated_at DESC",
            (user_id,),
        )
        rows = cur.fetchall() or []
    return [{"id": str(r[0]), "title": r[1], "updated_at": r[2].isoformat()} for r in rows]

@app.get("/chat/messages")
def get_messages(session_id: str, authorization: Optional[str] = Header(None)):
    """
    Fetch the ordered message history for a session.
    This is the long-term memory; the model uses only a sliding context window.
    """
    user_id, _ = verify_token(authorization)
    with pg_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT user_id FROM chat_sessions WHERE id=%s", (session_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Session not found")
        if str(row[0]) != user_id:
            raise HTTPException(status_code=403, detail="Forbidden")
        cur.execute(
            "SELECT role, content, created_at FROM chat_messages WHERE session_id=%s ORDER BY created_at ASC",
            (session_id,),
        )
        msgs = cur.fetchall() or []
    return [{"role": r, "content": c, "created_at": t.isoformat()} for (r, c, t) in msgs]


def fetch_last_messages(session_id: str, limit: int) -> List[Tuple[str, str, datetime]]:
    """
    Retrieve the last N messages with Redis caching.
    
    This function first checks Redis cache for the session's message history.
    If found (cache hit), returns cached data immediately (10-100x faster).
    If not found (cache miss), fetches from PostgreSQL and caches the result.
    
    Args:
        session_id: Chat session ID
        limit: Maximum number of messages to retrieve
        
    Returns:
        List of (role, content, created_at) tuples
    """
    # Try Redis cache first
    if REDIS_ENABLED and redis_health_check():
        cached_messages = get_cached_session_messages(session_id)
        if cached_messages is not None:
            print(f"✅ Cache HIT for session {session_id[:8]}... ({len(cached_messages)} messages)")
            return cached_messages[-limit:]  # Return last N messages
        print(f"⚠️  Cache MISS for session {session_id[:8]}...")
    
    # Cache miss or Redis unavailable - fetch from PostgreSQL
    with pg_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT role, content, created_at FROM chat_messages WHERE session_id=%s ORDER BY created_at DESC LIMIT %s",
            (session_id, limit),
        )
        rows = cur.fetchall() or []
    
    rows.reverse()
    
    # Cache for next time (if Redis is available)
    if REDIS_ENABLED and redis_health_check() and rows:
        cache_session_messages(session_id, rows)
        print(f"💾 Cached {len(rows)} messages for session {session_id[:8]}...")
    
    return rows


def save_message(session_id: str, role: str, content: str) -> None:
    """
    Append a message to the session transcript, update session timestamp,
    and invalidate Redis cache to ensure fresh data on next fetch.
    """
    with pg_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO chat_messages (session_id, role, content) VALUES (%s, %s, %s)",
            (session_id, role, content),
        )
        cur.execute("UPDATE chat_sessions SET updated_at=now() WHERE id=%s", (session_id,))
    
    # Invalidate cache so next fetch will rebuild it with the new message
    if REDIS_ENABLED and redis_health_check():
        invalidate_session_cache(session_id)


@app.post("/chat/send")
def send_message(body: SendBody, authorization: Optional[str] = Header(None)):
    """
    Main chat endpoint:
    1) Verifies session ownership and persists the user message
    2) Auto-sets session title from the first user message if empty
    3) If file_id provided, parse the document now (lazy parsing)
    4) Builds a short-term context (last N messages)
    5) Resolves patient and intent using the unified RagAnything model
    6) Early-stops if MD intent but no patient can be identified
    7) Routes to rag.ask() (handles SQL/MD/GENERAL) and persists assistant reply

    Returns the answer and lightweight metadata (context window size, intent,
    resolved patient id/name, parsing_status) so the UI can display progress.
    """
    user_id, _ = verify_token(authorization)
    # ensure session belongs to user
    with pg_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT user_id FROM chat_sessions WHERE id=%s", (body.session_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Session not found")
        owner = str(row[0])
        if owner != user_id:
            raise HTTPException(status_code=403, detail="Forbidden")

    # persist user message
    save_message(body.session_id, "user", body.message)
    # set title from first user message if missing
    with pg_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT title FROM chat_sessions WHERE id=%s", (body.session_id,))
        row = cur.fetchone()
        if row and (row[0] is None or not row[0].strip()):
            auto_title = (body.message.strip()[:60]).strip()
            if auto_title:
                cur.execute("UPDATE chat_sessions SET title=%s WHERE id=%s", (auto_title, body.session_id))

    # Use document content if file_id is provided
    # File should already be parsed and saved in data/parsed by the upload endpoint
    document_content = ""
    parsed_patient_id = None
    parsed_patient_name = None
    parsed_doc_filename = None
    if body.file_id:
        try:
            # Security: Files are stored per session, so file_id can only be accessed within its session
            # The upload_dir path includes session_id, ensuring isolation
            # If smart upload already cached parsed entities + markdown for this session/file_id, use it.
            if REDIS_ENABLED:
                try:
                    cached_doc = get_cached_uploaded_doc(body.session_id, body.file_id)
                    if cached_doc:
                        document_content = cached_doc.get("markdown_content") or ""
                        parsed_doc_filename = cached_doc.get("filename") or None
                        entities = cached_doc.get("entities") or {}
                        parsed_patient_id = entities.get("patient_id") or None
                        parsed_patient_name = entities.get("patient_name") or None
                        if document_content:
                            print("📄 Using cached parsed upload context from Redis")
                except Exception as e:
                    print(f"⚠️  Failed reading doc cache from Redis: {e}")

            # First, try to find already-parsed markdown in data/parsed
            # The smart upload endpoint saves structured markdown there
            # Files are stored per session to ensure isolation
            parse_dir = Path(ROOT) / "data" / "parsed"
            upload_dir = Path(ROOT) / "data" / "uploads" / user_id / body.session_id
            
            # Check if markdown was already extracted by smart upload
            markdown_found = False
            
            # Strategy 1: Look in data/parsed for recently created files
            # Smart upload creates files like: P51023514130014_Thyroid_20260107.md
            if parse_dir.exists() and SMART_PARSER_ENABLED:
                # Find the uploaded file's timestamp
                file_path = None
                for ext in [".pdf", ".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif"]:
                    potential_path = upload_dir / f"{body.file_id}{ext}"
                    if potential_path.exists():
                        file_path = potential_path
                        break
                
                if file_path:
                    upload_time = file_path.stat().st_mtime
                    
                    # Find markdown files created around the same time (within 60 seconds)
                    for md_file in parse_dir.glob("*.md"):
                        md_time = md_file.stat().st_mtime
                        if abs(md_time - upload_time) < 60:  # Within 60 seconds
                            document_content = md_file.read_text(encoding="utf-8")
                            markdown_found = True
                            # Extract patient ID from filename (format: PATIENTID_TESTTYPE_DATE.md)
                            parsed_patient_id = md_file.stem.split('_')[0]
                            parsed_doc_filename = md_file.name
                            print(f"📄 Using smart-parsed markdown: {md_file.name} (Patient: {parsed_patient_id})")
                            break
            
            # Strategy 2: Check temp directory in session-specific location
            if not markdown_found:
                temp_output = upload_dir / "temp" / body.file_id
                if temp_output.exists():
                    for md_file in temp_output.glob("*.md"):
                        document_content = md_file.read_text(encoding="utf-8")
                        markdown_found = True
                        print(f"📄 Using pre-parsed markdown from temp: {md_file.name}")
                        break
            
            # Strategy 3: Lazy parsing (if file wasn't pre-processed)
            if not markdown_found:
                # Find the uploaded file
                file_path = None
                for ext in [".pdf", ".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif"]:
                    potential_path = upload_dir / f"{body.file_id}{ext}"
                    if potential_path.exists():
                        file_path = potential_path
                        break
            
                if file_path and not markdown_found:
                    # Set output directory
                    output_dir = upload_dir / "parsed" / body.file_id
                    output_dir.mkdir(parents=True, exist_ok=True)
                    
                    # Parse with appropriate parser based on file type
                    file_ext = file_path.suffix.lower()
                    image_extensions = {'.png', '.jpg', '.jpeg', '.bmp', '.gif', '.webp', '.tiff', '.tif'}
                    
                    if file_ext in image_extensions:
                        # For images, use Vision API
                        print(f"Image file detected - using Vision API...")
                        
                        if SMART_PARSER_ENABLED:
                            # Use smart parser's Vision API method
                            try:
                                parser = get_parser()
                                markdown_text = parser.parse_image_with_vision(file_path)
                                if markdown_text:
                                    document_content = markdown_text
                                    print(f"✅ Extracted text from image using Vision API")
                            except Exception as e:
                                print(f"⚠️  Vision API failed: {e}")
                        else:
                            # Fallback: Direct Vision API call
                            try:
                                import base64
                                from openai import OpenAI
                                
                                client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
                                
                                with open(file_path, 'rb') as f:
                                    image_data = base64.b64encode(f.read()).decode('utf-8')
                                
                                ext = file_path.suffix.lower()
                                mime_types = {
                                    '.png': 'image/png',
                                    '.jpg': 'image/jpeg',
                                    '.jpeg': 'image/jpeg',
                                    '.bmp': 'image/bmp',
                                    '.gif': 'image/gif',
                                    '.webp': 'image/webp'
                                }
                                mime_type = mime_types.get(ext, 'image/jpeg')
                                
                                response = client.chat.completions.create(
                                    model="gpt-4o",
                                    messages=[
                                        {
                                            "role": "user",
                                            "content": [
                                                {
                                                    "type": "text",
                                                    "text": "Extract ALL text from this medical lab report image. Preserve all test names, values, reference ranges, patient information, dates, units, and table structure. Return in a clear, structured format."
                                                },
                                                {
                                                    "type": "image_url",
                                                    "image_url": {
                                                        "url": f"data:{mime_type};base64,{image_data}"
                                                    }
                                                }
                                            ]
                                        }
                                    ],
                                    max_tokens=4096
                                )
                                
                                document_content = response.choices[0].message.content
                                print(f"✅ Extracted text from image using Vision API (fallback)")
                            except Exception as e:
                                print(f"⚠️  Vision API fallback failed: {e}")
                        
                        content_list = []
                    else:
                        # For PDFs and docs, use Docling
                        from raganything.parser import DoclingParser
                        parser = DoclingParser()
                        content_list = parser.parse_document(
                            file_path=file_path,
                            output_dir=str(output_dir),
                            method="auto"
                        )
                        
                        # Extract markdown content from Docling output
                        # Docling creates: output_dir/{file_id}/{file_id}/docling/{file_id}.md
                        md_file = None
                        possible_paths = [
                            output_dir / file_path.stem / "docling" / f"{file_path.stem}.md",
                            output_dir / file_path.stem / file_path.stem / "docling" / f"{file_path.stem}.md",
                            output_dir / f"{body.file_id}.md",
                        ]
                        for possible_path in possible_paths:
                            if possible_path.exists():
                                md_file = possible_path
                                break
                        
                        if md_file and md_file.exists():
                            with open(md_file, "r", encoding="utf-8") as f:
                                document_content = f.read()
                    
                    # Fallback: Generate markdown from content_list if file not found
                    if not document_content and content_list:
                        for item in content_list:
                            if isinstance(item, dict) and "text" in item:
                                document_content += item["text"] + "\n\n"
        except Exception as e:
            # Log error but continue with query
            print(f"Error parsing document: {e}")

    # If a file is attached and we have parsed markdown, answer directly from that content.
    # This avoids forcing "md" intent + patient-id mapping for generic questions like "what is it about".
    if body.file_id and document_content:
        try:
            doc_name = parsed_doc_filename or f"{body.file_id}"
            answer_text = rag.ask_openai_from_md_files(body.message, [(doc_name, document_content)])
            save_message(body.session_id, "assistant", answer_text)

            cache_stats = get_cache_stats(body.session_id) if REDIS_ENABLED else None
            return {
                "answer": answer_text,
                "context_window": CONTEXT_WINDOW,
                "messages_used": len(fetch_last_messages(body.session_id, CONTEXT_WINDOW)),
                "intent": "doc",
                "patient_id": parsed_patient_id,
                "patient_name": parsed_patient_name,
                "document_parsed": True,
                "cache_stats": cache_stats if cache_stats else None,
                "cost_info": None,
            }
        except Exception as e:
            print(f"⚠️  Direct document QA failed, falling back to rag.ask(): {e}")

    # collect last N messages for context (long-term memory is in DB; we keep a sliding window)
    history = fetch_last_messages(body.session_id, CONTEXT_WINDOW)

    # Build conversation context from history
    conversation_context = ""
    if history and len(history) > 1:  # More than just the current message
        # Format: exclude the current user message (already in body.message)
        # History is ordered newest first, so reverse it
        reversed_history = list(reversed(history))
        context_lines = []
        for role, content, created_at in reversed_history:
            # Skip the last user message (it's the current one)
            if role == "user" and content == body.message:
                continue
            prefix = "User" if role == "user" else "Assistant"
            context_lines.append(f"{prefix}: {content}")
        
        if context_lines:
            conversation_context = "[Previous conversation]\n" + "\n".join(context_lines) + "\n\n"

    # Build final query with conversation history and document content
    q = body.message
    if conversation_context or document_content:
        parts = []
        if conversation_context:
            parts.append(conversation_context)
        if document_content:
            # If we know the patient from parsed filename, add it explicitly
            doc_header = "[Document Context]"
            if parsed_patient_id:
                doc_header = f"[Document Context - Patient ID: {parsed_patient_id}]"
            parts.append(f"{doc_header}\n{document_content[:3000]}")
        parts.append(f"[Current Question]\n{body.message}")
        q = "\n".join(parts)
    
    # Detect patient and intent ONCE for UI display (rag.ask will use its own internal detection)
    # This is just for returning metadata to the frontend for the progress display
    pid, pname, intent = None, None, "md"
    try:
        pid_to_name = rag.load_pid_to_name()
        
        # If we already extracted patient ID from structured markdown filename, use it!
        if parsed_patient_id and pid_to_name:
            pid = parsed_patient_id
            pname = pid_to_name.get(pid, None)
            if pname:
                print(f"Patient name - {pname}")
                print(f"Patient id - {pid}")
        elif pid_to_name and document_content:
            # Otherwise, try to detect from document content
            combined_text = f"{body.message}\n\nDocument excerpt:\n{document_content[:1000]}"
            pid, pname = rag.llm_resolve_patient(combined_text, pid_to_name)
        
        intent = rag.llm_classify_intent(body.message) or "md"
    except Exception as e:
        print(f"Warning: Could not detect patient/intent for UI: {e}")
    
    # Main RAG query (this will do its own internal patient/intent detection)
    # Track start time for estimation if no token count available
    import time
    start_time = time.time()
    
    answer_text = rag.ask(q)
    
    query_time = time.time() - start_time
    
    # Log model usage and cost (if enabled)
    cost_log = None
    if COST_LOGGING_ENABLED and ENABLE_COST_LOGGING:
        # Estimate tokens if not available from response
        # (Better: extract from actual API response if available)
        estimated_input_tokens = len(q.split()) * 1.3  # Rough estimate
        estimated_output_tokens = len(answer_text.split()) * 1.3
        
        try:
            cost_log = log_model_usage(
                user_id=user_id,
                session_id=body.session_id,
                operation_type="chat_query",
                model_name=DEFAULT_MODEL_NAME,
                input_tokens=int(estimated_input_tokens),
                output_tokens=int(estimated_output_tokens),
                metadata={
                    "patient_id": pid,
                    "patient_name": pname,
                    "intent": intent,
                    "has_document": bool(document_content),
                    "query_length": len(q),
                    "response_length": len(answer_text),
                    "query_time_seconds": round(query_time, 2)
                }
            )
        except Exception as e:
            print(f"⚠️  Cost logging failed: {e}")
    
    # persist assistant message
    save_message(body.session_id, "assistant", answer_text)

    # Get cache statistics (optional, for monitoring)
    cache_stats = {}
    if REDIS_ENABLED:
        cache_stats = get_cache_stats(body.session_id)

    return {
        "answer": answer_text,
        "context_window": CONTEXT_WINDOW,
        "messages_used": len(history),
        "intent": intent,
        "patient_id": pid,
        "patient_name": pname,
        "document_parsed": bool(document_content),
        "cache_stats": cache_stats if cache_stats else None,
        "cost_info": cost_log.get("costs") if cost_log else None,
    }


@app.post("/chat/upload")
async def upload_file(
    file: UploadFile = File(...),
    session_id: str = Form(...),  # Required: files must be tied to a session
    authorization: Optional[str] = Header(None)
):
    """
    Smart file upload with entity extraction:
    1. Validates session belongs to user
    2. Saves uploaded file in session-specific directory
    3. Parses with Docling (PDF) or OCR (images)
    4. Extracts entities: patient name, test type, analytes (no regex!)
    5. Saves to data/parsed with proper filename
    6. Returns extracted entities + markdown content
    
    Files are isolated per session - each session can only access its own uploads.
    """
    user_id, _ = verify_token(authorization)
    
    # Validate session belongs to user
    with pg_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT user_id FROM chat_sessions WHERE id=%s", (session_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Session not found")
        owner = str(row[0])
        if owner != user_id:
            raise HTTPException(status_code=403, detail="Session does not belong to user")
    
    # Validate file type
    allowed_extensions = {".pdf", ".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif"}
    file_ext = Path(file.filename).suffix.lower()
    if file_ext not in allowed_extensions:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {file_ext}. Supported types: {', '.join(allowed_extensions)}"
        )
    
    try:
        # Create session-specific directory for uploaded files
        # Files are isolated per session to prevent cross-session access
        upload_dir = Path(ROOT) / "data" / "uploads" / user_id / session_id
        upload_dir.mkdir(parents=True, exist_ok=True)
        
        # Generate unique file ID
        file_id = str(uuid.uuid4())
        file_path = upload_dir / f"{file_id}{file_ext}"
        
        # Save uploaded file
        with open(file_path, "wb") as f:
            content = await file.read()
            f.write(content)
        
        print(f"\n📤 File uploaded: {file.filename} → {file_id}{file_ext}")
        
        # Smart parsing: Extract entities from content
        if SMART_PARSER_ENABLED:
            print("🔍 Running smart entity extraction...")
            
            # Create temp output directory for parsing
            temp_output = upload_dir / "temp" / file_id
            temp_output.mkdir(parents=True, exist_ok=True)
            
            # Process upload (parse + extract entities)
            parser = get_parser()
            result = parser.process_upload(file_path, temp_output)
            
            if result["success"]:
                entities = result["entities"]
                markdown_content = result["markdown_content"]
                parsed_filename = result["filename"]
                
                print(f"✅ Entities extracted:")
                print(f"   Patient: {entities['patient_name']}")
                print(f"   Test: {entities['test_type']}")
                print(f"   Date: {entities['test_date']}")
                print(f"   Analytes: {len(entities.get('analytes', []))}")

                # Cache parsed upload context per session/file_id so /chat/send can answer
                # directly from structured content without requiring patient-id mapping.
                if REDIS_ENABLED and session_id:
                    try:
                        cache_uploaded_doc(
                            session_id=session_id,
                            file_id=file_id,
                            doc={
                                "file_id": file_id,
                                "filename": parsed_filename,
                                "entities": entities,
                                "markdown_content": markdown_content,
                                "markdown_path": str(result.get("markdown_path") or ""),
                                "uploaded_filename": file.filename,
                                "uploaded_ext": file_ext,
                            },
                        )
                        print("💾 Cached parsed upload context in Redis")
                    except Exception as e:
                        print(f"⚠️  Failed caching parsed upload context: {e}")
                
                return {
                    "file_id": file_id,
                    "filename": file.filename,
                    "parsed_filename": parsed_filename,
                    "content_type": "image" if file_ext in {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif"} else "pdf",
                    "entities": entities,
                    "markdown_content": markdown_content[:1000],  # First 1000 chars for preview
                    "processing_status": "success"
                }
            else:
                # Parsing failed - return basic info
                return {
                    "file_id": file_id,
                    "filename": file.filename,
                    "content_type": "image" if file_ext in {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif"} else "pdf",
                    "processing_status": "failed",
                    "error": result.get("error", "Unknown error")
                }
        
        else:
            # Smart parser not available - return basic upload info
            print("⚠️  Smart parser not available, basic upload only")
        return {
            "file_id": file_id,
            "filename": file.filename,
            "file_path": str(file_path),
                "content_type": "image" if file_ext in {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif"} else "pdf",
                "processing_status": "basic"
        }
    
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"File upload failed: {str(e)}")


@app.get("/api/costs/summary")
def get_costs_summary(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    authorization: Optional[str] = Header(None)
):
    """
    Get cost summary for the authenticated user
    
    Query params:
        start_date: Filter from date (YYYY-MM-DD)
        end_date: Filter to date (YYYY-MM-DD)
    
    Returns summary of API usage and costs
    """
    user_id, _ = verify_token(authorization)
    
    if not COST_LOGGING_ENABLED:
        raise HTTPException(
            status_code=503,
            detail="Cost logging is not enabled"
        )
    
    try:
        summary = get_cost_summary(
            user_id=user_id,
            start_date=start_date,
            end_date=end_date
        )
        return summary
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get cost summary: {str(e)}")


@app.get("/api/costs/daily")
def get_costs_daily(
    days: int = 30,
    authorization: Optional[str] = Header(None)
):
    """
    Get daily cost breakdown for last N days
    
    Query params:
        days: Number of days to retrieve (default: 30)
    
    Returns daily breakdown of usage and costs
    """
    user_id, _ = verify_token(authorization)
    
    if not COST_LOGGING_ENABLED:
        raise HTTPException(
            status_code=503,
            detail="Cost logging is not enabled"
        )
    
    try:
        daily_costs = get_daily_costs(days=min(days, 365))  # Max 1 year
        return {"daily_costs": daily_costs}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get daily costs: {str(e)}")


@app.get("/api/costs/session/{session_id}")
def get_session_costs(
    session_id: str,
    authorization: Optional[str] = Header(None)
):
    """
    Get cost breakdown for a specific session
    
    Returns all costs associated with a chat session
    """
    user_id, _ = verify_token(authorization)
    
    # Verify session belongs to user
    with pg_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT user_id FROM chat_sessions WHERE id=%s", (session_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Session not found")
        if str(row[0]) != user_id:
            raise HTTPException(status_code=403, detail="Forbidden")
    
    if not COST_LOGGING_ENABLED:
        raise HTTPException(
            status_code=503,
            detail="Cost logging is not enabled"
        )
    
    try:
        summary = get_cost_summary(session_id=session_id)
        return summary
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get session costs: {str(e)}")


if __name__ == "__main__":
    # Development runner. In production, prefer `uvicorn` CLI or a process manager.
    import uvicorn
    uvicorn.run(app, host=os.getenv("SERVER_HOST", "127.0.0.1"), port=int(os.getenv("SERVER_PORT", "8000")))


