"""MinerU Agent 轻量 API 适配器：上传单个文件并返回 Markdown。"""
import asyncio
import logging
import os
from typing import Any, Dict

import httpx


logger = logging.getLogger(__name__)


class MinerUParseError(RuntimeError):
    """MinerU 未能完成文档解析时抛出。"""


class MinerUParser:
    """封装 MinerU 签名上传、任务轮询与 Markdown 下载。"""

    def __init__(
        self,
        base_url: str | None = None,
        timeout_s: float | None = None,
        poll_interval_s: float | None = None,
    ) -> None:
        self._base_url = (
            base_url or os.getenv("MINERU_API_BASE_URL", "https://mineru.net/api/v1/agent")
        ).rstrip("/")
        self._timeout_s = timeout_s or float(os.getenv("MINERU_PARSE_TIMEOUT_S", "90"))
        self._poll_interval_s = poll_interval_s or float(
            os.getenv("MINERU_POLL_INTERVAL_S", "2")
        )

    async def parse(self, filename: str, content: bytes) -> str:
        """通过本地文件上传模式解析 PDF、DOCX 或图片，返回 Markdown。"""
        if not content:
            raise MinerUParseError("上传文件为空")

        suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        options: Dict[str, Any] = {"file_name": filename, "language": "ch"}
        if suffix == "pdf":
            # PDF 同时覆盖文本型与扫描件；MinerU 会执行 OCR 识别。
            options["is_ocr"] = True

        timeout = httpx.Timeout(self._timeout_s)
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                response = await client.post(f"{self._base_url}/parse/file", json=options)
                submission = self._payload(response, "创建 MinerU 解析任务失败")
                task_id = str(submission.get("task_id") or "")
                upload_url = str(submission.get("file_url") or "")
                if not task_id or not upload_url:
                    raise MinerUParseError("MinerU 未返回解析任务或上传地址")

                upload = await client.put(upload_url, content=content)
                upload.raise_for_status()

                loop = asyncio.get_running_loop()
                deadline = loop.time() + self._timeout_s
                while loop.time() < deadline:
                    await asyncio.sleep(self._poll_interval_s)
                    status = await client.get(f"{self._base_url}/parse/{task_id}")
                    result = self._payload(status, "查询 MinerU 解析任务失败")
                    state = result.get("state")
                    if state == "done":
                        markdown_url = str(result.get("markdown_url") or "")
                        if not markdown_url:
                            raise MinerUParseError("MinerU 解析完成但未返回 Markdown 地址")
                        text = await self._download_markdown(client, markdown_url)
                        if not text:
                            raise MinerUParseError("MinerU 返回的 Markdown 为空")
                        return text
                    if state == "failed":
                        raise MinerUParseError(
                            f"MinerU 解析失败: {result.get('err_msg') or '未知原因'}"
                        )
                raise MinerUParseError(f"MinerU 解析超时（{self._timeout_s:.0f} 秒）")
        except httpx.HTTPError as exc:
            detail = str(exc) or exc.__class__.__name__
            raise MinerUParseError(f"MinerU 网络请求失败: {detail}") from exc

    @staticmethod
    async def _download_markdown(client: httpx.AsyncClient, markdown_url: str) -> str:
        """优先通过 Windows 宿主机 Relay 下载，未配置或失败时回退为直连。"""
        relay_url = os.getenv("MINERU_RELAY_URL", "").rstrip("/")
        if relay_url:
            try:
                relay_response = await client.post(
                    f"{relay_url}/fetch", json={"url": markdown_url}
                )
                relay_response.raise_for_status()
                return relay_response.text.strip()
            except httpx.HTTPError as exc:
                logger.warning("MinerU Relay 下载失败，回退 CDN 直连: %s", exc)

        markdown = await client.get(markdown_url)
        markdown.raise_for_status()
        return markdown.text.strip()

    @staticmethod
    def _payload(response: httpx.Response, action: str) -> Dict[str, Any]:
        try:
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MinerUParseError(f"{action}: {exc}") from exc
        if not isinstance(body, dict) or body.get("code") != 0:
            message = body.get("msg") if isinstance(body, dict) else "响应格式错误"
            raise MinerUParseError(f"{action}: {message or '未知错误'}")
        data = body.get("data")
        if not isinstance(data, dict):
            raise MinerUParseError(f"{action}: 未返回任务数据")
        return data
