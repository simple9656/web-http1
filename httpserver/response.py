"""HTTP 响应构造与序列化。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from email.utils import formatdate
from html import escape
from http import HTTPStatus
from typing import Any


NO_BODY_STATUSES = {204, 304}


class Response:
    """一个待发送的 HTTP 响应。"""

    def __init__(
        self,
        status: int = 200,
        body: bytes = b"",
        *,
        content_type: str | None = None,
        headers: list[tuple[str, str]] | None = None,
    ) -> None:
        self.status = int(status)
        self.body = body
        self.content_type = content_type
        self.headers: list[tuple[str, str]] = list(headers or [])

    def add_header(self, name: str, value: str) -> "Response":
        self.headers.append((name, value))
        return self

    def set_header(self, name: str, value: str) -> "Response":
        lower = name.lower()
        self.headers = [(key, val) for key, val in self.headers if key.lower() != lower]
        self.headers.append((name, value))
        return self

    def get_header(self, name: str) -> str | None:
        lower = name.lower()
        for key, value in self.headers:
            if key.lower() == lower:
                return value
        return None

    @classmethod
    def html(cls, body: str, status: int = 200, headers: list[tuple[str, str]] | None = None) -> "Response":
        return cls(
            status,
            body.encode("utf-8"),
            content_type="text/html; charset=utf-8",
            headers=headers,
        )

    @classmethod
    def text(cls, body: str, status: int = 200) -> "Response":
        return cls(status, body.encode("utf-8"), content_type="text/plain; charset=utf-8")

    @classmethod
    def json(cls, value: Any, status: int = 200) -> "Response":
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        response = cls(status, data, content_type="application/json; charset=utf-8")
        response.add_header("Cache-Control", "no-store")
        return response

    @classmethod
    def redirect(cls, location: str, status: int = 301) -> "Response":
        safe_location = location.replace("\r", "").replace("\n", "")
        if safe_location.startswith("//"):
            safe_location = "/" + safe_location.lstrip("/")
        body = (
            "<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\">"
            f"<title>{status} 重定向</title>"
            f"<p>页面已移动到 <a href=\"{escape(safe_location)}\">{escape(safe_location)}</a>。</p>"
        )
        response = cls.html(body, status=status)
        response.add_header("Location", safe_location)
        return response

    def serialize(
        self,
        *,
        protocol: str,
        keep_alive: bool,
        head_only: bool,
        server_name: str,
        keep_alive_timeout: float,
    ) -> bytes:
        try:
            reason = HTTPStatus(self.status).phrase
        except ValueError:
            reason = "Unknown"
        if protocol not in ("HTTP/1.0", "HTTP/1.1"):
            protocol = "HTTP/1.1"

        body_forbidden = self.status in NO_BODY_STATUSES or 100 <= self.status < 200
        send_body = not head_only and not body_forbidden
        body_length = len(self.body)

        lines: list[str] = [f"{protocol} {self.status} {reason}"]
        existing = {name.lower() for name, _ in self.headers}
        if "date" not in existing:
            lines.append(f"Date: {formatdate(usegmt=True)}")
        if "server" not in existing:
            lines.append(f"Server: {server_name}")
        if self.content_type and "content-type" not in existing:
            lines.append(f"Content-Type: {self.content_type}")
        if not body_forbidden and "content-length" not in existing:
            lines.append(f"Content-Length: {body_length}")
        if "connection" not in existing:
            lines.append("Connection: keep-alive" if keep_alive else "Connection: close")
            if keep_alive:
                lines.append(f"Keep-Alive: timeout={max(1, int(keep_alive_timeout))}")

        for name, value in self.headers:
            if any(ch in name for ch in "\r\n:") or any(ch in value for ch in "\r\n"):
                raise ValueError("响应首部包含非法字符")
            try:
                name.encode("ascii")
                value.encode("latin-1")
            except UnicodeEncodeError as exc:
                raise ValueError("响应首部只能包含 ASCII/Latin-1 字符") from exc
            lines.append(f"{name}: {value}")

        header_bytes = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
        if send_body:
            return header_bytes + self.body
        return header_bytes


def error_response(
    status: int,
    detail: str | None = None,
    *,
    allow: str | None = None,
) -> Response:
    try:
        phrase = HTTPStatus(status).phrase
    except ValueError:
        phrase = "Error"
    title = {
        400: "请求格式错误",
        403: "禁止访问",
        404: "页面不存在",
        405: "方法不允许",
        408: "请求超时",
        413: "请求体过大",
        414: "URI 过长",
        415: "不支持的媒体类型",
        417: "无法满足 Expect",
        431: "请求头过大",
        500: "服务器内部错误",
        501: "尚未实现",
        505: "HTTP 版本不受支持",
    }.get(status, phrase)
    detail_html = f"<p class=\"detail\">{escape(detail)}</p>" if detail else ""
    body = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Crect width='16' height='16' fill='%23a33a26'/%3E%3C/svg%3E">
  <title>{status} {escape(title)}</title>
  <style>
    :root {{ color-scheme: light; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; min-height: 100vh; background: #f4f0e5; color: #1b211e;
           font-family: "Segoe UI", "Microsoft YaHei", system-ui, sans-serif; }}
    main {{ width: min(680px, calc(100% - 48px)); margin: 10vh auto 0;
            padding: 24px 0 52px; border-top: 4px solid #a33a26; }}
    .code {{ font-family: Georgia, "SimSun", serif;
             font-size: clamp(86px, 16vw, 132px); line-height: .85;
             letter-spacing: -.07em; }}
    h1 {{ margin: 18px 0 10px; font-family: Georgia, "SimSun", serif;
          font-size: 36px; font-weight: 500; letter-spacing: -.035em; }}
    p {{ color: #3f4842; line-height: 1.7; }}
    .detail {{ margin: 18px 0 28px; padding: 2px 0 2px 14px;
               border-left: 3px solid #c9c1b1; color: #4d554e; }}
    a {{ display: inline-block; padding: 8px 14px; border: 1px solid #1b211e;
         color: #1b211e; font-family: Consolas, monospace; font-size: 11px;
         text-decoration: none; }}
    a:hover {{ color: #f4f0e5; background: #1b211e; }}
  </style>
</head>
<body>
  <main>
    <div class="code">{status}</div>
    <h1>{escape(title)}</h1>
    {detail_html}
    <a href="/">返回首页</a>
  </main>
</body>
</html>"""
    response = Response.html(body, status=status)
    response.add_header("Cache-Control", "no-store")
    if allow:
        response.add_header("Allow", allow)
    return response
