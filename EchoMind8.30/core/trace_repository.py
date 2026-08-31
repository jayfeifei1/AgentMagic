"""MySQL-backed persistence and query helpers for Agent execution traces."""
import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

import aiomysql

from core.trace_time import trace_now

logger = logging.getLogger(__name__)


class TraceRepository:
    """Stores request, LLM-call, and tool-call traces without affecting chat availability."""

    def __init__(self) -> None:
        self._pool: Optional[aiomysql.Pool] = None
        self._config = {
            "host": os.getenv("MYSQL_HOST", "mysql"),
            "port": int(os.getenv("MYSQL_PORT", "3306")),
            "user": os.getenv("MYSQL_USER", ""),
            "password": os.getenv("MYSQL_PASSWORD", ""),
            "db": os.getenv("MYSQL_DATABASE", "echomind"),
            "connect_timeout": int(os.getenv("MYSQL_CONNECT_TIMEOUT", "3")),
        }

    @property
    def configured(self) -> bool:
        return bool(self._config["user"] and self._config["password"])

    async def connect(self) -> bool:
        """Create a pool and initialize the schema. Returns False when MySQL is unavailable."""
        if self._pool is not None:
            return True
        if not self.configured:
            logger.warning("Trace 持久化未配置 MYSQL_USER 或 MYSQL_PASSWORD，使用内存 Trace")
            return False
        try:
            self._pool = await aiomysql.create_pool(
                **self._config,
                minsize=1,
                maxsize=5,
                autocommit=False,
                charset="utf8mb4",
                cursorclass=aiomysql.DictCursor,
            )
            await self._initialize_schema()
            logger.info("MySQL Trace Repository 已连接")
            return True
        except Exception as ex:
            logger.warning("MySQL Trace Repository 连接失败，暂时使用内存 Trace: %s", ex)
            if self._pool is not None:
                self._pool.close()
                await self._pool.wait_closed()
                self._pool = None
            return False

    async def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None

    async def persist(self, trace: Dict[str, Any]) -> bool:
        """Persist one complete request trace. A failed write must not interrupt chat handling."""
        if self._pool is None and not await self.connect():
            return False
        try:
            await self._persist(trace)
            return True
        except Exception as ex:
            logger.warning("请求 %s 的 Trace 写入 MySQL 失败: %s", trace.get("request_id"), ex)
            return False

    async def get_trace(self, request_id: str) -> Optional[Dict[str, Any]]:
        if self._pool is None and not await self.connect():
            return None
        try:
            assert self._pool is not None
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        "SELECT * FROM request_traces WHERE request_id = %s", (request_id,)
                    )
                    request_trace = await cursor.fetchone()
                    if request_trace is None:
                        return None
                    await cursor.execute(
                        "SELECT * FROM llm_call_traces WHERE request_id = %s ORDER BY round_no, id",
                        (request_id,),
                    )
                    llm_calls = await cursor.fetchall()
                    await cursor.execute(
                        "SELECT * FROM tool_call_traces WHERE request_id = %s ORDER BY sequence_no, id",
                        (request_id,),
                    )
                    tool_calls = await cursor.fetchall()
            return self._build_trace(request_trace, llm_calls, tool_calls)
        except Exception as ex:
            logger.warning("读取请求 %s 的 MySQL Trace 失败: %s", request_id, ex)
            return None

    async def list_recent(
        self,
        limit: int = 20,
        agent_type: Optional[str] = None,
        tool_name: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        if self._pool is None and not await self.connect():
            return []
        limit = max(1, min(int(limit or 20), 100))
        try:
            assert self._pool is not None
            clauses: List[str] = []
            params: List[Any] = []
            if agent_type:
                clauses.append("primary_agent = %s")
                params.append(agent_type)
            if tool_name:
                clauses.append(
                    "EXISTS (SELECT 1 FROM tool_call_traces t "
                    "WHERE t.request_id = request_traces.request_id AND t.tool_name = %s)"
                )
                params.append(tool_name)
            if start_time is not None:
                clauses.append("created_at >= %s")
                params.append(start_time)
            if end_time is not None:
                clauses.append("created_at <= %s")
                params.append(end_time)
            where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
            params.append(limit)
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        f"SELECT request_id FROM request_traces{where} ORDER BY created_at DESC LIMIT %s",
                        tuple(params),
                    )
                    rows = await cursor.fetchall()
            items = []
            for row in rows:
                trace = await self.get_trace(row["request_id"])
                if trace is not None:
                    items.append(trace)
            return items
        except Exception as ex:
            logger.warning("读取最近 MySQL Trace 失败: %s", ex)
            return []

    async def _initialize_schema(self) -> None:
        assert self._pool is not None
        statements = (
            """
            CREATE TABLE IF NOT EXISTS request_traces (
                request_id VARCHAR(64) PRIMARY KEY,
                created_at DATETIME(3) NOT NULL,
                intent VARCHAR(64) NULL,
                primary_agent VARCHAR(32) NULL,
                supporting_agents JSON NOT NULL,
                tools_used JSON NOT NULL,
                escalated BOOLEAN NOT NULL DEFAULT FALSE,
                status VARCHAR(16) NOT NULL,
                routing_reason TEXT NOT NULL,
                routing_confidence DECIMAL(8, 4) NOT NULL DEFAULT 0,
                latency_ms DECIMAL(12, 1) NOT NULL DEFAULT 0,
                INDEX idx_request_traces_created_at (created_at),
                INDEX idx_request_traces_agent_created (primary_agent, created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """,
            """
            CREATE TABLE IF NOT EXISTS llm_call_traces (
                id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                request_id VARCHAR(64) NOT NULL,
                created_at DATETIME(3) NOT NULL,
                agent_type VARCHAR(32) NOT NULL,
                round_no INT NOT NULL,
                model VARCHAR(128) NOT NULL,
                status VARCHAR(16) NOT NULL,
                latency_ms DECIMAL(12, 1) NOT NULL DEFAULT 0,
                error_type VARCHAR(128) NULL,
                error_message TEXT NULL,
                INDEX idx_llm_traces_request_round (request_id, round_no),
                INDEX idx_llm_traces_agent_created (agent_type, created_at),
                CONSTRAINT fk_llm_trace_request FOREIGN KEY (request_id)
                    REFERENCES request_traces(request_id) ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """,
            """
            CREATE TABLE IF NOT EXISTS tool_call_traces (
                id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                request_id VARCHAR(64) NOT NULL,
                created_at DATETIME(3) NOT NULL,
                sequence_no INT NOT NULL,
                agent_type VARCHAR(32) NOT NULL,
                tool_name VARCHAR(128) NOT NULL,
                tool_use_id VARCHAR(128) NULL,
                input_json JSON NOT NULL,
                result_summary TEXT NULL,
                success BOOLEAN NOT NULL,
                result_success BOOLEAN NULL,
                latency_ms DECIMAL(12, 1) NOT NULL DEFAULT 0,
                cached BOOLEAN NOT NULL DEFAULT FALSE,
                reranked BOOLEAN NOT NULL DEFAULT FALSE,
                error_message TEXT NULL,
                INDEX idx_tool_traces_request_sequence (request_id, sequence_no),
                INDEX idx_tool_traces_name_created (tool_name, created_at),
                CONSTRAINT fk_tool_trace_request FOREIGN KEY (request_id)
                    REFERENCES request_traces(request_id) ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """,
        )
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                for statement in statements:
                    await cursor.execute(statement)
            await conn.commit()

    async def _persist(self, trace: Dict[str, Any]) -> None:
        assert self._pool is not None
        created_at = self._parse_timestamp(trace.get("timestamp"))
        llm_calls = list(trace.get("llm_calls") or [])
        tool_calls = list(trace.get("tool_calls") or [])
        async with self._pool.acquire() as conn:
            try:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        """
                        INSERT INTO request_traces (
                            request_id, created_at, intent, primary_agent, supporting_agents, tools_used,
                            escalated, status, routing_reason, routing_confidence, latency_ms
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            created_at=VALUES(created_at), intent=VALUES(intent),
                            primary_agent=VALUES(primary_agent), supporting_agents=VALUES(supporting_agents),
                            tools_used=VALUES(tools_used), escalated=VALUES(escalated), status=VALUES(status),
                            routing_reason=VALUES(routing_reason), routing_confidence=VALUES(routing_confidence),
                            latency_ms=VALUES(latency_ms)
                        """,
                        (
                            trace["request_id"], created_at, trace.get("intent"), trace.get("primary_agent"),
                            self._json(trace.get("supporting_agents", [])), self._json(trace.get("tools_used", [])),
                            bool(trace.get("escalated")), trace.get("status", "success"),
                            trace.get("routing_reason", ""), float(trace.get("routing_confidence", 0)),
                            float(trace.get("latency_ms", 0)),
                        ),
                    )
                    await cursor.execute("DELETE FROM llm_call_traces WHERE request_id = %s", (trace["request_id"],))
                    await cursor.execute("DELETE FROM tool_call_traces WHERE request_id = %s", (trace["request_id"],))
                    for item in llm_calls:
                        await cursor.execute(
                            """
                            INSERT INTO llm_call_traces (
                                request_id, created_at, agent_type, round_no, model, status, latency_ms,
                                error_type, error_message
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                            (
                                trace["request_id"], self._parse_timestamp(item.get("timestamp"), created_at),
                                item.get("agent_type", "unknown"), int(item.get("round_no", 0)),
                                item.get("model", "unknown"), item.get("status", "success"),
                                float(item.get("latency_ms", 0)), item.get("error_type"), item.get("error"),
                            ),
                        )
                    for sequence_no, item in enumerate(tool_calls, start=1):
                        await cursor.execute(
                            """
                            INSERT INTO tool_call_traces (
                                request_id, created_at, sequence_no, agent_type, tool_name, tool_use_id,
                                input_json, result_summary, success, result_success, latency_ms,
                                cached, reranked, error_message
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                            (
                                trace["request_id"], self._parse_timestamp(item.get("timestamp"), created_at),
                                sequence_no, item.get("agent_type", "unknown"), item.get("tool_name", "unknown"),
                                item.get("tool_use_id"), self._json(item.get("input", {})),
                                item.get("result_summary"), bool(item.get("success")), item.get("result_success"),
                                float(item.get("latency_ms", 0)), bool(item.get("cached")),
                                bool(item.get("reranked")), item.get("error"),
                            ),
                        )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, default=str)

    @staticmethod
    def _parse_timestamp(value: Any, default: Optional[datetime] = None) -> datetime:
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                pass
        return default or trace_now()

    @staticmethod
    def _load_json(value: Any, default: Any) -> Any:
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(value) if value else default
        except (TypeError, json.JSONDecodeError):
            return default

    def _build_trace(
        self,
        request_trace: Dict[str, Any],
        llm_calls: List[Dict[str, Any]],
        tool_calls: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        return {
            "request_id": request_trace["request_id"],
            "timestamp": request_trace["created_at"].isoformat(),
            "intent": request_trace["intent"],
            "primary_agent": request_trace["primary_agent"],
            "supporting_agents": self._load_json(request_trace["supporting_agents"], []),
            "tools_used": self._load_json(request_trace["tools_used"], []),
            "escalated": bool(request_trace["escalated"]),
            "status": request_trace["status"],
            "routing_reason": request_trace["routing_reason"],
            "routing_confidence": float(request_trace["routing_confidence"]),
            "latency_ms": float(request_trace["latency_ms"]),
            "llm_calls": [
                {
                    "agent_type": item["agent_type"], "round_no": item["round_no"],
                    "model": item["model"], "status": item["status"],
                    "latency_ms": float(item["latency_ms"]), "error_type": item["error_type"],
                    "error": item["error_message"], "timestamp": item["created_at"].isoformat(),
                }
                for item in llm_calls
            ],
            "tool_calls": [
                {
                    "agent_type": item["agent_type"], "tool_name": item["tool_name"],
                    "tool_use_id": item["tool_use_id"], "input": self._load_json(item["input_json"], {}),
                    "result_summary": item["result_summary"], "success": bool(item["success"]),
                    "result_success": None if item["result_success"] is None else bool(item["result_success"]),
                    "latency_ms": float(item["latency_ms"]), "cached": bool(item["cached"]),
                    "reranked": bool(item["reranked"]), "error": item["error_message"],
                    "timestamp": item["created_at"].isoformat(),
                }
                for item in tool_calls
            ],
        }
