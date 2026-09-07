import asyncio
from types import SimpleNamespace

from mcp.tool_manager import MCPToolManager, Tool


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def create(self, **kwargs):
        text = self.responses[self.calls]
        self.calls += 1
        return SimpleNamespace(content=[{"type": "text", "text": text}])


def make_manager(responses):
    manager = object.__new__(MCPToolManager)
    manager._model = "test-model"
    manager._tools = {}
    manager._cache = {}
    manager._client = SimpleNamespace(messages=FakeMessages(responses))
    return manager


def make_search_tool():
    async def handler(params, context):
        query = params["query"]
        return [
            {"title": "退款", "section_path": "退款条件", "content": query + " 退款条件", "score": 0.7},
            {"title": "物流", "section_path": "发货物流", "content": query + " 物流", "score": 0.6},
        ]

    return Tool(
        name="knowledge_search",
        description="test",
        handler=handler,
        schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        cache_ttl=300,
        supports_rerank=True,
    )


def test_rewrite_and_rerank_parse_markdown_json_and_report_success():
    manager = make_manager([
        "```json\n[\"退款流程\"]\n```",
        "排序如下：```json\n[2, 0]\n```",
    ])
    manager.register(make_search_tool())

    result = asyncio.run(manager.search_with_rewrite("knowledge_search", "已发货怎么退款", top_k=1))

    assert result.success is True
    assert result.reranked is True
    assert result.data[0]["section_path"] == "发货物流"


def test_rerank_failure_reports_false_and_full_pipeline_cache_is_used():
    manager = make_manager([
        "[\"退款流程\"]",
        "这次没有返回数组",
    ])
    manager.register(make_search_tool())

    first = asyncio.run(manager.search_with_rewrite("knowledge_search", "退款", top_k=1))
    calls_after_first = manager._client.messages.calls
    second = asyncio.run(manager.search_with_rewrite("knowledge_search", "退款", top_k=1))

    assert first.reranked is False
    assert second.cached is True
    assert second.reranked is False
    assert manager._client.messages.calls == calls_after_first


def test_multi_query_recall_deduplicates_by_chroma_chunk_id():
    async def handler(params, context):
        score = 0.9 if params["query"] == "退款时效" else 0.7
        return [{
            "chunk_id": "chroma-refund-001",
            "title": "退款政策",
            "section_path": "退款审核",
            "content": "退款审核完成后将按原路退回。",
            "score": score,
        }]

    manager = make_manager(['["退款时效"]'])
    manager.register(Tool(
        name="knowledge_search",
        description="test",
        handler=handler,
        schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        cache_ttl=300,
        supports_rerank=True,
    ))

    result = asyncio.run(manager.search_with_rewrite("knowledge_search", "退款多久到账", top_k=1))

    assert result.success is True
    assert len(result.data) == 1
    assert result.data[0]["chunk_id"] == "chroma-refund-001"
    assert result.data[0]["score"] == 0.9


def test_clear_cache_removes_raw_and_pipeline_entries_for_tool():
    manager = make_manager([])
    manager._cache = {
        "knowledge_search:key": ([], 9999999999, False),
        "knowledge_search:rewrite:key": ([], 9999999999, True),
        "another_tool:key": ([], 9999999999, False),
    }

    manager.clear_cache("knowledge_search")

    assert list(manager._cache) == ["another_tool:key"]
