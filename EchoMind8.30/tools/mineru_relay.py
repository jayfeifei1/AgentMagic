"""Windows 宿主机 Relay：为 Docker 下载 MinerU 的 Markdown 结果。"""
import argparse
import json
import subprocess
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


_MINERU_CDN_HOST = "cdn-mineru.openxlab.org.cn"
_MAX_MARKDOWN_BYTES = 10 * 1024 * 1024


def _is_mineru_markdown_url(url: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname == _MINERU_CDN_HOST
        and parsed.path.endswith(".md")
        and not parsed.username
        and not parsed.password
    )


def _download_markdown(url: str) -> bytes:
    result = subprocess.run(
        [
            "curl.exe",
            "--fail",
            "--silent",
            "--show-error",
            "--location",
            "--proto",
            "=https",
            "--proto-redir",
            "=https",
            "--connect-timeout",
            "15",
            "--max-time",
            "90",
            "--header",
            "Referer: https://mineru.net/",
            url,
        ],
        capture_output=True,
        timeout=95,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(message or f"curl.exe 退出码 {result.returncode}")
    if not result.stdout.strip():
        raise RuntimeError("MinerU 返回的 Markdown 为空")
    if len(result.stdout) > _MAX_MARKDOWN_BYTES:
        raise RuntimeError("MinerU Markdown 超过 10MB 限制")
    return result.stdout


class MinerURelayHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(HTTPStatus.OK, {"status": "ok"})
        else:
            self._send_json(HTTPStatus.NOT_FOUND, {"detail": "not found"})

    def do_POST(self) -> None:
        if self.path != "/fetch":
            self._send_json(HTTPStatus.NOT_FOUND, {"detail": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            url = body.get("url", "") if isinstance(body, dict) else ""
            if not isinstance(url, str) or not _is_mineru_markdown_url(url):
                raise ValueError("只允许下载 MinerU 官方 CDN 的 .md 文件")
            self._send_markdown(_download_markdown(url))
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"detail": str(exc)})
        except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
            self._send_json(HTTPStatus.BAD_GATEWAY, {"detail": f"下载失败: {exc}"})

    def log_message(self, format: str, *args: object) -> None:
        print(f"[mineru-relay] {format % args}")

    def _send_json(self, status: HTTPStatus, body: dict) -> None:
        content = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _send_markdown(self, content: bytes) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/markdown; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


def main() -> None:
    parser = argparse.ArgumentParser(description="MinerU Windows 宿主机下载 Relay")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18789)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), MinerURelayHandler)
    print(f"MinerU Relay 已启动: http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
