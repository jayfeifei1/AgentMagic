import asyncio
import io
from pathlib import Path

from fastapi import UploadFile

import api.main as main


class FakeKnowledgeBase:
    def __init__(self):
        self.documents = []

    async def add_documents_async(self, documents):
        self.documents.extend(documents)
        return len(documents)

    async def doc_count_async(self):
        return len(self.documents)

    async def search_handler(self, params, context):
        return []


class FakeToolManager:
    def __init__(self, knowledge_base):
        self._tools = {"knowledge_search": type("Tool", (), {"handler": knowledge_base.search_handler})()}


class FakeMinerUParser:
    async def parse(self, filename, content):
        assert filename == "扫描物流.pdf"
        assert content == b"fake-pdf"
        return "# 物流异常处理\n\n扫描件 OCR 解析结果。"


def test_pdf_upload_stores_raw_and_markdown_then_imports(monkeypatch, tmp_path):
    import mcp.mineru_parser as mineru_module

    kb = FakeKnowledgeBase()
    monkeypatch.setattr(main, "_tool_manager", FakeToolManager(kb))
    monkeypatch.setattr(mineru_module, "MinerUParser", FakeMinerUParser)
    monkeypatch.setenv("ECHOMIND_KNOWLEDGE_DIR", str(tmp_path / "knowledge"))
    upload = UploadFile(filename="扫描物流.pdf", file=io.BytesIO(b"fake-pdf"))

    result = asyncio.run(main.upload_knowledge(upload))

    document_id = result["document_id"]
    knowledge_dir = tmp_path / "knowledge"
    assert result["parse_source"] == "mineru"
    assert (knowledge_dir / "raw" / document_id / "扫描物流.pdf").read_bytes() == b"fake-pdf"
    assert (knowledge_dir / "parsed" / f"{document_id}.md").read_text(encoding="utf-8").startswith("# 物流")
    assert kb.documents[0]["metadata"]["source_file"] == f"parsed/{document_id}.md"
    assert kb.documents[0]["metadata"]["file_type"] == "pdf"


def test_txt_upload_reads_locally_without_mineru(monkeypatch, tmp_path):
    kb = FakeKnowledgeBase()
    monkeypatch.setattr(main, "_tool_manager", FakeToolManager(kb))
    monkeypatch.setenv("ECHOMIND_KNOWLEDGE_DIR", str(tmp_path / "knowledge"))
    upload = UploadFile(filename="售后说明.txt", file=io.BytesIO("退款审核中。".encode()))

    result = asyncio.run(main.upload_knowledge(upload))

    document_id = result["document_id"]
    assert result["parse_source"] == "native"
    assert kb.documents[0]["content"] == "退款审核中。"
    assert (tmp_path / "knowledge" / "parsed" / f"{document_id}.md").read_text(encoding="utf-8") == "退款审核中。"
