"""端口的内置默认实现:静态配置 + 内存指标(供独立使用与测试)。"""

from __future__ import annotations

import collections
import threading
import time

from marketdata.ports import SourceConfig


class StaticConfigProvider:
    """从一个 {datatype: [SourceConfig]} 字典提供配置。"""

    def __init__(self, mapping: dict[str, list[SourceConfig]]):
        self._mapping = mapping

    def sources_for(self, datatype: str, market: str | None) -> list[SourceConfig]:
        srcs = [s for s in self._mapping.get(datatype, []) if s.enabled]
        return sorted(srcs, key=lambda s: s.priority)


class _Metrics:
    """单 vendor 的滚动统计(最近 100 次),对齐 orchestrator._Metrics。"""

    def __init__(self):
        self.window: collections.deque = collections.deque(maxlen=100)
        self.last_error = ""
        self.last_success_at = 0.0
        self.last_attempt_at = 0.0
        self.consecutive_failures = 0
        self.circuit_open_until = 0.0

    def record(self, ok: bool, latency_ms: int, error: str = "") -> None:
        self.window.append((ok, latency_ms))
        self.last_attempt_at = time.time()
        if ok:
            self.last_success_at = time.time()
            self.consecutive_failures = 0
            self.circuit_open_until = 0.0
        elif error:
            self.last_error = error
            # 空结果在非交易日/无新闻时是正常业务状态，不能触发熔断。
            if error != "empty":
                self.consecutive_failures += 1
                if self.consecutive_failures >= 3:
                    self.circuit_open_until = time.time() + 300

    def snapshot(self) -> dict:
        total = len(self.window)
        if total == 0:
            return {"count": 0, "success_rate": None, "p50_latency_ms": None,
                    "last_error": self.last_error, "last_success_at": self.last_success_at,
                    "last_attempt_at": self.last_attempt_at,
                    "consecutive_failures": self.consecutive_failures,
                    "circuit_open": self.circuit_open_until > time.time(),
                    "circuit_open_until": self.circuit_open_until}
        success = sum(1 for ok, _ in self.window if ok)
        lat = sorted(v for _, v in self.window)
        return {
            "count": total,
            "success_rate": round(success / total, 3),
            "p50_latency_ms": lat[len(lat) // 2],
            "last_error": self.last_error,
            "last_success_at": self.last_success_at,
            "last_attempt_at": self.last_attempt_at,
            "consecutive_failures": self.consecutive_failures,
            "circuit_open": self.circuit_open_until > time.time(),
            "circuit_open_until": self.circuit_open_until,
        }


class InMemoryMetricsSink:
    """内存指标沉淀(不落库)。health via snapshot()。"""

    def __init__(self):
        self._by_key: dict[tuple[str, str, str], _Metrics] = {}
        self._lock = threading.Lock()

    def record(self, *, vendor: str, datatype: str, market: str | None,
               ok: bool, count: int, latency_ms: int, error: str = "") -> None:
        with self._lock:
            self._by_key.setdefault((datatype, vendor, market or ""), _Metrics()).record(ok, latency_ms, error)

    def should_attempt(self, *, vendor: str, datatype: str, market: str | None) -> bool:
        """熔断期间跳过故障源；冷却到期后的下一次调用即为恢复探测。"""
        with self._lock:
            row = self._by_key.get((datatype, vendor, market or ""))
            return row is None or row.circuit_open_until <= time.time()

    def health_for(self, *, vendor: str, datatype: str, market: str | None = None) -> dict | None:
        with self._lock:
            rows = [m for (dt, v, mk), m in self._by_key.items()
                    if dt == datatype and v == vendor and (market is None or mk == market)]
            if not rows:
                return None
            # 数据源页未指定市场时取最近一次尝试，避免混合不同市场的成功率。
            return max(rows, key=lambda m: m.last_attempt_at).snapshot()

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            # 保持旧 API（provider → snapshot）兼容；精确维度走 health_for。
            latest: dict[str, _Metrics] = {}
            for (_, vendor, _), metric in self._by_key.items():
                if vendor not in latest or metric.last_attempt_at > latest[vendor].last_attempt_at:
                    latest[vendor] = metric
            return {name: m.snapshot() for name, m in latest.items()}
