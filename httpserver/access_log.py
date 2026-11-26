"""访问日志：Common Log 格式，附带处理耗时、Host 与 User-Agent。

写文件操作放在独立的 QueueListener 线程中，避免每条请求都同步 flush 磁盘、
拖慢 Keep-Alive 连接上的后续请求。
"""
from __future__ import annotations

import logging
import queue
import sys
from datetime import datetime
from logging.handlers import QueueHandler, QueueListener
from pathlib import Path

from .config import ServerConfig


def _clean(value: str, limit: int) -> str:
    value = str(value).replace("\r", " ").replace("\n", " ").replace('"', "'")
    value = "".join(ch if ch >= " " and ch != "\x7f" else " " for ch in value)
    if len(value) > limit:
        value = value[:limit] + "..."
    return value


class AccessLogger:
    def __init__(self, config: ServerConfig) -> None:
        self.logger = logging.getLogger(f"httpserver.access.{id(self)}")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        self.logger.handlers.clear()
        formatter = logging.Formatter("%(message)s")
        self.handlers: list[logging.Handler] = []
        self.listener: QueueListener | None = None

        handler_targets: list[logging.Handler] = []
        if config.access_log is not None:
            path = Path(config.access_log)
            path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(
                path,
                encoding="utf-8",
                delay=True,
            )
            file_handler.setFormatter(formatter)
            handler_targets.append(file_handler)

        if config.access_log_console:
            console = logging.StreamHandler(sys.stdout)
            console.setFormatter(formatter)
            handler_targets.append(console)

        if handler_targets:
            self.handlers = handler_targets
            log_queue: queue.Queue = queue.Queue(maxsize=50000)
            self.logger.addHandler(QueueHandler(log_queue))
            self.listener = QueueListener(
                log_queue,
                *handler_targets,
                respect_handler_level=True,
            )
            self.listener.start()
        else:
            self.logger.addHandler(logging.NullHandler())

    def log(
        self,
        *,
        remote: str,
        request_line: str,
        status: int,
        size: int,
        duration: float,
        host: str,
        user_agent: str,
    ) -> None:
        timestamp = datetime.now().astimezone().strftime("%d/%b/%Y:%H:%M:%S %z")
        line = (
            f'{_clean(remote, 64)} - - [{timestamp}] "{_clean(request_line, 500)}" '
            f"{status} {size} {duration * 1000:.1f}ms "
            f'host="{_clean(host or "-", 255)}" ua="{_clean(user_agent or "-", 160)}"'
        )
        self.logger.info(line)

    def close(self) -> None:
        if self.listener is not None:
            self.listener.stop()
            self.listener = None
        for handler in list(self.logger.handlers):
            self.logger.removeHandler(handler)
        for handler in self.handlers:
            try:
                handler.flush()
            except Exception:
                pass
            try:
                handler.close()
            except Exception:
                pass
        self.handlers.clear()
