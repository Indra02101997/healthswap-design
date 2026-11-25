"""Runs the HealthSwap API locally on the PGlite development database (Windows, macOS and Linux).

    python scripts/run_local.py                  # dev database in .data/devdb (seeded on first use), API on http://127.0.0.1:8080
    python scripts/run_local.py --reset          # wipe .data/devdb, seed again
    python scripts/run_local.py --memory         # throwaway in-memory database, seeded on every run
    python scripts/run_local.py --database-url postgresql://user:pw@host:5432/db   # existing PostgreSQL with db/schema.sql (no dev database)

1. Starts tools/devdb (`node server.mjs`, output appended to .data/logs/devdb.log) and waits until it accepts connections.
2. Seeds the reference data, knowledge base and demo tenants (`hs seed --demo`) when the database has no partner yet
   (a new, reset or in-memory dev database), unless --no-seed.
3. Runs the API (`hs serve`, Development configuration: /v1, /admin, /mcp, /demo) in this process until Ctrl+C, then stops the
   dev database cleanly. If this script is killed instead, the dev database sees its stdin close and shuts down by itself.

Needs the package installed in the running Python (`python -m pip install -e .`), Node.js 20+ and `npm ci` in tools/devdb.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path

try:
    import httpx
    import psycopg

    from healthswap import cli
    from healthswap.devdb import DevDatabase, DevDatabaseError
except ModuleNotFoundError as missing:
    sys.exit(f"run_local: {missing}. Install the package into this Python first: python -m pip install -e .")

REPO = Path(__file__).resolve().parents[1]
DEFAULT_DATA = REPO / ".data" / "devdb"
DEVDB_LOG = REPO / ".data" / "logs" / "devdb.log"
ENVIRONMENT = "Development"


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="run_local.py", description="Run the HealthSwap API locally on the PGlite dev database (tools/devdb).")
    storage = p.add_mutually_exclusive_group()
    storage.add_argument("--memory", action="store_true", help="in-memory dev database (empty, so seeded, on every run)")
    storage.add_argument("--data", type=Path, metavar="DIR", help=f"dev database directory (default: {DEFAULT_DATA.relative_to(REPO).as_posix()})")
    p.add_argument("--reset", action="store_true", help="wipe the dev database directory first (it is then seeded again)")
    p.add_argument("--database-url", metavar="URL", help="use this PostgreSQL (with db/schema.sql applied) instead of starting the dev database")
    p.add_argument("--no-seed", action="store_true", help="do not run `hs seed --demo`, even on an empty database")
    p.add_argument("--host", default="127.0.0.1", help="API host (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=8080, help="API port (default: 8080)")
    return p


def _partner_count(url: str) -> int:
    with psycopg.connect(url, autocommit=True, connect_timeout=30) as conn:
        row = conn.execute("select count(*) from tenant.partner").fetchone()
    return 0 if row is None else int(row[0])


def _seed(url: str) -> bool:
    """`hs seed --demo` in a child process: its logs pass through, its JSON report is only shown when it fails."""
    print("Seeding the reference data, knowledge base and demo tenants (hs seed --demo) ...", flush=True)
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-m", "healthswap.cli", "seed", "--demo", "--env", ENVIRONMENT, "--database-url", url],
        cwd=REPO, stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", check=False,
    )
    if result.returncode != 0:
        print(result.stdout, end="")
        print(f"run_local: hs seed --demo failed with exit code {result.returncode}", file=sys.stderr)
        return False
    print(f"Seeded in {time.monotonic() - started:.1f} s", flush=True)
    return True


def _base_url(host: str, port: int) -> str:
    shown = "localhost" if host in ("", "0.0.0.0", "::") else f"[{host}]" if ":" in host else host
    return f"http://{shown}:{port}"


def _announce_when_ready(base: str, database: str) -> None:
    """Prints the demo URLs and clients once /healthz answers (the API logs its own startup lines first)."""
    deadline = time.monotonic() + 120
    with httpx.Client(timeout=2.0, trust_env=False) as client:
        while True:
            try:
                if client.get(base + "/healthz").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                return
            time.sleep(0.5)
    print("\n".join([
        "",
        f"HealthSwap API ({ENVIRONMENT}) is running on {base}",
        f"  Demo shop     {base}/demo",
        f"  Contract      {base}/openapi.yaml  (partner API under {base}/v1)",
        f"  MCP           {base}/mcp  (Streamable HTTP, bearer token)",
        f"  Tokens        POST {base}/oauth2/token  (client credentials)",
        f"  Health        {base}/healthz  {base}/readyz",
        "  Demo clients  quickbasket-server (QuickBasket, IN), hometown-server (Hometown Grocer, US), hs-admin (admin plane /admin)",
        "                development-only secrets: data/seed/demo/partners.json",
        f"  Database      {database}",
        f"  Smoke test    python scripts/smoke_test.py --base-url {base}",
        "Press Ctrl+C to stop.",
        "",
    ]), flush=True)


def _stop(devdb: DevDatabase) -> None:
    print("Stopping the dev database ...", flush=True)
    try:
        devdb.stop(grace_seconds=30)  # a clean shutdown flushes an on-disk database
    except KeyboardInterrupt:
        devdb.stop(grace_seconds=0)  # stop() has already killed the server when it was interrupted
    print("Dev database stopped.", flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    ns = parser.parse_args(argv)
    if ns.database_url and (ns.memory or ns.data or ns.reset):
        parser.error("--memory, --data and --reset apply to the dev database; they cannot be combined with --database-url")
    if not 0 < ns.port < 65536:
        parser.error(f"--port {ns.port} is not a TCP port")

    os.environ["HS_ENV"] = ENVIRONMENT
    devdb: DevDatabase | None = None
    try:
        if ns.database_url:
            url = ns.database_url
            database = "(from --database-url)"
            if cli.main(["db", "wait", "--timeout", "30", "--env", ENVIRONMENT, "--database-url", url]) != 0:
                return 1
        else:
            data_dir = None if ns.memory else (ns.data or DEFAULT_DATA).resolve()
            where = "in memory" if data_dir is None else f"{data_dir}{' (reset)' if ns.reset else ''}"
            print(f"Starting the dev database {where}; log: {DEVDB_LOG}", flush=True)
            started = time.monotonic()
            devdb = DevDatabase(REPO, memory=data_dir is None, data_dir=data_dir, reset=ns.reset, log_file=DEVDB_LOG)
            url = devdb.start()
            database = url
            print(f"Dev database ready in {time.monotonic() - started:.1f} s: {url}", flush=True)

        partners = _partner_count(url)
        if partners == 0 and ns.no_seed:
            print("The database has no partner and --no-seed was given: the demo clients will not work until `hs seed --demo` runs.")
        elif partners == 0:
            if not _seed(url):
                return 1
        else:
            print(f"Using the existing data ({partners} partners)" + ("; --reset seeds a fresh dev database." if devdb is not None else "."))

        threading.Thread(target=_announce_when_ready, args=(_base_url(ns.host, ns.port), database), name="announce", daemon=True).start()
        return cli.main(["serve", "--env", ENVIRONMENT, "--host", ns.host, "--port", str(ns.port), "--database-url", url])
    except DevDatabaseError as ex:
        print(f"run_local: {ex}", file=sys.stderr)
        return 1
    except psycopg.Error as ex:
        print(f"run_local: cannot read tenant.partner ({ex}); is db/schema.sql applied?", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        if devdb is not None:
            _stop(devdb)


if __name__ == "__main__":
    sys.exit(main())
