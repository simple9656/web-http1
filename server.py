"""命令行入口：python server.py [--config config.json] [--port 8080]。"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
from pathlib import Path

from httpserver.config import ConfigError, load_config
from httpserver.server import HTTPServer


PROJECT_DIR = Path(__file__).resolve().parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SimpleHTTP/1.0 — 手写 HTTP/1.1 静态文件与表单服务器",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=PROJECT_DIR / "config.json",
        help="配置文件路径（默认使用程序目录下的 config.json）",
    )
    parser.add_argument("--host", help="覆盖监听地址，例如 0.0.0.0")
    parser.add_argument("--port", type=int, help="覆盖监听端口，0 表示随机空闲端口")
    parser.add_argument("--quiet", action="store_true", help="不在控制台打印每条访问日志")
    parser.add_argument("--test-mode", action="store_true", help="启用 /__test__/error 测试路由")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2

    if args.host:
        config.host = args.host
    if args.port is not None:
        config.port = args.port
    if args.quiet:
        config.access_log_console = False
    if args.test_mode:
        config.test_mode = True

    logging.basicConfig(
        level=logging.ERROR,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    server = HTTPServer(config)
    try:
        address = server.start()
    except OSError as exc:
        print(f"无法监听 {config.host}:{config.port}: {exc}", file=sys.stderr)
        server.shutdown()
        return 1

    host, port = address
    display_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    print("=" * 64)
    print("SimpleHTTP/1.0  ·  raw HTTP/1.1 server")
    print(f"Listening : http://{display_host}:{port}/")
    print(f"Binding   : {host}:{port}")
    print(f"Workers   : thread-per-connection, max={config.max_connections}")
    print(f"Keep-Alive: timeout={config.keep_alive_timeout:g}s, max={config.max_requests_per_connection}")
    print("Virtual hosts:")
    for name, vhost in sorted(config.hosts.items()):
        print(f"  {name:<20} -> {vhost.root}")
    if config.access_log:
        print(f"Access log: {config.access_log}")
    print("=" * 64)
    print("Press Ctrl+C to stop.")
    sys.stdout.flush()

    def stop_handler(signum, frame) -> None:
        server.request_shutdown()

    old_int = signal.signal(signal.SIGINT, stop_handler)
    old_term = signal.signal(signal.SIGTERM, stop_handler)
    try:
        server.wait()
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)
        server.shutdown()
        print("Server stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
