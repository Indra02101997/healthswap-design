"""Launcher for the PGlite development database in tools/devdb (scripts/run_local.py and the integration tests).

    with DevDatabase(repo_root) as devdb:        # node server.mjs --memory --port <free port>, waits for "DEVDB READY"
        devdb.url                                # postgresql://postgres:postgres@127.0.0.1:<port>/postgres?sslmode=disable&...
    # leaving the block stops it: stdin is closed (server.mjs then shuts down cleanly), the process tree is killed after a grace period

The server runs in its own process group (Windows: CREATE_NEW_PROCESS_GROUP, POSIX: a new session), so Ctrl+C in a console only
reaches the parent, which stops the database after whatever uses it. If the parent dies, the server sees its stdin close and exits.
"""

from __future__ import annotations

import collections
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from types import TracebackType
from typing import IO, Any

READY_MARKER = "DEVDB READY"
URL_OPTIONS = "sslmode=disable&connect_timeout=30"
# The shared developer database listens there; a throwaway instance must never take it.
_RESERVED_PORTS = frozenset({55432})


class DevDatabaseError(RuntimeError):
    """tools/devdb could not be started."""


def unavailable_reason(repo_root: str | os.PathLike[str]) -> str | None:
    """Why the dev database cannot be started on this machine, or None when it can."""
    if shutil.which("node") is None:
        return "Node.js is not installed (the PGlite dev database in tools/devdb needs Node.js 20+ on PATH)"
    if not (Path(repo_root) / "tools" / "devdb" / "node_modules").is_dir():
        return "tools/devdb/node_modules is missing: run `npm ci` in tools/devdb (PGlite dev database)"
    return None


def free_port(host: str = "127.0.0.1") -> int:
    """A TCP port the OS reports as free right now (another process could still take it before it is used)."""
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((host, 0))
            port = int(s.getsockname()[1])
        if port not in _RESERVED_PORTS:
            return port


def kill_tree(process: subprocess.Popen[Any]) -> None:
    """Kills the process and its descendants (it must have been started as a process-group / session leader on POSIX)."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    if process.poll() is None:
        process.kill()


def new_process_group() -> dict[str, Any]:
    """subprocess.Popen keyword arguments that start the child in its own process group (no console Ctrl+C delivery)."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


class DevDatabase:
    """One `node tools/devdb/server.mjs` process.

    memory=True (default) keeps everything in memory; otherwise data lives in `data_dir` (server.mjs default: <repo>/.data/pglite)
    and `reset=True` wipes it first. The server output goes to `log_file` when given; the last lines are always kept for errors.
    Without an explicit `port` a free one is picked (and re-picked if another process grabs it before the server binds).
    """

    def __init__(self, repo_root: str | os.PathLike[str], *, port: int | None = None, host: str = "127.0.0.1", memory: bool = True,
                 data_dir: str | os.PathLike[str] | None = None, reset: bool = False, log_file: str | os.PathLike[str] | None = None,
                 startup_timeout: float = 90.0) -> None:
        self.repo_root = Path(repo_root)
        self.requested_port = port
        self.port = port
        self.host = host
        self.memory = memory
        self.data_dir = None if data_dir is None else Path(data_dir)
        self.reset = reset
        self.log_file = None if log_file is None else Path(log_file)
        self.startup_timeout = startup_timeout
        self.process: subprocess.Popen[str] | None = None
        self.url: str | None = None
        self._ready = threading.Event()
        self._tail: collections.deque[str] = collections.deque(maxlen=200)
        self._reader: threading.Thread | None = None
        self._log: IO[str] | None = None

    def __enter__(self) -> DevDatabase:
        self.start()
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None) -> None:
        self.stop()

    @property
    def pid(self) -> int | None:
        return None if self.process is None else self.process.pid

    def output(self) -> str:
        """The last lines the server printed."""
        return "\n".join(list(self._tail))

    def command(self) -> list[str]:
        node = shutil.which("node") or "node"
        cmd = [node, "server.mjs", "--host", self.host, "--port", str(self.port)]
        if self.memory:
            cmd.append("--memory")
        elif self.data_dir is not None:
            cmd += ["--data", str(self.data_dir)]
        if self.reset and not self.memory:
            cmd.append("--reset")
        return cmd

    def start(self) -> str:
        """Starts the server and waits until it accepts connections; returns the libpq URL."""
        if self.process is not None:
            raise DevDatabaseError("The dev database is already running")
        reason = unavailable_reason(self.repo_root)
        if reason is not None:
            raise DevDatabaseError(reason)
        attempts = 1 if self.requested_port is not None else 3
        for attempt in range(1, attempts + 1):
            self.port = self.requested_port if self.requested_port is not None else free_port(self.host)
            self._launch()
            if self._wait_ready():
                assert self.url is not None
                return self.url
            code = self.process.returncode if self.process is not None else None
            output = self.output()
            self.stop()
            if "EADDRINUSE" in output and attempt < attempts:
                continue
            raise DevDatabaseError(f"tools/devdb exited with code {code} before it was ready:\n{output}")
        raise AssertionError("unreachable")

    def stop(self, grace_seconds: float = 5.0) -> None:
        """Closes stdin (server.mjs then shuts down cleanly) and kills the process tree if it is still running after the grace period."""
        p = self.process
        if p is None:
            return
        try:
            if p.poll() is None:
                try:
                    if p.stdin is not None:
                        p.stdin.close()
                except OSError:
                    pass
                try:
                    p.wait(grace_seconds)
                except subprocess.TimeoutExpired:
                    kill_tree(p)
                    p.wait(10)
        finally:
            if p.poll() is None:
                kill_tree(p)
            for stream in (p.stdin, p.stdout):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass
            if self._reader is not None:
                self._reader.join(5)
                self._reader = None
            if self._log is not None:
                self._log.close()
                self._log = None
            self.process = None

    # --- internals ---------------------------------------------------------------------------------------------------

    def _launch(self) -> None:
        self._ready.clear()
        self.url = None
        self._tail.clear()
        if self.log_file is not None:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            self._log = self.log_file.open("a", encoding="utf-8")
        self.process = subprocess.Popen(
            self.command(), cwd=self.repo_root / "tools" / "devdb", stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1, **new_process_group(),
        )
        assert self.process.stdout is not None
        self._reader = threading.Thread(target=self._pump, args=(self.process.stdout,), name=f"devdb-{self.port}", daemon=True)
        self._reader.start()

    def _pump(self, stream: IO[str]) -> None:
        try:
            for raw in stream:
                line = raw.rstrip("\r\n")
                self._tail.append(line)
                if self._log is not None:
                    try:
                        self._log.write(line + "\n")
                        self._log.flush()
                    except (OSError, ValueError):
                        pass
                if not self._ready.is_set() and line.startswith(READY_MARKER):
                    url = line[len(READY_MARKER):].split()[0]
                    self.url = url + ("&" if "?" in url else "?") + URL_OPTIONS
                    self._ready.set()
        except (OSError, ValueError):
            pass  # the stream was closed by stop()

    def _wait_ready(self) -> bool:
        assert self.process is not None
        deadline = time.monotonic() + self.startup_timeout
        while not self._ready.wait(0.1):
            if self.process.poll() is not None:
                if self._reader is not None:
                    self._reader.join(2)
                return self._ready.is_set()
            if time.monotonic() > deadline:
                output = self.output()
                self.stop()
                raise DevDatabaseError(f"tools/devdb did not report {READY_MARKER} within {self.startup_timeout:.0f} s:\n{output}")
        return True
