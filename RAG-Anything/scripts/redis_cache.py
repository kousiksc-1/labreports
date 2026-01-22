"""
Redis cache for chat context window

This module provides Redis-based caching for chat message history,
significantly improving performance by reducing PostgreSQL queries.
"""
import os
import json
import redis
from typing import List, Tuple, Optional
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
load_dotenv(ROOT / ".env")

# Redis configuration
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD") or None
CONTEXT_CACHE_TTL = int(os.getenv("CONTEXT_CACHE_TTL", "3600"))  # 1 hour default

# Initialize Redis client
try:
    redis_client = redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        db=REDIS_DB,
        password=REDIS_PASSWORD,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2
    )
    # Test connection
    redis_client.ping()
    REDIS_AVAILABLE = True
    print(f"✅ Redis connected: {REDIS_HOST}:{REDIS_PORT}")
except Exception as e:
    REDIS_AVAILABLE = False
    print(f"⚠️  Redis not available: {e}")
    print("   Falling back to PostgreSQL-only mode")


def get_session_context_key(session_id: str) -> str:
    """Generate Redis key for session context"""
    return f"chat:context:{session_id}"


def get_session_doc_key(session_id: str, file_id: str) -> str:
    """Generate Redis key for a session's uploaded document context"""
    return f"chat:doc:{session_id}:{file_id}"


def cache_uploaded_doc(session_id: str, file_id: str, doc: dict, ttl_seconds: Optional[int] = None) -> None:
    """
    Cache uploaded document context (entities + markdown) in Redis.

    Args:
        session_id: Chat session ID
        file_id: Upload file ID (uuid)
        doc: JSON-serializable dict (entities, markdown_content, filename, etc.)
        ttl_seconds: Optional TTL override (defaults to CONTEXT_CACHE_TTL)
    """
    if not REDIS_AVAILABLE:
        return
    try:
        key = get_session_doc_key(session_id, file_id)
        ttl = ttl_seconds or CONTEXT_CACHE_TTL
        redis_client.setex(key, ttl, json.dumps(doc))
    except Exception as e:
        print(f"⚠️  Redis doc cache write failed: {e}")


def get_cached_uploaded_doc(session_id: str, file_id: str) -> Optional[dict]:
    """Retrieve cached uploaded document context from Redis."""
    if not REDIS_AVAILABLE:
        return None
    try:
        key = get_session_doc_key(session_id, file_id)
        cached = redis_client.get(key)
        if not cached:
            return None
        return json.loads(cached)
    except Exception as e:
        print(f"⚠️  Redis doc cache read failed: {e}")
        return None


def cache_session_messages(
    session_id: str, 
    messages: List[Tuple[str, str, datetime]]
) -> None:
    """
    Cache session messages in Redis
    
    Args:
        session_id: Chat session ID
        messages: List of (role, content, created_at) tuples
    """
    if not REDIS_AVAILABLE:
        return
    
    try:
        key = get_session_context_key(session_id)
        
        # Convert to serializable format
        cache_data = [
            {
                "role": role,
                "content": content,
                "created_at": created_at.isoformat()
            }
            for role, content, created_at in messages
        ]
        
        # Store in Redis with expiration
        redis_client.setex(
            key,
            CONTEXT_CACHE_TTL,
            json.dumps(cache_data)
        )
    except Exception as e:
        print(f"⚠️  Redis cache write failed: {e}")


def get_cached_session_messages(
    session_id: str
) -> Optional[List[Tuple[str, str, datetime]]]:
    """
    Retrieve cached session messages from Redis
    
    Args:
        session_id: Chat session ID
        
    Returns:
        List of messages or None if not cached
    """
    if not REDIS_AVAILABLE:
        return None
    
    try:
        key = get_session_context_key(session_id)
        cached = redis_client.get(key)
        
        if not cached:
            return None
        
        # Deserialize
        cache_data = json.loads(cached)
        return [
            (
                msg["role"],
                msg["content"],
                datetime.fromisoformat(msg["created_at"])
            )
            for msg in cache_data
        ]
    except Exception as e:
        print(f"⚠️  Redis cache read failed: {e}")
        return None


def invalidate_session_cache(session_id: str) -> None:
    """Invalidate cache when new message is added"""
    if not REDIS_AVAILABLE:
        return
    
    try:
        key = get_session_context_key(session_id)
        redis_client.delete(key)
    except Exception as e:
        print(f"⚠️  Redis cache invalidation failed: {e}")


def health_check() -> bool:
    """Check if Redis is available and responding"""
    if not REDIS_AVAILABLE:
        return False
    
    try:
        redis_client.ping()
        return True
    except:
        return False


def get_cache_stats(session_id: str) -> dict:
    """
    Get cache statistics for a session
    
    Returns:
        Dictionary with cache status information
    """
    if not REDIS_AVAILABLE:
        return {
            "redis_available": False,
            "cached": False,
            "ttl_seconds": None
        }
    
    try:
        key = get_session_context_key(session_id)
        exists = redis_client.exists(key)
        ttl = redis_client.ttl(key) if exists else -1
        
        return {
            "redis_available": True,
            "cached": bool(exists),
            "ttl_seconds": ttl if ttl > 0 else None,
            "expires_in_minutes": round(ttl / 60, 1) if ttl > 0 else None
        }
    except Exception as e:
        return {
            "redis_available": False,
            "error": str(e)
        }


def get_active_sessions_count() -> int:
    """Get count of active cached sessions"""
    if not REDIS_AVAILABLE:
        return 0
    
    try:
        pattern = "chat:context:*"
        keys = redis_client.keys(pattern)
        return len(keys)
    except:
        return 0


def clear_all_cache() -> int:
    """
    Clear all cached sessions (use with caution!)
    
    Returns:
        Number of keys deleted
    """
    if not REDIS_AVAILABLE:
        return 0
    
    try:
        pattern = "chat:context:*"
        keys = redis_client.keys(pattern)
        if keys:
            return redis_client.delete(*keys)
        return 0
    except Exception as e:
        print(f"⚠️  Failed to clear cache: {e}")
        return 0

