"""Memory RAG tool for openshrimp plugin system.

Store and retrieve information using LangChain PGVector and OpenRouter embeddings.
Uses sentence-transformers/all-minilm-l6-v2 by default (override via EMBEDDING_MODEL).
"""

import os
import sys
from pathlib import Path
from urllib.parse import quote_plus

from langchain_core.tools import tool
from langchain_openai import OpenAIEmbeddings
from langchain_community.vectorstores import PGVector

# Add src directory to path so we can import schemas when run standalone
_src = Path(__file__).resolve().parents[2]
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from schemas import ToolResult


# Embedding dimension for sentence-transformers/all-minilm-l6-v2 (and most all-MiniLM models)
EMBEDDING_DIMENSION = 384

_vector_store: PGVector | None = None


def _connection_string() -> str:
    """Build PostgreSQL connection string from env (same params as db.py)."""
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = int(os.environ.get("POSTGRES_PORT", "5432"))
    user = os.environ.get("POSTGRES_USER", "postgres")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    dbname = os.environ.get("POSTGRES_DB", "postgres")
    # URL-encode password for connection string
    enc_password = quote_plus(password) if password else ""
    return f"postgresql://{user}:{enc_password}@{host}:{port}/{dbname}"


def _get_embeddings() -> OpenAIEmbeddings:
    """OpenRouter-backed embeddings (OpenAI-compatible API)."""
    api_key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise ValueError(
            "OPENROUTER_API_KEY (or OPENAI_API_KEY) must be set for memory_rag embeddings."
        )
    model = os.environ.get(
        "EMBEDDING_MODEL",
        "sentence-transformers/all-minilm-l6-v2",
    )
    return OpenAIEmbeddings(
        model=model,
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
    )


def _get_vector_store() -> PGVector:
    """Lazy-init PGVector store; creates table on first use."""
    global _vector_store
    if _vector_store is not None:
        return _vector_store
    collection_name = os.environ.get("MEMORY_COLLECTION_NAME", "openshrimp_memory")
    _vector_store = PGVector(
        connection_string=_connection_string(),
        embedding_function=_get_embeddings(),
        embedding_length=EMBEDDING_DIMENSION,
        collection_name=collection_name,
        use_jsonb=True,
    )
    return _vector_store


@tool
def memory_add(content: str, source: str = "") -> str:
    """Add a piece of information to long-term memory for later retrieval.

    Use this when you want to remember a fact, finding, or summary for future
    conversations. The content will be stored and can be retrieved by semantic
    search (memory_retrieve).

    Args:
        content: The text to store (e.g. a fact, summary, or note).
        source: Optional context or source (e.g. URL or topic) for the content.
    """
    try:
        store = _get_vector_store()
        text = content.strip()
        if not text:
            result = ToolResult(
                status="error",
                data="Content to store cannot be empty.",
                plugin="memory_rag",
                extra={"content_length": 0},
            )
            return result.to_string()
        metadata = {} if not source.strip() else {"source": source.strip()}
        ids = store.add_texts([text], metadatas=[metadata])
        result = ToolResult(
            status="ok",
            data=f"Stored in memory (id: {ids[0] if ids else 'ok'}).",
            plugin="memory_rag",
            extra={"content_length": len(text), "source": source or None},
        )
        return result.to_string()
    except Exception as e:
        result = ToolResult(
            status="error",
            data=str(e),
            plugin="memory_rag",
            extra={},
        )
        return result.to_string()


@tool
def memory_retrieve(query: str, top_k: int = 5) -> str:
    """Search long-term memory for relevant past information.

    Use this when you need to recall something the user or the system previously
    stored, or when answering questions that might be informed by past context.

    Args:
        query: Natural language query (what you are looking for).
        top_k: Maximum number of results to return (default 5).
    """
    try:
        store = _get_vector_store()
        docs = store.similarity_search(query, k=min(max(1, top_k), 20))
        if not docs:
            result = ToolResult(
                status="ok",
                data="No relevant memories found.",
                plugin="memory_rag",
                extra={"query": query, "top_k": top_k},
            )
            return result.to_string()
        parts = []
        for i, doc in enumerate(docs, 1):
            source = doc.metadata.get("source", "")
            line = doc.page_content
            if source:
                line = f"[{source}] {line}"
            parts.append(f"{i}. {line}")
        result = ToolResult(
            status="ok",
            data="\n".join(parts),
            plugin="memory_rag",
            extra={"query": query, "top_k": top_k, "returned": len(docs)},
        )
        return result.to_string()
    except Exception as e:
        result = ToolResult(
            status="error",
            data=str(e),
            plugin="memory_rag",
            extra={"query": query},
        )
        return result.to_string()


TOOLS = [memory_add, memory_retrieve]
