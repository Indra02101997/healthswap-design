"""Azure OpenAI chat fine-tuning datasets for the offline LLM tasks, built from the `hs ml export` files.

    python ml/finetune/build_datasets.py [--data ml/data] [--out ml/finetune/out] [--tasks t5,t4,t3] [--validation-share 0.2] [--seed 7]

One example per line, {"messages": [system, user, assistant]}:
* system: the production prompt of the task (PromptRegistry: T3 category_classifier, T4 substitutability_judge, T5
  explanation_writer);
* user: the exact message the service sends at runtime, built by the same platform functions (category_payload, judge_payload,
  explanation_payload) inside the task's delimiter (PromptSpec.user_content);
* assistant: the target JSON for the task's strict JSON schema:
    T5 (explanation_facts.jsonl): the deterministic template rendering of the facts, kept only when it passes ExplanationValidator
       (the validator chain every polished explanation goes through);
    T4 (substitutability_pairs.jsonl + products.jsonl): the gold label as the score (0.9 / 0.8 with minor differences / 0.1), the
       attribute differences as issues;
    T3 (category_examples.jsonl): the curated category with confidence 0.95; the allowed codes are the kb.category enum the
       ingestor sends, rebuilt from data/seed/kb/categories.json in seeding order.
Every example is validated like submit.py does before uploading (roles, production system prompt, canonical runtime user content,
assistant JSON against the task schema with jsonschema, runtime acceptance checks: confidence/score range, the T5 validator);
invalid ones are dropped and counted. The train/validation split is deterministic: seeded, stratified (T5 locale, T4 label,
T3 category) and grouped (both directions of a T4 pair, all locales of a T5 fact sheet stay together).

Output per task in --out: <task>_train.jsonl and <task>_validation.jsonl, UTF-8 with a byte order mark (required by Azure OpenAI),
refused at 512 MB or more; for T5 also t5_validation_facts.jsonl, the held-out export lines for `hs llm eval --input`.
"""

from __future__ import annotations

import argparse
import codecs
import json
import os
import random
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from healthswap.domain.primitives import NetQuantity
from healthswap.domain.knowledge.products import decimal_div
from healthswap.domain.recommendation.explanations import ExplanationTemplates, ExplanationValidator
from healthswap.platform import jsonutil, netjson
from healthswap.platform.knowledge.ingestor import allowed_categories, category_payload
from healthswap.platform.knowledge.jobs import explanation_payload, judge_attributes, judge_payload
from healthswap.platform.knowledge.reference_data import CategoriesFile, ReferenceDataLoader, RefJson
from healthswap.platform.llm.gateway import LlmError, PromptRegistry, PromptSpec
from healthswap.platform.llm.mock import _facts  # the fact parser of `hs llm eval` (MlData.evaluate_explanations)

REPO = Path(__file__).resolve().parents[2]
DEFAULT_DATA = REPO / "ml" / "data"
DEFAULT_OUT = REPO / "ml" / "finetune" / "out"
DEFAULT_CATEGORIES = REPO / "data" / "seed" / "kb" / "categories.json"

TASKS = ("t5", "t4", "t3")
SOURCES = {"t5": "explanation_facts.jsonl", "t4": "substitutability_pairs.jsonl", "t3": "category_examples.jsonl"}
BOM = codecs.BOM_UTF8
MAX_FILE_BYTES = 512 * 1024 * 1024  # Azure OpenAI: training files must be smaller than 512 MB
MIN_TRAINING_EXAMPLES = 10  # Azure OpenAI rejects jobs with fewer training examples
ROLES = ("system", "user", "assistant")

# The specs fix the delimiter and schema per task; the parameter (allowed codes, market, locale) does not change them.
_DELIMITERS = {
    "t3": PromptRegistry.category_classifier([]).delimiter,
    "t4": PromptRegistry.substitutability_judge("").delimiter,
    "t5": PromptRegistry.explanation_writer("").delimiter,
}

Problem = tuple[str, str]  # (code, message)


class DatasetError(Exception):
    """Missing or unusable input, or an output file that Azure OpenAI would refuse."""


@dataclass(frozen=True, slots=True)
class Example:
    system: str
    user: str
    assistant: str
    group: str  # related examples share a group and always land in the same split
    stratum: str  # the split keeps the share per stratum
    source: str | None = None  # T5: the explanation_facts.jsonl line (for t5_validation_facts.jsonl)

    def messages(self) -> list[dict[str, str]]:
        return [{"role": "system", "content": self.system}, {"role": "user", "content": self.user}, {"role": "assistant", "content": self.assistant}]

    def line(self) -> str:
        return json.dumps({"messages": self.messages()}, ensure_ascii=False, separators=(",", ":"))


@dataclass(slots=True)
class FileReport:
    path: Path
    examples: int
    tokens: int
    size: int


@dataclass(slots=True)
class TaskReport:
    task: str
    source: str
    candidates: int
    dropped: Counter[str] = field(default_factory=Counter)
    files: list[FileReport] = field(default_factory=list)


def assistant_json(value: Any) -> str:
    """Compact JSON with readable non-ASCII text (what a model writes; fewer tokens than \\uXXXX escapes)."""
    return netjson.serialize_hs(value)


def approx_tokens(messages: Iterable[dict[str, str]]) -> int:
    """Rough chat token count: about 4 UTF-8 bytes per token, plus 3 per message and 3 for the reply (chat format overhead)."""
    return 3 + sum(3 + -(-len(m["content"].encode("utf-8")) // 4) for m in messages)


def read_jsonl(path: Path) -> Iterator[tuple[int, str, Any]]:
    """(line number, text, value) per non-empty line; numbers with a fraction stay Decimal (jsonutil.loads), as the services read them."""
    if not path.is_file():
        raise DatasetError(f"{path} not found: run `hs ml export --out {path.parent}` first")
    with open(path, encoding="utf-8-sig") as f:
        for n, raw in enumerate(f, 1):
            text = raw.rstrip("\r\n")
            if not text.strip():
                continue
            try:
                yield n, text, jsonutil.loads(text)
            except ValueError as ex:
                raise DatasetError(f"{path.name}:{n}: not a JSON line ({ex})") from None


# ---------------------------------------------------------------------------------------------------------------------
# Examples per task
# ---------------------------------------------------------------------------------------------------------------------


def t5_examples(data_dir: Path, dropped: Counter[str]) -> list[Example]:
    """explanation_facts.jsonl -> T5: the facts are read like `hs llm eval` reads them; the target is the template rendering."""
    out: list[Example] = []
    for n, text, root in read_jsonl(data_dir / SOURCES["t5"]):
        try:
            facts = _facts(root["facts"])
            locale: str = root["locale"]
        except (KeyError, TypeError, ValueError, LlmError):
            dropped["source"] += 1
            continue
        basis = root["basis"] if "basis" in root else "PER_100G"
        if not facts or any(f is None for f in facts):
            dropped["source"] += 1
            continue
        rendered = ExplanationTemplates.render(facts, locale)
        template = root.get("template") if isinstance(root.get("template"), dict) else {}
        if template.get("short") != rendered.short or list(template.get("bullets") or []) != list(rendered.bullets):
            dropped["stale_template"] += 1  # exported by other template code: export again
            continue
        spec = PromptRegistry.explanation_writer(locale)
        target = {"short": rendered.short, "bullets": list(rendered.bullets), "factIdsUsed": list(rendered.fact_ids_used)}
        group = f"{root.get('market')}|{explanation_payload(facts, basis, '')}"  # one fact sheet, every locale
        out.append(Example(spec.system, spec.user_content(explanation_payload(facts, basis, locale)), assistant_json(target), group, locale, text))
    return out


_ISSUE_ORDER = ("DIFFERENT_USE", "DIFFERENT_FORM", "DIFFERENT_FLAVOUR_FAMILY", "DIETARY_CLASS_MISMATCH", "PACK_SIZE_MISMATCH")
_VEG = ("VEG", "VEGAN")


def _quantity(product: dict[str, Any]) -> NetQuantity | None:
    value, unit = product.get("netQuantity"), product.get("netUnit")
    if value is None or unit is None:
        return None
    return NetQuantity(value if isinstance(value, Decimal) else Decimal(value), unit)


def _show(value: Any) -> str:
    return "unknown" if value is None else str(value)


def t4_judgment(a: dict[str, Any], b: dict[str, Any], distance: int, label: int) -> dict[str, Any] | None:
    """The T4 target for products A -> B (products.jsonl rows). The blocking differences are the conditions of the gold label
    (MlData.gold_substitutable); flavour family and form at an approved cross-category map are listed but do not block.
    None when the attributes contradict the exported label (stale export)."""
    blocking: list[tuple[str, str]] = []
    minor: list[tuple[str, str]] = []
    if distance > 2:
        blocking.append(("DIFFERENT_USE", f"unrelated categories ({a['categoryCode']} vs {b['categoryCode']})"))
    if a.get("physicalState") != b.get("physicalState"):
        blocking.append(("DIFFERENT_FORM", f"{_show(a.get('physicalState')).lower()} vs {_show(b.get('physicalState')).lower()}"))
    if (a.get("dietClass") in _VEG) != (b.get("dietClass") in _VEG):
        blocking.append(("DIETARY_CLASS_MISMATCH", f"diet {_show(a.get('dietClass'))} vs {_show(b.get('dietClass'))}"))
    qa, qb = _quantity(a), _quantity(b)
    if qa is not None and qb is not None and qa.is_comparable_to(qb) and qa.value > 0:
        ratio = decimal_div(qb.value, qa.value)
        if ratio < Decimal("0.5") or ratio > 2:
            blocking.append(("PACK_SIZE_MISMATCH", f"pack {qb.value.normalize():f} {qb.unit} vs {qa.value.normalize():f} {qa.unit}"))
    ua, ub = a.get("useOccasion"), b.get("useOccasion")
    if ua is not None and ub is not None and ua != ub:
        blocking.append(("DIFFERENT_USE", f"use {ua} vs {ub}"))
    fa, fb = a.get("form"), b.get("form")
    if fa is not None and fb is not None and fa != fb:
        (blocking if distance < 2 else minor).append(("DIFFERENT_FORM", f"form {fa} vs {fb}"))
    la, lb = a.get("flavourFamily"), b.get("flavourFamily")
    if la is not None and lb is not None and la != lb:
        minor.append(("DIFFERENT_FLAVOUR_FAMILY", f"flavour {la} vs {lb}"))
    if (not blocking) != (label == 1):
        return None
    found = {issue for issue, _ in blocking + minor}
    issues = [issue for issue in _ISSUE_ORDER if issue in found] or ["NONE"]
    if blocking:
        score, rationale = 0.1, "Not a substitute: " + "; ".join(reason for _, reason in blocking) + "."
    elif minor:
        score, rationale = 0.8, "Acceptable substitute for the same use; minor difference: " + "; ".join(reason for _, reason in minor) + "."
    else:
        score, rationale = 0.9, "Same use and form, compatible dietary class and pack size."
    return {"score": score, "sameUseOccasion": "DIFFERENT_USE" not in found, "formMatch": "DIFFERENT_FORM" not in found, "issues": issues,
            "rationale": rationale}


def _judge_view(p: dict[str, Any]) -> dict[str, Any]:
    return judge_attributes(p["name"], p["categoryCode"], p.get("form"), p.get("flavourFamily"), p.get("useOccasion"), p.get("dietClass"),
                            p.get("netQuantity"))


def t4_examples(data_dir: Path, dropped: Counter[str]) -> list[Example]:
    """substitutability_pairs.jsonl (+ products.jsonl for the attributes the judge sees) -> T4."""
    products = {(p.get("market"), p.get("productId")): p for _, _, p in read_jsonl(data_dir / "products.jsonl")}
    out: list[Example] = []
    for _, _, pair in read_jsonl(data_dir / SOURCES["t4"]):
        market = pair.get("market")
        a, b = products.get((market, pair.get("from"))), products.get((market, pair.get("to")))
        features, label = pair.get("features"), pair.get("label")
        if a is None or b is None or not isinstance(features, list) or not features or label not in (0, 1) or isinstance(label, bool):
            dropped["source"] += 1
            continue
        judgment = t4_judgment(a, b, int(features[0]), label)
        if judgment is None:
            dropped["label_mismatch"] += 1
            continue
        spec = PromptRegistry.substitutability_judge(market)
        group = "|".join([market, *sorted((pair["from"], pair["to"]))])
        out.append(Example(spec.system, spec.user_content(judge_payload(_judge_view(a), _judge_view(b))), assistant_json(judgment), group, str(label)))
    return out


def t3_examples(data_dir: Path, dropped: Counter[str], categories_file: Path = DEFAULT_CATEGORIES) -> list[Example]:
    """category_examples.jsonl -> T3 with the allowed codes of a freshly seeded kb.category (seeding order)."""
    categories = RefJson.read(CategoriesFile, categories_file).categories
    allowed = allowed_categories(c.code for c in ReferenceDataLoader.category_seed_order(categories))
    names = {c.code: c.name for c in categories}
    spec = PromptRegistry.category_classifier(allowed)
    out: list[Example] = []
    for _, _, ex in read_jsonl(data_dir / SOURCES["t3"]):
        category, name = ex.get("category"), ex.get("name")
        if not isinstance(category, str) or not isinstance(name, str):
            dropped["source"] += 1
            continue
        target = {"categoryCode": category, "confidence": 0.95,
                  "rationale": f"The title and brand describe {names.get(category, category).lower()} ({category})."}
        user = spec.user_content(category_payload(name, ex.get("brand"), allowed))
        out.append(Example(spec.system, user, assistant_json(target), user, category))
    return out


# ---------------------------------------------------------------------------------------------------------------------
# Validation (also used by submit.py before uploading)
# ---------------------------------------------------------------------------------------------------------------------


def _template_parameter(make: Callable[[str], PromptSpec], system: str) -> str | None:
    """The parameter (market or locale) a PromptRegistry system prompt was made with, or None if `system` is not one of them."""
    prefix, _, suffix = make("\x00").system.partition("\x00")
    if len(system) < len(prefix) + len(suffix) or not system.startswith(prefix) or not system.endswith(suffix):
        return None
    value = system[len(prefix):len(system) - len(suffix)]
    return value if make(value).system == system else None


def _schema_errors(schema: dict[str, Any], value: Any) -> list[str]:
    return [f"{'/'.join(str(p) for p in e.absolute_path) or '$'}: {e.message}" for e in Draft202012Validator(schema).iter_errors(value)]


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_messages(task: str, messages: Any) -> list[Problem]:
    """Problems of one example, empty when it is valid: roles and contents, the production system prompt, the canonical runtime
    user content (rebuilt with the platform payload functions), the assistant JSON against the task's strict schema and the
    runtime acceptance checks (T3 confidence and T4 score in [0, 1], the T5 validator chain)."""
    if not isinstance(messages, list) or [m.get("role") if isinstance(m, dict) else None for m in messages] != list(ROLES):
        return [("roles", "expected exactly the messages system, user, assistant")]
    if any(set(m) != {"role", "content"} or not isinstance(m["content"], str) or not m["content"].strip() for m in messages):
        return [("content", "every message needs exactly role and a non-empty string content")]
    system, user, assistant = (m["content"] for m in messages)
    tag = _DELIMITERS[task]
    head, tail = f"<{tag}>", f"</{tag}>"
    if not (user.startswith(head) and user.endswith(tail)) or len(user) < len(head) + len(tail):
        return [("user", f"the user message must be the runtime payload inside {head}...{tail}")]
    try:
        payload = jsonutil.loads(user[len(head):len(user) - len(tail)])
    except ValueError:
        return [("user", "the payload is not JSON")]
    if not isinstance(payload, dict):
        return [("user", "the payload is not a JSON object")]

    facts: list[Any] = []
    locale = ""
    try:
        if task == "t3":
            allowed = payload.get("allowed")
            if not isinstance(allowed, list) or not all(isinstance(x, str) for x in allowed):
                return [("user", "the T3 payload needs an 'allowed' list of category codes")]
            spec = PromptRegistry.category_classifier(allowed)
            canonical = category_payload(payload.get("name"), payload.get("brand"), allowed)
        elif task == "t4":
            market = _template_parameter(PromptRegistry.substitutability_judge, system)
            if market is None:
                return [("system", "not the production T4 prompt (PromptRegistry.substitutability_judge)")]
            spec = PromptRegistry.substitutability_judge(market)
            a, b = payload.get("A"), payload.get("B")
            if not isinstance(a, dict) or not isinstance(b, dict):
                return [("user", "the T4 payload needs products A and B")]
            canonical = judge_payload(_judge_view_of(a), _judge_view_of(b))
        else:
            locale = payload.get("locale")
            if not isinstance(locale, str):
                return [("user", "the T5 payload needs a locale")]
            spec = PromptRegistry.explanation_writer(locale)
            facts = _facts(payload.get("facts"))
            if not facts or any(f is None for f in facts):
                return [("user", "the T5 payload needs facts")]
            canonical = explanation_payload(facts, payload.get("basis") if isinstance(payload.get("basis"), str) else "PER_100G", locale)
    except (KeyError, TypeError, ValueError, LlmError) as ex:
        return [("user", f"the payload cannot be read like the service reads it ({ex})")]
    if system != spec.system:
        return [("system", f"not the production {spec.task.value} prompt ({spec.version})")]
    if user != spec.user_content(canonical):
        return [("user", "not the canonical runtime user content (different field order, escaping or number format)")]

    try:
        output = json.loads(assistant)
    except json.JSONDecodeError as ex:
        return [("assistant", f"not JSON ({ex.msg})")]
    errors = _schema_errors(spec.schema, output)
    if errors:
        return [("schema", f"assistant does not match {spec.schema_name}: {errors[0]}")]
    if task == "t3" and not (_number(output["confidence"]) and 0 <= output["confidence"] <= 1):
        return [("range", "confidence must be between 0 and 1")]
    if task == "t4" and not (_number(output["score"]) and 0 <= output["score"] <= 1):
        return [("range", "score must be between 0 and 1")]
    if task == "t5":
        report = ExplanationValidator.validate(output["short"], output["bullets"], facts, locale)
        if not report.passed:
            failed = [check for check, passed in report.checks.items() if not passed]
            return [(f"validator.{failed[0] if failed else 'unknown'}", "explanation rejected by the validator chain: " + "; ".join(report.problems))]
    return []


def _judge_view_of(p: dict[str, Any]) -> dict[str, Any]:
    """The judge attributes of a parsed T4 payload product (missing members as null, like the runtime writes them)."""
    return judge_attributes(p.get("name"), p.get("category"), p.get("form"), p.get("flavourFamily"), p.get("useOccasion"), p.get("dietClass"),
                            p.get("netQuantity"))


def validate_file(task: str, path: Path, limit: int = 20) -> tuple[int, list[str]]:
    """(examples, problems) of a fine-tuning file: byte order mark, size below 512 MB, one JSON object per line, valid examples."""
    problems: list[str] = []
    data = path.read_bytes()
    if not data.startswith(BOM):
        problems.append(f"{path.name}: no UTF-8 byte order mark (Azure OpenAI requires UTF-8 with BOM)")
    if len(data) >= MAX_FILE_BYTES:
        problems.append(f"{path.name}: {len(data)} bytes; Azure OpenAI accepts files smaller than 512 MB")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as ex:
        return 0, [*problems, f"{path.name}: not UTF-8 ({ex.reason} at byte {ex.start})"]
    count = 0
    for n, line in enumerate(text.split("\n"), 1):
        if not line.strip():
            continue
        count += 1
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as ex:
            problems.append(f"{path.name}:{n}: not a JSON object ({ex.msg})")
            continue
        found = validate_messages(task, obj.get("messages")) if isinstance(obj, dict) and set(obj) == {"messages"} else [
            ("line", "each line must be an object with only a 'messages' array")]
        problems.extend(f"{path.name}:{n}: {code}: {message}" for code, message in found)
        if len(problems) >= limit:
            problems.append(f"{path.name}: stopped after {limit} problems")
            break
    if count == 0:
        problems.append(f"{path.name}: no examples")
    return count, problems


# ---------------------------------------------------------------------------------------------------------------------
# Split and output
# ---------------------------------------------------------------------------------------------------------------------


def split(examples: Sequence[Example], share: float, seed: int) -> tuple[list[Example], list[Example]]:
    """Deterministic grouped, stratified split: per stratum the groups are shuffled with Random(seed) and round(share x groups)
    of them go to validation; both outputs list the groups in one seeded order."""
    groups: dict[str, list[Example]] = {}
    for e in examples:
        groups.setdefault(e.group, []).append(e)
    strata: dict[str, list[str]] = {}
    for key, items in groups.items():
        strata.setdefault(items[0].stratum, []).append(key)
    rng = random.Random(seed)
    held_out: set[str] = set()
    for stratum in sorted(strata):
        keys = sorted(strata[stratum])
        rng.shuffle(keys)
        held_out.update(keys[:round(share * len(keys))])
    order = sorted(groups)
    rng.shuffle(order)
    train = [e for k in order if k not in held_out for e in groups[k]]
    validation = [e for k in order if k in held_out for e in groups[k]]
    return train, validation


def write_jsonl(path: Path, lines: Sequence[str], bom: bool = True) -> int:
    """Writes the lines (UTF-8, LF, optional byte order mark) atomically; refuses 512 MB or more. Returns the size in bytes."""
    encoded = [line.encode("utf-8") + b"\n" for line in lines]
    size = (len(BOM) if bom else 0) + sum(len(x) for x in encoded)
    if size >= MAX_FILE_BYTES:
        raise DatasetError(f"{path.name} would be {size / 2**20:.1f} MiB; Azure OpenAI accepts files smaller than 512 MB (use fewer examples)")
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "wb") as f:
        if bom:
            f.write(BOM)
        f.writelines(encoded)
    os.replace(tmp, path)
    return size


def build_task(task: str, data_dir: Path, out_dir: Path, share: float, seed: int, categories_file: Path = DEFAULT_CATEGORIES) -> TaskReport:
    report = TaskReport(task, SOURCES[task], 0)
    if task == "t5":
        candidates = t5_examples(data_dir, report.dropped)
    elif task == "t4":
        candidates = t4_examples(data_dir, report.dropped)
    else:
        candidates = t3_examples(data_dir, report.dropped, categories_file)
    report.candidates = len(candidates) + sum(report.dropped.values())
    valid: list[Example] = []
    for e in candidates:
        problems = validate_messages(task, e.messages())
        if problems:
            report.dropped[problems[0][0]] += 1
            continue
        try:
            e.line().encode("utf-8")
        except UnicodeEncodeError:
            report.dropped["encoding"] += 1  # lone surrogates cannot be written as UTF-8
            continue
        valid.append(e)
    if not valid:
        raise DatasetError(f"{task}: no valid examples in {SOURCES[task]} (dropped: {dict(report.dropped)})")
    train, validation = split(valid, share, seed)
    outputs = [(out_dir / f"{task}_train.jsonl", train), (out_dir / f"{task}_validation.jsonl", validation)]
    for path, examples in outputs:
        if not examples:
            path.unlink(missing_ok=True)
            continue
        size = write_jsonl(path, [e.line() for e in examples])
        report.files.append(FileReport(path, len(examples), sum(approx_tokens(e.messages()) for e in examples), size))
    if task == "t5":
        facts_path = out_dir / "t5_validation_facts.jsonl"
        lines = [e.source for e in validation if e.source is not None]
        if lines:
            report.files.append(FileReport(facts_path, len(lines), 0, write_jsonl(facts_path, lines, bom=False)))
        else:
            facts_path.unlink(missing_ok=True)
    return report


def _rel(path: Path) -> str:
    """The path relative to the working directory when it is below it (shorter output), else as given."""
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return str(path)


def _print_report(r: TaskReport) -> None:
    dropped = sum(r.dropped.values())
    reasons = ", ".join(f"{k} {v}" for k, v in sorted(r.dropped.items(), key=lambda kv: (-kv[1], kv[0])))
    print(f"{r.task}: {r.candidates} candidate examples from {r.source}, {r.candidates - dropped} valid, dropped {dropped}"
          + (f" ({reasons})" if reasons else ""))
    for f in r.files:
        if f.path.name.endswith("_facts.jsonl"):
            print(f"    {f.path.name:<28} {f.examples:>6} held-out export lines (hs llm eval --input {_rel(f.path)})")
            continue
        print(f"    {f.path.name:<28} {f.examples:>6} examples  ~{f.tokens:>9,} tokens  {f.size / 1024:>9.1f} KiB")
        if f.path.name.endswith("_train.jsonl") and f.examples < MIN_TRAINING_EXAMPLES:
            print(f"    warning: Azure OpenAI needs at least {MIN_TRAINING_EXAMPLES} training examples")
    if not any(f.path.name.endswith("_validation.jsonl") for f in r.files):
        print("    (no validation file: --validation-share is 0 or too few examples)")


def _tasks(value: str) -> list[str]:
    tasks = [t.strip().lower() for t in value.split(",") if t.strip()]
    unknown = [t for t in tasks if t not in TASKS]
    if unknown or not tasks:
        raise argparse.ArgumentTypeError(f"unknown task(s) {', '.join(unknown) or value!r}; choose from {', '.join(TASKS)}")
    return list(dict.fromkeys(tasks))


def _share(value: str) -> float:
    share = float(value)
    if not 0 <= share <= 0.5:
        raise argparse.ArgumentTypeError("the validation share must be between 0 and 0.5")
    return share


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ml/finetune/build_datasets.py", description="Build Azure OpenAI fine-tuning datasets (T3, T4, T5).")
    p.add_argument("--data", type=Path, default=DEFAULT_DATA, help="hs ml export output (default: ml/data)")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output directory (default: ml/finetune/out)")
    p.add_argument("--tasks", type=_tasks, default=list(TASKS), help="comma-separated tasks (default: t5,t4,t3)")
    p.add_argument("--validation-share", type=_share, default=0.2, help="share of example groups held out for validation (default: 0.2)")
    p.add_argument("--seed", type=int, default=7, help="split seed (default: 7)")
    p.add_argument("--categories", type=Path, default=DEFAULT_CATEGORIES, help="category taxonomy for the T3 enum (default: data/seed/kb/categories.json)")
    args = p.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    failed = 0
    for task in args.tasks:
        try:
            _print_report(build_task(task, args.data, args.out, args.validation_share, args.seed, args.categories))
        except (DatasetError, OSError) as ex:
            print(f"build_datasets {task}: error: {ex}", file=sys.stderr)
            failed += 1
    print("Token counts are estimates (about 4 bytes per token); the service reports the billed tokens.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
