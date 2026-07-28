"""端口的内置默认实现:静态配置 + 内存指标(供独立使用与测试)。"""

from __future__ import annotations

import collections
import os
import threading
import time

from marketdata.ports import SourceConfig

# 熔断默认值(R2,docs/29 §R2 初始阈值"先保守")。
# 不写死在业务逻辑中:InMemoryMetricsSink 构造参数可覆盖,
# 未显式传参时读环境变量,最后回落到这两个常量。
DEFAULT_CIRCUIT_FAILURE_THRESHOLD = 3
DEFAULT_CIRCUIT_COOLDOWN_SEC = 300.0

ENV_CIRCUIT_FAILURE_THRESHOLD = "MARKETDATA_CIRCUIT_FAILURE_THRESHOLD"
ENV_CIRCUIT_COOLDOWN_SEC = "MARKETDATA_CIRCUIT_COOLDOWN_SEC"

# 错误分类(R2,docs/29 §R2-3):配置异常单列,不与网络异常混淆;
# 空数据(非交易日等正常业务态)不参与熔断。
ERROR_CLASS_EMPTY = "empty"
ERROR_CLASS_CONFIG = "config"
ERROR_CLASS_TRANSPORT = "transport"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


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
        self.last_error_class = ""
        self.last_success_at = 0.0
        self.last_attempt_at = 0.0
        self.consecutive_failures = 0
        self.consecutive_config_failures = 0
        self.circuit_open_until = 0.0

    def record(self, ok: bool, latency_ms: int, error: str = "",
               error_class: str = "", *, failure_threshold: int,
               cooldown_sec: float) -> None:
        self.window.append((ok, latency_ms))
        self.last_attempt_at = time.time()
        if ok:
            self.last_success_at = time.time()
            self.consecutive_failures = 0
            self.consecutive_config_failures = 0
            self.circuit_open_until = 0.0
        elif error:
            self.last_error = error
            self.last_error_class = error_class
            if error_class == ERROR_CLASS_CONFIG:
                # 配置异常(token 缺失/鉴权失败/依赖未安装):单列计数,
                # 不计入熔断——配置问题不会因冷却自愈,熔断了反而掩盖原因。
                self.consecutive_config_failures += 1
            elif error_class != ERROR_CLASS_EMPTY:
                # 空结果在非交易日/无新闻时是正常业务状态，不能触发熔断。
                self.consecutive_failures += 1
                if self.consecutive_failures >= failure_threshold:
                    self.circuit_open_until = time.time() + cooldown_sec

    def snapshot(self) -> dict:
        total = len(self.window)
        base = {
            "last_error": self.last_error,
            "last_error_class": self.last_error_class,
            "last_success_at": self.last_success_at,
            "last_attempt_at": self.last_attempt_at,
            "consecutive_failures": self.consecutive_failures,
            "consecutive_config_failures": self.consecutive_config_failures,
            "circuit_open": self.circuit_open_until > time.time(),
            "circuit_open_until": self.circuit_open_until,
        }
        if total == 0:
            return {"count": 0, "success_rate": None, "p50_latency_ms": None, **base}
        success = sum(1 for ok, _ in self.window if ok)
        lat = sorted(v for _, v in self.window)
        return {
            "count": total,
            "success_rate": round(success / total, 3),
            "p50_latency_ms": lat[len(lat) // 2],
            **base,
        }


class InMemoryMetricsSink:
    """内存指标沉淀(不落库)。health via snapshot()。

    熔断参数(可配置,docs/29 §R2"阈值和时长不写死在业务逻辑中"):
    显式构造参数 > 环境变量(MARKETDATA_CIRCUIT_FAILURE_THRESHOLD /
    MARKETDATA_CIRCUIT_COOLDOWN_SEC) > 内置默认(3 次 / 300 秒)。
    """

    def __init__(self, *, circuit_failure_threshold: int | None = None,
                 circuit_cooldown_sec: float | None = None):
        self._failure_threshold = (
            circuit_failure_threshold
            if circuit_failure_threshold is not None
            else _env_int(ENV_CIRCUIT_FAILURE_THRESHOLD, DEFAULT_CIRCUIT_FAILURE_THRESHOLD)
        )
        self._cooldown_sec = (
            circuit_cooldown_sec
            if circuit_cooldown_sec is not None
            else _env_float(ENV_CIRCUIT_COOLDOWN_SEC, DEFAULT_CIRCUIT_COOLDOWN_SEC)
        )
        self._by_key: dict[tuple[str, str, str], _Metrics] = {}
        self._lock = threading.Lock()

    def record(self, *, vendor: str, datatype: str, market: str | None,
               ok: bool, count: int, latency_ms: int, error: str = "",
               error_class: str = "") -> None:
        with self._lock:
            self._by_key.setdefault((datatype, vendor, market or ""), _Metrics()).record(
                ok, latency_ms, error, error_class,
                failure_threshold=self._failure_threshold,
                cooldown_sec=self._cooldown_sec,
            )

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
