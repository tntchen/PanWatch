"""SPA 静态路由路径穿越回归测试（2026-07-29 审计 P0）。

背景：server.py serve_spa 旧实现 os.path.join(static_dir, path) + isfile
直接放行，`GET /../data/panwatch.db` 可未授权下载数据库。TestClient/httpx
会规范化 ".." 点段，无法复现，必须用原始 ASGI scope 构造未归一化路径
（等价 curl --path-as-is 经过 uvicorn 的请求形态）。
"""

from __future__ import annotations

import asyncio

import pytest

import server

pytestmark = pytest.mark.skipif(
    not server.app or not __import__("os").path.isdir(
        __import__("os").path.join(__import__("os").path.dirname(server.__file__), "static")
    ),
    reason="static/ 目录不存在时 SPA 路由未注册",
)


def _raw_get(path: str) -> tuple[int, bytes]:
    """以原始 ASGI scope 请求 app（不经过 httpx 的 URL 点段规范化）。"""
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "headers": [],
        "query_string": b"",
    }
    status: list[int] = []
    body: list[bytes] = []

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(msg):
        if msg["type"] == "http.response.start":
            status.append(msg["status"])
        elif msg["type"] == "http.response.body":
            body.append(msg.get("body", b""))

    asyncio.run(server.app(scope, receive, send))
    return status[0], b"".join(body)


def test_traversal_to_database_is_blocked():
    code, body = _raw_get("/../data/panwatch.db")
    assert code == 200  # SPA 语义：回退 index.html，而非报错
    assert not body.startswith(b"SQLite format 3")
    assert b"<html" in body.lower()


def test_traversal_to_source_file_is_blocked():
    code, body = _raw_get("/../server.py")
    assert code == 200
    assert b"uvicorn.run" not in body  # server.py 源码不得外泄
    assert b"<html" in body.lower()


def test_nested_traversal_is_blocked():
    code, body = _raw_get("/foo/../../data/panwatch.db")
    assert code == 200
    assert not body.startswith(b"SQLite format 3")


def test_legit_static_file_still_served():
    code, body = _raw_get("/index.html")
    assert code == 200
    assert b"<html" in body.lower()
