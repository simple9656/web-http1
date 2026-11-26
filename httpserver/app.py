"""路由、静态文件、目录列表、表单与虚拟主机业务逻辑。"""
from __future__ import annotations

import mimetypes
import os
import stat as stat_module
from datetime import datetime, timezone
from email.utils import formatdate, parsedate_to_datetime
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote

from .config import ServerConfig, VirtualHost
from .http_parser import Request
from .response import Response, error_response


MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".xml": "application/xml; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".pdf": "application/pdf",
    ".wasm": "application/wasm",
    ".mp4": "video/mp4",
}

WINDOWS_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}
WINDOWS_INVALID = set('<>:"|?*')


def guess_content_type(path: Path) -> str:
    explicit = MIME_TYPES.get(path.suffix.lower())
    if explicit:
        return explicit
    guessed, encoding = mimetypes.guess_type(path.name, strict=False)
    if not guessed:
        return "application/octet-stream"
    if guessed.startswith("text/") or guessed in {
        "application/javascript",
        "application/json",
        "application/xml",
        "image/svg+xml",
    }:
        if encoding:
            return f"{guessed}; charset={encoding}"
        return f"{guessed}; charset=utf-8"
    return guessed


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


class Application:
    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self.started_at = datetime.now(timezone.utc)

    def resolve_host(self, request: Request) -> VirtualHost | None:
        return self.config.vhost_for(request.authority)

    def handle(self, request: Request, vhost: VirtualHost | None) -> Response:
        if vhost is None:
            return error_response(404, f"未知的虚拟主机: {request.authority or '-'}")

        if self.config.test_mode and request.path == "/__test__/error":
            if request.method not in ("GET", "HEAD"):
                return error_response(405, "测试错误路由只支持 GET/HEAD", allow="GET, HEAD")
            raise RuntimeError("intentional 500 response for the test suite")

        if request.path == "/healthz":
            if request.method not in ("GET", "HEAD"):
                return error_response(405, "健康检查只支持 GET/HEAD", allow="GET, HEAD")
            now = datetime.now(timezone.utc)
            return Response.json(
                {
                    "status": "ok",
                    "host": vhost.name,
                    "uptime_seconds": round((now - self.started_at).total_seconds(), 3),
                    "time": now.isoformat(),
                }
            )

        if request.path == "/submit":
            return self._handle_submit(request)

        if request.path == "/api/echo":
            if request.method not in ("GET", "HEAD", "POST"):
                return error_response(405, "该接口支持 GET/HEAD/POST", allow="GET, HEAD, POST")
            return self._handle_echo(request, vhost)

        if request.method not in ("GET", "HEAD"):
            return error_response(405, "静态资源只支持 GET/HEAD", allow="GET, HEAD")

        return self._serve_static(request, vhost)

    def _handle_echo(self, request: Request, vhost: VirtualHost) -> Response:
        try:
            body_text = request.body.decode("utf-8")
        except UnicodeDecodeError:
            body_text = request.body.decode("latin-1")
        truncated = len(body_text) > 4096
        payload = {
            "method": request.method,
            "target": request.target,
            "path": request.path,
            "query": request.query,
            "version": request.version,
            "host": vhost.name,
            "headers": request.headers,
            "body": body_text[:4096],
            "body_truncated": truncated,
        }
        return Response.json(payload)

    def _handle_submit(self, request: Request) -> Response:
        if request.method != "POST":
            return error_response(405, "表单提交只接受 POST", allow="POST")

        content_type = (request.header("content-type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/x-www-form-urlencoded":
            return error_response(
                415,
                "请使用 application/x-www-form-urlencoded 提交表单",
            )
        try:
            text = request.body.decode("utf-8")
        except UnicodeDecodeError:
            return error_response(400, "表单内容必须是 UTF-8 编码")
        try:
            parsed = parse_qs(text, keep_blank_values=True, max_num_fields=50)
        except ValueError:
            return error_response(400, "表单字段过多")

        values = {key: items[0] if items else "" for key, items in parsed.items()}
        name = values.get("name", "").strip()
        email = values.get("email", "").strip()
        message = values.get("message", "").strip()
        topic = values.get("topic", "一般咨询").strip() or "一般咨询"

        if not name or not message:
            return error_response(400, "姓名和留言内容不能为空")
        if len(name) > 80 or len(email) > 120 or len(message) > 2000:
            return error_response(400, "表单字段超过长度限制")

        accept = request.header("accept") or ""
        if "application/json" in accept.lower():
            return Response.json(
                {
                    "ok": True,
                    "name": name,
                    "email": email,
                    "topic": topic,
                    "message": message,
                }
            )

        html = self._success_page(name=name, email=email, topic=topic, message=message)
        response = Response.html(html)
        response.add_header("Cache-Control", "no-store")
        return response

    def _success_page(self, *, name: str, email: str, topic: str, message: str) -> str:
        safe_name = escape(name)
        safe_email = escape(email) if email else "未填写"
        safe_topic = escape(topic)
        safe_message = escape(message).replace("\n", "<br>")
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Crect width='16' height='16' fill='%23a33a26'/%3E%3C/svg%3E">
  <title>提交成功 · SimpleHTTP</title>
  <style>
    :root {{ color-scheme: light; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; min-height: 100vh; background: #f4f0e5; color: #1b211e;
           font-family: "Segoe UI", "Microsoft YaHei", system-ui, sans-serif; }}
    main {{ width: min(720px, calc(100% - 48px)); margin: 10vh auto 0;
           padding: 24px 0 46px; border-top: 4px solid #a33a26; }}
    h1 {{ margin: 0 0 26px; font-family: Georgia, "SimSun", serif;
          font-size: 40px; font-weight: 500; letter-spacing: -.04em; }}
    dl {{ display: grid; grid-template-columns: 90px minmax(0, 1fr); gap: 0;
          margin: 0; border-top: 1px solid #c8c0b0; }}
    dt, dd {{ padding: 11px 0; border-bottom: 1px solid #c8c0b0; }}
    dt {{ color: #687069; font-family: Consolas, monospace; font-size: 10px;
          letter-spacing: .05em; text-transform: uppercase; }}
    dd {{ margin: 0; overflow-wrap: anywhere; }}
    .message {{ margin-top: 24px; padding: 16px 18px; background: #e8e2d4;
                border-left: 3px solid #a33a26; line-height: 1.8; color: #3f4842; }}
    a {{ display: inline-block; margin-top: 26px; padding: 8px 14px;
         border: 1px solid #1b211e; color: #1b211e; font-family: Consolas, monospace;
         font-size: 11px; text-decoration: none; }}
    a:hover {{ color: #f4f0e5; background: #1b211e; }}
  </style>
</head>
<body>
  <main>
    <h1>表单已成功处理</h1>
    <dl>
      <dt>姓名</dt><dd>{safe_name}</dd>
      <dt>邮箱</dt><dd>{safe_email}</dd>
      <dt>主题</dt><dd>{safe_topic}</dd>
    </dl>
    <div class="message">{safe_message}</div>
    <a href="/form.html">返回表单</a>
  </main>
</body>
</html>"""

    def _decode_path_segments(self, path: str) -> tuple[list[str], Response | None]:
        segments: list[str] = []
        for segment in path.split("/"):
            if segment in ("", "."):
                continue
            if segment == "..":
                return [], error_response(403, "路径不能包含 ..")
            if "\x00" in segment or "\\" in segment:
                return [], error_response(403, "路径包含不安全字符")
            if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in segment):
                return [], error_response(400, "路径包含控制字符")
            if os.name == "nt":
                if any(ch in WINDOWS_INVALID for ch in segment):
                    return [], error_response(403, "路径包含 Windows 不允许的字符")
                if segment.endswith(" ") or segment.endswith("."):
                    return [], error_response(403, "路径包含 Windows 不安全的结尾字符")
                if segment.split(".", 1)[0].casefold() in WINDOWS_RESERVED:
                    return [], error_response(403, "路径命中 Windows 保留设备名")
            if len(segment) > 255:
                return [], error_response(404, "路径组件过长")
            segments.append(segment)
        return segments, None

    def _safe_candidate(self, root: Path, segments: list[str]) -> tuple[Path | None, Response | None]:
        candidate = root.joinpath(*segments)
        try:
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError):
            return None, error_response(404, "路径无法解析")
        try:
            resolved.relative_to(root)
        except ValueError:
            return None, error_response(403, "路径越界")
        return resolved, None

    def _serve_static(self, request: Request, vhost: VirtualHost) -> Response:
        if vhost.denies(request.path):
            return error_response(403, "该路径已被服务器禁止访问")

        segments, error = self._decode_path_segments(request.path)
        if error is not None:
            return error
        candidate, error = self._safe_candidate(vhost.root, segments)
        if error is not None:
            return error
        assert candidate is not None

        try:
            info = candidate.stat()
        except FileNotFoundError:
            return error_response(404, f"找不到资源: {request.path}")
        except PermissionError:
            return error_response(403, "没有读取该路径的权限")
        except OSError:
            return error_response(404, "文件不可访问")

        if stat_module.S_ISDIR(info.st_mode):
            if not request.path.endswith("/"):
                # 避免 Location: //host/ 被浏览器解释为协议相对 URL。
                safe_path = "/" + request.path.lstrip("/")
                location = quote(safe_path, safe="/") + "/"
                if request.query:
                    location += "?" + request.query
                return Response.redirect(location)
            if vhost.denies(request.path + "index.html"):
                return error_response(403, "该路径已被服务器禁止访问")
            index = candidate / "index.html"
            try:
                index_info = index.stat()
            except OSError:
                index_info = None
            if index_info is not None and stat_module.S_ISREG(index_info.st_mode):
                return self._serve_file(request, vhost, index, known_stat=index_info)
            return self._directory_listing(request, vhost, candidate)

        if stat_module.S_ISREG(info.st_mode):
            if request.path.endswith("/"):
                return error_response(404, f"不是目录: {request.path}")
            return self._serve_file(
                request,
                vhost,
                candidate,
                known_stat=info,
                pre_resolved=True,
            )

        return error_response(404, f"找不到资源: {request.path}")

    def _within_root(self, path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def _serve_file(
        self,
        request: Request,
        vhost: VirtualHost,
        path: Path,
        *,
        known_stat: os.stat_result | None = None,
        pre_resolved: bool = False,
    ) -> Response:
        if pre_resolved:
            resolved = path
        else:
            try:
                resolved = path.resolve(strict=True)
            except (OSError, RuntimeError):
                return error_response(404, "文件不存在")
        if not self._within_root(resolved, vhost.root):
            return error_response(403, "文件位于站点根目录之外")

        try:
            info = known_stat if known_stat is not None else resolved.stat()
            if not stat_module.S_ISREG(info.st_mode):
                return error_response(404, "文件不存在")
            data = resolved.read_bytes()
        except PermissionError:
            return error_response(403, "没有读取该文件的权限")
        except FileNotFoundError:
            return error_response(404, "文件不存在")
        except OSError:
            return error_response(500, "读取文件失败")

        etag = f'"{info.st_mtime_ns:x}-{info.st_size:x}"'
        last_modified = formatdate(info.st_mtime, usegmt=True)
        content_type = guess_content_type(resolved)

        if self._not_modified(request, etag, info.st_mtime):
            response = Response(304)
            response.add_header("ETag", etag)
            response.add_header("Last-Modified", last_modified)
            response.add_header("Cache-Control", "no-cache")
            return response

        response = Response(200, data, content_type=content_type)
        response.add_header("ETag", etag)
        response.add_header("Last-Modified", last_modified)
        response.add_header("Cache-Control", "no-cache")
        response.add_header("X-Content-Type-Options", "nosniff")
        return response

    def _not_modified(self, request: Request, etag: str, mtime: float) -> bool:
        if_none_match = request.header("if-none-match")
        if if_none_match is not None:
            tags = [item.strip() for item in if_none_match.split(",")]
            normalized = {tag[2:].strip() if tag.startswith("W/") else tag for tag in tags}
            return "*" in tags or etag in normalized

        if_modified_since = request.header("if-modified-since")
        if not if_modified_since:
            return False
        try:
            since = parsedate_to_datetime(if_modified_since)
        except (TypeError, ValueError, OverflowError):
            return False
        if since is None:
            return False
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        try:
            return int(mtime) <= int(since.timestamp())
        except (OverflowError, OSError, ValueError):
            return False

    def _directory_listing(self, request: Request, vhost: VirtualHost, directory: Path) -> Response:
        records: list[dict[str, Any]] = []
        display_path = "/" + request.path.lstrip("/")
        base_url = display_path if display_path.endswith("/") else display_path + "/"
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                        is_symlink = entry.is_symlink()
                        stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    entry_url = base_url + quote(entry.name, safe="")
                    if vhost.denies(entry_url):
                        continue
                    records.append(
                        {
                            "name": entry.name,
                            "is_dir": is_dir,
                            "is_symlink": is_symlink,
                            "size": stat.st_size,
                            "mtime": stat.st_mtime,
                        }
                    )
        except PermissionError:
            return error_response(403, "没有读取该目录的权限")
        except OSError:
            return error_response(500, "读取目录失败")

        records.sort(key=lambda item: (0 if item["is_dir"] else 1, item["name"].casefold()))

        rows: list[str] = []
        if display_path != "/":
            parent = display_path.rstrip("/").rsplit("/", 1)[0] + "/"
            if not parent:
                parent = "/"
            rows.append(
                f'<tr><td><a class="name dir" href="{escape(parent, quote=True)}">../</a></td>'
                '<td class="meta">上级目录</td><td class="meta">—</td>'
                '<td class="meta">—</td></tr>'
            )

        for record in records:
            name = record["name"]
            is_dir = record["is_dir"]
            url = base_url + quote(name, safe="") + ("/" if is_dir else "")
            label = escape(name + ("/" if is_dir else ""))
            kind = "目录" if is_dir else "文件"
            if record["is_symlink"]:
                kind += " · 链接"
            size_text = "—" if is_dir else human_size(record["size"])
            modified = datetime.fromtimestamp(record["mtime"]).strftime("%Y-%m-%d %H:%M")
            rows.append(
                f'<tr><td><a class="name{" dir" if is_dir else ""}" '
                f'href="{escape(url, quote=True)}">{label}</a></td>'
                f'<td class="meta">{kind}</td><td class="meta">{size_text}</td>'
                f'<td class="meta">{modified}</td></tr>'
            )

        if not rows:
            rows.append('<tr><td colspan="4" class="empty">目录为空</td></tr>')

        title = escape(display_path)
        head = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Crect width='16' height='16' fill='%23a33a26'/%3E%3C/svg%3E">
  <title>目录列表 · SimpleHTTP</title>
  <style>
    :root { color-scheme: light; }
    * { box-sizing: border-box; }
    body { margin: 0; background: #f3efe4; color: #1b211e;
           font-family: "Segoe UI", "Microsoft YaHei", system-ui, sans-serif; }
    main { width: min(980px, calc(100% - 48px)); margin: 8vh auto 0;
           padding: 24px 0 42px; border-top: 4px solid #a33a26; }
    h1 { margin: 0 0 4px; font-family: Georgia, "SimSun", serif;
         font-size: 42px; font-weight: 500; letter-spacing: -.04em; }
    .path { margin: 0 0 24px; color: #6b716b; font-family: Consolas, monospace;
            font-size: 12px; }
    table { width: 100%; border-collapse: collapse; border-top: 2px solid #1b211e;
            font-size: 13px; }
    th, td { padding: 11px 12px 11px 0; text-align: left;
             border-bottom: 1px solid #c8c0b0; }
    th { color: #687069; font-family: Consolas, monospace; font-size: 9px;
         letter-spacing: .1em; text-transform: uppercase; }
    tr:hover td { background: #faf7ef; }
    a { color: #1f5b56; text-decoration: none; text-underline-offset: 3px; }
    a:hover { color: #a33a26; text-decoration: underline; }
    .dir { color: #8a3b2a; font-weight: 600; }
    .meta { color: #687069; font-size: 12px; white-space: nowrap; }
    .empty { color: #6b716b; text-align: center; padding: 36px; }
    .back { margin-top: 24px; }
    .back a { display: inline-block; padding: 8px 14px; border: 1px solid #1b211e;
              color: #1b211e; font-family: Consolas, monospace; font-size: 11px;
              text-decoration: none; }
    .back a:hover { color: #f4f0e5; background: #1b211e; }
  </style>
</head>
<body>
  <main>
    <h1>目录列表</h1>
    <p class="path">"""
        tail = f"""</p>
    <table>
      <thead><tr><th>名称</th><th>类型</th><th>大小</th><th>修改时间</th></tr></thead>
      <tbody>
        {''.join(rows)}
      </tbody>
    </table>
    <p class="back"><a href="/">返回首页</a></p>
  </main>
</body>
</html>"""
        response = Response.html(head + title + tail)
        response.add_header("Cache-Control", "no-store")
        response.add_header("X-Content-Type-Options", "nosniff")
        return response
