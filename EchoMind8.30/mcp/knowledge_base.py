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
import html
import hashlib
import logging
import os
import re
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
    SEED_SOURCE = "echomind_default_seed_v1"
    UPLOADED_SOURCE = "echomind_uploaded_document_v1"
    TOKEN_TARGET = 384
    TOKEN_LIMIT = 480
    OVERLAP_TOKENS = 48
    PARENT_CONTEXT_TOKENS = 80
    _HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
    _HTML_TABLE_RE = re.compile(r"<table\b.*?</table>", re.IGNORECASE | re.DOTALL)
    _CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)

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
        self._token_count_url = self._embedding_url.rsplit("/", 1)[0] + "/token-count"
        self._token_cache: Dict[str, int] = {}
        self._embedding_dimensions: Optional[int] = None
        self._embedding_model = ""
        self._knowledge_dir = Path(os.getenv(
            "ECHOMIND_KNOWLEDGE_DIR",
            str(Path(__file__).resolve().parents[1] / "data" / "knowledge"),
        ))
        self._seed_dir = Path(seed_dir) if seed_dir else (
            self._knowledge_dir / "seed"
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
    async def initialize(self) -> None:
        """验证本地 BGE 服务，并同步默认知识与已解析文档。"""
        await asyncio.to_thread(self._embed_texts, ["EchoMind RAG 向量服务健康检查"])
        logger.info(
            "RAG Embedding 已就绪: model=%s, dimensions=%s, collection=%s",
            self._embedding_model or "unknown",
            self._embedding_dimensions,
            self.COLLECTION_NAME,
        )
        await self._load_seed_documents()
        await self._load_persisted_documents()

    # ── 文档管理 ──────────────────────────────────────────────────────────────

    def add_documents(self, documents: List[Dict[str, str]]) -> int:
        """
        批量导入文档到知识库。

        documents 格式: [{"title": "...", "content": "..."}, ...]。
        文档按标题层级切为业务 Section，仅在超长 Section 内递归切分。
        """
        ids, docs, metas = [], [], []

        for doc in documents:
            title = doc.get("title", "")
            content = doc.get("content", "")
            raw_metadata = doc.get("metadata", {})
            extra_metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
            extra_metadata.setdefault(
                "content_hash",
                hashlib.sha256(content.encode("utf-8")).hexdigest(),
            )
            chunks = self._chunk_document(title, content)

            for i, chunk in enumerate(chunks):
                identity = str(extra_metadata.get("document_id") or title)
                chunk_text = chunk["content"]
                doc_id = hashlib.md5(f"{identity}_{i}_{chunk_text[:50]}".encode()).hexdigest()
                ids.append(doc_id)
                docs.append(chunk_text)
                metas.append({
                    "title": title,
                    "chunk_index": i,
                    "total_chunks": len(chunks),
                    "chunker_version": "hierarchical_v1",
                    **chunk["metadata"],
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
                    "section_path": meta.get("section_path", ""),
                    "document_id": meta.get("document_id", ""),
                    "source_file": meta.get("source_file", ""),
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

    def _chunk_document(self, title: str, text: str) -> List[Dict[str, Any]]:
        """按 Markdown 标题建立 Section，仅在过长 Section 内继续切分。"""
        sections = self._parse_sections(title, text)
        direct_text = {
            tuple(section["path"]): self._limit_tokens("\n".join(section["lines"]))
            for section in sections
            if section["lines"]
        }
        chunks: List[Dict[str, Any]] = []

        for section_index, section in enumerate(sections):
            body = "\n".join(section["lines"]).strip()
            if not body:
                continue
            parent_intro = direct_text.get(tuple(section["path"][:-1]), "")
            context = self._build_context(title, section["path"], parent_intro)
            section_chunks = self._split_section(context, self._split_blocks(body))
            for chunk_index, item in enumerate(section_chunks):
                chunk_text = f"{context}\n{item['content']}"
                chunks.append({
                    "content": chunk_text,
                    "metadata": {
                        "section_path": " > ".join(section["path"]),
                        "section_level": section["level"],
                        "section_index": section_index,
                        "chunk_index_in_section": chunk_index,
                        "total_chunks_in_section": len(section_chunks),
                        "content_type": item["content_type"],
                        "token_count": self._count_tokens(chunk_text),
                    },
                })
        return chunks

    def _parse_sections(self, title: str, text: str) -> List[Dict[str, Any]]:
        """从 Markdown 标题恢复 Section；无标题文本归入文件标题下。"""
        sections: List[Dict[str, Any]] = []
        path: List[str] = []
        current: Optional[Dict[str, Any]] = None

        def flush() -> None:
            if current is not None:
                sections.append(current)

        for line in text.replace("\r\n", "\n").split("\n"):
            matched = self._HEADING_RE.match(line.strip())
            if matched:
                flush()
                level = len(matched.group(1))
                heading = matched.group(2).strip()
                path = path[:level - 1]
                path.append(heading)
                current = {"path": path.copy(), "level": level, "lines": []}
            else:
                if current is None:
                    current = {"path": [title or "未命名文档"], "level": 0, "lines": []}
                current["lines"].append(line)
        flush()
        return sections

    def _build_context(self, title: str, path: List[str], parent_intro: str) -> str:
        parts = [f"文档：{title or path[0]}", f"章节：{' > '.join(path)}"]
        if parent_intro:
            parts.append(f"父级说明：{parent_intro}")
        return "\n".join(parts) + "\n内容："

    def _split_blocks(self, text: str) -> List[Dict[str, str]]:
        """保留表格、代码和连续列表，普通正文按空行形成段落块。"""
        blocks: List[Dict[str, str]] = []
        pattern = re.compile(
            f"({self._HTML_TABLE_RE.pattern}|{self._CODE_BLOCK_RE.pattern})",
            re.IGNORECASE | re.DOTALL,
        )
        for part in re.split(pattern, text):
            stripped = part.strip()
            if not stripped:
                continue
            if self._HTML_TABLE_RE.fullmatch(stripped):
                blocks.append({"content": stripped, "content_type": "table"})
                continue
            if self._CODE_BLOCK_RE.fullmatch(stripped):
                blocks.append({"content": stripped, "content_type": "code"})
                continue
            for paragraph in re.split(r"\n\s*\n", stripped):
                paragraph = paragraph.strip()
                if not paragraph:
                    continue
                lines = [line.strip() for line in paragraph.splitlines() if line.strip()]
                if len(lines) >= 2 and all(line.startswith("|") for line in lines):
                    kind = "table"
                elif lines and all(re.match(r"(?:[-*+]\s+|\d+[.)]\s+)", line) for line in lines):
                    kind = "list"
                else:
                    kind = "text"
                blocks.append({"content": "\n".join(lines), "content_type": kind})
        return blocks

    def _split_section(self, context: str, blocks: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """以 384 Token 为目标；表格始终独立，480 Token 是硬上限。"""
        chunks: List[Dict[str, str]] = []
        current: List[Dict[str, str]] = []

        def flush() -> None:
            nonlocal current
            if current:
                chunks.append({
                    "content": self._join_blocks(current),
                    "content_type": self._content_type(current),
                })
                current = []

        for block in blocks:
            # 表格不与叙述文字混装；超长表格按行拆并重复表头。
            if block["content_type"] == "table":
                flush()
                chunks.extend(self._split_table(context, block["content"]))
                continue

            candidate = current + [block]
            if current and self._count_tokens(self._with_context(context, candidate)) > self.TOKEN_TARGET:
                previous = current
                flush()
                current = self._overlap_blocks(context, previous)

            candidate = current + [block]
            if self._count_tokens(self._with_context(context, candidate)) <= self.TOKEN_LIMIT:
                current = candidate
                continue

            # 重叠本身与新块仍超过硬上限时，不写入一份只有重叠内容的重复块。
            if current:
                current = []
            chunks.extend(self._split_oversized_block(context, block))
        flush()
        return chunks

    def _split_oversized_block(self, context: str, block: Dict[str, str]) -> List[Dict[str, str]]:
        units = self._split_block_units(block)
        chunks: List[Dict[str, str]] = []
        current: List[str] = []
        for unit in units:
            candidate = "\n".join(current + [unit])
            if current and self._count_tokens(f"{context}\n{candidate}") > self.TOKEN_TARGET:
                chunks.append({"content": "\n".join(current), "content_type": block["content_type"]})
                current = self._overlap_units(context, current)
            candidate_with_context = context + "\n" + "\n".join(current + [unit])
            if self._count_tokens(candidate_with_context) <= self.TOKEN_LIMIT:
                current.append(unit)
            else:
                current = []
                chunks.extend(self._hard_split_unit(context, unit, block["content_type"]))
        if current:
            chunks.append({"content": "\n".join(current), "content_type": block["content_type"]})
        return chunks

    def _split_block_units(self, block: Dict[str, str]) -> List[str]:
        content = block["content"]
        if block["content_type"] in {"table", "list", "code"}:
            return [line for line in content.splitlines() if line.strip()] or [content]
        return [part.strip() for part in re.findall(r"[^。！？!?；;]+[。！？!?；;]?", content) if part.strip()] or [content]

    def _split_table(self, context: str, table: str) -> List[Dict[str, str]]:
        """表格独立成块；Markdown/HTML 表格超长时按行切分并保留表头。"""
        if self._count_tokens(f"{context}\n{table}") <= self.TOKEN_LIMIT:
            return [{"content": table, "content_type": "table"}]

        lines = [line for line in table.splitlines() if line.strip()]
        if len(lines) >= 3 and all(line.lstrip().startswith("|") for line in lines[:2]):
            header = "\n".join(lines[:2])
            rows = lines[2:]
            return self._pack_table_rows(
                context,
                rows,
                lambda selected: header + "\n" + "\n".join(selected),
            )

        html_rows = re.findall(r"<tr\b.*?</tr>", table, re.IGNORECASE | re.DOTALL)
        if len(html_rows) >= 2:
            opening = re.search(r"<table\b[^>]*>", table, re.IGNORECASE)
            table_open = opening.group(0) if opening else "<table>"
            header = html_rows[0]
            return self._pack_table_rows(
                context,
                html_rows[1:],
                lambda selected: table_open + header + "".join(selected) + "</table>",
            )

        return self._split_oversized_block(context, {"content": table, "content_type": "table"})

    def _pack_table_rows(
        self,
        context: str,
        rows: List[str],
        render: Any,
    ) -> List[Dict[str, str]]:
        """按 Token 预算装入表格行，render 负责为每块重复表头。"""
        chunks: List[Dict[str, str]] = []
        current: List[str] = []
        for row in rows:
            candidate = render(current + [row])
            if current and self._count_tokens(f"{context}\n{candidate}") > self.TOKEN_TARGET:
                chunks.append({"content": render(current), "content_type": "table"})
                current = []
            candidate = render(current + [row])
            if self._count_tokens(f"{context}\n{candidate}") <= self.TOKEN_LIMIT:
                current.append(row)
            else:
                # 极端超长单行转为文本片段再装回单元格，保持表头与 HTML 完整。
                row_text = re.sub(r"<[^>]+>", " ", row)
                row_text = re.sub(r"\s+", " ", row_text).strip()
                for piece in self._hard_split_unit(context, row_text, "table"):
                    safe_row = f"<tr><td>{html.escape(piece['content'])}</td></tr>"
                    chunks.append({"content": render([safe_row]), "content_type": "table"})
        if current:
            chunks.append({"content": render(current), "content_type": "table"})
        return chunks

    def _hard_split_unit(self, context: str, unit: str, content_type: str) -> List[Dict[str, str]]:
        pieces: List[Dict[str, str]] = []
        current = ""
        for char in unit:
            candidate = current + char
            if current and self._count_tokens(f"{context}\n{candidate}") > self.TOKEN_LIMIT:
                pieces.append({"content": current, "content_type": content_type})
                overlap = self._tail_text(current, self.OVERLAP_TOKENS)
                current = overlap + char
                if self._count_tokens(f"{context}\n{current}") > self.TOKEN_LIMIT:
                    current = char
            else:
                current = candidate
        if current:
            pieces.append({"content": current, "content_type": content_type})
        return pieces

    def _tail_text(self, text: str, token_limit: int) -> str:
        tail = ""
        for char in reversed(text):
            candidate = char + tail
            if self._count_tokens(candidate) > token_limit:
                break
            tail = candidate
        return tail

    def _overlap_blocks(self, context: str, blocks: List[Dict[str, str]]) -> List[Dict[str, str]]:
        tail: List[Dict[str, str]] = []
        for block in reversed(blocks):
            candidate = [block] + tail
            if self._count_tokens(self._with_context(context, candidate)) > self.OVERLAP_TOKENS + self._count_tokens(context):
                break
            tail = candidate
        return tail

    def _overlap_units(self, context: str, units: List[str]) -> List[str]:
        tail: List[str] = []
        for unit in reversed(units):
            candidate = [unit] + tail
            if self._count_tokens(f"{context}\n{'\n'.join(candidate)}") > self.OVERLAP_TOKENS + self._count_tokens(context):
                break
            tail = candidate
        return tail

    @staticmethod
    def _join_blocks(blocks: List[Dict[str, str]]) -> str:
        return "\n\n".join(block["content"] for block in blocks)

    def _with_context(self, context: str, blocks: List[Dict[str, str]]) -> str:
        return f"{context}\n{self._join_blocks(blocks)}"

    @staticmethod
    def _content_type(blocks: List[Dict[str, str]]) -> str:
        kinds = {block["content_type"] for block in blocks}
        return next(iter(kinds)) if len(kinds) == 1 else "mixed"

    def _limit_tokens(self, text: str) -> str:
        result = ""
        for unit in self._split_block_units({"content": text, "content_type": "text"}):
            if self._count_tokens(unit) > self.PARENT_CONTEXT_TOKENS:
                break
            candidate = f"{result}{unit}" if result else unit
            if self._count_tokens(candidate) > self.PARENT_CONTEXT_TOKENS:
                break
            result = candidate
        return result

    def _count_tokens(self, text: str) -> int:
        """复用 BGE 服务已加载的 tokenizer，避免字符数与模型输入预算不一致。"""
        cleaned = str(text or "")
        cached = self._token_cache.get(cleaned)
        if cached is not None:
            return cached
        with httpx.Client(timeout=self._embedding_timeout_s) as client:
            response = client.post(self._token_count_url, json={"texts": [cleaned]})
            response.raise_for_status()
            counts = response.json().get("counts")
        if not isinstance(counts, list) or len(counts) != 1 or not isinstance(counts[0], int):
            raise ValueError("Embedding 服务返回的 Token 数量不正确")
        self._token_cache[cleaned] = counts[0]
        return counts[0]

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
                "metadata": {
                    "source": self.SEED_SOURCE,
                    "source_file": path.name,
                    "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                },
            })
        return documents

    async def _load_seed_documents(self) -> None:
        """按内容哈希同步 data/knowledge/seed，文件修改后替换旧分片。"""
        documents = self._read_seed_documents()
        existing = await asyncio.to_thread(
            self._collection.get,
            where={"source": self.SEED_SOURCE},
            include=["metadatas"],
        )
        entries: Dict[str, List[tuple]] = {}
        for doc_id, metadata in zip(existing.get("ids") or [], existing.get("metadatas") or []):
            if isinstance(metadata, dict):
                entries.setdefault(str(metadata.get("source_file", "")), []).append((doc_id, metadata))

        active_files = {str(doc["metadata"]["source_file"]) for doc in documents}
        stale_ids = [
            doc_id
            for source_file, items in entries.items()
            if source_file not in active_files
            for doc_id, _ in items
        ]
        pending = []
        for document in documents:
            metadata = document["metadata"]
            current = entries.get(str(metadata["source_file"]), [])
            if current and all(item[1].get("content_hash") == metadata["content_hash"] for item in current):
                continue
            stale_ids.extend(item[0] for item in current)
            pending.append(document)
        if stale_ids:
            await asyncio.to_thread(self._collection.delete, ids=list(dict.fromkeys(stale_ids)))
        if pending:
            await self.add_documents_async(pending)
            logger.info("已同步 %s 篇默认知识: %s", len(pending), self._seed_dir)

    async def _load_persisted_documents(self) -> None:
        """按内容哈希同步已落盘 Markdown；原始文件仍保留在 raw/。"""
        parsed_dir = self._knowledge_dir / "parsed"
        if not parsed_dir.exists():
            return
        existing = await asyncio.to_thread(
            self._collection.get,
            where={"source": self.UPLOADED_SOURCE},
            include=["metadatas"],
        )
        entries: Dict[str, List[tuple]] = {}
        for doc_id, metadata in zip(existing.get("ids") or [], existing.get("metadatas") or []):
            if isinstance(metadata, dict) and metadata.get("document_id"):
                entries.setdefault(str(metadata["document_id"]), []).append((doc_id, metadata))
        documents: List[Dict[str, Any]] = []
        active_ids = set()
        stale_ids: List[str] = []
        for parsed_path in sorted(parsed_dir.glob("*.md")):
            document_id = parsed_path.stem
            active_ids.add(document_id)
            content = parsed_path.read_text(encoding="utf-8").strip()
            if not content:
                continue
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            current = entries.get(document_id, [])
            if current and all(item[1].get("content_hash") == content_hash for item in current):
                continue
            stale_ids.extend(item[0] for item in current)
            raw_dir = self._knowledge_dir / "raw" / document_id
            original = next((path for path in raw_dir.iterdir() if path.is_file()), None) if raw_dir.exists() else None
            filename = original.name if original else parsed_path.name
            suffix = original.suffix.lower().lstrip(".") if original else "md"
            documents.append({
                "title": original.stem if original else parsed_path.stem,
                "content": content,
                "metadata": {
                    "source": self.UPLOADED_SOURCE,
                    "document_id": document_id,
                    "original_filename": filename,
                    "file_type": suffix,
                    "parse_source": "native" if suffix in {"txt", "md"} else "mineru",
                    "source_file": str(parsed_path.relative_to(self._knowledge_dir)),
                    "content_hash": content_hash,
                },
            })
        stale_ids.extend(
            doc_id
            for document_id, items in entries.items()
            if document_id not in active_ids
            for doc_id, _ in items
        )
        if stale_ids:
            await asyncio.to_thread(self._collection.delete, ids=list(dict.fromkeys(stale_ids)))
        if documents:
            await self.add_documents_async(documents)
            logger.info("已同步 %s 篇已解析文档", len(documents))

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
