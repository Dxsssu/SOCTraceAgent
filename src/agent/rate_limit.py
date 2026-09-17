"""Process-wide admission control for concurrent OpenAI-compatible calls.

Input tokens are conservatively estimated from UTF-8 bytes before admission;
actual usage replaces the reservation afterward. This does not coordinate
separate processes or other clients sharing the provider quota.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from threading import Condition, Lock


@dataclass
class Reservation:
    tokens: int
    finished_at: float | None = None


class LLMRateLimiter:
    def __init__(self, rpm: int, tpm: int, *, window_seconds: float = 60.0):
        if rpm <= 0 or tpm <= 0 or window_seconds <= 0:
            raise ValueError('Rate limits and window must be positive')
        self.rpm, self.tpm, self.window = rpm, tpm, window_seconds
        self._condition = Condition()
        self._reservations: list[Reservation] = []

    def acquire(self, estimated_tokens: int) -> Reservation:
        if estimated_tokens <= 0 or estimated_tokens > self.tpm:
            raise ValueError('Request token reservation exceeds the configured TPM budget')
        with self._condition:
            while True:
                now = time.monotonic()
                self._reservations = [r for r in self._reservations if r.finished_at is None or now - r.finished_at < self.window]
                if len(self._reservations) < self.rpm and sum(r.tokens for r in self._reservations) + estimated_tokens <= self.tpm:
                    reservation = Reservation(estimated_tokens)
                    self._reservations.append(reservation)
                    return reservation
                self._condition.wait(timeout=min(1.0, self.window))

    def finish(self, reservation: Reservation, actual_tokens: int | None = None) -> None:
        with self._condition:
            if actual_tokens is not None:
                reservation.tokens = max(0, actual_tokens)
            reservation.finished_at = time.monotonic()
            self._condition.notify_all()


_registry: dict[tuple[int, int], LLMRateLimiter] = {}
_registry_lock = Lock()


def get_rate_limiter() -> LLMRateLimiter:
    utilization = float(os.getenv('PARATERA_RATE_UTILIZATION', '0.8'))
    if not 0 < utilization <= 1:
        raise ValueError('PARATERA_RATE_UTILIZATION must be in (0, 1]')
    limits = (int(int(os.getenv('PARATERA_RPM_LIMIT', '2500')) * utilization),
              int(int(os.getenv('PARATERA_TPM_LIMIT', '5000000')) * utilization))
    with _registry_lock:
        if limits not in _registry:
            _registry[limits] = LLMRateLimiter(*limits)
        return _registry[limits]
