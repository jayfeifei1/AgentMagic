import asyncio
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

    def get(self, ids=None, include=None):
        ids = ids or list(self.documents)
        selected = [(doc_id, self.documents[doc_id]) for doc_id in ids if doc_id in self.documents]
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


def test_legacy_documents_are_reembedded_before_migration():
    target = FakeCollection()
    legacy = FakeCollection({
        "legacy-refund": ("退款原文", {"title": "自定义退款知识", "chunk_index": 0}),
    })
    kb = make_knowledge_base(target)
    kb._legacy_collection = legacy
    kb.EMBEDDING_BATCH_SIZE = 64

    asyncio.run(kb._migrate_legacy_documents())

    assert target.add_calls[0]["ids"] == ["legacy-refund"]
    assert target.add_calls[0]["documents"] == ["退款原文"]
    assert target.add_calls[0]["embeddings"] == [[0.1, 0.2, 0.3]]


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
        },
    }]
