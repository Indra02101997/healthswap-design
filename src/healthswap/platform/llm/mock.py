"""Deterministic offline LLM provider (C# Llm/MockLlmProvider.cs) used for local development, tests and CI. It honours the
same prompts and schemas as the Azure OpenAI provider so the whole pipeline (gateway, validators, audit, caching,
fallbacks) runs without a model. Outputs are identical to C#: the same JSON text (System.Text.Json default escaping and
number formatting), the same token estimates and the same MockFaultEvery fault injection (every Nth explanation call
returns an unsafe or unverifiable "short" text so the validators can be exercised).

Failures follow the C# exception types: a payload that is not a JSON object or has values of the wrong type raises
InvalidOperationError, facts that cannot be read as Fact records raise JsonError, invalid JSON raises
json.JSONDecodeError (all recorded by the gateway) and a missing required node raises ValueError (a C#
NullReferenceException, which the gateway does not catch).

The T5 payload is {"basis", "locale", "facts": [Fact...]} with Fact properties matched case-insensitively like C#
(HotCache.Json: id, kind, nutrient, from, to, unit, basis, relChange, direction, impact, claimEligible, mustDisclose,
codes); the snake_case names of the Python Fact dataclass (from_, rel_change, claim_eligible, must_disclose) are accepted
as well.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Any

from healthswap.domain.knowledge.parsing import to_lower_invariant, utf16_len
from healthswap.domain.recommendation.explanations import ExplanationTemplates, Fact
from healthswap.domain.text import HashingEmbedder, round_double
from healthswap.platform import netjson
from healthswap.platform.llm.gateway import InvalidOperationError, JsonError, LlmTask, PromptSpec, ProviderResponse, parse_json
from healthswap.platform.settings import LlmOptions

_DELIMITED = re.compile(r"<(?P<tag>product|products|facts)>(?P<body>.*)</(?P=tag)>", re.S)

_CATEGORY_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("noodle", "noodles.instant"), ("oats", "breakfast.cereals"), ("cereal", "breakfast.cereals"), ("flakes", "breakfast.cereals"),
    ("muesli", "breakfast.cereals"), ("granola", "breakfast.cereals"), ("cola", "beverages.carbonated.soft-drinks"), ("soda", "beverages.carbonated.soft-drinks"),
    ("sparkling", "beverages.carbonated.flavoured-water"), ("biscuit", "biscuits.sweet"), ("cookie", "biscuits.sweet"), ("rusk", "biscuits.sweet"),
    ("chips", "snacks.chips"), ("crisps", "snacks.chips"), ("bhujia", "snacks.namkeen"), ("namkeen", "snacks.namkeen"), ("chana", "snacks.namkeen"),
    ("yogurt", "dairy.yogurt"), ("dahi", "dairy.yogurt"), ("curd", "dairy.yogurt"), ("bread", "bread.sliced"), ("chocolate milk", "beverages.flavoured-milk"),
    ("chocolate", "confectionery.chocolate"), ("ketchup", "sauces.ketchup"),
)


def _not_null(node: Any) -> Any:
    if node is None:
        raise ValueError("Object reference not set to an instance of an object.")
    return node


def _object(node: Any) -> dict[str, Any]:
    if not isinstance(node, dict):
        raise InvalidOperationError("The node must be of type 'JsonObject'.")
    return node


def _string(node: Any) -> str | None:
    """node?.GetValue<string>()."""
    if node is not None and not isinstance(node, str):
        raise InvalidOperationError(f"The JSON value {node!r} cannot be converted to a String.")
    return node


def _double(node: Any) -> float:
    """node.GetValue<double>() (the JSON number text read as a double)."""
    if isinstance(node, bool) or not isinstance(node, (int, float, Decimal)):
        raise InvalidOperationError(f"The JSON value {node!r} cannot be converted to a Double.")
    return float(node)


# ---------------------------------------------------------------------------------------------------------------------
# Facts (JsonSerializer.Deserialize<List<Fact>>(json, HotCache.Json))
# ---------------------------------------------------------------------------------------------------------------------


def _fact_string(value: Any) -> str | None:
    if value is not None and not isinstance(value, str):
        raise JsonError(f"The JSON value {value!r} could not be converted to System.String.")
    return value


def _fact_decimal(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise JsonError(f"The JSON value {value!r} could not be converted to System.Decimal.")
    return Decimal(value) if isinstance(value, int) else value


def _fact_decimal_or_none(value: Any) -> Decimal | None:
    return None if value is None else _fact_decimal(value)


def _fact_bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise JsonError(f"The JSON value {value!r} could not be converted to System.Boolean.")
    return value


def _fact_codes(value: Any) -> tuple[str | None, ...] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise JsonError(f"The JSON value {value!r} could not be converted to IReadOnlyList<String>.")
    return tuple(_fact_string(x) for x in value)


# (C# property name lower-cased, Python field name, reader)
_FACT_PROPERTIES: tuple[tuple[str, str, Callable[[Any], Any]], ...] = (
    ("id", "id", _fact_string), ("kind", "kind", _fact_string), ("nutrient", "nutrient", _fact_string), ("from", "from_", _fact_decimal),
    ("to", "to", _fact_decimal), ("unit", "unit", _fact_string), ("basis", "basis", _fact_string), ("relchange", "rel_change", _fact_decimal_or_none),
    ("direction", "direction", _fact_string), ("impact", "impact", _fact_decimal), ("claimeligible", "claim_eligible", _fact_bool),
    ("mustdisclose", "must_disclose", _fact_bool), ("codes", "codes", _fact_codes),
)


def _fact(item: Any) -> Fact | None:
    if item is None:
        return None
    if not isinstance(item, dict):
        raise JsonError(f"The JSON value {item!r} could not be converted to Fact.")
    values = {str(k).lower(): v for k, v in item.items()}
    kwargs: dict[str, Any] = {}
    for json_name, attr, read in _FACT_PROPERTIES:
        if json_name in values:
            kwargs[attr] = read(values[json_name])
        elif attr in values:
            kwargs[attr] = read(values[attr])
    missing = [name for name in ("id", "kind") if name not in kwargs]
    if missing:
        raise JsonError(f"JSON deserialization for type 'Fact' was missing required properties including: {', '.join(repr(m) for m in missing)}.")
    return Fact(**kwargs)


def _facts(node: Any) -> list[Fact | None]:
    if not isinstance(node, list):
        raise JsonError(f"The JSON value could not be converted to List<Fact>: {node!r}")
    return [_fact(x) for x in node]


def _lower_first(s: str) -> str:
    """char.ToLowerInvariant(s[0]) + s[1..] (a supplementary-plane first character is a surrogate unit: unchanged)."""
    return s if ord(s[0]) > 0xFFFF else to_lower_invariant(s[0]) + s[1:]


class MockLlmProvider:
    """
    Deterministic offline provider used for local development, tests and CI. It honours the same prompts and schemas as
    the Azure OpenAI provider so the whole pipeline (gateway, validators, audit, caching, fallbacks) runs without a model.
    """

    def __init__(self, options: LlmOptions) -> None:
        self.options = options
        self._t5_calls = 0
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return "mock"

    @staticmethod
    def _payload(user: str) -> dict[str, Any]:
        m = _DELIMITED.search(user)
        return _object(_not_null(parse_json(m.group("body") if m else user)))

    def complete_json(self, spec: PromptSpec, user_content: str, deployment: str) -> ProviderResponse:
        p = self._payload(user_content)
        task = LlmTask(spec.task)
        if task == LlmTask.T3:
            result = self._classify(p)
        elif task == LlmTask.T4:
            result = self._judge(p)
        elif task == LlmTask.T5:
            result = self._explain(p)
        else:
            raise InvalidOperationError(f"Mock provider does not implement {task.value}")
        text = netjson.to_json_string(result)
        tokens_in = utf16_len(user_content) // 4 + utf16_len(spec.system) // 4
        return ProviderResponse(text, tokens_in, utf16_len(text) // 4, f"mock/{deployment}")

    def embed(self, texts: Sequence[str], deployment: str, dimensions: int) -> list[list[float]]:
        return [HashingEmbedder.embed(t) for t in texts]

    @staticmethod
    def _classify(p: dict[str, Any]) -> dict[str, Any]:
        name = _string(p.get("name"))
        brand = _string(p.get("brand"))
        text = to_lower_invariant(("" if name is None else name) + " " + ("" if brand is None else brand))
        allowed_node = p.get("allowed")
        allowed: list[str] = []
        if allowed_node is not None:
            if not isinstance(allowed_node, list):
                raise InvalidOperationError("The node must be of type 'JsonArray'.")
            allowed = list(dict.fromkeys(_string(_not_null(x)) for x in allowed_node))
        allowed_set = set(allowed)
        hit = next(((kw, category) for kw, category in _CATEGORY_KEYWORDS if kw in text and category in allowed_set), None)
        return {
            "categoryCode": hit[1] if hit is not None else allowed[0] if allowed else "",
            "confidence": 0.2 if hit is None else 0.86,
            "rationale": "No keyword matched" if hit is None else f"Title mentions '{hit[0]}'",
        }

    @staticmethod
    def _judge(p: dict[str, Any]) -> dict[str, Any]:
        a = _object(_not_null(p.get("A")))
        b = _object(_not_null(p.get("B")))

        def s(o: dict[str, Any], k: str) -> str | None:
            return _string(o.get(k))

        issues: list[str] = []
        score = 0.82 if s(a, "category") == s(b, "category") else 0.62
        form_match = s(a, "form") == s(b, "form")
        use_match = s(a, "useOccasion") == s(b, "useOccasion")
        if not form_match:
            score -= 0.30
            issues.append("DIFFERENT_FORM")
        if not use_match:
            score -= 0.25
            issues.append("DIFFERENT_USE")
        if s(a, "flavourFamily") != s(b, "flavourFamily"):
            score -= 0.12
            issues.append("DIFFERENT_FLAVOUR_FAMILY")
        diet_a, diet_b = s(a, "dietClass"), s(b, "dietClass")
        if diet_a != diet_b and not (diet_a in ("VEG", "VEGAN") and diet_b in ("VEG", "VEGAN")):
            score -= 0.30
            issues.append("DIETARY_CLASS_MISMATCH")
        qa, qb = a.get("netQuantity"), b.get("netQuantity")
        if qa is not None and qb is not None and _double(qa) > 0:
            ratio = _double(qb) / _double(qa)
            if ratio < 0.5 or ratio > 2.0:
                score -= 0.10
                issues.append("PACK_SIZE_MISMATCH")
        if not issues:
            issues.append("NONE")
        value = score + 0.1
        score = round_double(0.0 if value < 0 else 1.0 if value > 1 else value, 2)
        return {
            "score": score, "sameUseOccasion": use_match, "formMatch": form_match, "issues": issues,
            "rationale": "Same form and use occasion" if form_match and use_match else "Form or use occasion differs",
        }

    def _explain(self, p: dict[str, Any]) -> dict[str, Any]:
        locale_node = _string(p.get("locale"))
        locale = "en-IN" if locale_node is None else locale_node
        facts = _facts(_not_null(p.get("facts")))
        lang = ExplanationTemplates.lang(locale)
        sentences = [ExplanationTemplates.sentence(_not_null(f), lang, locale) for f in facts]
        if len(sentences) >= 2:
            joiner = {"hi": " और ", "es": " y "}.get(lang, ", plus ")
            second = _lower_first(sentences[1]) if lang == "en" and sentences[1] else sentences[1]
            short_text = sentences[0] + joiner + second
            if utf16_len(short_text) > 90:
                short_text = sentences[0]
        else:
            short_text = sentences[0] if sentences else ""

        with self._lock:
            self._t5_calls += 1
            n = self._t5_calls
        if self.options.mock_fault_every > 0 and n % self.options.mock_fault_every == 0:
            short_text = "Guilt-free choice with 99% less sugar" if lang == "en" else short_text + " 99%"

        return {"short": short_text, "bullets": sentences[:3], "factIdsUsed": [f.id for f in facts]}
