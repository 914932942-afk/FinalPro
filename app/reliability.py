"""可靠性基础设施：幂等键和简单线程安全熔断器。"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, TypeVar
from .errors import AgentError, IdempotencyConflict, OperationPending, CircuitOpenError

T = TypeVar("T")


@dataclass
class CircuitBreaker:
    failure_threshold: int = 3
    recovery_timeout: float = 30.0

    def __post_init__(self) -> None:
        if self.failure_threshold < 1 or self.recovery_timeout < 0:
            raise ValueError("invalid circuit breaker thresholds")
        self._failures = 0
        self._opened_at: float | None = None
        self._lock = threading.Lock()
        self._probe = False
        self._generation = 0

    @property
    def state(self) -> str:
        with self._lock:
            if self._opened_at is None:
                return "closed"
            if time.monotonic() - self._opened_at >= self.recovery_timeout:
                return "half_open"
            return "open"

    def call(self, fn: Callable[[], T]) -> T:
        with self._lock:
            if self._opened_at is not None and time.monotonic() - self._opened_at < self.recovery_timeout:
                raise CircuitOpenError("downstream circuit is open")
            if self._probe:
                raise CircuitOpenError("recovery probe in progress")
            if self._opened_at is not None:
                self._probe = True
            generation = self._generation
        try:
            result = fn()
        except Exception as exc:
            with self._lock:
                if generation == self._generation:
                    self._probe = False
                    counted = not isinstance(exc, AgentError) or exc.circuit_failure
                    if counted:
                        self._failures += 1
                    if counted and self._failures >= self.failure_threshold:
                        self._opened_at = time.monotonic()
                        self._generation += 1
                    elif not counted and self._opened_at is not None:
                        # 半开探测得到业务拒绝，不能宣称依赖恢复，等待下一探测窗口。
                        self._opened_at = time.monotonic()
            raise
        with self._lock:
            if generation == self._generation:
                self._probe = False
                self._failures = 0
                self._opened_at = None
        return result
