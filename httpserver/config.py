"""配置加载与虚拟主机定义。

配置文件为 JSON，所有相对路径都相对于配置文件所在目录解析。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """配置文件不合法。"""


def normalize_host_header(value: str) -> str:
    """把 Host 首部规范化为小写主机名，去掉端口和末尾的点。

    支持 example.test:8080、[::1]:8080 等写法。
    """
    value = value.strip().lower()
    if not value:
        return ""

    if value.startswith("["):
        end = value.find("]")
        if end < 0:
            return value
        host = value[1:end]
        rest = value[end + 1 :]
        if rest and not rest.startswith(":"):
            return value
        value = host
    elif value.count(":") == 1:
        host, port = value.rsplit(":", 1)
        if port == "" or port.isdigit():
            value = host

    if value.endswith("."):
        value = value[:-1]
    return value


def _normalize_prefix(prefix: str) -> str:
    prefix = "/" + prefix.strip().strip("/")
    return prefix.lower()


@dataclass(frozen=True)
class VirtualHost:
    name: str
    root: Path
    denied_prefixes: tuple[str, ...] = ()

    def denies(self, path: str) -> bool:
        """判断 URL 解码后的路径是否命中禁止访问前缀。"""
        candidate = path.lower()
        for prefix in self.denied_prefixes:
            if candidate == prefix or candidate.startswith(prefix + "/"):
                return True
        return False


@dataclass
class ServerConfig:
    project_root: Path
    host: str = "127.0.0.1"
    port: int = 8080
    backlog: int = 128
    max_connections: int = 256
    keep_alive_timeout: float = 10.0
    max_requests_per_connection: int = 1000
    max_header_bytes: int = 32 * 1024
    max_body_bytes: int = 1024 * 1024
    max_headers: int = 100
    access_log: Path | None = None
    access_log_console: bool = True
    server_name: str = "SimpleHTTP/1.0"
    test_mode: bool = False
    default_host: str = ""
    hosts: dict[str, VirtualHost] = field(default_factory=dict)

    @property
    def default_vhost(self) -> VirtualHost:
        return self.hosts[self.default_host]

    def vhost_for(self, authority: str) -> VirtualHost | None:
        """按 Host 查找虚拟主机；空值使用默认主机。"""
        if not authority:
            return self.hosts.get(self.default_host)
        return self.hosts.get(normalize_host_header(authority))


def _as_int(server: dict[str, Any], key: str, default: int, minimum: int = 0) -> int:
    value = server.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"server.{key} 必须是整数")
    if value < minimum:
        raise ConfigError(f"server.{key} 不能小于 {minimum}")
    return value


def _as_float(server: dict[str, Any], key: str, default: float, minimum: float = 0.1) -> float:
    value = server.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"server.{key} 必须是数字")
    if value < minimum:
        raise ConfigError(f"server.{key} 不能小于 {minimum}")
    return float(value)


def load_config(path: str | Path = "config.json") -> ServerConfig:
    config_path = Path(path)
    if not config_path.is_absolute():
        config_path = (Path.cwd() / config_path).resolve()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"找不到配置文件: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件不是合法 JSON: {config_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError("配置文件顶层必须是对象")

    base_dir = config_path.parent
    server = raw.get("server", {})
    if not isinstance(server, dict):
        raise ConfigError("server 必须是对象")

    project_root = (base_dir / str(server.get("project_root", "."))).resolve()
    project_root.mkdir(parents=True, exist_ok=True)

    port = _as_int(server, "port", 8080, 0)
    if port > 65535:
        raise ConfigError("server.port 必须在 0..65535 之间")

    access_log_value = server.get("access_log", "logs/access.log")
    access_log: Path | None
    if access_log_value in (None, "", False):
        access_log = None
    else:
        access_log = (project_root / str(access_log_value)).resolve()

    config = ServerConfig(
        project_root=project_root,
        host=str(server.get("host", "127.0.0.1")),
        port=port,
        backlog=_as_int(server, "backlog", 128, 1),
        max_connections=_as_int(server, "max_connections", 256, 1),
        keep_alive_timeout=_as_float(server, "keep_alive_timeout", 10.0, 0.1),
        max_requests_per_connection=_as_int(server, "max_requests_per_connection", 1000, 1),
        max_header_bytes=_as_int(server, "max_header_bytes", 32 * 1024, 1024),
        max_body_bytes=_as_int(server, "max_body_bytes", 1024 * 1024, 0),
        max_headers=_as_int(server, "max_headers", 100, 1),
        access_log=access_log,
        access_log_console=bool(server.get("access_log_console", True)),
        server_name=str(server.get("server_name", "SimpleHTTP/1.0")),
        test_mode=bool(server.get("test_mode", False)),
    )
    if "\r" in config.server_name or "\n" in config.server_name:
        raise ConfigError("server.server_name 不能包含换行符")

    hosts_raw = raw.get("hosts")
    if not isinstance(hosts_raw, dict) or not hosts_raw:
        raise ConfigError("hosts 必须是非空对象")

    hosts: dict[str, VirtualHost] = {}
    for raw_name, spec in hosts_raw.items():
        name = normalize_host_header(str(raw_name))
        if not name:
            raise ConfigError("hosts 中存在空主机名")
        if name in hosts:
            raise ConfigError(f"Host 归一化后重复: {raw_name}")
        if isinstance(spec, str):
            root_value = spec
            denied_raw: list[Any] = []
        elif isinstance(spec, dict):
            root_value = spec.get("root")
            denied_raw = spec.get("denied_prefixes", [])
            if not isinstance(denied_raw, list):
                raise ConfigError(f"hosts.{raw_name}.denied_prefixes 必须是数组")
        else:
            raise ConfigError(f"hosts.{raw_name} 必须是字符串或对象")
        if not isinstance(root_value, str) or not root_value:
            raise ConfigError(f"hosts.{raw_name}.root 不能为空")
        root = (project_root / root_value).resolve()
        if not root.is_dir():
            raise ConfigError(f"虚拟主机根目录不存在: {root}")
        denied = tuple(
            _normalize_prefix(str(item)) for item in denied_raw if str(item).strip("/")
        )
        hosts[name] = VirtualHost(name=name, root=root, denied_prefixes=denied)

    default_host = normalize_host_header(str(raw.get("default_host", "localhost")))
    if default_host not in hosts:
        fallback = next(iter(hosts))
        if raw.get("default_host") is None:
            default_host = fallback
        else:
            raise ConfigError(f"default_host 不在 hosts 中: {raw.get('default_host')}")
    config.default_host = default_host
    config.hosts = hosts
    return config
