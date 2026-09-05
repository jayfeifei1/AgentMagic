"""
RAG 知识库 —— 基于 ChromaDB 的真实检索实现。

功能：
  1. 文档导入：将文本切片后，用本地 BGE 服务生成向量并存入 ChromaDB
  2. 语义检索：用同一个 BGE 服务向量化 query，召回相关文档片段
  3. 与 MCP 工具框架集成：作为 knowledge_search 工具的真实 handler

ChromaDB 在这里的角色：
  - memory/ 中用于存储对话记忆（情景记忆 + 用户画像）
  - 这里用于存储知识库文档（RAG 检索）
  两者是不同的 collection，互不干扰。
"""
import asyncio
import hashlib
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import chromadb
import httpx

logger = logging.getLogger(__name__)


class KnowledgeBase:
    """
    基于 ChromaDB 的 RAG 知识库。

    文档与查询均使用已部署的 BAAI/bge-small-zh-v1.5 服务向量化，
    再由 ChromaDB 进行余弦相似度检索。
    """

    COLLECTION_NAME = "knowledge_base_bge_v1"
    LEGACY_COLLECTION_NAME = "knowledge_base"
    EMBEDDING_BATCH_SIZE = 64
    SEED_SOURCE = "echomind_default_seed_v1"
    LEGACY_DEFAULT_TITLES = {
        "退款政策", "订单查询", "账户安全", "技术故障排查", "会员与积分", "配送说明",
    }

    def __init__(
        self,
        chroma_host: str = "localhost",
        chroma_port: int = 8000,
        chroma_path: str = "./data/chroma",
        embedding_url: Optional[str] = None,
        embedding_timeout_s: Optional[float] = None,
        seed_dir: Optional[str] = None,
    ):
        self._embedding_url = (
            embedding_url or os.getenv("ECHOMIND_EMBEDDING_URL", "http://embedding:8080/embed")
        ).rstrip("/")
        self._embedding_timeout_s = embedding_timeout_s or float(
            os.getenv("ECHOMIND_EMBEDDING_TIMEOUT_S", "5")
        )
        self._embedding_dimensions: Optional[int] = None
        self._embedding_model = ""
        self._seed_dir = Path(seed_dir) if seed_dir else (
            Path(__file__).resolve().parents[1] / "data" / "knowledge" / "seed"
        )

        # 优先连接独立 ChromaDB 服务，连不上才使用本地嵌入式模式。
        try:
            # HttpClient 默认也会初始化 ChromaDB telemetry；显式关闭避免 posthog 兼容性错误日志。
            self._client = chromadb.HttpClient(
                host=chroma_host,
                port=chroma_port,
                settings=chromadb.Settings(anonymized_telemetry=False),
            )
            self._client.heartbeat()
            logger.info(f"知识库 ChromaDB 已连接: {chroma_host}:{chroma_port}")
        except Exception:
            logger.info(f"知识库 ChromaDB 服务不可用，使用本地模式: {chroma_path}")
            self._client = chromadb.PersistentClient(
                path=chroma_path,
                settings=chromadb.Settings(anonymized_telemetry=False),
            )

        # 显式传入 BGE 向量，因此不会使用 ChromaDB 默认 MiniLM Embedding Function。
        self._collection = self._client.get_or_create_collection(
            name=self.COLLECTION_NAME,
            metadata={
                "description": "EchoMind RAG 知识库（本地 BGE 向量）",
                "hnsw:space": "cosine",
                "embedding_model": "BAAI/bge-small-zh-v1.5",
            },
        )
        try:
            self._legacy_collection = self._client.get_collection(self.LEGACY_COLLECTION_NAME)
        except Exception:
            self._legacy_collection = None

    async def initialize(self) -> None:
        """验证本地 BGE 服务，迁移自定义旧文档，并导入默认种子知识。"""
        await asyncio.to_thread(self._embed_texts, ["EchoMind RAG 向量服务健康检查"])
        logger.info(
            "RAG Embedding 已就绪: model=%s, dimensions=%s, collection=%s",
            self._embedding_model or "unknown",
            self._embedding_dimensions,
            self.COLLECTION_NAME,
        )
        await self._migrate_legacy_documents()
        await self._remove_legacy_default_documents()
        await self._load_seed_documents()

    # ── 文档管理 ──────────────────────────────────────────────────────────────

    def add_documents(self, documents: List[Dict[str, str]]) -> int:
        """
        批量导入文档到知识库。

        documents 格式: [{"title": "...", "content": "..."}, ...]
        长文档会自动切片（每片 500 字）。
        """
        ids, docs, metas = [], [], []

        for doc in documents:
            title = doc.get("title", "")
            content = doc.get("content", "")
            extra_metadata = doc.get("metadata", {})
            if not isinstance(extra_metadata, dict):
                extra_metadata = {}
            chunks  = self._chunk_text(content, chunk_size=500)

            for i, chunk in enumerate(chunks):
                doc_id = hashlib.md5(f"{title}_{i}_{chunk[:50]}".encode()).hexdigest()
                ids.append(doc_id)
                docs.append(chunk)
                metas.append({
                    "title": title,
                    "chunk_index": i,
                    "total_chunks": len(chunks),
                    **extra_metadata,
                })

        if ids:
            embeddings = self._embed_texts(docs)
            self._collection.add(
                ids=ids,
                documents=docs,
                embeddings=embeddings,
                metadatas=metas,
            )
            logger.info(f"知识库导入 {len(ids)} 个文档片段")

        return len(ids)

    async def add_documents_async(self, documents: List[Dict[str, str]]) -> int:
        """异步导入文档；ChromaDB 客户端为同步实现，因此放入线程池执行。"""
        return await asyncio.to_thread(self.add_documents, documents)

    def search(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """
        语义检索：根据 query 返回最相关的文档片段。

        使用本地 BGE 将 query 转为向量，与 BGE 文档向量做余弦相似度匹配。
        """
        if not query.strip():
            return []
        query_embedding = self._embed_texts([query])[0]
        results = self._collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
        )

        items = []
        if results["documents"] and results["documents"][0]:
            for doc, meta, dist in zip(
                results["documents"][0],
                results["metadatas"][0],
                results["distances"][0],
            ):
                items.append({
                    "title":    meta.get("title", ""),
                    "content":  doc,
                    "score":    round(1.0 - dist, 4),  # ChromaDB 返回距离，转为相似度
                    "chunk":    meta.get("chunk_index", 0),
                })

        return items

    async def search_async(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """异步检索；ChromaDB 客户端为同步实现，因此放入线程池执行。"""
        return await asyncio.to_thread(self.search, query, top_k)

    @property
    def doc_count(self) -> int:
        return self._collection.count()

    async def doc_count_async(self) -> int:
        """异步获取文档片段数量。"""
        return await asyncio.to_thread(self._collection.count)

    # ── MCP 工具 handler ─────────────────────────────────────────────────────

    async def search_handler(self, params: Dict[str, Any], context: Any) -> List[Dict]:
        """
        作为 MCP 工具的 handler 注册。

        MCPToolManager.register(Tool(
            name="knowledge_search",
            handler=kb.search_handler,
            ...
        ))
        """
        query = params.get("query", "")
        top_k = params.get("top_k", 5)
        return await self.search_async(query, top_k=top_k)

    # ── 内部方法 ──────────────────────────────────────────────────────────────

    def _chunk_text(self, text: str, chunk_size: int = 500) -> List[str]:
        """将长文本按 chunk_size 切片，保留语义完整性（按句号/换行切分）。"""
        if len(text) <= chunk_size:
            return [text] if text.strip() else []

        chunks = []
        current = ""
        # 按句子切分
        sentences = text.replace("\n", "。").split("。")
        for sent in sentences:
            sent = sent.strip()
            if not sent:
                continue
            if len(current) + len(sent) + 1 > chunk_size:
                if current:
                    chunks.append(current)
                current = sent
            else:
                current = f"{current}。{sent}" if current else sent

        if current:
            chunks.append(current)

        return chunks

    def _read_seed_documents(self) -> List[Dict[str, Any]]:
        """读取版本受控的默认 Markdown 知识，不再将业务规则写死在 Python 中。"""
        if not self._seed_dir.exists():
            logger.warning("默认知识目录不存在: %s", self._seed_dir)
            return []
        documents = []
        for path in sorted(self._seed_dir.glob("*.md")):
            content = path.read_text(encoding="utf-8").strip()
            if not content:
                continue
            title = next(
                (line.lstrip("#").strip() for line in content.splitlines() if line.startswith("# ")),
                path.stem,
            )
            documents.append({
                "title": title,
                "content": content,
                "metadata": {"source": self.SEED_SOURCE, "source_file": path.name},
            })
        return documents

    async def _load_seed_documents(self) -> None:
        """首次加载 data/knowledge/seed 下的默认知识。"""
        existing = await asyncio.to_thread(
            self._collection.get,
            where={"source": self.SEED_SOURCE},
            include=["metadatas"],
        )
        if existing.get("ids"):
            return
        documents = self._read_seed_documents()
        if not documents:
            return
        await self.add_documents_async(documents)
        logger.info("已从默认知识目录导入 %s 篇文档: %s", len(documents), self._seed_dir)

    def _embed_texts(self, texts: List[str]) -> List[List[float]]:
        """调用本地 BGE 服务，并校验向量数量与维度。"""
        cleaned = [str(text or "").encode("utf-8", errors="ignore").decode("utf-8") for text in texts]
        if not cleaned:
            return []
        with httpx.Client(timeout=self._embedding_timeout_s) as client:
            response = client.post(self._embedding_url, json={"texts": cleaned})
            response.raise_for_status()
            payload = response.json()

        vectors = payload.get("vectors")
        if not isinstance(vectors, list) or len(vectors) != len(cleaned):
            raise ValueError("Embedding 服务返回的向量数量不正确")
        normalized = [[float(value) for value in vector] for vector in vectors]
        if not normalized or not normalized[0]:
            raise ValueError("Embedding 服务返回空向量")
        dimensions = len(normalized[0])
        if any(len(vector) != dimensions for vector in normalized):
            raise ValueError("Embedding 服务返回的向量维度不一致")
        if self._embedding_dimensions is not None and dimensions != self._embedding_dimensions:
            raise ValueError(
                f"Embedding 向量维度变化: {self._embedding_dimensions} -> {dimensions}"
            )
        self._embedding_dimensions = dimensions
        self._embedding_model = str(payload.get("model") or self._embedding_model)
        return normalized

    async def _migrate_legacy_documents(self) -> None:
        """将旧 MiniLM Collection 的原文档重新编码为 BGE 向量，写入新 Collection。"""
        if self._legacy_collection is None:
            return
        legacy_count = await asyncio.to_thread(self._legacy_collection.count)
        if legacy_count == 0:
            return
        legacy = await asyncio.to_thread(
            self._legacy_collection.get,
            include=["documents", "metadatas"],
        )
        entries = [
            (doc_id, document, metadata if isinstance(metadata, dict) else {})
            for doc_id, document, metadata in zip(
                legacy.get("ids") or [],
                legacy.get("documents") or [],
                legacy.get("metadatas") or [],
            )
            if (
                isinstance(doc_id, str)
                and isinstance(document, str)
                and document.strip()
                and (
                    not isinstance(metadata, dict)
                    or metadata.get("title") not in self.LEGACY_DEFAULT_TITLES
                )
            )
        ]
        if not entries:
            return

        existing = await asyncio.to_thread(
            self._collection.get,
            ids=[entry[0] for entry in entries],
        )
        existing_ids = set(existing.get("ids") or [])
        pending = [entry for entry in entries if entry[0] not in existing_ids]
        for start in range(0, len(pending), self.EMBEDDING_BATCH_SIZE):
            batch = pending[start:start + self.EMBEDDING_BATCH_SIZE]
            vectors = await asyncio.to_thread(self._embed_texts, [entry[1] for entry in batch])
            await asyncio.to_thread(
                self._collection.add,
                ids=[entry[0] for entry in batch],
                documents=[entry[1] for entry in batch],
                embeddings=vectors,
                metadatas=[entry[2] for entry in batch],
            )
        if pending:
            logger.info("已迁移 %s 个旧知识库片段到 %s", len(pending), self.COLLECTION_NAME)

    async def _remove_legacy_default_documents(self) -> None:
        """移除曾由 Python 写死的六篇演示文档，避免与新的 Markdown 种子重复。"""
        existing = await asyncio.to_thread(self._collection.get, include=["metadatas"])
        legacy_ids = [
            doc_id
            for doc_id, metadata in zip(existing.get("ids") or [], existing.get("metadatas") or [])
            if isinstance(metadata, dict)
            and metadata.get("title") in self.LEGACY_DEFAULT_TITLES
        ]
        if legacy_ids:
            await asyncio.to_thread(self._collection.delete, ids=legacy_ids)
            logger.info("已移除 %s 个旧默认知识片段", len(legacy_ids))
