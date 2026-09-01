"""MySQL-backed demo business data used by Agent tools."""
import logging
import os
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional

import aiomysql

from core.trace_time import trace_now

logger = logging.getLogger(__name__)


class BusinessRepository:
    """Provides scoped, parameterized access to demo order and support data."""

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
    def available(self) -> bool:
        return self._pool is not None

    async def connect(self) -> bool:
        if self._pool is not None:
            return True
        if not self._config["user"] or not self._config["password"]:
            logger.warning("业务数据未配置 MySQL 凭据")
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
            await self._seed_demo_data()
            logger.info("MySQL 业务数据仓储已连接")
            return True
        except Exception as ex:
            logger.warning("MySQL 业务数据仓储连接失败: %s", ex)
            await self.close()
            return False

    async def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None

    async def get_order_status(self, user_id: str, order_id: str) -> Dict[str, Any]:
        row = await self._fetch_order(user_id, order_id)
        if row is None:
            return self._not_found("未找到属于当前用户的订单")
        return {
            "success": True,
            "order_id": row["order_id"],
            "status": row["status"],
            "total_amount": self._number(row["total_amount"]),
            "currency": row["currency"],
            "created_at": self._timestamp(row["created_at"]),
            "updated_at": self._timestamp(row["updated_at"]),
        }

    async def get_order_payment_summary(self, user_id: str, order_id: str) -> Dict[str, Any]:
        order = await self._fetch_order(user_id, order_id)
        if order is None:
            return self._not_found("未找到属于当前用户的订单")
        rows = await self._fetchall(
            """
            SELECT transaction_id, amount, currency, channel, status, paid_at
            FROM payment_transactions WHERE order_id = %s ORDER BY paid_at
            """,
            (order_id,),
        )
        payments = [
            {
                "transaction_id": row["transaction_id"],
                "amount": self._number(row["amount"]),
                "currency": row["currency"],
                "channel": row["channel"],
                "status": row["status"],
                "paid_at": self._timestamp(row["paid_at"]),
            }
            for row in rows
        ]
        successful = [item for item in payments if item["status"] == "PAID"]
        paid_total = round(sum(item["amount"] for item in successful), 2)
        expected_amount = self._number(order["total_amount"])
        return {
            "success": True,
            "order_id": order_id,
            "order_status": order["status"],
            "expected_amount": expected_amount,
            "paid_total": paid_total,
            "successful_payment_count": len(successful),
            "duplicate_payment_suspected": len(successful) > 1 and paid_total > expected_amount,
            "payments": payments,
        }

    async def get_refund_status(self, user_id: str, order_id: str) -> Dict[str, Any]:
        row = await self._fetchone(
            """
            SELECT r.refund_id, r.amount, r.currency, r.status, r.reason, r.requested_at, r.completed_at
            FROM refunds r JOIN orders o ON o.order_id = r.order_id
            WHERE r.order_id = %s AND o.user_id = %s
            ORDER BY r.requested_at DESC LIMIT 1
            """,
            (order_id, user_id),
        )
        if row is None:
            return self._not_found("未找到属于当前用户的退款记录")
        return {
            "success": True,
            "refund_id": row["refund_id"],
            "order_id": order_id,
            "amount": self._number(row["amount"]),
            "currency": row["currency"],
            "status": row["status"],
            "reason": row["reason"],
            "requested_at": self._timestamp(row["requested_at"]),
            "completed_at": self._timestamp(row["completed_at"]),
        }

    async def get_error_code_playbook(self, error_code: str) -> Dict[str, Any]:
        row = await self._fetchone(
            "SELECT error_code, title, description, recommended_actions FROM error_code_playbooks WHERE error_code = %s",
            (self._text(error_code, 20).upper(),),
        )
        if row is None:
            return self._not_found("未找到该错误码的排障手册")
        return {
            "success": True,
            "error_code": row["error_code"],
            "title": row["title"],
            "description": row["description"],
            "recommended_actions": row["recommended_actions"],
        }

    async def query_incident_status(self, error_code: str, service_name: str = "") -> Dict[str, Any]:
        sql = """
            SELECT incident_id, service_name, error_code, severity, status, affected_scope, started_at, updated_at
            FROM service_incidents WHERE error_code = %s
        """
        params: List[Any] = [self._text(error_code, 20).upper()]
        service_name = self._text(service_name, 80)
        if service_name:
            sql += " AND service_name = %s"
            params.append(service_name)
        sql += " ORDER BY updated_at DESC LIMIT 1"
        row = await self._fetchone(sql, tuple(params))
        if row is None:
            return self._not_found("当前没有匹配的服务故障记录")
        return {
            "success": True,
            "incident_id": row["incident_id"],
            "service_name": row["service_name"],
            "error_code": row["error_code"],
            "severity": row["severity"],
            "status": row["status"],
            "affected_scope": row["affected_scope"],
            "started_at": self._timestamp(row["started_at"]),
            "updated_at": self._timestamp(row["updated_at"]),
        }

    async def create_handoff_ticket(
        self,
        request_id: str,
        user_id: str,
        category: str,
        priority: str,
        reason: str,
    ) -> Dict[str, Any]:
        if not await self._ensure_pool():
            return {"success": False, "error": "业务数据服务不可用"}
        priority = priority.lower()
        if priority not in {"normal", "high", "critical"}:
            priority = "normal"
        ticket_id = f"TKT-{uuid.uuid4().hex[:10].upper()}"
        now = trace_now()
        try:
            assert self._pool is not None
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        """
                        INSERT INTO handoff_tickets (
                            ticket_id, request_id, user_id, category, priority, reason, status, created_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, 'OPEN', %s)
                        ON DUPLICATE KEY UPDATE reason = VALUES(reason), priority = VALUES(priority)
                        """,
                        (
                            ticket_id,
                            self._text(request_id, 64),
                            self._text(user_id, 64),
                            self._text(category, 32),
                            priority,
                            self._text(reason, 500),
                            now,
                        ),
                    )
                    await cursor.execute(
                        "SELECT ticket_id, priority, status, created_at FROM handoff_tickets WHERE request_id = %s",
                        (self._text(request_id, 64),),
                    )
                    row = await cursor.fetchone()
                await conn.commit()
            return {
                "success": True,
                "ticket_id": row["ticket_id"],
                "priority": row["priority"],
                "status": row["status"],
                "created_at": self._timestamp(row["created_at"]),
            }
        except Exception as ex:
            logger.warning("创建人工交接工单失败: %s", ex)
            return {"success": False, "error": "创建人工交接工单失败"}

    async def _fetch_order(self, user_id: str, order_id: str) -> Optional[Dict[str, Any]]:
        return await self._fetchone(
            """
            SELECT order_id, user_id, status, total_amount, currency, created_at, updated_at
            FROM orders WHERE order_id = %s AND user_id = %s
            """,
            (self._text(order_id, 64), self._text(user_id, 64)),
        )

    async def _fetchone(self, sql: str, params: tuple[Any, ...]) -> Optional[Dict[str, Any]]:
        if not await self._ensure_pool():
            return None
        try:
            assert self._pool is not None
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(sql, params)
                    return await cursor.fetchone()
        except Exception as ex:
            logger.warning("业务数据查询失败: %s", ex)
            return None

    async def _fetchall(self, sql: str, params: tuple[Any, ...]) -> List[Dict[str, Any]]:
        if not await self._ensure_pool():
            return []
        try:
            assert self._pool is not None
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(sql, params)
                    return await cursor.fetchall()
        except Exception as ex:
            logger.warning("业务数据查询失败: %s", ex)
            return []

    async def _ensure_pool(self) -> bool:
        return self._pool is not None or await self.connect()

    async def _initialize_schema(self) -> None:
        assert self._pool is not None
        statements = (
            """
            CREATE TABLE IF NOT EXISTS orders (
                order_id VARCHAR(64) PRIMARY KEY,
                user_id VARCHAR(64) NOT NULL,
                status VARCHAR(32) NOT NULL,
                total_amount DECIMAL(12, 2) NOT NULL,
                currency CHAR(3) NOT NULL DEFAULT 'CNY',
                created_at DATETIME(3) NOT NULL,
                updated_at DATETIME(3) NOT NULL,
                INDEX idx_orders_user_created (user_id, created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """,
            """
            CREATE TABLE IF NOT EXISTS payment_transactions (
                transaction_id VARCHAR(64) PRIMARY KEY,
                order_id VARCHAR(64) NOT NULL,
                amount DECIMAL(12, 2) NOT NULL,
                currency CHAR(3) NOT NULL DEFAULT 'CNY',
                channel VARCHAR(32) NOT NULL,
                status VARCHAR(32) NOT NULL,
                paid_at DATETIME(3) NOT NULL,
                INDEX idx_payments_order_paid (order_id, paid_at),
                CONSTRAINT fk_payment_order FOREIGN KEY (order_id) REFERENCES orders(order_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """,
            """
            CREATE TABLE IF NOT EXISTS refunds (
                refund_id VARCHAR(64) PRIMARY KEY,
                order_id VARCHAR(64) NOT NULL,
                amount DECIMAL(12, 2) NOT NULL,
                currency CHAR(3) NOT NULL DEFAULT 'CNY',
                status VARCHAR(32) NOT NULL,
                reason VARCHAR(255) NOT NULL,
                requested_at DATETIME(3) NOT NULL,
                completed_at DATETIME(3) NULL,
                INDEX idx_refunds_order_requested (order_id, requested_at),
                CONSTRAINT fk_refund_order FOREIGN KEY (order_id) REFERENCES orders(order_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """,
            """
            CREATE TABLE IF NOT EXISTS error_code_playbooks (
                error_code VARCHAR(20) PRIMARY KEY,
                title VARCHAR(128) NOT NULL,
                description TEXT NOT NULL,
                recommended_actions JSON NOT NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """,
            """
            CREATE TABLE IF NOT EXISTS service_incidents (
                incident_id VARCHAR(64) PRIMARY KEY,
                service_name VARCHAR(80) NOT NULL,
                error_code VARCHAR(20) NOT NULL,
                severity VARCHAR(16) NOT NULL,
                status VARCHAR(32) NOT NULL,
                affected_scope VARCHAR(255) NOT NULL,
                started_at DATETIME(3) NOT NULL,
                updated_at DATETIME(3) NOT NULL,
                INDEX idx_incidents_error_updated (error_code, updated_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """,
            """
            CREATE TABLE IF NOT EXISTS handoff_tickets (
                ticket_id VARCHAR(32) PRIMARY KEY,
                request_id VARCHAR(64) NOT NULL UNIQUE,
                user_id VARCHAR(64) NOT NULL,
                category VARCHAR(32) NOT NULL,
                priority VARCHAR(16) NOT NULL,
                reason VARCHAR(500) NOT NULL,
                status VARCHAR(32) NOT NULL,
                created_at DATETIME(3) NOT NULL,
                INDEX idx_handoff_user_created (user_id, created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """,
        )
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                for statement in statements:
                    await cursor.execute(statement)
            await conn.commit()

    async def _seed_demo_data(self) -> None:
        assert self._pool is not None
        now = trace_now()
        orders = (
            ("ORD-DEMO-1001", "u1001", "PAID", 99.00, "CNY", now - timedelta(days=2), now - timedelta(days=2)),
            ("ORD-DEMO-1002", "u1001", "REFUNDING", 199.00, "CNY", now - timedelta(days=4), now - timedelta(hours=6)),
            ("ORD-DEMO-1004", "u1001", "SHIPPED", 289.00, "CNY", now - timedelta(days=3), now - timedelta(hours=9)),
            ("ORD-DEMO-1005", "u1001", "REFUNDED", 49.90, "CNY", now - timedelta(days=12), now - timedelta(days=8)),
            ("ORD-DEMO-1006", "u1001", "PENDING_PAYMENT", 129.00, "CNY", now - timedelta(hours=2), now - timedelta(hours=2)),
            ("ORD-DEMO-2001", "u2002", "PAID", 399.00, "CNY", now - timedelta(days=1), now - timedelta(days=1)),
            ("ORD-DEMO-2002", "u2002", "CANCELLED", 79.00, "CNY", now - timedelta(days=9), now - timedelta(days=8)),
            ("ORD-DEMO-3001", "u3003", "DELIVERED", 999.00, "CNY", now - timedelta(days=15), now - timedelta(days=10)),
        )
        payments = (
            ("PAY-DEMO-1001A", "ORD-DEMO-1001", 99.00, "CNY", "ALIPAY", "PAID", now - timedelta(days=2, minutes=3)),
            ("PAY-DEMO-1001B", "ORD-DEMO-1001", 99.00, "CNY", "ALIPAY", "PAID", now - timedelta(days=2, minutes=2)),
            ("PAY-DEMO-1002A", "ORD-DEMO-1002", 199.00, "CNY", "WECHAT", "PAID", now - timedelta(days=4)),
            ("PAY-DEMO-1004A", "ORD-DEMO-1004", 289.00, "CNY", "CARD", "PAID", now - timedelta(days=3)),
            ("PAY-DEMO-1005A", "ORD-DEMO-1005", 49.90, "CNY", "ALIPAY", "PAID", now - timedelta(days=12)),
            ("PAY-DEMO-1006A", "ORD-DEMO-1006", 129.00, "CNY", "WECHAT", "FAILED", now - timedelta(hours=2)),
            ("PAY-DEMO-2001A", "ORD-DEMO-2001", 399.00, "CNY", "CARD", "PAID", now - timedelta(days=1)),
            ("PAY-DEMO-2002A", "ORD-DEMO-2002", 79.00, "CNY", "ALIPAY", "PAID", now - timedelta(days=9)),
            ("PAY-DEMO-3001A", "ORD-DEMO-3001", 999.00, "CNY", "WECHAT", "PAID", now - timedelta(days=15)),
        )
        refunds = (
            ("RFD-DEMO-2001", "ORD-DEMO-1002", 199.00, "CNY", "PROCESSING", "七天无理由退款", now - timedelta(days=1), None),
            ("RFD-DEMO-2002", "ORD-DEMO-1005", 49.90, "CNY", "COMPLETED", "商品尺寸不合适", now - timedelta(days=10), now - timedelta(days=8)),
            ("RFD-DEMO-2003", "ORD-DEMO-2002", 79.00, "CNY", "COMPLETED", "订单取消后原路退款", now - timedelta(days=9), now - timedelta(days=8)),
        )
        playbooks = (
            ("401", "认证失败", "认证令牌、会话或请求签名校验未通过。", '["确认登录状态或 Token 是否过期", "核对系统时间与请求签名", "记录 request_id 后联系技术支持"]'),
            ("403", "权限不足", "账号、资源权限或 IP 白名单校验未通过。", '["确认账号角色和套餐权限", "核对资源授权范围", "检查 IP 白名单配置"]'),
            ("404", "资源不存在", "接口路径、资源标识或环境配置不正确。", '["确认接口路径和请求环境", "核对订单或资源标识", "检查版本发布记录"]'),
            ("500", "服务端处理异常", "服务端在处理请求时发生未预期错误。", '["记录错误发生时间和 request_id", "确认依赖服务状态", "若影响持续扩大则申请人工介入"]'),
            ("502", "网关或上游服务异常", "网关未能从上游服务获得有效响应。", '["确认上游服务健康状态", "检查网关超时和重试配置", "记录影响范围并升级处理"]'),
        )
        incidents = (
            ("INC-DEMO-401", "auth-gateway", "401", "MINOR", "RESOLVED", "历史认证服务短暂波动，当前已恢复", now - timedelta(days=1), now - timedelta(hours=20)),
            ("INC-DEMO-403", "permission-service", "403", "MINOR", "RESOLVED", "企业账号权限同步延迟已恢复", now - timedelta(days=3), now - timedelta(days=3, hours=-1)),
            ("INC-DEMO-404", "catalog-service", "404", "LOW", "RESOLVED", "商品目录缓存刷新期间短暂返回资源不存在", now - timedelta(days=5), now - timedelta(days=5, hours=-1)),
            ("INC-DEMO-500", "order-service", "500", "MAJOR", "INVESTIGATING", "订单服务部分接口受影响，技术团队正在排查", now - timedelta(minutes=45), now - timedelta(minutes=5)),
            ("INC-DEMO-502", "payment-gateway", "502", "CRITICAL", "INVESTIGATING", "支付网关上游响应不稳定，部分支付请求失败", now - timedelta(minutes=20), now - timedelta(minutes=2)),
        )
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.executemany(
                    """
                    INSERT INTO orders (order_id, user_id, status, total_amount, currency, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE status = VALUES(status), total_amount = VALUES(total_amount), updated_at = VALUES(updated_at)
                    """,
                    orders,
                )
                await cursor.executemany(
                    """
                    INSERT INTO payment_transactions (transaction_id, order_id, amount, currency, channel, status, paid_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE status = VALUES(status), amount = VALUES(amount), paid_at = VALUES(paid_at)
                    """,
                    payments,
                )
                await cursor.executemany(
                    """
                    INSERT INTO refunds (refund_id, order_id, amount, currency, status, reason, requested_at, completed_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE status = VALUES(status), completed_at = VALUES(completed_at)
                    """,
                    refunds,
                )
                await cursor.executemany(
                    """
                    INSERT INTO error_code_playbooks (error_code, title, description, recommended_actions)
                    VALUES (%s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE title = VALUES(title), description = VALUES(description), recommended_actions = VALUES(recommended_actions)
                    """,
                    playbooks,
                )
                await cursor.executemany(
                    """
                    INSERT INTO service_incidents (incident_id, service_name, error_code, severity, status, affected_scope, started_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE severity = VALUES(severity), status = VALUES(status), affected_scope = VALUES(affected_scope), updated_at = VALUES(updated_at)
                    """,
                    incidents,
                )
            await conn.commit()

    @staticmethod
    def _not_found(message: str) -> Dict[str, Any]:
        return {"success": False, "found": False, "error": message}

    @staticmethod
    def _text(value: Any, limit: int) -> str:
        return str(value or "").strip()[:limit]

    @staticmethod
    def _number(value: Decimal | int | float) -> float:
        return float(value)

    @staticmethod
    def _timestamp(value: Optional[datetime]) -> Optional[str]:
        return value.isoformat() if value else None
