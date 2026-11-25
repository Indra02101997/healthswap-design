"""Configuration: every C# options class (and the appsettings.json values the API and CLI read) in one place.

Layering, later wins:
    1. code defaults (the dataclasses below; the same defaults as the C# options classes)
    2. config/settings.toml
    3. config/settings.<environment lowercase>.toml (for example config/settings.development.toml)
    4. environment variables: HS_<SECTION>__<KEY> (case-insensitive, underscores optional: HS_LLM__API_KEY or
       HS_LLM__APIKEY), dictionary entries HS_<SECTION>__<KEY>__<NAME> (HS_LLM__DEPLOYMENTS__T5), DATABASE_URL as a
       shortcut for database.url and HEALTHSWAP_REPO_ROOT for repo_root
    5. `overrides` passed to load_settings with dotted keys ({"scheduler.enabled": False, "repo_root": path})

The environment is the `environment` argument, else HS_ENV, else "Production". Strings are coerced to the field type
(bool "true/false/1/0", int, float, Decimal, Path, lists from "a,b" or a JSON array, dicts from a JSON object; an empty
string clears an optional value). Unknown keys raise ValueError naming the key. Dictionary values are merged into the
defaults (like the .NET configuration binder); lists are replaced. Relative paths are resolved against the repository
root, and the defaults reproduce C# PlatformRegistration: models/, .data/lake, .data/feeds, .data/keys.
Secrets (llm.api_key, llm.bearer_token) are accepted from environment variables and code overrides only, never TOML.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import tomllib
import types
import typing
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any

from healthswap.paths import repo_root as _default_repo_root
from healthswap.platform.db import DbOptions

_SECRET = {"secret": True}

# ---------------------------------------------------------------------------------------------------------------------
# Sections (C# options classes; field names are the snake_case of the C# properties)
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(slots=True)
class CacheOptions:
    """Hot cache (C# configuration keys Cache:Redis and Cache:WarmOnStartup)."""

    # StackExchange.Redis configuration ("host:6380,password=...,ssl=True") or a redis:// or rediss:// URL.
    # None selects the in-process MemoryCacheStore (local development, tests, single-node demos).
    redis: str | None = None
    # Warm the hot cache from PostgreSQL when the API starts (CacheWarmer).
    warm_on_startup: bool = True


def _default_deployments() -> dict[str, str]:
    return {"T3": "hs-t3-classifier", "T4": "hs-t4-judge", "T5": "hs-t5-explainer", "T7": "hs-embeddings"}


@dataclass(slots=True)
class LlmOptions:
    """LLM gateway (Llm/LlmGateway.cs)."""

    # Mock (deterministic, offline) or AzureOpenAI (Microsoft Foundry / Azure OpenAI v1 API).
    provider: str = "Mock"
    endpoint: str | None = None
    # Secrets: environment variables only (HS_LLM__API_KEY, HS_LLM__BEARER_TOKEN).
    api_key: str | None = field(default=None, repr=False, metadata=_SECRET)
    bearer_token: str | None = field(default=None, repr=False, metadata=_SECRET)
    # Deployment (model) name per task; configured entries are merged into these defaults.
    deployments: dict[str, str] = field(default_factory=_default_deployments)
    max_calls_per_task_per_run: int = 5000
    enable_explanation_polish: bool = True
    # Mock provider only: deliberately produce invalid output for 1 in N calls to exercise the validator chain (0 = never).
    mock_fault_every: int = 0
    # Sampling temperature; set to -1 to omit it (required for reasoning models that reject the parameter).
    temperature: float = 0.0


@dataclass(slots=True)
class ModelOptions:
    """ONNX models written by ml/ (Ml/ModelRegistry.cs, configuration section Models)."""

    directory: Path = Path("models")


ModelsOptions = ModelOptions


@dataclass(slots=True)
class LakeOptions:
    """Bronze layer of the lakehouse: immutable JSONL files per topic and day (Bus/EventBus.cs)."""

    enabled: bool = True
    root: Path = Path(".data/lake")


@dataclass(slots=True)
class GraphOptions:
    """Swap-graph builder (Knowledge/Jobs.cs)."""

    # Ask the T4 substitutability judge (fine-tuned LLM, offline) for pairs in the uncertain band.
    llm_judge_uncertain: bool = True
    uncertain_low: Decimal = Decimal("0.60")
    uncertain_high: Decimal = Decimal("0.80")


@dataclass(slots=True)
class EvaluationOptions:
    """Cart evaluation hot path (Services/EvaluationService.cs)."""

    # Internal compute budget for the hot path; beyond it remaining lines are skipped (degraded TIMEOUT_BUDGET).
    internal_budget_ms: int = 100
    suggestion_ttl_minutes: int = 30
    max_alternatives_cap: int = 5


@dataclass(slots=True)
class WebhookOptions:
    """Webhook delivery (Services/Infrastructure.cs)."""

    # Development only: allow http://localhost endpoints (production requires https and public hosts).
    allow_http_localhost: bool = False
    max_attempt_hours: int = 24
    timeout_seconds: int = 10


@dataclass(slots=True)
class FeedOptions:
    """Bulk catalog feeds (Services/CatalogServices.cs)."""

    storage_root: Path = Path(".data/feeds")
    upload_url_minutes: int = 60
    max_upload_bytes: int = 200 * 1024 * 1024


@dataclass(slots=True)
class SchedulerOptions:
    """RA-1 charge and FP-1 return-window finalizers (Services/EventServices.cs)."""

    enabled: bool = True
    reco_finalizer_seconds: int = 30
    attribution_finalizer_seconds: int = 60


@dataclass(slots=True)
class AuthOptions:
    """OAuth 2.0 token validation and the development identity provider (HealthSwap.Api/Auth.cs)."""

    # External OAuth authority (for example Microsoft Entra ID). When set, the built-in development token endpoint is disabled.
    authority: str | None = None
    issuer: str = "http://localhost:8080/oauth2"
    audience: str = "healthswap-api"
    access_token_minutes: int = 10
    sdk_session_max_seconds: int = 900
    # Development identity provider: client secrets are read from this file and kept only as PBKDF2 hashes in memory.
    clients_file: Path | None = Path("data/seed/demo/partners.json")
    # ES256 signing key for locally issued tokens (created on first use).
    signing_key_file: Path | None = Path(".data/keys/jwt-es256.pem")


@dataclass(slots=True)
class RateLimitOptions:
    """Token buckets per partner and route group (HealthSwap.Api/Http.cs)."""

    enabled: bool = True
    cart_evaluations_per_second: int = 50
    default_per_second: int = 20
    burst_multiplier: int = 2


def _default_origins() -> list[str]:
    return ["http://localhost:8080"]


@dataclass(slots=True)
class CorsOptions:
    """Browser origins allowed to call the API with SDK session tokens (Cors:Origins)."""

    origins: list[str] = field(default_factory=_default_origins)


@dataclass(slots=True)
class DemoOptions:
    """Demo endpoints and the /demo UI (Demo:Enabled; the .NET API also enables them in Development)."""

    enabled: bool = False


@dataclass(slots=True)
class DataProtectionOptions:
    """Key ring for webhook secrets and feed upload tokens (DataProtection:KeysDirectory)."""

    keys_directory: Path = Path(".data/keys")


@dataclass(slots=True)
class ServerOptions:
    """API listen address (applicationUrl http://localhost:8080 in the .NET launchSettings.json)."""

    host: str = "localhost"
    port: int = 8080

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


# ---------------------------------------------------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(slots=True)
class Settings:
    """All configuration sections. Relative paths in any section are resolved against `repo_root` on construction."""

    environment: str = "Production"
    repo_root: Path = field(default_factory=_default_repo_root)
    database: DbOptions = field(default_factory=DbOptions)
    cache: CacheOptions = field(default_factory=CacheOptions)
    llm: LlmOptions = field(default_factory=LlmOptions)
    models: ModelOptions = field(default_factory=ModelOptions)
    lake: LakeOptions = field(default_factory=LakeOptions)
    graph: GraphOptions = field(default_factory=GraphOptions)
    evaluation: EvaluationOptions = field(default_factory=EvaluationOptions)
    webhooks: WebhookOptions = field(default_factory=WebhookOptions)
    feeds: FeedOptions = field(default_factory=FeedOptions)
    scheduler: SchedulerOptions = field(default_factory=SchedulerOptions)
    auth: AuthOptions = field(default_factory=AuthOptions)
    rate_limits: RateLimitOptions = field(default_factory=RateLimitOptions)
    cors: CorsOptions = field(default_factory=CorsOptions)
    demo: DemoOptions = field(default_factory=DemoOptions)
    data_protection: DataProtectionOptions = field(default_factory=DataProtectionOptions)
    server: ServerOptions = field(default_factory=ServerOptions)

    def __post_init__(self) -> None:
        self.repo_root = Path(self.repo_root).expanduser().resolve()
        for name in _SECTIONS:
            section = getattr(self, name)
            hints = _hints(type(section))
            for f in dataclasses.fields(section):
                if not _is_path_type(hints[f.name]):
                    continue
                value = getattr(section, f.name)
                if isinstance(value, str):
                    value = Path(value) if value.strip() else None
                if isinstance(value, Path):
                    value = value.expanduser()
                    if not value.is_absolute():
                        value = self.repo_root / value
                setattr(section, f.name, value)

    @property
    def is_development(self) -> bool:
        return self.environment.strip().lower() == "development"


_SECTIONS: dict[str, type] = {
    name: tp for name, tp in typing.get_type_hints(Settings).items() if name not in ("environment", "repo_root")
}

# ---------------------------------------------------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------------------------------------------------

_ENVIRONMENT_RX = re.compile(r"[A-Za-z0-9_.-]+")


def load_settings(
    environment: str | None = None, overrides: Mapping[str, Any] | None = None, env: Mapping[str, str] | None = None
) -> Settings:
    """Builds Settings from code defaults, config/*.toml, environment variables (`env`, default os.environ) and overrides."""
    environ = dict(os.environ if env is None else env)
    environment = (environment or _env_get(environ, "HS_ENV") or "Production").strip()
    if not _ENVIRONMENT_RX.fullmatch(environment):
        raise ValueError(f"Invalid environment name {environment!r}")
    rest = dict(overrides or {})
    root_value = rest.pop("repo_root", None) or _env_get(environ, "HEALTHSWAP_REPO_ROOT")
    root = Path(root_value).expanduser().resolve() if root_value else _default_repo_root()

    sections: dict[str, Any] = {name: cls() for name, cls in _SECTIONS.items()}
    config_dir = root / "config"
    for path in (config_dir / "settings.toml", config_dir / f"settings.{environment.lower()}.toml"):
        if path.is_file():
            _apply_toml(sections, path)
    _apply_env(sections, environ)
    for key, value in rest.items():
        source = f"override {key!r}"
        if "." not in key and isinstance(value, Mapping):
            for name, item in value.items():
                _assign(sections, [key, str(name)], item, source)
        else:
            _assign(sections, key.split("."), value, source)
    return Settings(environment=environment, repo_root=root, **sections)


def _env_get(environ: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive environment lookup; empty values count as unset."""
    for key, value in environ.items():
        if key.upper() == name and value.strip():
            return value.strip()
    return None


def _apply_toml(sections: dict[str, Any], path: Path) -> None:
    with path.open("rb") as f:
        data = tomllib.load(f, parse_float=Decimal)
    source = str(path)
    for section, table in data.items():
        if not isinstance(table, dict):
            raise ValueError(f"Unknown setting '{section}' ({source}): top-level entries must be [section] tables")
        for key, value in table.items():
            _assign(sections, [section, key], value, source, from_file=True)


def _apply_env(sections: dict[str, Any], environ: Mapping[str, str]) -> None:
    url = _env_get(environ, "DATABASE_URL")
    if url:
        _assign(sections, ["database", "url"], url, "environment variable DATABASE_URL")
    for name in sorted(environ):
        if name.upper().startswith("HS_") and "__" in name:
            _assign(sections, name[3:].split("__"), environ[name], f"environment variable {name}")


def _norm(name: str) -> str:
    return name.replace("_", "").replace("-", "").lower()


def _assign(sections: dict[str, Any], parts: Sequence[str], value: Any, source: str, from_file: bool = False) -> None:
    label = ".".join(parts)
    section_name = next((s for s in sections if _norm(s) == _norm(parts[0])), None) if parts else None
    if section_name is None or len(parts) < 2:
        raise ValueError(f"Unknown setting '{label}' ({source})")
    section = sections[section_name]
    f = next((f for f in dataclasses.fields(section) if _norm(f.name) == _norm(parts[1])), None)
    if f is None:
        raise ValueError(f"Unknown setting '{section_name}.{parts[1]}' ({source})")
    name = f"{section_name}.{f.name}"
    secret = bool(f.metadata.get("secret"))
    if from_file and secret:
        raise ValueError(f"'{name}' is a secret and must not be stored in {source}; set HS_{section_name.upper()}__{f.name.upper()} instead")
    tp = _hints(type(section))[f.name]
    if len(parts) > 2:
        if len(parts) > 3 or typing.get_origin(tp) is not dict:
            raise ValueError(f"Unknown setting '{name}.{'.'.join(parts[2:])}' ({source})")
        value = {parts[2]: value}
    setattr(section, f.name, _coerce(tp, value, getattr(section, f.name), name, source, secret))


@lru_cache(maxsize=64)
def _hints(cls: type) -> dict[str, Any]:
    return typing.get_type_hints(cls)


def _is_path_type(tp: Any) -> bool:
    return tp is Path or (typing.get_origin(tp) in (typing.Union, types.UnionType) and Path in typing.get_args(tp))


def _coerce(tp: Any, value: Any, current: Any, name: str, source: str, secret: bool = False) -> Any:
    def bad(expected: str) -> ValueError:
        shown = "***" if secret else repr(value)
        return ValueError(f"Invalid value {shown} for setting '{name}' ({source}): expected {expected}")

    origin, args = typing.get_origin(tp), typing.get_args(tp)
    if origin in (typing.Union, types.UnionType):
        if value is None or (isinstance(value, str) and not value.strip()):
            if type(None) in args:
                return None
            raise bad("a value")
        inner = [a for a in args if a is not type(None)]
        return _coerce(inner[0], value, current, name, source, secret)
    if value is None:
        raise bad("a value")
    if tp is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        text = value.strip().lower() if isinstance(value, str) else None
        if text in ("true", "1"):
            return True
        if text in ("false", "0"):
            return False
        raise bad("true, false, 1 or 0")
    if tp is int:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
            return int(value)
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            try:
                return int(value.strip())
            except ValueError:
                pass
        raise bad("an integer")
    if tp is float:
        if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                pass
        raise bad("a number")
    if tp is Decimal:
        number: Decimal | None = None
        if isinstance(value, Decimal):
            number = value
        elif isinstance(value, int) and not isinstance(value, bool):
            number = Decimal(value)
        elif isinstance(value, float):
            number = Decimal(repr(value))
        elif isinstance(value, str):
            try:
                number = Decimal(value.strip())
            except InvalidOperation:
                number = None
        if number is None or not number.is_finite():
            raise bad("a decimal number")
        return number
    if tp is str:
        if isinstance(value, str):
            return value
        raise bad("a string")
    if tp is Path:
        if isinstance(value, Path):
            return value
        if isinstance(value, str) and value.strip():
            return Path(value.strip())
        raise bad("a path")
    if origin is list:
        item_type = args[0] if args else str
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                try:
                    value = json.loads(text)
                except json.JSONDecodeError:
                    raise bad("a JSON array or comma-separated values") from None
            else:
                value = [p.strip() for p in text.split(",") if p.strip()]
        if not isinstance(value, (list, tuple)):
            raise bad("a list")
        return [_coerce(item_type, v, None, name, source, secret) for v in value]
    if origin is dict:
        value_type = args[1] if len(args) == 2 else str
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                raise bad("a JSON object") from None
        if not isinstance(value, Mapping):
            raise bad("a table of values")
        result = dict(current or {})
        for k, v in value.items():
            # Keys match case-insensitively (like .NET configuration); an existing key keeps its spelling.
            key = next((existing for existing in result if existing.lower() == str(k).lower()), str(k))
            result[key] = _coerce(value_type, v, result.get(key), f"{name}.{key}", source, secret)
        return result
    raise TypeError(f"Unsupported setting type {tp!r} for '{name}'")
