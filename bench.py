#!/usr/bin/env python3
"""SimpleHTTP 并发压测脚本（仅标准库）。

默认同时测试 Keep-Alive 和 Connection: close：
    py -3.11 bench.py

指定一个模式：
    py -3.11 bench.py --mode keepalive
    py -3.11 bench.py --mode close
"""
from __future__ import annotations

import argparse
import http.client
import math
import sys
import threading
import time
from collections import Counter
from urllib.parse import urlsplit


def percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    position = (len(values) - 1) * ratio
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return values[low]
    return values[low] + (values[high] - values[low]) * (position - low)


def parse_target(url: str, host_override: str | None) -> tuple[str, int, str, str]:
    parsed = urlsplit(url)
    if parsed.scheme != "http":
        raise ValueError("只支持 http:// URL")
    if not parsed.hostname:
        raise ValueError("URL 缺少主机名")
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    return (
        parsed.hostname,
        parsed.port or 80,
        path,
        host_override or parsed.netloc,
    )


def warmup(host: str, port: int, path: str, host_header: str, timeout: float) -> None:
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request("GET", path, headers={"Host": host_header})
        response = connection.getresponse()
        response.read()
        if response.status >= 400:
            raise RuntimeError(f"预热请求返回 {response.status}")
    finally:
        connection.close()


def worker(
    host: str,
    port: int,
    path: str,
    host_header: str,
    keepalive: bool,
    count: int,
    timeout: float,
    barrier: threading.Barrier,
    result: dict,
) -> None:
    statuses: Counter[int] = Counter()
    errors: Counter[str] = Counter()
    latencies: list[float] = []
    total_bytes = 0
    connection: http.client.HTTPConnection | None = None
    headers = {
        "Host": host_header,
        "User-Agent": "SimpleHTTP-bench/1.0",
        "Accept": "*/*",
        "Connection": "keep-alive" if keepalive else "close",
    }

    try:
        barrier.wait()
        for _ in range(count):
            started = time.perf_counter()
            try:
                if connection is None:
                    connection = http.client.HTTPConnection(host, port, timeout=timeout)
                connection.request("GET", path, headers=headers)
                response = connection.getresponse()
                body = response.read()
                latencies.append(time.perf_counter() - started)
                statuses[response.status] += 1
                total_bytes += len(body)
                response_connection = (response.getheader("Connection") or "").lower()
                if not keepalive or response_connection == "close":
                    connection.close()
                    connection = None
            except Exception as exc:
                errors[type(exc).__name__] += 1
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass
                    connection = None
    finally:
        if connection is not None:
            connection.close()
        result["statuses"] = statuses
        result["errors"] = errors
        result["latencies"] = latencies
        result["bytes"] = total_bytes


def run_mode(
    mode: str,
    host: str,
    port: int,
    path: str,
    host_header: str,
    clients: int,
    requests: int,
    timeout: float,
    warmup_count: int,
) -> int:
    keepalive = mode == "keepalive"
    for _ in range(warmup_count):
        warmup(host, port, path, host_header, timeout)

    effective_clients = min(clients, requests)
    base = requests // effective_clients
    remainder = requests % effective_clients
    counts = [base + (1 if index < remainder else 0) for index in range(effective_clients)]
    barrier = threading.Barrier(effective_clients + 1)
    results: list[dict] = [{} for _ in range(effective_clients)]
    threads: list[threading.Thread] = []

    for index, count in enumerate(counts):
        thread = threading.Thread(
            target=worker,
            kwargs={
                "host": host,
                "port": port,
                "path": path,
                "host_header": host_header,
                "keepalive": keepalive,
                "count": count,
                "timeout": timeout,
                "barrier": barrier,
                "result": results[index],
            },
            name=f"bench-{index}",
            daemon=True,
        )
        threads.append(thread)
        thread.start()

    started = time.perf_counter()
    barrier.wait()
    for thread in threads:
        thread.join()
    elapsed = time.perf_counter() - started

    statuses: Counter[int] = Counter()
    errors: Counter[str] = Counter()
    latencies: list[float] = []
    total_bytes = 0
    for result in results:
        statuses.update(result.get("statuses", Counter()))
        errors.update(result.get("errors", Counter()))
        latencies.extend(result.get("latencies", []))
        total_bytes += int(result.get("bytes", 0))

    completed = sum(statuses.values())
    successes = statuses.get(200, 0)
    attempted = sum(counts)
    avg_ms = (sum(latencies) / len(latencies) * 1000) if latencies else 0.0
    p50 = percentile(latencies, 0.50) * 1000
    p95 = percentile(latencies, 0.95) * 1000
    p99 = percentile(latencies, 0.99) * 1000
    rps = completed / elapsed if elapsed else 0.0
    success_rate = successes / attempted * 100 if attempted else 0.0
    mib = total_bytes / elapsed / 1024 / 1024 if elapsed else 0.0

    label = "Keep-Alive" if keepalive else "Connection: close"
    print()
    print("=" * 58)
    print(f"  {label}")
    print("=" * 58)
    print(f"URL          : {host_header}{path}")
    print(f"Clients      : {effective_clients}")
    print(f"Requests     : {attempted}")
    print(f"Elapsed      : {elapsed:.3f} s")
    print(f"Throughput   : {rps:.1f} req/s")
    print(f"Success      : {success_rate:.2f}% ({successes}/{attempted})")
    print(f"Bytes        : {mib:.2f} MiB/s")
    print(f"Latency ms   : avg={avg_ms:.2f} p50={p50:.2f} p95={p95:.2f} p99={p99:.2f}")
    print("Status       : " + (", ".join(f"{key}={value}" for key, value in sorted(statuses.items())) or "-"))
    if errors:
        print("Errors       : " + ", ".join(f"{key}={value}" for key, value in errors.items()))
    print("=" * 58)
    return 0 if successes == attempted else 1


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(description="SimpleHTTP 并发压测（仅标准库）")
    parser.add_argument("--url", default="http://127.0.0.1:8080/healthz", help="压测 URL")
    parser.add_argument("--clients", type=int, default=50, help="并发客户端数")
    parser.add_argument("--requests", type=int, default=1000, help="总请求数")
    parser.add_argument("--mode", choices=("both", "keepalive", "close"), default="both", help="默认同时测试两种连接模式")
    parser.add_argument("--host", help="覆盖 Host 首部，用于虚拟主机")
    parser.add_argument("--timeout", type=float, default=10.0, help="单请求超时秒数")
    parser.add_argument("--warmup", type=int, default=5, help="每种模式的预热请求数")
    args = parser.parse_args(argv)

    if args.clients <= 0 or args.requests <= 0:
        parser.error("--clients 和 --requests 必须大于 0")
    if args.warmup < 0:
        parser.error("--warmup 不能为负数")

    try:
        host, port, path, host_header = parse_target(args.url, args.host)
        warmup(host, port, path, host_header, args.timeout)
    except Exception as exc:
        print(f"无法连接服务器: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    modes = ["keepalive", "close"] if args.mode == "both" else [args.mode]
    exit_code = 0
    for mode in modes:
        exit_code |= run_mode(
            mode,
            host,
            port,
            path,
            host_header,
            args.clients,
            args.requests,
            args.timeout,
            args.warmup,
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
