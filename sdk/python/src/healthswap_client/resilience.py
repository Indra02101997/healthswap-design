"""Resilience primitives: the circuit breaker that guards cart evaluation and the retry backoff."""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

__all__ = ["CircuitBreaker", "Permit", "backoff_delay"]


@dataclass(frozen=True, slots=True)
class Permit:
    """Permission to make one guarded call. `probe` is True for the single trial call of a half-open breaker."""

    probe: bool
    generation: int


class CircuitBreaker:
    """Consecutive-failure circuit breaker (thread-safe).

    CLOSED: calls pass; `failure_threshold` consecutive failures open the breaker. OPEN: calls are rejected for
    `cooldown_seconds`. HALF_OPEN: after the cool-down exactly one probe call passes (concurrent callers are still rejected);
    its success closes the breaker, its failure opens it for another cool-down. Outcomes of calls that started before the
    last state change are ignored, so a slow straggler cannot close or re-open the breaker.
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(self, failure_threshold: int = 5, cooldown_seconds: float = 30.0, *, clock: Callable[[], float] = time.monotonic) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        if cooldown_seconds <= 0:
            raise ValueError("cooldown_seconds must be positive")
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._state = self.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._generation = 0

    @property
    def state(self) -> str:
        """closed, open or half_open (an open breaker whose cool-down has passed reports half_open)."""
        with self._lock:
            if self._state == self.OPEN and self._clock() - self._opened_at >= self.cooldown_seconds:
                return self.HALF_OPEN
            return self._state

    @property
    def consecutive_failures(self) -> int:
        with self._lock:
            return self._failures

    def acquire(self) -> Permit | None:
        """A permit for one call, or None when the breaker rejects it (open, or half-open with the probe in flight). A probe
        whose outcome is never reported is replaced by a new one after another cool-down."""
        with self._lock:
            if self._state == self.CLOSED:
                return Permit(False, self._generation)
            now = self._clock()
            if now - self._opened_at >= self.cooldown_seconds:
                self._state = self.HALF_OPEN
                self._opened_at = now  # the probe's start
                self._generation += 1
                return Permit(True, self._generation)
            return None

    def record(self, permit: Permit, success: bool) -> None:
        """Reports the outcome of a call made with `permit`."""
        with self._lock:
            if permit.generation != self._generation:
                return  # the breaker changed state since this call started
            if permit.probe:
                if success:
                    self._close()
                else:
                    self._open()
            elif self._state == self.CLOSED:
                if success:
                    self._failures = 0
                else:
                    self._failures += 1
                    if self._failures >= self.failure_threshold:
                        self._open()

    def reset(self) -> None:
        """Closes the breaker (for example after a configuration change)."""
        with self._lock:
            self._close()

    def _open(self) -> None:
        self._state = self.OPEN
        self._opened_at = self._clock()
        self._generation += 1

    def _close(self) -> None:
        self._state = self.CLOSED
        self._failures = 0
        self._generation += 1


def backoff_delay(attempt: int, base: float = 0.5, cap: float = 8.0, rand: Callable[[], float] = random.random) -> float:
    """Exponential backoff with jitter for retry number `attempt` (0-based): uniform in [d/2, d] with d = min(cap, base * 2**attempt)."""
    ceiling = min(cap, base * (2 ** max(0, attempt)))
    return ceiling / 2 + rand() * ceiling / 2
