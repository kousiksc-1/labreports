"""
raganything package

Exports the RagAnything class, a unified interface that encapsulates:
- LLM-based intent classification (sql, md, general)
- Patient resolution (robust to swapped order and minor typos)
- Report selection (parsed markdown preferred, PDF fallback)
- SQL generation and execution against Postgres
- General knowledge answering

Downstream consumers (CLI, FastAPI) should instantiate RagAnything once
and call its 'ask' or 'answer_md' methods.
"""
from .model import RagAnything

# NOTE: The upstream HKUDS project also provides a `RAGAnything` class that
# depends on LightRAG (`lightrag`). Our FastAPI app (`scripts/07_chat_api.py`)
# uses `RagAnything` from `model.py` and should not require LightRAG.
#
# In Docker (and many local setups), `lightrag` is not installed, so importing
# it unconditionally breaks *any* `import raganything`.
try:
    from .raganything import RAGAnything as RAGAnything  # optional
except Exception:  # pragma: no cover
    RAGAnything = None  # type: ignore

from .config import RAGAnythingConfig as RAGAnythingConfig

__version__ = "1.2.8"
__author__ = "Zirui Guo"
__url__ = "https://github.com/HKUDS/RAG-Anything"

__all__ = ["RagAnything", "RAGAnything", "RAGAnythingConfig"]
