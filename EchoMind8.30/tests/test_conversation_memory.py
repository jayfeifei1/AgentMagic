import asyncio

from memory.conversation_memory import MemoryManager


def test_episodic_search_uses_chroma_and_filter_for_session_scope():
    manager = object.__new__(MemoryManager)
    calls = []

    async def query_episodic(query_text, n_results, where):
        calls.append(where)
        return {"documents": [["已存储的会话摘要"]]}

    manager._query_episodic = query_episodic

    history = asyncio.run(manager._search_episodic("u-p3", "c-p3", "退款进度"))

    assert history == ["已存储的会话摘要"]
    assert calls[0] == {
        "$and": [
            {"user_id": "u-p3"},
            {"conv_id": "c-p3"},
        ]
    }
    assert calls[1] == {"user_id": "u-p3"}


def test_episodic_query_uses_local_bge_vector():
    manager = object.__new__(MemoryManager)
    calls = []

    class Collection:
        def query(self, **kwargs):
            calls.append(kwargs)
            return {"documents": [["退款历史摘要"]]}

    async def embed_texts(texts):
        assert texts == ["退款进度"]
        return [[0.1, 0.2, 0.3]]

    manager._episodic = Collection()
    manager._embed_texts = embed_texts

    result = asyncio.run(manager._query_episodic("退款进度", 5, {"user_id": "u-p3"}))

    assert result == {"documents": [["退款历史摘要"]]}
    assert calls == [{
        "query_embeddings": [[0.1, 0.2, 0.3]],
        "n_results": 5,
        "where": {"user_id": "u-p3"},
        "include": ["documents"],
    }]
