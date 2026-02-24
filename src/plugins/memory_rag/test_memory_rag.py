"""Unit tests for memory_rag user isolation."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure src is on path
_src = Path(__file__).resolve().parents[2]
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))


def _make_fake_doc(content: str, metadata: dict | None = None):
    doc = MagicMock()
    doc.page_content = content
    doc.metadata = metadata or {}
    return doc


@pytest.fixture(autouse=True)
def reset_vector_store():
    """Reset the cached vector store between tests."""
    import src.plugins.memory_rag.tool as mod
    original = mod._vector_store
    mod._vector_store = None
    yield
    mod._vector_store = original


@patch("src.plugins.memory_rag.tool._get_vector_store")
@patch("src.plugins.memory_rag.tool._get_user_id", return_value=42)
def test_memory_add_includes_user_id_in_metadata(mock_uid, mock_store):
    from src.plugins.memory_rag.tool import memory_add

    store = MagicMock()
    store.add_texts.return_value = ["abc123"]
    mock_store.return_value = store

    memory_add.invoke({"content": "hello world", "source": ""})

    store.add_texts.assert_called_once()
    _, kwargs = store.add_texts.call_args
    metadatas = store.add_texts.call_args[1].get("metadatas") or store.add_texts.call_args[0][1]
    assert metadatas[0]["user_id"] == 42


@patch("src.plugins.memory_rag.tool._get_vector_store")
@patch("src.plugins.memory_rag.tool._get_user_id", return_value=42)
def test_memory_retrieve_filters_by_user_id(mock_uid, mock_store):
    from src.plugins.memory_rag.tool import memory_retrieve

    store = MagicMock()
    store.similarity_search.return_value = [_make_fake_doc("remembered thing")]
    mock_store.return_value = store

    memory_retrieve.invoke({"query": "thing", "top_k": 3})

    store.similarity_search.assert_called_once()
    call_kwargs = store.similarity_search.call_args[1]
    assert call_kwargs.get("filter") == {"user_id": 42}


@patch("src.plugins.memory_rag.tool._get_vector_store")
@patch("src.plugins.memory_rag.tool._get_user_id", return_value=None)
def test_memory_add_no_user_id_when_no_context(mock_uid, mock_store):
    from src.plugins.memory_rag.tool import memory_add

    store = MagicMock()
    store.add_texts.return_value = ["xyz"]
    mock_store.return_value = store

    memory_add.invoke({"content": "standalone fact", "source": "wiki"})

    _, kwargs = store.add_texts.call_args
    metadatas = store.add_texts.call_args[1].get("metadatas") or store.add_texts.call_args[0][1]
    assert "user_id" not in metadatas[0]
    assert metadatas[0].get("source") == "wiki"


@patch("src.plugins.memory_rag.tool._get_vector_store")
@patch("src.plugins.memory_rag.tool._get_user_id", return_value=None)
def test_memory_retrieve_no_filter_when_no_context(mock_uid, mock_store):
    from src.plugins.memory_rag.tool import memory_retrieve

    store = MagicMock()
    store.similarity_search.return_value = []
    mock_store.return_value = store

    memory_retrieve.invoke({"query": "anything"})

    call_kwargs = store.similarity_search.call_args[1]
    assert call_kwargs.get("filter") is None
