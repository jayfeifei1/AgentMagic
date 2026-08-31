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
