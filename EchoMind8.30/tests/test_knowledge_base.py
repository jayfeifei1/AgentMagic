import asyncio
import hashlib
from pathlib import Path

from mcp.knowledge_base import KnowledgeBase


class FakeCollection:
    def __init__(self, documents=None):
        self.documents = documents or {}
        self.add_calls = []
        self.query_calls = []

    def add(self, **kwargs):
        self.add_calls.append(kwargs)
        for doc_id, document, metadata in zip(
            kwargs["ids"], kwargs["documents"], kwargs["metadatas"]
        ):
            self.documents[doc_id] = (document, metadata)

    def query(self, **kwargs):
        self.query_calls.append(kwargs)
        return {
            "documents": [["退款审核通过后原路退回"]],
            "metadatas": [[{"title": "退款政策", "chunk_index": 0}]],
            "distances": [[0.2]],
        }

    def count(self):
        return len(self.documents)

    def delete(self, ids):
        for doc_id in ids:
            self.documents.pop(doc_id, None)

    def get(self, ids=None, include=None, where=None):
        ids = ids or list(self.documents)
        selected = [(doc_id, self.documents[doc_id]) for doc_id in ids if doc_id in self.documents]
        if where:
            selected = [
                item for item in selected
                if all(item[1][1].get(key) == value for key, value in where.items())
            ]
        return {
            "ids": [doc_id for doc_id, _ in selected],
            "documents": [value[0] for _, value in selected],
            "metadatas": [value[1] for _, value in selected],
        }


def make_knowledge_base(collection):
    kb = object.__new__(KnowledgeBase)
    kb._collection = collection
    kb._embedding_dimensions = 3
    kb._embedding_model = "BAAI/bge-small-zh-v1.5"
    kb._embed_texts = lambda texts: [[0.1, 0.2, 0.3] for _ in texts]
    kb._token_cache = {}
    kb._count_tokens = lambda text: len(text)
    return kb


def test_knowledge_base_passes_bge_vectors_for_document_and_query():
    collection = FakeCollection()
    kb = make_knowledge_base(collection)

    added = kb.add_documents([{"title": "退款政策", "content": "退款审核后原路退回。"}])
    results = kb.search("退款多久到账", top_k=3)

    assert added == 1
    assert collection.add_calls[0]["embeddings"] == [[0.1, 0.2, 0.3]]
    assert collection.query_calls[0]["query_embeddings"] == [[0.1, 0.2, 0.3]]
    assert "query_texts" not in collection.query_calls[0]
    assert results[0]["title"] == "退款政策"


def test_hierarchical_chunk_keeps_path_and_parent_intro():
    kb = make_knowledge_base(FakeCollection())
    chunks = kb._chunk_document(
        "退款政策",
        "# 退款政策\n\n本章说明退款的适用范围。\n\n## 退款时效\n\n审核通过后原路退回。",
    )

    child = next(chunk for chunk in chunks if chunk["metadata"]["section_path"] == "退款政策 > 退款时效")
    assert "父级说明：本章说明退款的适用范围。" in child["content"]
    assert child["metadata"]["section_level"] == 2


def test_long_section_recursively_splits_within_hard_token_limit():
    kb = make_knowledge_base(FakeCollection())
    kb.TOKEN_TARGET = 60
    kb.TOKEN_LIMIT = 80
    kb.OVERLAP_TOKENS = 8
    content = "\n\n".join([
        "第一段说明退款申请的基本条件和审核范围。",
        "第二段说明退款审核通过后的到账时间。",
        "第三段说明特殊订单需要人工复核。",
        "第四段说明用户应保留支付凭证。",
    ])

    chunks = kb._chunk_document("退款政策", "# 退款政策\n\n" + content)

    assert len(chunks) >= 2
    assert all(chunk["metadata"]["token_count"] <= kb.TOKEN_LIMIT for chunk in chunks)
    assert "第二段说明" in chunks[0]["content"] or "第一段说明" in chunks[0]["content"]
    assert "退款审核" in "\n".join(chunk["content"] for chunk in chunks)


def test_table_is_stored_as_a_separate_chunk():
    kb = make_knowledge_base(FakeCollection())
    chunks = kb._chunk_document(
        "退款政策",
        "# 退款政策\n\n退款规则如下。\n\n| 状态 | 时效 |\n| --- | --- |\n| 审核中 | 1 个工作日 |",
    )

    table_chunks = [chunk for chunk in chunks if chunk["metadata"]["content_type"] == "table"]
    assert len(table_chunks) == 1
    assert "| 状态 | 时效 |" in table_chunks[0]["content"]
    assert "退款规则如下。" not in table_chunks[0]["content"]


def test_long_html_table_repeats_header_in_each_chunk():
    kb = make_knowledge_base(FakeCollection())
    kb.TOKEN_TARGET = 150
    kb.TOKEN_LIMIT = 180
    header = "<tr><th>处理阶段</th><th>时效</th></tr>"
    rows = "".join(
        f"<tr><td>阶段{i}</td><td>{'说明' * 12}</td></tr>"
        for i in range(8)
    )
    chunks = kb._chunk_document("退款时效", f"# 退款时效\n\n<table>{header}{rows}</table>")

    assert len(chunks) > 1
    assert all(chunk["metadata"]["content_type"] == "table" for chunk in chunks)
    assert all(header in chunk["content"] for chunk in chunks)
    assert all(chunk["metadata"]["token_count"] <= kb.TOKEN_LIMIT for chunk in chunks)


def test_seed_markdown_is_read_with_title_and_source_metadata(tmp_path):
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    (seed_dir / "01_refund.md").write_text("# 退款与售后\n\n退款规则正文。", encoding="utf-8")

    kb = object.__new__(KnowledgeBase)
    kb._seed_dir = Path(seed_dir)

    documents = kb._read_seed_documents()

    assert documents == [{
        "title": "退款与售后",
        "content": "# 退款与售后\n\n退款规则正文。",
        "metadata": {
            "source": KnowledgeBase.SEED_SOURCE,
            "source_file": "01_refund.md",
            "content_hash": hashlib.sha256("# 退款与售后\n\n退款规则正文。".encode()).hexdigest(),
        },
    }]


def test_changed_seed_document_replaces_old_chunks(tmp_path):
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    path = seed_dir / "01_refund.md"
    path.write_text("# 退款政策\n\n新规则。", encoding="utf-8")
    old_meta = {
        "source": KnowledgeBase.SEED_SOURCE,
        "source_file": path.name,
        "content_hash": "old-hash",
    }
    collection = FakeCollection({"old-id": ("旧规则。", old_meta)})
    kb = make_knowledge_base(collection)
    kb._seed_dir = Path(seed_dir)

    asyncio.run(kb._load_seed_documents())

    assert "old-id" not in collection.documents
    assert any("新规则。" in document for document, _ in collection.documents.values())
