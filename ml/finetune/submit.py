"""Supervised fine-tuning on Azure OpenAI (v1 REST API) for the datasets written by build_datasets.py.

    python ml/finetune/submit.py [--tasks t5] [--datasets ml/finetune/out] [--endpoint URL] [--auth entra|key]
        [--model gpt-4.1-mini-2025-04-14|gpt-4.1-nano-2025-04-14] [--training-type Standard|GlobalStandard|Developer]
        [--batch-size auto|N] [--learning-rate-multiplier auto|X] [--n-epochs auto|N] [--suffix hs-t5] [--seed 7]
        [--submit [--wait] [--poll-seconds 30]]

DRY RUN BY DEFAULT: without --submit the files are validated (UTF-8 byte order mark, one JSON object per line, roles, the
production system prompt, the canonical runtime user content, the assistant JSON against the task schema and the runtime checks)
and the HTTP requests are printed with the credentials redacted. Nothing is sent and no token is requested.

--submit, per task (Microsoft Learn, "Fine-tune a model with supervised fine-tuning", REST):
  POST {endpoint}/openai/v1/files              multipart: purpose=fine-tune, file=<task>_train.jsonl / <task>_validation.jsonl
  GET  {endpoint}/openai/v1/files/{id}         until the status is "processed"
  POST {endpoint}/openai/v1/fine_tuning/jobs   {"model", "training_file", "validation_file", "trainingType", "method": {"type":
                                               "supervised", "supervised": {"hyperparameters": {...}}}, "suffix", "seed"}
--wait follows the jobs (GET {endpoint}/openai/v1/fine_tuning/jobs/{id} and .../events) until they succeed, fail or are
cancelled, then prints the fine-tuned model and the next steps: deployment, service configuration and `hs llm eval`.

Credentials come from the environment only and are never printed or logged:
  --auth entra (default)  Microsoft Entra ID bearer token from AZURE_OPENAI_BEARER_TOKEN, else from
                          `az account get-access-token --resource https://cognitiveservices.azure.com` (Foundry User role needed)
  --auth key              AZURE_OPENAI_API_KEY, sent as the api-key header
Endpoint: --endpoint or AZURE_OPENAI_ENDPOINT, e.g. https://<resource>.openai.azure.com or https://<resource>.services.ai.azure.com.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from build_datasets import DEFAULT_OUT, MIN_TRAINING_EXAMPLES, TASKS, validate_file  # sibling script in ml/finetune
from healthswap.platform.settings import LlmOptions

MODELS = ("gpt-4.1-mini-2025-04-14", "gpt-4.1-nano-2025-04-14")
TRAINING_TYPES = ("Standard", "GlobalStandard", "Developer")
COGNITIVE_SERVICES = "https://cognitiveservices.azure.com"
PLACEHOLDER_ENDPOINT = "https://<resource>.openai.azure.com"
FILE_CONTENT_TYPE = "application/json"
TIMEOUT = httpx.Timeout(60.0, connect=15.0, write=600.0)  # uploads of up to 512 MB need a long write timeout
FILE_POLL_SECONDS = 5.0
FILE_TIMEOUT_SECONDS = 30 * 60
TERMINAL = ("succeeded", "failed", "cancelled")
FILE_FAILED = ("error", "deleting", "deleted")
MAX_TRANSIENT_FAILURES = 5
REDACTED = "<redacted>"


class SubmitError(Exception):
    """A validation, configuration or API failure, with a message that never contains a credential."""

    def __init__(self, message: str, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


# ---------------------------------------------------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Credential:
    header: str  # "api-key" or "Authorization"
    secret: str  # the header value; empty in a dry run that did not need it. Never printed.
    source: str

    def headers(self) -> dict[str, str]:
        if not self.secret:
            raise SubmitError("No credential available")
        return {self.header: self.secret}

    def redacted(self) -> str:
        return f"{self.header}: {'Bearer ' if self.header == 'Authorization' else ''}{REDACTED} ({self.source})"


def _env(environ: Mapping[str, str], name: str) -> str:
    return (environ.get(name) or "").strip()


def az_access_token(run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> str:
    """A Microsoft Entra ID token for Azure OpenAI from the signed-in Azure CLI (`az login`)."""
    az = shutil.which("az")
    if az is None:
        raise SubmitError("The Azure CLI (az) is not installed: set AZURE_OPENAI_BEARER_TOKEN, or use --auth key with AZURE_OPENAI_API_KEY")
    try:
        result = run([az, "account", "get-access-token", "--resource", COGNITIVE_SERVICES, "--query", "accessToken", "-o", "tsv"],
                     capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as ex:
        raise SubmitError(f"az account get-access-token failed: {type(ex).__name__}") from None
    token = (result.stdout or "").strip()
    if result.returncode != 0 or not token:
        detail = next((line.strip() for line in (result.stderr or "").splitlines() if line.strip()), "no token returned")
        raise SubmitError(f"az account get-access-token failed (run `az login`): {detail[:300]}")
    return token


def credential(auth: str, environ: Mapping[str, str], *, fetch: bool,
               run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> Credential:
    """The request credential; with fetch=False (dry run) the Azure CLI is not called and the secret may stay empty."""
    if auth == "key":
        key = _env(environ, "AZURE_OPENAI_API_KEY")
        if not key and fetch:
            raise SubmitError("--auth key needs the AZURE_OPENAI_API_KEY environment variable")
        return Credential("api-key", key, "AZURE_OPENAI_API_KEY" if key else "AZURE_OPENAI_API_KEY, not set")
    token = _env(environ, "AZURE_OPENAI_BEARER_TOKEN")
    if token:
        return Credential("Authorization", "Bearer " + token, "Microsoft Entra ID token from AZURE_OPENAI_BEARER_TOKEN")
    if not fetch:
        return Credential("Authorization", "", "Microsoft Entra ID token from `az account get-access-token`, requested when submitting")
    return Credential("Authorization", "Bearer " + az_access_token(run), "Microsoft Entra ID token from az account get-access-token")


def resolve_endpoint(value: str | None, environ: Mapping[str, str]) -> str | None:
    """The resource endpoint (https://host[/path]) without a trailing /openai/v1, or None when neither option nor variable is set."""
    raw = (value or _env(environ, "AZURE_OPENAI_ENDPOINT")).strip()
    if not raw:
        return None
    parts = urlsplit(raw)
    if parts.scheme != "https" or not parts.hostname or parts.query or parts.fragment or parts.username or parts.password:
        raise SubmitError("The endpoint must be an https URL such as https://<resource>.openai.azure.com (no query, no credentials)")
    path = parts.path.rstrip("/")
    for suffix in ("/openai/v1", "/openai"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    return f"https://{parts.netloc}{path}"


def _positive_int_or_auto(value: str) -> str | int:
    if value == "auto":
        return value
    try:
        number = int(value)
    except ValueError:
        number = 0
    if number < 1:
        raise argparse.ArgumentTypeError(f"expected auto or a positive integer, got {value!r}")
    return number


def _positive_number_or_auto(value: str) -> str | float:
    if value == "auto":
        return value
    try:
        number = float(value)
    except ValueError:
        number = 0.0
    if not number > 0 or number == float("inf"):
        raise argparse.ArgumentTypeError(f"expected auto or a positive number, got {value!r}")
    return number


def _tasks(value: str) -> list[str]:
    tasks = [t.strip().lower() for t in value.split(",") if t.strip()]
    unknown = [t for t in tasks if t not in TASKS]
    if unknown or not tasks:
        raise argparse.ArgumentTypeError(f"unknown task(s) {', '.join(unknown) or value!r}; choose from {', '.join(TASKS)}")
    return list(dict.fromkeys(tasks))


def suffix_for(task: str, suffix: str | None, tasks: Sequence[str]) -> str:
    """--suffix (with -<task> appended when several tasks are submitted), default hs-<task>; at most 64 characters."""
    value = f"hs-{task}" if not suffix else suffix if len(tasks) == 1 else f"{suffix}-{task}"
    if len(value) > 64:
        raise SubmitError(f"The suffix {value!r} is longer than 64 characters")
    return value


# ---------------------------------------------------------------------------------------------------------------------
# Files and requests
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TaskFiles:
    task: str
    train: Path
    validation: Path | None
    train_examples: int
    validation_examples: int


def check_files(task: str, datasets: Path) -> tuple[TaskFiles | None, list[str]]:
    """Validates <task>_train.jsonl (required, at least 10 examples) and <task>_validation.jsonl (optional) like the service expects."""
    train = datasets / f"{task}_train.jsonl"
    validation = datasets / f"{task}_validation.jsonl"
    if not train.is_file():
        return None, [f"{train} not found: run `python ml/finetune/build_datasets.py --tasks {task}` first"]
    n_train, problems = validate_file(task, train)
    if not problems and n_train < MIN_TRAINING_EXAMPLES:
        problems.append(f"{train.name}: {n_train} examples; Azure OpenAI needs at least {MIN_TRAINING_EXAMPLES} training examples")
    n_validation = 0
    if validation.is_file():
        n_validation, more = validate_file(task, validation)
        problems.extend(more)
        overlap = _lines(train) & _lines(validation)
        if overlap:
            problems.append(f"{len(overlap)} example(s) appear in both {train.name} and {validation.name}; the same data must not be in both")
    return TaskFiles(task, train, validation if validation.is_file() else None, n_train, n_validation), problems


def _lines(path: Path) -> set[str]:
    return {line for line in path.read_bytes().decode("utf-8-sig", errors="replace").split("\n") if line.strip()}


def job_body(args: argparse.Namespace, task: str, training_file: str, validation_file: str | None) -> dict[str, Any]:
    """The fine-tuning job request (v1 API; trainingType is the Azure-specific training type)."""
    body: dict[str, Any] = {"model": args.model, "training_file": training_file}
    if validation_file is not None:
        body["validation_file"] = validation_file
    body["trainingType"] = args.training_type
    body["method"] = {
        "type": "supervised",
        "supervised": {"hyperparameters": {
            "batch_size": args.batch_size, "learning_rate_multiplier": args.learning_rate_multiplier, "n_epochs": args.n_epochs,
        }},
    }
    body["suffix"] = suffix_for(task, args.suffix, args.tasks)
    body["seed"] = args.seed
    return body


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def describe_requests(args: argparse.Namespace, files: TaskFiles, endpoint: str, cred: Credential) -> list[str]:
    """The HTTP requests --submit sends for one task, credentials redacted (ids that the service assigns are placeholders)."""
    base = f"{endpoint}/openai/v1"
    lines = [f"Requests for {files.task}:"]
    uploads = [(files.train, "training")] + ([(files.validation, "validation")] if files.validation is not None else [])
    for path, role in uploads:
        lines += [
            f"  POST {base}/files",
            f"    {cred.redacted()}",
            "    Content-Type: multipart/form-data; boundary=<generated>",
            "    form field purpose=fine-tune",
            f"    form file  file={path.name} ({FILE_CONTENT_TYPE}, {path.stat().st_size:,} bytes, sha256 {_sha256(path)})",
            f"    -> the {role} file id",
        ]
    for _, role in uploads:
        lines.append(f"  GET  {base}/files/<{role} file id>   every {FILE_POLL_SECONDS:g} s until \"status\": \"processed\"")
    body = job_body(args, files.task, "<training file id>", "<validation file id>" if files.validation is not None else None)
    lines += [f"  POST {base}/fine_tuning/jobs", f"    {cred.redacted()}", "    Content-Type: application/json"]
    lines += ["    " + line for line in json.dumps(body, indent=2).splitlines()]
    if args.wait:
        lines.append(f"  GET  {base}/fine_tuning/jobs/<job id>  and  .../events?limit=100   every {args.poll_seconds:g} s until "
                     "succeeded, failed or cancelled")
    return lines


# ---------------------------------------------------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------------------------------------------------

_HINTS = {
    400: "the service rejected the request (check --model, --training-type and the hyperparameters for this region)",
    401: "authentication failed: check the credential (--auth) and that it is for this resource",
    403: "access denied: the identity needs the Foundry User (Azure AI User) role on the resource",
    404: "not found: check --endpoint and that fine-tuning of --model is available in the resource's region",
    429: "rate limited or quota exceeded: retry later",
}


def _error_message(method: str, url: str, response: httpx.Response) -> str:
    detail = ""
    try:
        doc = response.json()
        error = doc.get("error") if isinstance(doc, dict) else None
        if isinstance(error, dict):
            detail = " ".join(str(error[k]) for k in ("code", "message") if error.get(k))
    except ValueError:
        detail = response.text.strip()
    hint = _HINTS.get(response.status_code) or ("service error: retry later" if response.status_code >= 500 else "")
    text = f"{method} {url} returned HTTP {response.status_code} {response.reason_phrase}"
    return text + (f": {detail[:500]}" if detail else "") + (f" ({hint})" if hint else "")


class FineTuningApi:
    """The Azure OpenAI v1 files and fine-tuning endpoints used here."""

    def __init__(self, endpoint: str, cred: Credential, http: httpx.Client, *, sleep: Callable[[float], None] = time.sleep,
                 say: Callable[[str], None] = print) -> None:
        self.base = f"{endpoint}/openai/v1"
        self.cred = cred
        self.http = http
        self.sleep = sleep
        self.say = say

    def _send(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        url = f"{self.base}/{path}"
        try:
            response = self.http.request(method, url, headers=self.cred.headers(), **kwargs)
        except httpx.TimeoutException:
            raise SubmitError(f"{method} {url} timed out", transient=True) from None
        except httpx.HTTPError as ex:
            raise SubmitError(f"{method} {url} failed: {type(ex).__name__}: {ex}", transient=True) from None
        if response.status_code >= 400:
            raise SubmitError(_error_message(method, url, response), transient=response.status_code == 429 or response.status_code >= 500)
        try:
            doc = response.json()
        except ValueError:
            raise SubmitError(f"{method} {url} returned HTTP {response.status_code} without a JSON body") from None
        if not isinstance(doc, dict):
            raise SubmitError(f"{method} {url} returned an unexpected JSON value")
        return doc

    def upload(self, path: Path) -> dict[str, Any]:
        with open(path, "rb") as f:
            doc = self._send("POST", "files", data={"purpose": "fine-tune"}, files={"file": (path.name, f, FILE_CONTENT_TYPE)})
        if not isinstance(doc.get("id"), str):
            raise SubmitError(f"The upload of {path.name} returned no file id")
        return doc

    def wait_processed(self, file_id: str, name: str, timeout: float = FILE_TIMEOUT_SECONDS) -> dict[str, Any]:
        """Polls the file until the service has processed (validated) it."""
        deadline = time.monotonic() + timeout
        last = None
        failures = 0
        while True:
            try:
                doc = self._send("GET", f"files/{quote(file_id, safe='')}")
                failures = 0
            except SubmitError as ex:
                failures += 1
                if not ex.transient or failures > MAX_TRANSIENT_FAILURES:
                    raise
                self.say(f"  warning: {ex}; retrying")
                self.sleep(FILE_POLL_SECONDS)
                continue
            status = doc.get("status")
            if status != last:
                self.say(f"  {name} ({file_id}): {status}")
                last = status
            if status == "processed":
                return doc
            if status in FILE_FAILED:
                details = doc.get("status_details") or "see the error of the fine-tuning job for details"
                raise SubmitError(f"{name} ({file_id}) was not processed: status {status}: {details}")
            if time.monotonic() > deadline:
                raise SubmitError(f"{name} ({file_id}) was not processed within {timeout / 60:g} minutes (status {status})")
            self.sleep(FILE_POLL_SECONDS)

    def create_job(self, body: Mapping[str, Any]) -> dict[str, Any]:
        doc = self._send("POST", "fine_tuning/jobs", json=dict(body))
        if not isinstance(doc.get("id"), str):
            raise SubmitError("The fine-tuning job request returned no job id")
        return doc

    def get_job(self, job_id: str) -> dict[str, Any]:
        return self._send("GET", f"fine_tuning/jobs/{quote(job_id, safe='')}")

    def new_events(self, job_id: str, seen: set[str]) -> list[dict[str, Any]]:
        """Events not printed yet, oldest first (the API lists the newest first; older pages follow with `after`)."""
        fresh: list[dict[str, Any]] = []
        after: str | None = None
        for _ in range(10):
            params: dict[str, Any] = {"limit": 100}
            if after is not None:
                params["after"] = after
            page = self._send("GET", f"fine_tuning/jobs/{quote(job_id, safe='')}/events", params=params)
            data = [e for e in page.get("data") or [] if isinstance(e, dict)]
            new = [e for e in data if str(e.get("id")) not in seen]
            fresh.extend(new)
            if not data or not page.get("has_more") or len(new) < len(data):
                break
            after = str(data[-1].get("id"))
        seen.update(str(e.get("id")) for e in fresh)
        fresh.reverse()
        return sorted(fresh, key=lambda e: e.get("created_at") if isinstance(e.get("created_at"), (int, float)) else 0)

    def follow(self, jobs: Sequence[tuple[str, str]], poll_seconds: float) -> dict[str, dict[str, Any]]:
        """Polls every job and prints new events and status changes until all reach a terminal state; returns them by job id."""
        seen: dict[str, set[str]] = {job_id: set() for _, job_id in jobs}
        status: dict[str, Any] = {}
        done: dict[str, dict[str, Any]] = {}
        failures = 0
        while len(done) < len(jobs):
            for task, job_id in jobs:
                if job_id in done:
                    continue
                try:
                    job = self.get_job(job_id)
                    events = self.new_events(job_id, seen[job_id])
                    failures = 0
                except SubmitError as ex:
                    failures += 1
                    if not ex.transient or failures > MAX_TRANSIENT_FAILURES:
                        raise
                    self.say(f"warning: {ex}; retrying")
                    continue
                for e in events:
                    self.say(f"{task} {_time(e.get('created_at'))} [{e.get('level', 'info')}] {e.get('message', '')}")
                if job.get("status") != status.get(job_id):
                    status[job_id] = job.get("status")
                    self.say(f"{task}: job {job_id} is {job.get('status')}")
                if job.get("status") in TERMINAL:
                    done[job_id] = job
            if len(done) < len(jobs):
                self.sleep(poll_seconds)
        return done


def _time(value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, UTC).strftime("%Y-%m-%d %H:%M:%SZ")
    return "-"


# ---------------------------------------------------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------------------------------------------------

_EVALUATION = {
    "t5": "Evaluate it through the production validator chain on the held-out split:\n"
          "       hs llm eval --input ml/finetune/out/t5_validation_facts.jsonl --max 500",
    "t4": "Rebuild the swap graphs (`hs kb build-graph`): the judge scores the uncertain pairs; compare acceptance in\n"
          "       GET /admin/llm-audit?task=T4 (validation results per call) before switching the default.",
    "t3": "Ingest a feed without source categories (`hs kb ingest ...`): T3 classifies them; compare acceptance in\n"
          "       GET /admin/llm-audit?task=T3 before switching the default.",
}


def next_steps(task: str, model: str, endpoint: str, args: argparse.Namespace) -> list[str]:
    key = task.upper()
    deployment = LlmOptions().deployments.get(key, f"hs-{task}")
    return [
        f"Next steps for {key} ({model}):",
        "  1. Deploy the fine-tuned model (Azure CLI; --sku-name GlobalStandard, Standard, or Developer for evaluation, as offered in",
        "     the region):",
        f"       az cognitiveservices account deployment create --resource-group <resource-group> --name <resource-name> "
        f"--deployment-name {deployment} --model-name {model} --model-version 1 --model-format OpenAI --sku-name GlobalStandard --sku-capacity 1",
        "  2. Point the service at it (environment variables only; secrets never go into config/*.toml):",
        "       HS_LLM__PROVIDER=AzureOpenAI",
        f"       HS_LLM__ENDPOINT={endpoint}",
        f"       HS_LLM__DEPLOYMENTS__{key}={deployment}",
        "       HS_LLM__BEARER_TOKEN=<Microsoft Entra ID token for https://cognitiveservices.azure.com>   (or HS_LLM__API_KEY=<key>)",
        f"  3. {_EVALUATION[task]}",
    ]


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ml/finetune/submit.py", description="Submit Azure OpenAI supervised fine-tuning jobs (dry run by default).")
    p.add_argument("--tasks", type=_tasks, default=["t5"], help="comma-separated tasks to fine-tune (default: t5)")
    p.add_argument("--datasets", type=Path, default=DEFAULT_OUT, help="build_datasets.py output (default: ml/finetune/out)")
    p.add_argument("--endpoint", help="Azure OpenAI / Foundry resource endpoint (default: AZURE_OPENAI_ENDPOINT)")
    p.add_argument("--auth", choices=("entra", "key"), default="entra", help="entra (default): bearer token; key: AZURE_OPENAI_API_KEY")
    p.add_argument("--model", choices=MODELS, default=MODELS[0], help=f"base model (default: {MODELS[0]})")
    p.add_argument("--training-type", choices=TRAINING_TYPES, default="Standard", help="training type (default: Standard)")
    p.add_argument("--batch-size", type=_positive_int_or_auto, default="auto", help="auto (default) or a positive integer")
    p.add_argument("--learning-rate-multiplier", type=_positive_number_or_auto, default="auto", help="auto (default) or a positive number")
    p.add_argument("--n-epochs", type=_positive_int_or_auto, default="auto", help="auto (default) or a positive integer")
    p.add_argument("--suffix", help="model name suffix, at most 64 characters (default: hs-<task>)")
    p.add_argument("--seed", type=int, default=7, help="job seed for reproducibility (default: 7)")
    p.add_argument("--submit", action="store_true", help="upload the files and create the jobs (default: dry run)")
    p.add_argument("--wait", action="store_true", help="follow the jobs until they finish")
    p.add_argument("--poll-seconds", type=float, default=30.0, help="--wait polling interval (default: 30)")
    return p


def _scrub(text: str, cred: Credential | None) -> str:
    """Defense in depth: no credential text in anything printed."""
    if cred is not None and cred.secret:
        text = text.replace(cred.secret, REDACTED)
        if cred.secret.startswith("Bearer "):
            text = text.replace(cred.secret[len("Bearer "):], REDACTED)
    return text


def main(argv: Sequence[str] | None = None, *, transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] = time.sleep,
         environ: Mapping[str, str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not 0 <= args.seed <= 2147483647:
        parser.error("--seed must be between 0 and 2147483647")
    if args.poll_seconds < 1:
        parser.error("--poll-seconds must be at least 1")
    env: Mapping[str, str] = os.environ if environ is None else environ
    cred: Credential | None = None
    try:
        for task in args.tasks:
            suffix_for(task, args.suffix, args.tasks)
        plans: list[TaskFiles] = []
        problems: list[str] = []
        for task in args.tasks:
            files, found = check_files(task, args.datasets)
            problems.extend(found)
            if files is not None:
                plans.append(files)
                summary = f"{files.train.name} {files.train_examples} examples"
                summary += f", {files.validation.name} {files.validation_examples} examples" if files.validation else ", no validation file"
                print(f"{task}: {summary}: {'INVALID' if found else 'valid'}")
        if problems:
            for problem in problems:
                print(f"  {problem}", file=sys.stderr)
            print("submit: the files are not valid; fix them or rebuild them with ml/finetune/build_datasets.py", file=sys.stderr)
            return 1
        endpoint = resolve_endpoint(args.endpoint, env)

        if not args.submit:
            cred = credential(args.auth, env, fetch=False)
            print(f"\nDRY RUN: nothing is sent (add --submit to upload the files and create {len(plans)} job(s)).")
            print(f"Endpoint: {endpoint or PLACEHOLDER_ENDPOINT + '   (not set: --endpoint or AZURE_OPENAI_ENDPOINT)'}")
            for files in plans:
                print()
                print("\n".join(describe_requests(args, files, endpoint or PLACEHOLDER_ENDPOINT, cred)))
            print()
            print("\n".join(next_steps(plans[0].task, "<fine-tuned model id>", endpoint or PLACEHOLDER_ENDPOINT, args)))
            return 0

        if endpoint is None:
            raise SubmitError("Set --endpoint or AZURE_OPENAI_ENDPOINT to the Azure OpenAI / Foundry resource endpoint")
        cred = credential(args.auth, env, fetch=True)
        with httpx.Client(timeout=TIMEOUT, transport=transport, follow_redirects=False) as http:
            api = FineTuningApi(endpoint, cred, http, sleep=sleep, say=lambda text: print(_scrub(text, cred), flush=True))
            jobs: list[tuple[str, str]] = []
            for files in plans:
                training = api.upload(files.train)
                print(f"{files.task}: uploaded {files.train.name} as {training['id']}")
                validation = None
                if files.validation is not None:
                    validation = api.upload(files.validation)
                    print(f"{files.task}: uploaded {files.validation.name} as {validation['id']}")
                api.wait_processed(training["id"], files.train.name)
                if validation is not None:
                    api.wait_processed(validation["id"], files.validation.name)
                job = api.create_job(job_body(args, files.task, training["id"], None if validation is None else validation["id"]))
                print(f"{files.task}: created fine-tuning job {job['id']} ({job.get('status')})")
                jobs.append((files.task, job["id"]))
            if not args.wait:
                for task, job_id in jobs:
                    print(f"{task}: follow it with GET {api.base}/fine_tuning/jobs/{job_id} and .../events (or rerun with --wait)")
                return 0
            finished = api.follow(jobs, args.poll_seconds)
        failed = 0
        for task, job_id in jobs:
            job = finished[job_id]
            if job.get("status") == "succeeded" and isinstance(job.get("fine_tuned_model"), str):
                print(f"\n{task}: job {job_id} succeeded: fine-tuned model {job['fine_tuned_model']} (trained tokens: {job.get('trained_tokens')})")
                print("\n".join(next_steps(task, job["fine_tuned_model"], endpoint, args)))
            else:
                failed += 1
                error = job.get("error") if isinstance(job.get("error"), dict) else {}
                detail = " ".join(str(error[k]) for k in ("code", "message", "param") if error.get(k)) or "no error details"
                print(_scrub(f"\n{task}: job {job_id} {job.get('status')}: {detail}", cred), file=sys.stderr)
        return 1 if failed else 0
    except SubmitError as ex:
        print(_scrub(f"submit: error: {ex}", cred), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("submit: interrupted; jobs that were created keep running (GET .../openai/v1/fine_tuning/jobs/<id>)", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
