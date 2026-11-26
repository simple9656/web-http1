"""多线程 TCP 服务器：每连接一个线程，线程内循环处理 Keep-Alive 请求。"""
from __future__ import annotations

import logging
import socket
import threading
import time
from types import TracebackType

from .access_log import AccessLogger
from .app import Application
from .config import ServerConfig
from .http_parser import ConnectionClosed, Request, RequestError, SocketReader, parse_request
from .response import NO_BODY_STATUSES, Response, error_response


LOG = logging.getLogger("httpserver.server")


class HTTPServer:
    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self.application = Application(config)
        self.access_logger = AccessLogger(config)
        self._listener: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._connection_slots = threading.BoundedSemaphore(config.max_connections)
        self._lock = threading.Lock()
        self._active_connections: set[socket.socket] = set()
        self._worker_threads: set[threading.Thread] = set()
        self.server_address: tuple[str, int] | None = None

    @property
    def stopped(self) -> bool:
        return self._stop_event.is_set()

    def start(self) -> tuple[str, int]:
        if self._listener is not None:
            assert self.server_address is not None
            return self.server_address

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((self.config.host, self.config.port))
            listener.listen(self.config.backlog)
            listener.settimeout(0.5)
        except OSError:
            listener.close()
            raise
        self._stop_event.clear()
        self._listener = listener
        address = listener.getsockname()
        self.server_address = (str(address[0]), int(address[1]))
        self._accept_thread = threading.Thread(
            target=self._accept_loop,
            name="http-accept",
            daemon=True,
        )
        self._accept_thread.start()
        return self.server_address

    def wait(self) -> None:
        while not self._stop_event.wait(0.5):
            pass

    def request_shutdown(self) -> None:
        self._stop_event.set()
        listener = self._listener
        self._listener = None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass

    def shutdown(self) -> None:
        self.request_shutdown()
        accept_thread = self._accept_thread
        if accept_thread is not None and accept_thread.is_alive():
            accept_thread.join(timeout=2.0)

        with self._lock:
            connections = list(self._active_connections)
            workers = list(self._worker_threads)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass
        for worker in workers:
            if worker.is_alive():
                worker.join(timeout=2.0)
        self.access_logger.close()

    def _accept_loop(self) -> None:
        assert self._listener is not None
        listener = self._listener
        while not self._stop_event.is_set():
            try:
                connection, address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop_event.is_set():
                    break
                LOG.exception("accept failed")
                continue
            if self._stop_event.is_set():
                connection.close()
                break

            while not self._stop_event.is_set():
                if self._connection_slots.acquire(timeout=0.25):
                    break
            else:
                connection.close()
                break
            if self._stop_event.is_set():
                connection.close()
                try:
                    self._connection_slots.release()
                except ValueError:
                    pass
                break

            try:
                connection.settimeout(self.config.keep_alive_timeout)
                connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                connection.close()
                try:
                    self._connection_slots.release()
                except ValueError:
                    pass
                continue

            worker = threading.Thread(
                target=self._handle_connection,
                args=(connection, address),
                name=f"http-{address[0]}:{address[1]}",
                daemon=True,
            )
            with self._lock:
                self._worker_threads.add(worker)
            worker.start()

    def _handle_connection(self, connection: socket.socket, address: tuple[str, int]) -> None:
        remote = str(address[0])
        with self._lock:
            self._active_connections.add(connection)
        reader = SocketReader(connection)
        request_count = 0
        try:
            while not self._stop_event.is_set():
                try:
                    request = parse_request(
                        reader,
                        max_header_bytes=self.config.max_header_bytes,
                        max_headers=self.config.max_headers,
                        max_body_bytes=self.config.max_body_bytes,
                        continue_cb=lambda: self._send_continue(connection),
                    )
                except ConnectionClosed:
                    break
                except RequestError as exc:
                    if not self._stop_event.is_set():
                        response = error_response(exc.status, exc.message)
                        self._send_error(
                            connection,
                            remote=remote,
                            response=response,
                            status=exc.status,
                        )
                    break
                except Exception:
                    LOG.exception("unexpected parse failure from %s", remote)
                    if not self._stop_event.is_set():
                        response = error_response(500, "服务器无法解析请求")
                        self._send_error(
                            connection,
                            remote=remote,
                            response=response,
                            status=500,
                        )
                    break

                request_count += 1
                started = time.perf_counter()
                vhost = self.application.resolve_host(request)
                keep_alive = (
                    self._should_keep_alive(request)
                    and request_count < self.config.max_requests_per_connection
                    and not self._stop_event.is_set()
                )
                try:
                    response = self.application.handle(request, vhost)
                except Exception:
                    LOG.exception(
                        "handler failure: %s %s (host=%s)",
                        request.method,
                        request.target,
                        request.authority or "-",
                    )
                    response = error_response(500, "处理请求时发生内部错误")
                    keep_alive = False

                duration = time.perf_counter() - started
                try:
                    body_size = self._send_response(
                        connection,
                        request=request,
                        response=response,
                        keep_alive=keep_alive,
                    )
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.timeout):
                    self._log_access(
                        remote=remote,
                        request=request,
                        status=response.status,
                        size=0,
                        duration=duration,
                        host=vhost.name if vhost is not None else request.authority,
                    )
                    break
                except OSError:
                    self._log_access(
                        remote=remote,
                        request=request,
                        status=response.status,
                        size=0,
                        duration=duration,
                        host=vhost.name if vhost is not None else request.authority,
                    )
                    break

                self._log_access(
                    remote=remote,
                    request=request,
                    status=response.status,
                    size=body_size,
                    duration=duration,
                    host=vhost.name if vhost is not None else request.authority,
                )
                if not keep_alive:
                    break
        except Exception:
            LOG.exception("connection handler crashed for %s", remote)
        finally:
            with self._lock:
                self._active_connections.discard(connection)
                self._worker_threads.discard(threading.current_thread())
            try:
                connection.close()
            except OSError:
                pass
            try:
                self._connection_slots.release()
            except ValueError:
                pass

    def _send_continue(self, connection: socket.socket) -> None:
        connection.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")

    def _should_keep_alive(self, request: Request) -> bool:
        tokens = request.connection_tokens
        if "close" in tokens:
            return False
        if request.version == "HTTP/1.1":
            return True
        return "keep-alive" in tokens

    def _send_response(
        self,
        connection: socket.socket,
        *,
        request: Request,
        response: Response,
        keep_alive: bool,
    ) -> int:
        wire = response.serialize(
            protocol=request.version,
            keep_alive=keep_alive,
            head_only=request.method == "HEAD",
            server_name=self.config.server_name,
            keep_alive_timeout=self.config.keep_alive_timeout,
        )
        connection.sendall(wire)
        if request.method == "HEAD" or response.status in NO_BODY_STATUSES:
            return 0
        return len(response.body)

    def _send_error(
        self,
        connection: socket.socket,
        *,
        remote: str,
        response: Response,
        status: int,
    ) -> None:
        try:
            wire = response.serialize(
                protocol="HTTP/1.1",
                keep_alive=False,
                head_only=False,
                server_name=self.config.server_name,
                keep_alive_timeout=self.config.keep_alive_timeout,
            )
            connection.sendall(wire)
            size = len(response.body)
        except OSError:
            size = 0
        self.access_logger.log(
            remote=remote,
            request_line="-",
            status=status,
            size=size,
            duration=0.0,
            host="-",
            user_agent="-",
        )

    def _log_access(
        self,
        *,
        remote: str,
        request: Request,
        status: int,
        size: int,
        duration: float,
        host: str,
    ) -> None:
        self.access_logger.log(
            remote=remote,
            request_line=request.request_line,
            status=status,
            size=size,
            duration=duration,
            host=host,
            user_agent=request.header("user-agent") or "-",
        )

    def __enter__(self) -> "HTTPServer":
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.shutdown()
