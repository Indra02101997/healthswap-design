"""HTTP API layer (HealthSwap.Api ported to FastAPI): the partner API, the admin plane, the MCP tools and the local demo."""

from healthswap.api.app import create_app

__all__ = ["create_app"]
