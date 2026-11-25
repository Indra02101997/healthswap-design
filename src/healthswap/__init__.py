"""HealthSwap reference implementation (Python).

Packages:
    healthswap.domain    pure rules: scoring, recommendation, fees (FP-1, RA-1), ledger, knowledge parsing. No I/O.
    healthswap.platform  PostgreSQL, cache, event bus, LLM gateway, ONNX models, knowledge jobs and partner services.
    healthswap.api       FastAPI app implementing api/openapi.yaml, the admin plane and the MCP server.
    healthswap.cli       `hs` command line (seed, knowledge-base jobs, billing, ML data export).
"""

__version__ = "0.1.0"
