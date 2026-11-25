"""ml/finetune: the dataset builder (format, prompts equal to the runtime ones, validation, split) and the submit tool (dry run and a
mocked Azure OpenAI v1 REST service; nothing leaves the machine)."""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from decimal import Decimal
from itertools import permutations
from pathlib import Path
from typing import Any

import httpx
import pytest
from jsonschema import Draft202012Validator

import build_datasets as bd  # ml/finetune/build_datasets.py, put on sys.path by tests/ml/conftest.py
import submit  # ml/finetune/submit.py

from healthswap.domain.recommendation.explanations import ExplanationTemplates, Fact
from healthswap.domain.recommendation.ranking import Substitutability
from healthswap.platform import jsonutil, netjson
from healthswap.platform.knowledge.ingestor import allowed_categories, category_payload
from healthswap.platform.knowledge.jobs import ExplanationPregen, _fact_json, judge_attributes, judge_payload
from healthswap.platform.knowledge.reference_data import CategoriesFile, ReferenceDataLoader, RefJson
from healthswap.platform.llm.gateway import LlmResult, PromptRegistry
from healthswap.platform.llm.mock import _facts

LOCALES = {"IN": ("en-IN", "hi-IN"), "US": ("en-US", "es-US")}
SHEETS = 12

# productId, name, brand, category, form, flavour family, use occasion, diet class, net quantity, unit, physical state
PRODUCTS = [
    ("p1", "Swaad Masala Noodles 280 g", "Swaad", "noodles.instant", "NOODLE", "MASALA", "MEAL", "VEG", Decimal("280"), "g", "SOLID"),
    ("p2", "Swaad Tomato Noodles 300 g", "Swaad", "noodles.instant", "NOODLE", "TOMATO", "MEAL", "VEG", Decimal("300.0"), "g", "SOLID"),
    ("p3", "Kwik Cup Noodles 70 g", "Kwik", "noodles.instant", "CUP", "MASALA", "MEAL", "VEGAN", Decimal("70"), "g", "SOLID"),
    ("p4", "Crunchy Salted Chips 50 g", "Crunchy", "snacks.chips", "CHIP", "SALTED", "SNACK", "VEG", Decimal("50"), "g", "SOLID"),
    ("p5", "Fizz Cola 750 ml", None, "beverages.carbonated.soft-drinks", "BOTTLE", "COLA", "BEVERAGE", "VEGAN", Decimal("750"), "ml", "LIQUID"),
]
FIELDS = ("productId", "name", "brand", "categoryCode", "form", "flavourFamily", "useOccasion", "dietClass", "netQuantity", "netUnit", "physicalState")


def _rel(a: Decimal, b: Decimal) -> Decimal:
    return ((b - a) / a).quantize(Decimal("0.0001"))


def fact_sheet(i: int) -> list[Fact]:
    """Facts like FactSheets.select returns them: a sugar reduction, sometimes a sodium reduction and a must-disclose sweetener."""
    s_from, s_to = Decimal(f"{20 + i}.0"), Decimal(f"{6 + i % 7}.5")
    rel = _rel(s_from, s_to)
    facts = [Fact(id="f1", kind="NUTRIENT", nutrient="totalSugarsG", from_=s_from, to=s_to, unit="g", rel_change=rel, direction="BETTER",
                  impact=min(Decimal(3), abs(rel)), claim_eligible=abs(rel) >= Decimal("0.25"))]
    if i % 2:
        n_from, n_to = Decimal(700 + 10 * i), Decimal(520 + 10 * i)
        rel = _rel(n_from, n_to)
        facts.append(Fact(id="f2", kind="NUTRIENT", nutrient="sodiumMg", from_=n_from, to=n_to, unit="mg", rel_change=rel, direction="BETTER",
                          impact=Decimal("0.9") * abs(rel), claim_eligible=abs(rel) >= Decimal("0.25")))
    if i % 3 == 0:
        facts.append(Fact(id="f3", kind="ADDITIVE_INTRODUCED", nutrient="sweeteners", codes=("951",), direction="WORSE", must_disclose=True))
    return facts


def explanation_line(market: str, locale: str, facts: list[Fact], short: str | None = None) -> str:
    """An explanation_facts.jsonl line as MlData.export writes it."""
    rendered = ExplanationTemplates.render(facts, locale)
    return netjson.serialize_hs({
        "market": market, "locale": locale, "basis": "PER_100G", "facts": [_fact_json(f) for f in facts],
        "template": {"short": rendered.short if short is None else short, "bullets": list(rendered.bullets)},
    })


def _write(path: Path, lines: list[str]) -> None:
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")


@pytest.fixture()
def exported(tmp_path: Path) -> Path:
    """`hs ml export` output with known good and bad lines."""
    data = tmp_path / "data"
    data.mkdir()
    t5 = [explanation_line(market, locale, fact_sheet(i)) for i in range(SHEETS) for market in ("IN" if i % 2 == 0 else "US",) for locale in LOCALES[market]]
    t5.append(explanation_line("IN", "en-IN", fact_sheet(0), short="Copy from an older template"))  # stale_template
    t5.append('{"market":"IN","locale":"en-IN","basis":"PER_100G","facts":"not a list"}')  # source
    _write(data / "explanation_facts.jsonl", t5)

    products = [dict(zip(FIELDS, p, strict=True)) | {"market": "IN"} for p in PRODUCTS]
    _write(data / "products.jsonl", [netjson.serialize_hs(p) for p in products])
    pairs = []
    for a, b in permutations(products, 2):
        distance = Substitutability.category_distance(a["categoryCode"], b["categoryCode"], False)
        label = int({a["productId"], b["productId"]} == {"p1", "p2"})
        pairs.append({"market": "IN", "from": a["productId"], "to": b["productId"], "features": [float(distance)] + [0.5] * 9, "label": label})
    pairs.append({"market": "IN", "from": "p1", "to": "p3", "features": [0.0] + [0.5] * 9, "label": 1})  # label_mismatch: pack 70 g vs 280 g
    pairs.append({"market": "IN", "from": "p1", "to": "p9", "features": [0.0] + [0.5] * 9, "label": 0})  # source: unknown product
    _write(data / "substitutability_pairs.jsonl", [json.dumps(p) for p in pairs])

    examples = [{"name": p["name"], "brand": p["brand"], "market": "IN", "category": p["categoryCode"]} for p in products]
    examples.append({"name": "Tiny Rice Cereal 300 g", "brand": "Tiny", "market": "IN", "category": "infant-foods"})  # schema: not offered to T3
    examples.append({"brand": "Nameless", "market": "IN", "category": "snacks.chips"})  # source
    _write(data / "category_examples.jsonl", [netjson.serialize_hs(e) for e in examples])
    return data


@pytest.fixture()
def built(exported: Path, tmp_path: Path) -> Path:
    out = tmp_path / "out"
    assert bd.main(["--data", str(exported), "--out", str(out)]) == 0
    return out


def _examples(path: Path) -> list[list[dict[str, str]]]:
    return [json.loads(line)["messages"] for line in path.read_bytes().decode("utf-8-sig").splitlines() if line.strip()]


def _payload(user: str) -> Any:
    return jsonutil.loads(user[user.index(">") + 1:user.rindex("<")])


# ---------------------------------------------------------------------------------------------------------------------
# build_datasets.py
# ---------------------------------------------------------------------------------------------------------------------


def test_build_writes_valid_chat_files(exported: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "out"
    assert bd.main(["--data", str(exported), "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "t5: 26 candidate examples from explanation_facts.jsonl, 24 valid, dropped 2 (source 1, stale_template 1)" in printed
    assert "t4: 22 candidate examples from substitutability_pairs.jsonl, 20 valid, dropped 2 (label_mismatch 1, source 1)" in printed
    assert "t3: 7 candidate examples from category_examples.jsonl, 5 valid, dropped 2 (schema 1, source 1)" in printed

    expected = {"t5": (20, 4), "t4": (16, 4), "t3": (4, 1)}
    specs = {"t5": PromptRegistry.explanation_writer, "t4": PromptRegistry.substitutability_judge}
    for task, counts in expected.items():
        for path, count in zip((out / f"{task}_train.jsonl", out / f"{task}_validation.jsonl"), counts, strict=True):
            assert path.read_bytes().startswith(bd.BOM), path.name
            assert bd.validate_file(task, path) == (count, [])
            for messages in _examples(path):
                assert [m["role"] for m in messages] == ["system", "user", "assistant"]
                payload = _payload(messages[1]["content"])
                if task == "t3":
                    spec = PromptRegistry.category_classifier(payload["allowed"])
                else:
                    spec = specs[task](payload["locale"] if task == "t5" else "IN")
                assert messages[0]["content"] == spec.system
                assert not list(Draft202012Validator(spec.schema).iter_errors(json.loads(messages[2]["content"])))

    # The held-out T5 export lines, for `hs llm eval --input`: the validation examples' sources, without a byte order mark.
    facts_lines = (out / "t5_validation_facts.jsonl").read_bytes()
    assert not facts_lines.startswith(bd.BOM) and len(facts_lines.decode("utf-8").splitlines()) == 4
    held_out = {json.dumps(_payload(m[1]["content"])["facts"], default=str) for m in _examples(out / "t5_validation.jsonl")}
    assert {json.dumps(jsonutil.loads(line)["facts"], default=str) for line in facts_lines.decode("utf-8").splitlines()} == held_out


def test_t5_examples_are_the_runtime_request(built: Path) -> None:
    class RecordingLlm:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, str]] = []

        def run(self, spec: Any, user: str) -> LlmResult:
            self.calls.append((spec, user))
            return LlmResult(False, None, "recorder", spec.version, 0, 0, 0, "not sent", True)

        def audit(self, *args: Any) -> None:
            raise AssertionError("cached results are not audited")

    llm = RecordingLlm()
    pregen = ExplanationPregen(None, None, llm, None, None)  # type: ignore[arg-type]  (polish only needs the LLM)
    examples = _examples(built / "t5_train.jsonl") + _examples(built / "t5_validation.jsonl")
    assert {_payload(m[1]["content"])["locale"] for m in examples} == {"en-IN", "hi-IN", "en-US", "es-US"}
    for system, user, assistant in ((m[0]["content"], m[1]["content"], m[2]["content"]) for m in examples):
        payload = _payload(user)
        facts = _facts(payload["facts"])
        pregen.polish(facts, payload["basis"], payload["locale"])
        spec, sent = llm.calls[-1]
        assert (system, user) == (spec.system, sent)  # byte-identical to what ExplanationPregen sends
        rendered = ExplanationTemplates.render(facts, payload["locale"])
        assert json.loads(assistant) == {"short": rendered.short, "bullets": list(rendered.bullets), "factIdsUsed": list(rendered.fact_ids_used)}


def test_t4_and_t3_examples_use_the_runtime_payloads(built: Path) -> None:
    products = {p[0]: p for p in PRODUCTS}
    targets: dict[tuple[str, str], dict[str, Any]] = {}
    for messages in _examples(built / "t4_train.jsonl") + _examples(built / "t4_validation.jsonl"):
        payload = _payload(messages[1]["content"])
        ids = [next(k for k, p in products.items() if p[1] == payload[x]["name"]) for x in ("A", "B")]
        views = [judge_attributes(*(products[i][j] for j in (1, 3, 4, 5, 6, 7, 8))) for i in ids]  # what SwapGraphBuilder._judge sends
        assert messages[1]["content"] == PromptRegistry.substitutability_judge("IN").user_content(judge_payload(*views))
        targets[(ids[0], ids[1])] = json.loads(messages[2]["content"])
    assert len(targets) == 20
    assert targets[("p1", "p2")] == {"score": 0.8, "sameUseOccasion": True, "formMatch": True, "issues": ["DIFFERENT_FLAVOUR_FAMILY"],
                                     "rationale": "Acceptable substitute for the same use; minor difference: flavour MASALA vs TOMATO."}
    assert targets[("p1", "p3")]["score"] == 0.1 and targets[("p1", "p3")]["issues"] == ["DIFFERENT_FORM", "PACK_SIZE_MISMATCH"]
    assert targets[("p4", "p5")]["issues"][0] == "DIFFERENT_USE" and targets[("p4", "p5")]["formMatch"] is False
    assert all((t["score"] > 0.5) == ({a, b} == {"p1", "p2"}) for (a, b), t in targets.items())

    categories = RefJson.read(CategoriesFile, bd.DEFAULT_CATEGORIES).categories
    allowed = allowed_categories(c.code for c in ReferenceDataLoader.category_seed_order(categories))
    assert "infant-foods" not in allowed and "noodles" not in allowed and "noodles.instant" in allowed
    spec = PromptRegistry.category_classifier(allowed)
    seen = set()
    for messages in _examples(built / "t3_train.jsonl") + _examples(built / "t3_validation.jsonl"):
        payload = _payload(messages[1]["content"])
        product = next(p for p in PRODUCTS if p[1] == payload["name"])
        assert messages[1]["content"] == spec.user_content(category_payload(product[1], product[2], allowed))
        target = json.loads(messages[2]["content"])
        assert target["categoryCode"] == product[3] and target["confidence"] == 0.95
        seen.add(product[0])
    assert seen == set(products)


def test_split_is_deterministic_and_grouped(exported: Path, tmp_path: Path) -> None:
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    for out, seed in ((a, "7"), (b, "7"), (c, "8")):
        assert bd.main(["--data", str(exported), "--out", str(out), "--seed", seed, "--tasks", "t4,t5"]) == 0
    names = sorted(p.name for p in a.iterdir())
    assert names == sorted(p.name for p in b.iterdir()) and all((a / n).read_bytes() == (b / n).read_bytes() for n in names)
    assert any((a / n).read_bytes() != (c / n).read_bytes() for n in names)

    # Both directions of a T4 pair, and every locale of a T5 fact sheet, are on the same side.
    side: dict[str, set[str]] = {}
    for name in ("t4_train.jsonl", "t4_validation.jsonl", "t5_train.jsonl", "t5_validation.jsonl"):
        for messages in _examples(a / name):
            payload = _payload(messages[1]["content"])
            key = "|".join(sorted((payload["A"]["name"], payload["B"]["name"]))) if "A" in payload else json.dumps(payload["facts"], default=str)
            side.setdefault(key, set()).add(name.split("_")[1])
    assert len(side) == 10 + SHEETS and all(len(s) == 1 for s in side.values())


def test_validation_rejects_off_spec_examples(built: Path) -> None:
    t5 = _examples(built / "t5_train.jsonl")[0]
    assert bd.validate_messages("t5", t5) == []

    def changed(messages: list[dict[str, str]], index: int, content: str) -> list[dict[str, str]]:
        return [dict(m, content=content) if i == index else m for i, m in enumerate(messages)]

    payload = _payload(t5[1]["content"])
    target = json.loads(t5[2]["content"])
    assert bd.validate_messages("t5", t5[1:])[0][0] == "roles"
    assert bd.validate_messages("t5", changed(t5, 0, "You are a helpful assistant."))[0][0] == "system"
    assert bd.validate_messages("t5", changed(t5, 1, "<facts>" + json.dumps(payload, default=str, indent=1) + "</facts>"))[0][0] == "user"
    assert bd.validate_messages("t5", changed(t5, 1, json.dumps(payload, default=str)))[0][0] == "user"  # no delimiter
    assert bd.validate_messages("t5", changed(t5, 2, json.dumps(target | {"extra": 1})))[0][0] == "schema"
    claim = target | {"short": "Guilt-free pick with 99% less sugar"}
    assert bd.validate_messages("t5", changed(t5, 2, json.dumps(claim, ensure_ascii=False)))[0][0].startswith("validator.")

    t3 = _examples(built / "t3_train.jsonl")[0]
    assert bd.validate_messages("t3", t3) == []
    assert bd.validate_messages("t3", changed(t3, 2, json.dumps(json.loads(t3[2]["content"]) | {"confidence": 1.5})))[0][0] == "range"
    assert bd.validate_messages("t3", changed(t3, 2, json.dumps(json.loads(t3[2]["content"]) | {"categoryCode": "infant-foods"})))[0][0] == "schema"
    t4 = _examples(built / "t4_train.jsonl")[0]
    assert bd.validate_messages("t4", t4) == []
    assert bd.validate_messages("t4", changed(t4, 2, json.dumps(json.loads(t4[2]["content"]) | {"score": -0.2})))[0][0] == "range"
    assert bd.validate_messages("t4", changed(t4, 0, PromptRegistry.substitutability_judge("US").system)) == []  # any market's prompt


def test_file_checks(tmp_path: Path, built: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    no_bom = tmp_path / "no_bom.jsonl"
    no_bom.write_bytes((built / "t4_train.jsonl").read_bytes()[len(bd.BOM):])
    count, problems = bd.validate_file("t4", no_bom)
    assert count == 16 and problems == ["no_bom.jsonl: no UTF-8 byte order mark (Azure OpenAI requires UTF-8 with BOM)"]

    monkeypatch.setattr(bd, "MAX_FILE_BYTES", 100)
    with pytest.raises(bd.DatasetError, match="smaller than 512 MB"):
        bd.write_jsonl(tmp_path / "big.jsonl", ['{"messages":[]}'] * 10)
    assert not (tmp_path / "big.jsonl").exists()

    with pytest.raises(bd.DatasetError, match="hs ml export"):
        bd.build_task("t5", tmp_path / "missing", tmp_path, 0.2, 7)


# ---------------------------------------------------------------------------------------------------------------------
# submit.py
# ---------------------------------------------------------------------------------------------------------------------

ENDPOINT = "https://unit-test.openai.azure.com"


def test_dry_run_sends_nothing(built: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a dry run must not create an HTTP client or request a token")

    monkeypatch.setattr(submit.httpx, "Client", forbidden)
    monkeypatch.setattr(submit, "az_access_token", forbidden)
    capsys.readouterr()
    env = {"AZURE_OPENAI_ENDPOINT": ENDPOINT + "/openai/v1/", "AZURE_OPENAI_API_KEY": "key-SECRET-123"}
    assert submit.main(["--tasks", "t5,t4", "--datasets", str(built), "--auth", "key", "--n-epochs", "3"], environ=env) == 0
    out = capsys.readouterr().out
    assert "t5: t5_train.jsonl 20 examples, t5_validation.jsonl 4 examples: valid" in out and "DRY RUN" in out
    assert f"POST {ENDPOINT}/openai/v1/files" in out and f"POST {ENDPOINT}/openai/v1/fine_tuning/jobs" in out
    assert "form field purpose=fine-tune" in out and '"trainingType": "Standard"' in out and '"n_epochs": 3' in out
    assert '"suffix": "hs-t5"' in out and '"suffix": "hs-t4"' in out and '"validation_file": "<validation file id>"' in out
    assert "api-key: <redacted>" in out and "key-SECRET-123" not in out

    assert submit.main(["--datasets", str(built)], environ={}) == 0  # Entra ID without a token: nothing requested in a dry run
    out = capsys.readouterr().out
    assert "Authorization: Bearer <redacted>" in out and submit.PLACEHOLDER_ENDPOINT in out


def test_submit_refuses_invalid_or_small_files(built: Path, capsys: pytest.CaptureFixture[str]) -> None:
    capsys.readouterr()
    assert submit.main(["--tasks", "t3", "--datasets", str(built)], environ={}) == 1
    assert "Azure OpenAI needs at least 10 training examples" in capsys.readouterr().err

    path = built / "t5_train.jsonl"
    lines = path.read_bytes().decode("utf-8-sig").splitlines()
    lines[3] = lines[3].replace("You write short", "You write long")
    path.write_bytes(bd.BOM + ("\n".join(lines) + "\n").encode("utf-8"))
    assert submit.main(["--datasets", str(built), "--submit"], environ={"AZURE_OPENAI_ENDPOINT": ENDPOINT}) == 1
    err = capsys.readouterr().err
    assert "t5_train.jsonl:4: system: not the production T5 prompt" in err


class FakeAzureOpenAI:
    """The v1 files and fine-tuning endpoints, as httpx.MockTransport handler."""

    def __init__(self, job_error: bool = False) -> None:
        self.requests: list[httpx.Request] = []
        self.uploads: dict[str, bytes] = {}
        self.file_polls: Counter[str] = Counter()
        self.job_polls = 0
        self.job_body: dict[str, Any] | None = None
        self.job_error = job_error

    def __call__(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        method, path = request.method, request.url.path
        if (method, path) == ("POST", "/openai/v1/files"):
            name = "t5_train.jsonl" if b'filename="t5_train.jsonl"' in request.content else "t5_validation.jsonl"
            file_id = f"file-{name.split('_')[1].split('.')[0]}"
            self.uploads[file_id] = request.content
            return httpx.Response(200, json={"id": file_id, "object": "file", "purpose": "fine-tune", "filename": name, "status": "pending"})
        if method == "GET" and path.startswith("/openai/v1/files/"):
            file_id = path.rsplit("/", 1)[1]
            self.file_polls[file_id] += 1
            return httpx.Response(200, json={"id": file_id, "status": "running" if self.file_polls[file_id] == 1 else "processed"})
        if (method, path) == ("POST", "/openai/v1/fine_tuning/jobs"):
            self.job_body = json.loads(request.content)
            if self.job_error:
                return httpx.Response(400, json={"error": {"code": "invalidPayload", "message": "The model is not available for fine-tuning."}})
            return httpx.Response(201, json={"id": "ftjob-1", "object": "fine_tuning.job", "status": "validating_files"})
        if (method, path) == ("GET", "/openai/v1/fine_tuning/jobs/ftjob-1"):
            self.job_polls += 1
            status = ("queued", "running", "succeeded")[min(self.job_polls, 3) - 1]
            body: dict[str, Any] = {"id": "ftjob-1", "status": status}
            if status == "succeeded":
                body |= {"fine_tuned_model": "gpt-4.1-mini-2025-04-14.ft-0123abcd-hs-explainer", "trained_tokens": 4321}
            return httpx.Response(200, json=body)
        if (method, path) == ("GET", "/openai/v1/fine_tuning/jobs/ftjob-1/events"):
            events = [{"id": "ftevent-2", "created_at": 1767225660, "level": "info", "message": "Training started."},
                      {"id": "ftevent-1", "created_at": 1767225600, "level": "info", "message": "Validating training file."}]
            return httpx.Response(200, json={"object": "list", "data": events[2 - min(self.job_polls, 2):], "has_more": False})
        return httpx.Response(404, json={"error": {"code": "NotFound", "message": f"{method} {path}"}})


def test_submit_and_wait(built: Path, capsys: pytest.CaptureFixture[str]) -> None:
    service = FakeAzureOpenAI()
    sleeps: list[float] = []
    capsys.readouterr()
    argv = ["--datasets", str(built), "--endpoint", ENDPOINT + "/", "--submit", "--wait", "--training-type", "GlobalStandard",
            "--learning-rate-multiplier", "0.5", "--n-epochs", "2", "--suffix", "hs-explainer", "--seed", "11"]
    code = submit.main(argv, transport=httpx.MockTransport(service), sleep=sleeps.append, environ={"AZURE_OPENAI_BEARER_TOKEN": "tok-SECRET-9"})
    captured = capsys.readouterr()
    assert code == 0, captured.err

    assert all(r.headers["authorization"] == "Bearer tok-SECRET-9" and "api-key" not in r.headers for r in service.requests)
    assert all(str(r.url).startswith(ENDPOINT + "/openai/v1/") for r in service.requests)
    for file_id, name in (("file-train", "t5_train.jsonl"), ("file-validation", "t5_validation.jsonl")):
        body = service.uploads[file_id]
        assert b'name="purpose"\r\n\r\nfine-tune\r\n' in body
        assert f'filename="{name}"\r\nContent-Type: application/json\r\n\r\n'.encode() + bd.BOM + (built / name).read_bytes()[len(bd.BOM):] in body
    assert service.file_polls == {"file-train": 2, "file-validation": 2}
    assert service.job_body == {
        "model": "gpt-4.1-mini-2025-04-14", "training_file": "file-train", "validation_file": "file-validation", "trainingType": "GlobalStandard",
        "method": {"type": "supervised", "supervised": {"hyperparameters": {"batch_size": "auto", "learning_rate_multiplier": 0.5, "n_epochs": 2}}},
        "suffix": "hs-explainer", "seed": 11,
    }
    out = captured.out
    assert out.count("Validating training file.") == 1 and out.index("Validating training file.") < out.index("Training started.")
    assert "t5: job ftjob-1 is queued" in out and "t5: job ftjob-1 is succeeded" in out
    assert "fine-tuned model gpt-4.1-mini-2025-04-14.ft-0123abcd-hs-explainer (trained tokens: 4321)" in out
    assert "--deployment-name hs-t5-explainer --model-name gpt-4.1-mini-2025-04-14.ft-0123abcd-hs-explainer" in out
    assert f"HS_LLM__ENDPOINT={ENDPOINT}\n" in out and "HS_LLM__DEPLOYMENTS__T5=hs-t5-explainer" in out
    assert "hs llm eval --input ml/finetune/out/t5_validation_facts.jsonl" in out
    assert "tok-SECRET-9" not in out + captured.err
    assert sleeps == [submit.FILE_POLL_SECONDS, submit.FILE_POLL_SECONDS, 30.0, 30.0]


def test_submit_reports_api_errors_without_secrets(built: Path, capsys: pytest.CaptureFixture[str]) -> None:
    service = FakeAzureOpenAI(job_error=True)
    capsys.readouterr()
    env = {"AZURE_OPENAI_ENDPOINT": ENDPOINT, "AZURE_OPENAI_API_KEY": "key-SECRET-123"}
    code = submit.main(["--datasets", str(built), "--auth", "key", "--submit"], transport=httpx.MockTransport(service), sleep=lambda _: None, environ=env)
    captured = capsys.readouterr()
    assert code == 1
    assert all(r.headers["api-key"] == "key-SECRET-123" and "authorization" not in r.headers for r in service.requests)
    assert f"POST {ENDPOINT}/openai/v1/fine_tuning/jobs returned HTTP 400 Bad Request: invalidPayload The model is not available" in captured.err
    assert "check --model, --training-type" in captured.err and "key-SECRET-123" not in captured.out + captured.err


def test_entra_token_from_azure_cli(built: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    real_az_access_token = submit.az_access_token
    monkeypatch.setattr(submit, "az_access_token", lambda run=None: "az-SECRET-7")
    service = FakeAzureOpenAI()
    capsys.readouterr()
    assert submit.main(["--datasets", str(built), "--submit"], transport=httpx.MockTransport(service), sleep=lambda _: None,
                       environ={"AZURE_OPENAI_ENDPOINT": ENDPOINT}) == 0
    captured = capsys.readouterr()
    assert {r.headers["authorization"] for r in service.requests} == {"Bearer az-SECRET-7"}
    assert "t5: created fine-tuning job ftjob-1 (validating_files)" in captured.out and "--wait" in captured.out
    assert service.job_polls == 0 and "az-SECRET-7" not in captured.out + captured.err

    monkeypatch.setattr(submit, "az_access_token", real_az_access_token)
    monkeypatch.setattr(submit.shutil, "which", lambda name: None)
    with pytest.raises(submit.SubmitError, match="not installed"):
        submit.credential("entra", {}, fetch=True)
    monkeypatch.setattr(submit.shutil, "which", lambda name: "az")

    def failing(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert cmd[1:5] == ["account", "get-access-token", "--resource", "https://cognitiveservices.azure.com"]
        return subprocess.CompletedProcess(cmd, 1, "", "ERROR: Please run 'az login' to setup account.\n")

    with pytest.raises(submit.SubmitError, match="run `az login`"):
        submit.credential("entra", {}, fetch=True, run=failing)


def test_endpoint_and_arguments() -> None:
    assert submit.resolve_endpoint("https://x.openai.azure.com/openai/v1/", {}) == "https://x.openai.azure.com"
    assert submit.resolve_endpoint(None, {"AZURE_OPENAI_ENDPOINT": "https://y.services.ai.azure.com/"}) == "https://y.services.ai.azure.com"
    assert submit.resolve_endpoint(None, {}) is None
    for bad in ("http://x.openai.azure.com", "https://user:pw@x.openai.azure.com", "https://x.openai.azure.com/?api-key=1"):
        with pytest.raises(submit.SubmitError):
            submit.resolve_endpoint(bad, {})
    assert submit.suffix_for("t4", None, ["t4"]) == "hs-t4" and submit.suffix_for("t4", "exp", ["t5", "t4"]) == "exp-t4"
    with pytest.raises(submit.SubmitError, match="64"):
        submit.suffix_for("t5", "x" * 65, ["t5"])
    with pytest.raises(SystemExit):
        submit.main(["--n-epochs", "0"], environ={})
