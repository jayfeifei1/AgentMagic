import asyncio

from mcp.mineru_parser import MinerUParser


class FakeResponse:
    def __init__(self, payload=None, text=""):
        self._payload = payload
        self.text = text

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeAsyncClient:
    instances = []

    def __init__(self, **kwargs):
        self.calls = []
        self.status_requests = 0
        self.__class__.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        if url == "http://relay.test/fetch":
            return FakeResponse(text="# Relay 下载结果\n\n已返回 Markdown。")
        return FakeResponse({"code": 0, "data": {"task_id": "task-1", "file_url": "https://upload"}})

    async def put(self, url, **kwargs):
        self.calls.append(("put", url, kwargs))
        return FakeResponse()

    async def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        if url.endswith("/task-1"):
            self.status_requests += 1
            return FakeResponse({
                "code": 0,
                "data": {"state": "done", "markdown_url": "https://result/document.md"},
            })
        return FakeResponse(text="# 退款规则\n\n退款审核通过后原路退回。")


def test_mineru_parser_uploads_pdf_with_ocr_and_returns_markdown(monkeypatch):
    import mcp.mineru_parser as module

    FakeAsyncClient.instances.clear()
    monkeypatch.setattr(module.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.delenv("MINERU_RELAY_URL", raising=False)
    parser = MinerUParser(base_url="https://mineru.test/api", timeout_s=1, poll_interval_s=0.001)

    markdown = asyncio.run(parser.parse("退款规则.pdf", b"pdf-content"))

    client = FakeAsyncClient.instances[0]
    assert markdown.startswith("# 退款规则")
    assert client.calls[0] == (
        "post",
        "https://mineru.test/api/parse/file",
        {"json": {"file_name": "退款规则.pdf", "language": "ch", "is_ocr": True}},
    )
    assert client.calls[1] == ("put", "https://upload", {"content": b"pdf-content"})
    assert client.status_requests == 1


def test_mineru_parser_uses_configured_relay_for_markdown(monkeypatch):
    import mcp.mineru_parser as module

    FakeAsyncClient.instances.clear()
    monkeypatch.setattr(module.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setenv("MINERU_RELAY_URL", "http://relay.test")
    parser = MinerUParser(base_url="https://mineru.test/api", timeout_s=1, poll_interval_s=0.001)

    markdown = asyncio.run(parser.parse("退款规则.docx", b"docx-content"))

    client = FakeAsyncClient.instances[0]
    assert markdown.startswith("# Relay 下载结果")
    assert (
        "post",
        "http://relay.test/fetch",
        {"json": {"url": "https://result/document.md"}},
    ) in client.calls
