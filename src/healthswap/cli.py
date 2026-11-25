"""hs - HealthSwap operations CLI (port of src/HealthSwap.Cli/Program.cs). Run `hs help` for commands.

Every command except `db wait` and `serve` builds the platform Container, starts only the bus, the outbox relay and the evaluation
recorder (like the C# CLI: no scheduler, cache warmer or webhook dispatcher), runs, prints its report as indented JSON on
stdout, lets the outbox relay publish what the command produced (lake files for ml/) and stops. Logs go to stderr.
`serve` runs the HTTP API (healthswap.api) with uvicorn and every worker.
Exit codes: 0 success, 1 failure (database unreachable, command error), 2 usage errors.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from typing import Any

import psycopg

from healthswap.domain.primitives import period_of, utc_now
from healthswap.platform import jsonutil
from healthswap.platform.container import Container
from healthswap.platform.errors import HsError
from healthswap.platform.knowledge.connectors import Connectors
from healthswap.platform.settings import Settings, load_settings

log = logging.getLogger("healthswap.cli")

USAGE = """\
hs - HealthSwap operations CLI

  hs db wait [--timeout 60]                   wait until PostgreSQL (or the PGlite dev server) accepts connections
  hs seed [--demo]                            reference data + knowledge base (+ demo partners, catalogs, inventory) + scores + graphs + explanations
  hs kb ingest --source brand|off|fdc --file <path>
  hs kb recalls --file <openfda.json>
  hs kb score | build-graph | explain | rebuild
  hs jobs finalize                            RA-1 charge finalizer + FP-1 return-window finalizer
  hs billing month-close [--period YYYY-MM] [--partner <id>]
  hs ml export [--out ml/data]                training data for ml/ (features from the serving code)
  hs ml simulate [--partner <id>] [--sessions 2000] [--out ml/data/ranker_events.jsonl] [--seed 7]
  hs llm eval [--input ml/data/explanation_facts.jsonl] [--max 200]   run T5 through the production validator chain
  hs serve [--host 127.0.0.1] [--port <server.port>] [--reload]      run the HTTP API (/v1, /admin, /mcp; /demo in Development)

Global options (anywhere on the command line):
  --env <name>             configuration environment (default HS_ENV, else Production): config/settings.<env>.toml
  --database-url <url>     PostgreSQL URL (overrides DATABASE_URL and config/*.toml)
  --verbose                debug logging

Configuration: config/settings.toml, config/settings.<env>.toml and environment variables (DATABASE_URL, HS_LLM__PROVIDER,
HS_LLM__ENDPOINT, HS_LLM__API_KEY, HS_LLM__DEPLOYMENTS__T5, ...); see src/healthswap/platform/settings.py.
"""

# The demo partner simulated by `hs ml simulate` (QuickBasket, data/seed/demo/partners.json).
DEMO_PARTNER_ID = "6f1d0c2a-5b7e-4c1a-9d3e-1a2b3c4d5e01"


# ---------------------------------------------------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # usage errors: exit 2 with a hint (C#: "Unknown command ... Run 'hs help'.")
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: {message}\nRun 'hs help' for commands.\n")


def _parser() -> argparse.ArgumentParser:
    # Global options are accepted before and after the command (SUPPRESS keeps a value given at either level).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--env", default=argparse.SUPPRESS, help="configuration environment (default HS_ENV, else Production)")
    common.add_argument("--database-url", default=argparse.SUPPRESS, help="PostgreSQL URL")
    common.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS, help="debug logging")

    parser = _Parser(prog="hs", description="HealthSwap operations CLI", parents=[common])
    groups = parser.add_subparsers(dest="group", metavar="<command>", parser_class=_Parser)
    groups.required = True

    def leaf(sub: argparse._SubParsersAction, name: str, command: str, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text, parents=[common])
        p.set_defaults(command=command)
        return p

    db = groups.add_parser("db", help="database utilities").add_subparsers(dest="action", metavar="<action>", parser_class=_Parser)
    db.required = True
    leaf(db, "wait", "db wait", "wait until PostgreSQL accepts connections and has the schema").add_argument("--timeout", type=int, default=60)

    leaf(groups, "seed", "seed", "reference data, knowledge base, scores, graphs, explanations").add_argument(
        "--demo", action="store_true", help="also demo partners, catalogs and inventory")

    kb = groups.add_parser("kb", help="knowledge-base jobs").add_subparsers(dest="action", metavar="<action>", parser_class=_Parser)
    kb.required = True
    ingest = leaf(kb, "ingest", "kb ingest", "ingest product records from a file")
    ingest.add_argument("--source", choices=("brand", "off", "fdc"), default="brand")
    ingest.add_argument("--file", required=True)
    leaf(kb, "recalls", "kb recalls", "ingest an openFDA enforcement file").add_argument("--file", required=True)
    leaf(kb, "score", "kb score", "score every market")
    leaf(kb, "build-graph", "kb build-graph", "build and activate the swap graphs")
    leaf(kb, "explain", "kb explain", "pre-generate explanations")
    leaf(kb, "rebuild", "kb rebuild", "score + build-graph + explain")

    jobs = groups.add_parser("jobs", help="scheduled jobs").add_subparsers(dest="action", metavar="<action>", parser_class=_Parser)
    jobs.required = True
    leaf(jobs, "finalize", "jobs finalize", "RA-1 charge finalizer + FP-1 return-window finalizer")

    billing = groups.add_parser("billing", help="billing").add_subparsers(dest="action", metavar="<action>", parser_class=_Parser)
    billing.required = True
    close = leaf(billing, "month-close", "billing month-close", "close a billing month (default: the current UTC month) for one or all partners")
    close.add_argument("--period", help="YYYY-MM")
    close.add_argument("--partner", type=uuid.UUID, help="partner id (default: every LIVE, SHADOW and SANDBOX partner)")

    ml = groups.add_parser("ml", help="training data").add_subparsers(dest="action", metavar="<action>", parser_class=_Parser)
    ml.required = True
    leaf(ml, "export", "ml export", "export training data (default ml/data)").add_argument("--out")
    simulate = leaf(ml, "simulate", "ml simulate", "simulate shopper sessions against the evaluation pipeline (ranker rows)")
    simulate.add_argument("--partner", type=uuid.UUID, default=uuid.UUID(DEMO_PARTNER_ID))
    simulate.add_argument("--sessions", type=int, default=2000)
    simulate.add_argument("--out")
    simulate.add_argument("--seed", type=int, default=7)

    llm = groups.add_parser("llm", help="LLM evaluation").add_subparsers(dest="action", metavar="<action>", parser_class=_Parser)
    llm.required = True
    evaluate = leaf(llm, "eval", "llm eval", "run T5 through the production validator chain")
    evaluate.add_argument("--input")
    evaluate.add_argument("--max", type=int, default=200)

    serve = leaf(groups, "serve", "serve", "run the HTTP API with uvicorn")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, help="default: server.port (8080)")
    serve.add_argument("--reload", action="store_true", help="restart on code changes (development)")
    return parser


# ---------------------------------------------------------------------------------------------------------------------
# Logging and output
# ---------------------------------------------------------------------------------------------------------------------

_LEVELS = {logging.DEBUG: "dbug", logging.INFO: "info", logging.WARNING: "warn", logging.ERROR: "fail", logging.CRITICAL: "crit"}


class _ConsoleFormatter(logging.Formatter):
    """Single-line console logs like the .NET simple console ("HH:mm:ss info: Category message")."""

    def format(self, record: logging.LogRecord) -> str:
        record.short_level = _LEVELS.get(record.levelno, record.levelname.lower()[:4])
        return super().format(record)


def _configure_logging(verbose: bool) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_ConsoleFormatter("%(asctime)s %(short_level)s: %(name)s %(message)s", datefmt="%H:%M:%S"))
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for noisy in ("httpx", "httpcore", "psycopg", "psycopg.pool", "urllib3", "mcp.server.streamable_http"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _print(value: Any) -> None:
    print(jsonutil.dumps(value, indent=2))


# ---------------------------------------------------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------------------------------------------------


def _settings(ns: argparse.Namespace) -> Settings:
    overrides: dict[str, Any] = {}
    if getattr(ns, "database_url", None):
        overrides["database.url"] = ns.database_url
    return load_settings(getattr(ns, "env", None), overrides)


def _db_wait(settings: Settings, timeout_seconds: int) -> int:
    """Polls every second until the database accepts connections and kb.product exists (no workers, no container)."""
    started = time.monotonic()
    while True:
        try:
            with psycopg.connect(settings.database.url, autocommit=True, connect_timeout=5) as conn:
                row = conn.execute("select count(*) from information_schema.tables where table_schema = 'kb' and table_name = 'product'").fetchone()
                if row is not None and row[0] > 0:
                    print(f"Database ready ({int((time.monotonic() - started) * 1000)} ms)")
                    return 0
        except (psycopg.Error, OSError, TimeoutError):
            pass
        if time.monotonic() - started > timeout_seconds:
            print("Database not reachable or schema missing", file=sys.stderr)
            return 1
        time.sleep(1.0)


def _kb_ingest(c: Container, ns: argparse.Namespace) -> Any:
    now = c.clock.utc_now()
    read = {"brand": Connectors.brand_portal, "off": Connectors.open_food_facts, "fdc": Connectors.usda_fdc_branded}[ns.source]
    return c.product_ingestor.ingest(list(read(ns.file, now)))


def _jobs_finalize(c: Container, ns: argparse.Namespace) -> Any:
    final, rejected = c.reco_charge_service.finalize_due()
    return {"recoCharges": {"final": final, "rejected": rejected}, "attributionsFinal": c.attribution_engine.finalize_due()}


def _ml_simulate(c: Container, ns: argparse.Namespace) -> Any:
    c.cache_warmer.warm_all()  # the simulator reads the hot cache, so warm it from PostgreSQL first
    rows = c.ml_data.simulate(ns.partner, ns.sessions, ns.out or c.repo_root / "ml" / "data" / "ranker_events.jsonl", ns.seed)
    c.evaluation_recorder.flush(60.0)
    return {"rows": rows}


def _llm_eval(c: Container, ns: argparse.Namespace) -> Any:
    report = c.ml_data.evaluate_explanations(ns.input or c.repo_root / "ml" / "data" / "explanation_facts.jsonl", ns.max)
    return {"provider": c.ml_data.provider, "report": report}


COMMANDS: dict[str, Callable[[Container, argparse.Namespace], Any]] = {
    "seed": lambda c, ns: c.demo_seeder.seed(c.repo_root, ns.demo).steps,
    "kb ingest": _kb_ingest,
    "kb recalls": lambda c, ns: {"matched": c.recall_ingestor.ingest(list(Connectors.open_fda_enforcement(ns.file)))},
    "kb score": lambda c, ns: c.scoring_job.run_all(),
    "kb build-graph": lambda c, ns: c.swap_graph_builder.build_all(),
    "kb explain": lambda c, ns: c.explanation_pregen.run_all(),
    "kb rebuild": lambda c, ns: {
        "scoring": c.scoring_job.run_all(), "graphs": c.swap_graph_builder.build_all(), "explanations": c.explanation_pregen.run_all(),
    },
    "jobs finalize": _jobs_finalize,
    # The default period is the wall-clock UTC month (C# Period.Of(DateTime.UtcNow)), not the time-travel business clock.
    "billing month-close": lambda c, ns: c.billing_service.close_month(ns.period or period_of(utc_now()), ns.partner),
    "ml export": lambda c, ns: c.ml_data.export(ns.out or c.repo_root / "ml" / "data"),
    "ml simulate": _ml_simulate,
    "llm eval": _llm_eval,
}


def _run(settings: Settings, command: str, ns: argparse.Namespace) -> int:
    with Container(settings) as c:
        c.start_workers(include_scheduler=False, include_cache_warmer=False, include_webhooks=False)
        result = COMMANDS[command](c, ns)
        _print(result)
        # Let the outbox relay publish what the command produced (webhook fan-out, lake files for ml/), then stop.
        while c.outbox_relay.pump() > 0:
            pass
        if not c.bus.wait_idle(30.0):
            log.warning("The event bus did not drain within 30 s; %d message(s) dropped", c.bus.pending)
    return 0


def _serve(settings: Settings, ns: argparse.Namespace) -> int:
    """Runs the API (uvicorn + the healthswap.api.app:create_app factory). The factory reads its settings from the environment,
    so the command-line choices are handed over as HS_ENV, DATABASE_URL and HS_SERVER__HOST / HS_SERVER__PORT (this also
    reaches --reload workers). Access logs are off: feed upload URLs carry their token in the query string."""
    import uvicorn

    port: int = ns.port if ns.port is not None else settings.server.port
    os.environ["HS_ENV"] = settings.environment
    if getattr(ns, "database_url", None):
        os.environ["DATABASE_URL"] = ns.database_url
    os.environ["HS_SERVER__HOST"] = ns.host
    os.environ["HS_SERVER__PORT"] = str(port)
    uvicorn.run("healthswap.api.app:create_app", factory=True, host=ns.host, port=port, reload=ns.reload, access_log=False, log_config=None)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("help", "--help", "-h"):
        print(USAGE, end="")
        return 0
    try:
        ns = _parser().parse_args(args)
    except SystemExit as ex:  # argparse: --help exits 0, usage errors 2
        return int(ex.code or 0) if isinstance(ex.code, int) or ex.code is None else 2
    _configure_logging(bool(getattr(ns, "verbose", False)))
    command: str = ns.command
    try:
        settings = _settings(ns)
        if command == "db wait":
            return _db_wait(settings, ns.timeout)
        if command == "serve":
            return _serve(settings, ns)
        return _run(settings, command, ns)
    except KeyboardInterrupt:
        return 130
    except (HsError, ValueError, OSError, LookupError, psycopg.Error) as ex:
        log.debug("Command failed", exc_info=True)
        print(f"hs {command}: error: {ex}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
