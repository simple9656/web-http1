"""手写的 HTTP/1.1 请求解析器。

只依赖标准库 socket，不借助 http.server 的解析逻辑。解析器负责：
请求行、首部、Content-Length / chunked 请求体，以及 Host / 绝对 URI。
"""
from __future__ import annotations

import re
import socket
from dataclasses import dataclass
from typing import Callable
from urllib.parse import unquote_to_bytes, urlsplit


TOKEN_RE = re.compile(r"^[!#$%&'*+\-.^_\x60|~0-9A-Za-z]+$")
VERSION_RE = re.compile(r"^HTTP/(\d+)\.(\d+)$")
MAX_REQUEST_LINE = 8192
MAX_CHUNK_LINE = 8192


class ConnectionClosed(Exception):
    """客户端正常关闭或空闲超时，不产生响应。"""


class RequestError(Exception):
    """请求无法继续处理，需要返回错误响应。"""

    def __init__(self, status: int, message: str, *, close: bool = True) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.close = close


class SocketReader:
    """带缓冲区的 socket 读取器，保留跨请求的流水线数据。"""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buffer = bytearray()

    def _recv_more(self) -> None:
        try:
            data = self.sock.recv(65536)
        except socket.timeout as exc:
            if self.buffer:
                raise RequestError(408, "请求读取超时") from exc
            raise ConnectionClosed() from exc
        except (ConnectionResetError, ConnectionAbortedError) as exc:
            if self.buffer:
                raise RequestError(400, "连接被对端重置") from exc
            raise ConnectionClosed() from exc
        except OSError as exc:
            if self.buffer:
                raise RequestError(400, "连接读取失败") from exc
            raise ConnectionClosed() from exc
        if not data:
            if self.buffer:
                raise RequestError(400, "请求不完整，连接已关闭")
            raise ConnectionClosed()
        self.buffer.extend(data)

    def read_header_block(self, limit: int) -> bytes:
        while True:
            candidates: list[tuple[int, int]] = []
            crlf = self.buffer.find(b"\r\n\r\n")
            if crlf >= 0:
                candidates.append((crlf, 4))
            lf = self.buffer.find(b"\n\n")
            if lf >= 0:
                candidates.append((lf, 2))
            if candidates:
                index, separator_len = min(candidates, key=lambda item: item[0])
                if index > limit:
                    raise RequestError(431, "请求头过大")
                block = bytes(self.buffer[:index])
                del self.buffer[: index + separator_len]
                if b"\r" in block.replace(b"\r\n", b""):
                    raise RequestError(400, "请求头包含非法换行")
                return block
            if len(self.buffer) > limit:
                raise RequestError(431, "请求头过大")
            self._recv_more()

    def read_line(self, limit: int = MAX_CHUNK_LINE) -> bytes:
        while True:
            index = self.buffer.find(b"\n")
            if index >= 0:
                line = bytes(self.buffer[:index])
                del self.buffer[: index + 1]
                if line.endswith(b"\r"):
                    line = line[:-1]
                if len(line) > limit:
                    raise RequestError(400, "行长度超限")
                if b"\r" in line:
                    raise RequestError(400, "行中包含非法回车")
                return line
            if len(self.buffer) > limit:
                raise RequestError(400, "行长度超限")
            self._recv_more()

    def read_exact(self, size: int) -> bytes:
        while len(self.buffer) < size:
            try:
                self._recv_more()
            except ConnectionClosed as exc:
                raise RequestError(400, "请求体不完整") from exc
        data = bytes(self.buffer[:size])
        del self.buffer[:size]
        return data


@dataclass
class Request:
    method: str
    target: str
    version: str
    raw_path: str
    path: str
    query: str
    headers: dict[str, str]
    body: bytes
    authority: str = ""

    def header(self, name: str, default: str | None = None) -> str | None:
        return self.headers.get(name.lower(), default)

    @property
    def connection_tokens(self) -> set[str]:
        value = self.headers.get("connection", "")
        return {part.strip().lower() for part in value.split(",") if part.strip()}

    @property
    def request_line(self) -> str:
        return f"{self.method} {self.target} {self.version}"

    @property
    def http_version(self) -> tuple[int, int]:
        match = VERSION_RE.match(self.version)
        if not match:
            return (1, 0)
        return int(match.group(1)), int(match.group(2))


def _split_headers(block: bytes) -> list[bytes]:
    normalized = block.replace(b"\r\n", b"\n")
    return normalized.split(b"\n")


def _decode_request_line(raw_line: bytes) -> tuple[str, str, str]:
    if not raw_line or len(raw_line) > MAX_REQUEST_LINE:
        raise RequestError(400, "请求行缺失或过长")
    if any(byte < 0x20 or byte == 0x7F for byte in raw_line):
        raise RequestError(400, "请求行包含控制字符")
    try:
        line = raw_line.decode("iso-8859-1")
    except UnicodeDecodeError as exc:
        raise RequestError(400, "请求行编码非法") from exc
    parts = line.split()
    if len(parts) != 3:
        raise RequestError(400, "请求行必须由方法、目标、版本三部分组成")
    method, target, version = parts
    if not TOKEN_RE.match(method):
        raise RequestError(400, "非法 HTTP 方法")
    version_match = VERSION_RE.match(version)
    if not version_match:
        raise RequestError(400, "HTTP 版本格式非法")
    major, minor = int(version_match.group(1)), int(version_match.group(2))
    if (major, minor) not in ((1, 0), (1, 1)):
        raise RequestError(505, "仅支持 HTTP/1.0 与 HTTP/1.1")
    if len(target) > MAX_REQUEST_LINE:
        raise RequestError(414, "请求目标过长")
    return method.upper(), target, version


def _parse_header_lines(lines: list[bytes], max_headers: int) -> dict[str, str]:
    headers: dict[str, str] = {}
    count = 0
    for raw_line in lines:
        if not raw_line:
            continue
        if raw_line[:1] in (b" ", b"\t"):
            raise RequestError(400, "不支持折行首部")
        if b":" not in raw_line:
            raise RequestError(400, "首部缺少冒号")
        raw_name, raw_value = raw_line.split(b":", 1)
        try:
            name = raw_name.decode("ascii").lower()
            value = raw_value.decode("iso-8859-1").strip(" \t")
        except UnicodeDecodeError as exc:
            raise RequestError(400, "首部编码非法") from exc
        if not name or not TOKEN_RE.match(name):
            raise RequestError(400, "首部名称非法")
        if any((ord(ch) < 0x20 and ch != "\t") or ord(ch) == 0x7F for ch in value):
            raise RequestError(400, "首部值包含控制字符")
        count += 1
        if count > max_headers:
            raise RequestError(431, "首部数量过多")
        if name == "host" and name in headers:
            raise RequestError(400, "Host 首部重复")
        if name in headers:
            headers[name] = headers[name] + ", " + value
        else:
            headers[name] = value
    return headers


def _parse_target(target: str, headers: dict[str, str], version: str) -> tuple[str, str, str, str]:
    authority = ""
    if target.startswith("http://"):
        try:
            parsed = urlsplit(target)
        except ValueError as exc:
            raise RequestError(400, "绝对 URI 非法") from exc
        if parsed.scheme != "http" or not parsed.netloc or parsed.fragment:
            raise RequestError(400, "仅支持无片段 http 绝对 URI")
        if "@" in parsed.netloc:
            raise RequestError(400, "请求目标不能包含用户信息")
        authority = parsed.netloc
        raw_path = parsed.path or "/"
        query = parsed.query
    elif target.startswith("https://"):
        raise RequestError(400, "普通 HTTP 端口不支持 https 绝对 URI")
    else:
        if target == "*":
            raise RequestError(400, "不支持 asterisk-form 请求目标")
        if not target.startswith("/"):
            raise RequestError(400, "请求目标必须以 / 开头")
        if "#" in target:
            raise RequestError(400, "请求目标不能包含片段")
        raw_path, separator, query = target.partition("?")
        if not separator:
            query = ""

    if len(raw_path) > MAX_REQUEST_LINE:
        raise RequestError(414, "请求路径过长")

    try:
        decoded = unquote_to_bytes(raw_path)
    except Exception as exc:
        raise RequestError(400, "URL 解码失败") from exc
    if b"\x00" in decoded:
        raise RequestError(400, "路径不能包含 NUL")
    try:
        path = decoded.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RequestError(400, "路径必须是 UTF-8 编码") from exc
    if not path.startswith("/"):
        raise RequestError(400, "解码后的路径非法")

    host_header = headers.get("host", "").strip()
    if host_header:
        if len(host_header) > 255 or re.search(r"[\s/\\]", host_header):
            raise RequestError(400, "Host 首部非法")
    if not authority:
        authority = host_header
    if version == "HTTP/1.1" and not authority:
        raise RequestError(400, "HTTP/1.1 请求必须包含 Host")
    return raw_path, path, query, authority


def _read_chunked_body(
    reader: SocketReader,
    max_body: int,
    max_headers: int,
    continue_cb: Callable[[], None] | None,
) -> bytes:
    if continue_cb is not None:
        continue_cb()
    chunks: list[bytes] = []
    total = 0
    while True:
        line = reader.read_line()
        size_part = line.split(b";", 1)[0].strip()
        if not size_part:
            raise RequestError(400, "chunk 大小为空")
        try:
            size = int(size_part, 16)
        except ValueError as exc:
            raise RequestError(400, "chunk 大小非法") from exc
        if size < 0:
            raise RequestError(400, "chunk 大小非法")
        if size == 0:
            trailer_count = 0
            while True:
                trailer = reader.read_line()
                if trailer == b"":
                    return b"".join(chunks)
                trailer_count += 1
                if trailer_count > max_headers:
                    raise RequestError(431, "Trailer 过多")
                if b":" not in trailer:
                    raise RequestError(400, "Trailer 格式非法")
        total += size
        if total > max_body:
            raise RequestError(413, "请求体超过限制")
        chunks.append(reader.read_exact(size))
        ending = reader.read_exact(2)
        if ending != b"\r\n":
            raise RequestError(400, "chunk 后缺少 CRLF")


def parse_request(
    reader: SocketReader,
    *,
    max_header_bytes: int,
    max_headers: int,
    max_body_bytes: int,
    continue_cb: Callable[[], None] | None = None,
) -> Request:
    block = reader.read_header_block(max_header_bytes)
    lines = _split_headers(block)
    if not lines:
        raise RequestError(400, "空请求")
    method, target, version = _decode_request_line(lines[0])
    headers = _parse_header_lines(lines[1:], max_headers)
    raw_path, path, query, authority = _parse_target(target, headers, version)

    initial_expect = headers.get("expect", "").strip().lower()
    if initial_expect and version != "HTTP/1.1":
        raise RequestError(417, "Expect 仅支持 HTTP/1.1")

    transfer_encoding = headers.get("transfer-encoding")
    content_length = headers.get("content-length")
    body = b""

    if transfer_encoding is not None:
        if content_length is not None:
            raise RequestError(400, "Transfer-Encoding 与 Content-Length 不能同时出现")
        codings = [item.strip().lower() for item in transfer_encoding.split(",") if item.strip()]
        if codings != ["chunked"]:
            raise RequestError(501, "不支持的 Transfer-Encoding")
        expect = headers.get("expect", "").strip().lower()
        if expect and expect != "100-continue":
            raise RequestError(417, "不支持 Expect")
        body = _read_chunked_body(
            reader,
            max_body_bytes,
            max_headers,
            continue_cb if expect == "100-continue" else None,
        )
    elif content_length is not None:
        values = [item.strip() for item in content_length.split(",")]
        if not values or any(not item.isdigit() for item in values):
            raise RequestError(400, "Content-Length 非法")
        if any(len(item) > 20 for item in values):
            raise RequestError(413, "Content-Length 过大")
        numbers = {int(item) for item in values}
        if len(numbers) != 1:
            raise RequestError(400, "Content-Length 值冲突")
        length = numbers.pop()
        if length > max_body_bytes:
            raise RequestError(413, "请求体超过限制")
        expect = headers.get("expect", "").strip().lower()
        if expect:
            if expect != "100-continue":
                raise RequestError(417, "不支持 Expect")
            if length > 0 and version == "HTTP/1.1" and continue_cb is not None:
                continue_cb()
        if length:
            body = reader.read_exact(length)
    else:
        expect = headers.get("expect", "").strip().lower()
        if expect and expect != "100-continue":
            raise RequestError(417, "不支持 Expect")

    return Request(
        method=method,
        target=target,
        version=version,
        raw_path=raw_path,
        path=path,
        query=query,
        headers=headers,
        body=body,
        authority=authority,
    )
