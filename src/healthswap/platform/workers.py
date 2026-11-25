"""Background workers (C# BackgroundService / IHostedService): a daemon thread per worker with a stop event.

Subclasses implement `execute()` as their loop (C# ExecuteAsync) and poll `self.stopping` (the C# stoppingToken):

    class FinalizerWorker(BackgroundWorker):
        def __init__(self, reco: RecoChargeService, options: SchedulerOptions) -> None:
            super().__init__()
            ...

        def execute(self) -> None:
            while not self.stopping.is_set():
                try:
                    self.reco.finalize_due()
                except Exception:
                    self.log.warning("Finalizer iteration failed", exc_info=True)
                if not self.sleep(self.options.reco_finalizer_seconds):   # Task.Delay(..., stoppingToken)
                    break

Queue consumers (C# `await foreach (var x in channel.Reader.ReadAllAsync(stoppingToken))`) use `read_all(queue)`.
An exception escaping `execute()` is logged and the loop is restarted after `restart_delay_seconds`, so one failure never
kills the worker thread; returning normally from `execute()` ends the thread (for example a disabled scheduler).
"""

from __future__ import annotations

import logging
import queue
import threading
from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import TypeVar

T = TypeVar("T")


class BackgroundWorker(ABC):
    """Daemon-thread worker with `start()`, `stop(timeout)`, a `stopping` event and an abstract `execute()` loop."""

    # Delay before `execute()` is restarted after an unhandled exception.
    restart_delay_seconds: float = 2.0

    def __init__(self, name: str | None = None) -> None:
        self.name = name or type(self).__name__
        self.stopping = threading.Event()
        self.log = logging.getLogger(f"{type(self).__module__}.{type(self).__qualname__}")
        self._thread: threading.Thread | None = None
        self._state_lock = threading.Lock()

    @abstractmethod
    def execute(self) -> None:
        """The worker loop; it must return soon after `self.stopping` is set."""

    def start(self) -> None:
        """Starts the worker thread (C# StartAsync). Calling it while the worker runs does nothing."""
        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self.stopping.clear()
            self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
            self._thread.start()

    def stop(self, timeout: float | None = 10.0) -> bool:
        """Signals `stopping` and waits up to `timeout` seconds for the thread (C# StopAsync). True when it has ended."""
        self.stopping.set()
        thread = self._thread
        if thread is None or thread is threading.current_thread():
            return True
        thread.join(timeout)
        return not thread.is_alive()

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def sleep(self, seconds: float) -> bool:
        """Waits up to `seconds` (C# Task.Delay with the stopping token). Returns False when the worker is stopping."""
        return not self.stopping.wait(max(0.0, float(seconds)))

    def read_all(self, source: queue.Queue[T], poll_seconds: float = 0.25) -> Iterator[T]:
        """Yields queue items until the worker stops; `task_done()` is called after the loop body has handled each item."""
        while not self.stopping.is_set():
            try:
                item = source.get(timeout=poll_seconds)
            except queue.Empty:
                continue
            try:
                yield item
            finally:
                source.task_done()

    def _run(self) -> None:
        while not self.stopping.is_set():
            try:
                self.execute()
                return
            except Exception:
                self.log.exception("%s failed; restarting in %.1f s", self.name, self.restart_delay_seconds)
                if not self.sleep(self.restart_delay_seconds):
                    return
