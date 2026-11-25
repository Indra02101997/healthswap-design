"""Trains the ONNX models served by healthswap.platform.ml.registry.ModelRegistry (ranker, substitutability, entity_resolution).

    python ml/train.py {substitutability|ranker|er|all} [--data ml/data] [--out ml/out/models] [--install [--force]] [--seed 7]

Inputs are the files written by `hs ml export --out ml/data` (substitutability_pairs.jsonl, er_pairs.jsonl) and
`hs ml simulate --out ml/data/ranker_events.jsonl`. Their feature vectors come from the serving code in the order of the domain
constants (Substitutability / EntityResolution / RankerFeatures FEATURE_NAMES), so X is used exactly as written; y is `label`.

* substitutability and er (-> entity_resolution): StandardScaler + LogisticRegression, or GradientBoostingClassifier when it is
  clearly better (validation log loss at least 10% lower). Stratified train/validation split (one fold of five), grouped so that
  both directions of a product pair (substitutability) or all variants of a product (er) stay on one side.
* ranker: StandardScaler + LogisticRegression with inverse-propensity sample weights (1/propensity, clipped at --ips-clip) from
  ranker_events.jsonl; split grouped by suggestion; reports AUC plus IPS-weighted AUC, log loss and acceptance rate.

Every model is exported with skl2onnx (float32 input "input", output "probabilities" without ZipMap) and verified with ONNX
Runtime against scikit-learn predict_proba on the validation set (max |difference| <= 1e-5) before <name>.onnx and
<name>.meta.json are written. The metadata follows ModelMetadata (name, version, featureNames, inputName, outputName,
positiveClassIndex, metrics, trainedAt) plus the calibration bins, the candidate scores and the input file hash.

The default output is ml/out/models, NOT the served directory. --install copies the files into the configured models directory
(settings models.directory, default <repo>/models; HS_MODELS__DIRECTORY overrides) and refuses a model whose validation AUC is
below 0.6 unless --force; the running API picks them up with POST /admin/models/reload (admin token) or a restart.

Needs the [ml] extra: python -m pip install -e ".[ml]".
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sys
import warnings
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from healthswap.domain.knowledge.quality import EntityResolution
from healthswap.domain.recommendation.ranking import RankerFeatures, Substitutability

REPO = Path(__file__).resolve().parents[1]
DEFAULT_DATA = REPO / "ml" / "data"
DEFAULT_OUT = REPO / "ml" / "out" / "models"

INPUT_NAME = "input"  # ModelMetadata.input_name default
OUTPUT_NAME = "probabilities"  # ModelMetadata.output_name default
ONNX_OPSET = {"": 17, "ai.onnx.ml": 3}
VALIDATION_FOLDS = 5  # the validation set is one fold of five (20%)
GBM_MIN_GAIN = 0.10  # GradientBoosting must lower the validation log loss by at least 10% to replace the logistic regression
PARITY_TOLERANCE = 1e-5
MIN_INSTALL_AUC = 0.6
CALIBRATION_BINS = 10


class TrainingError(Exception):
    """Bad or missing training data, or an export that failed verification."""


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """One served model: registry name, training file and the domain feature order."""

    name: str
    data_file: str
    feature_names: tuple[str, ...]
    produce: str  # the command that writes data_file
    algorithms: tuple[str, ...]
    ips_weighted: bool
    group: Callable[[Mapping[str, Any]], str]
    baseline: Callable[[Mapping[str, Any]], float] | None = None  # the serving heuristic, for comparison


def _pair_group(row: Mapping[str, Any]) -> str:
    a, b = sorted((str(row.get("from")), str(row.get("to"))))
    return f"{row.get('market')}|{a}|{b}"


SPECS: dict[str, ModelSpec] = {
    "substitutability": ModelSpec(
        "substitutability", "substitutability_pairs.jsonl", tuple(Substitutability.FEATURE_NAMES), "hs ml export --out ml/data",
        ("lr", "gbm"), False, _pair_group, lambda r: float(r["heuristic"]),
    ),
    "ranker": ModelSpec(
        "ranker", "ranker_events.jsonl", tuple(RankerFeatures.FEATURE_NAMES), "hs ml simulate --out ml/data/ranker_events.jsonl",
        ("lr",), True, lambda r: str(r.get("suggestionId")),
    ),
    "er": ModelSpec(
        "entity_resolution", "er_pairs.jsonl", tuple(EntityResolution.FEATURE_NAMES), "hs ml export --out ml/data",
        ("lr", "gbm"), False, lambda r: str(r.get("a")), lambda r: float(EntityResolution.heuristic_score(r["features"])),
    ),
}
COMMANDS = ("substitutability", "ranker", "er")


@dataclass(slots=True)
class Dataset:
    spec: ModelSpec
    path: Path
    sha256: str
    x: np.ndarray  # float32 (rows, features): the serving vectors
    y: np.ndarray  # int64 labels
    groups: np.ndarray
    weights: np.ndarray  # IPS weights (ones for unweighted models)
    baseline: np.ndarray | None
    propensities: np.ndarray | None = None


@dataclass(slots=True)
class TrainedModel:
    spec: ModelSpec
    algorithm: str
    estimator: Any
    metadata: dict[str, Any]
    onnx: bytes
    parity: float
    rows: tuple[int, int]  # (train, validation)
    candidates: dict[str, float] = field(default_factory=dict)  # validation log loss per algorithm


# ---------------------------------------------------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------------------------------------------------


def load_dataset(spec: ModelSpec, data_dir: str | Path, ips_clip: float = 10.0) -> Dataset:
    """Reads the JSONL rows of `spec` and checks them: feature count = len(FEATURE_NAMES), finite values, labels 0/1, propensities
    in (0, 1] for the ranker."""
    path = Path(data_dir) / spec.data_file
    if not path.is_file():
        raise TrainingError(f"{path} not found: run `{spec.produce}` first")
    raw = path.read_bytes()
    rows: list[dict[str, Any]] = []
    for n, line in enumerate(raw.decode("utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as ex:
            raise TrainingError(f"{path.name}:{n}: not a JSON line ({ex})") from None
        features = row.get("features") if isinstance(row, dict) else None
        if not isinstance(features, list) or len(features) != len(spec.feature_names):
            got = len(features) if isinstance(features, list) else "none"
            raise TrainingError(f"{path.name}:{n}: expected {len(spec.feature_names)} features ({', '.join(spec.feature_names)}), got {got}")
        label = row.get("label")
        if isinstance(label, bool) or label not in (0, 1):
            raise TrainingError(f"{path.name}:{n}: label must be 0 or 1, got {label!r}")
        rows.append(row)
    if not rows:
        raise TrainingError(f"{path} has no rows: run `{spec.produce}` first")
    try:
        x = np.asarray([r["features"] for r in rows], dtype=np.float32)
    except (TypeError, ValueError) as ex:
        raise TrainingError(f"{path.name}: features must be numbers ({ex})") from None
    if not np.isfinite(x).all():
        raise TrainingError(f"{path.name}: features must be finite numbers")
    y = np.asarray([r["label"] for r in rows], dtype=np.int64)
    if len(np.unique(y)) < 2:
        raise TrainingError(f"{path.name}: both labels (0 and 1) are needed, got only {int(y[0])}")
    groups = np.asarray([spec.group(r) for r in rows])
    weights = np.ones(len(rows), dtype=np.float64)
    propensities = None
    if spec.ips_weighted:
        values = [r.get("propensity") for r in rows]
        bad = next((i for i, p in enumerate(values) if isinstance(p, bool) or not isinstance(p, (int, float)) or not 0 < p <= 1), None)
        if bad is not None:
            raise TrainingError(f"{path.name}: row {bad + 1} has propensity {values[bad]!r}; the logging policy must give a probability in (0, 1]")
        propensities = np.asarray(values, dtype=np.float64)
        weights = np.minimum(1.0 / propensities, ips_clip)
    baseline = np.asarray([spec.baseline(r) for r in rows], dtype=np.float64) if spec.baseline is not None else None
    return Dataset(spec, path, hashlib.sha256(raw).hexdigest(), x, y, groups, weights, baseline, propensities)


def split(ds: Dataset, seed: int) -> tuple[np.ndarray, np.ndarray]:
    """Indices of the training and validation rows: one fold of a stratified, grouped, shuffled 5-fold split."""
    from sklearn.model_selection import StratifiedGroupKFold

    if len(np.unique(ds.groups)) < VALIDATION_FOLDS:
        raise TrainingError(f"{ds.path.name}: at least {VALIDATION_FOLDS} groups are needed for the train/validation split")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)  # "the least populated class has only n members" on tiny files
        train, validation = next(StratifiedGroupKFold(n_splits=VALIDATION_FOLDS, shuffle=True, random_state=seed).split(ds.x, ds.y, ds.groups))
    for name, idx in (("training", train), ("validation", validation)):
        if len(np.unique(ds.y[idx])) < 2:
            raise TrainingError(f"{ds.path.name}: the {name} split has a single label; more data is needed")
    return train, validation


# ---------------------------------------------------------------------------------------------------------------------
# Models and metrics
# ---------------------------------------------------------------------------------------------------------------------


def _estimator(algorithm: str, seed: int) -> Any:
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    if algorithm == "lr":
        return Pipeline([("scaler", StandardScaler()), ("clf", LogisticRegression(max_iter=5000))])
    if algorithm == "gbm":
        return GradientBoostingClassifier(n_estimators=200, learning_rate=0.05, max_depth=3, random_state=seed)
    raise ValueError(f"Unknown algorithm {algorithm}")


def _fit(model: Any, x: np.ndarray, y: np.ndarray, w: np.ndarray) -> Any:
    from sklearn.pipeline import Pipeline

    if isinstance(model, Pipeline):
        return model.fit(x, y, scaler__sample_weight=w, clf__sample_weight=w)
    return model.fit(x, y, sample_weight=w)


def _positive_index(model: Any) -> int:
    classes = [int(c) for c in model.classes_]
    if sorted(classes) != [0, 1]:
        raise TrainingError(f"Expected classes [0, 1], got {classes}")
    return classes.index(1)


def binary_metrics(y: np.ndarray, p: np.ndarray, w: np.ndarray | None = None) -> dict[str, float]:
    """AUC, log loss, accuracy (threshold 0.5) and Brier score, optionally sample-weighted."""
    from sklearn.metrics import accuracy_score, brier_score_loss, log_loss, roc_auc_score

    return {
        "auc": float(roc_auc_score(y, p, sample_weight=w)),
        "logLoss": float(log_loss(y, p, sample_weight=w, labels=[0, 1])),
        "accuracy": float(accuracy_score(y, p >= 0.5, sample_weight=w)),
        "brier": float(brier_score_loss(y, p, sample_weight=w)),
    }


def calibration_bins(y: np.ndarray, p: np.ndarray, w: np.ndarray, bins: int = CALIBRATION_BINS) -> tuple[list[dict[str, Any]], float]:
    """Equal-width reliability bins (weighted) and the expected calibration error."""
    index = np.minimum((p * bins).astype(int), bins - 1)
    total = float(w.sum())
    out: list[dict[str, Any]] = []
    ece = 0.0
    for b in range(bins):
        mask = index == b
        entry: dict[str, Any] = {"lower": round(b / bins, 4), "upper": round((b + 1) / bins, 4), "count": int(mask.sum())}
        if mask.any():
            bw = w[mask]
            predicted = float(np.average(p[mask], weights=bw))
            observed = float(np.average(y[mask], weights=bw))
            ece += float(bw.sum()) / total * abs(predicted - observed)
            entry.update(meanPredicted=round(predicted, 6), fractionPositive=round(observed, 6))
        out.append(entry)
    return out, ece


# ---------------------------------------------------------------------------------------------------------------------
# ONNX
# ---------------------------------------------------------------------------------------------------------------------


def to_onnx(model: Any, n_features: int, metadata: Mapping[str, str]) -> bytes:
    """skl2onnx export: float32 [N, n_features] input named "input", class probabilities as the tensor "probabilities"."""
    import onnx
    from skl2onnx import convert_sklearn
    from skl2onnx.common.data_types import FloatTensorType
    from sklearn.pipeline import Pipeline

    classifier = model.steps[-1][1] if isinstance(model, Pipeline) else model
    onx = convert_sklearn(model, initial_types=[(INPUT_NAME, FloatTensorType([None, n_features]))], options={id(classifier): {"zipmap": False}},
                          target_opset=ONNX_OPSET)
    outputs = [o.name for o in onx.graph.output]
    if OUTPUT_NAME not in outputs:
        raise TrainingError(f"The ONNX graph has no output {OUTPUT_NAME!r} (outputs: {outputs})")
    # A pipeline lists the default domain once per step; keep one entry per domain (highest version).
    versions: dict[str, int] = {}
    for entry in onx.opset_import:
        versions[entry.domain] = max(versions.get(entry.domain, 0), entry.version)
    del onx.opset_import[:]
    for domain, version in versions.items():
        onx.opset_import.append(onnx.helper.make_opsetid(domain, version))
    for key, value in metadata.items():
        prop = onx.metadata_props.add()
        prop.key, prop.value = key, value
    onnx.checker.check_model(onx)
    return onx.SerializeToString()


def onnx_parity(onnx_bytes: bytes, model: Any, x: np.ndarray, positive: int) -> float:
    """Max |ONNX Runtime probability - scikit-learn predict_proba| of the positive class on `x`; raises above 1e-5."""
    import onnxruntime as ort

    session = ort.InferenceSession(onnx_bytes, providers=["CPUExecutionProvider"])
    onnx_p = np.asarray(session.run([OUTPUT_NAME], {INPUT_NAME: x.astype(np.float32)})[0], dtype=np.float64)[:, positive]
    sklearn_p = model.predict_proba(x)[:, positive]
    diff = float(np.max(np.abs(onnx_p - sklearn_p))) if len(x) else 0.0
    if not diff <= PARITY_TOLERANCE:
        raise TrainingError(f"ONNX probabilities differ from scikit-learn by {diff:.3g} (> {PARITY_TOLERANCE}) on the validation set")
    return diff


# ---------------------------------------------------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------------------------------------------------


def _clean(metrics: Mapping[str, float]) -> dict[str, float]:
    """Finite values rounded for the meta.json (ModelMetadata.metrics is a map of finite doubles)."""
    return {k: round(float(v), 6) for k, v in metrics.items() if math.isfinite(float(v))}


def train_model(command: str, data_dir: str | Path = DEFAULT_DATA, *, seed: int = 7, ips_clip: float = 10.0, now: datetime | None = None) -> TrainedModel:
    """Trains, evaluates and exports one model (`command`: substitutability, ranker or er); nothing is written."""
    spec = SPECS[command]
    ds = load_dataset(spec, data_dir, ips_clip)
    train_idx, val_idx = split(ds, seed)
    x_train, y_train, w_train = ds.x[train_idx], ds.y[train_idx], ds.weights[train_idx]
    x_val, y_val, w_val = ds.x[val_idx], ds.y[val_idx], ds.weights[val_idx]

    fitted: dict[str, tuple[Any, np.ndarray, float]] = {}
    for algorithm in spec.algorithms:
        model = _fit(_estimator(algorithm, seed), x_train, y_train, w_train)
        p = model.predict_proba(x_val)[:, _positive_index(model)]
        fitted[algorithm] = (model, p, binary_metrics(y_val, p, w_val if spec.ips_weighted else None)["logLoss"])
    algorithm = "lr"
    if "gbm" in fitted and fitted["gbm"][2] <= fitted["lr"][2] * (1 - GBM_MIN_GAIN):
        algorithm = "gbm"
    model, p_val, _ = fitted[algorithm]
    positive = _positive_index(model)

    metrics = binary_metrics(y_val, p_val)
    if spec.ips_weighted:
        weighted = binary_metrics(y_val, p_val, w_val)
        metrics.update({f"{k}Ips": v for k, v in weighted.items()})
        metrics["acceptRateIps"] = float(np.average(y_val, weights=w_val))  # self-normalized IPS estimate of P(accept)
        metrics["meanPredictedIps"] = float(np.average(p_val, weights=w_val))
        metrics["effectiveSampleSize"] = float(w_val.sum() ** 2 / (w_val**2).sum())
        metrics["ipsClip"] = ips_clip
    bins, ece = calibration_bins(y_val, p_val, w_val if spec.ips_weighted else np.ones(len(y_val)))
    metrics["ece"] = ece
    if ds.baseline is not None:
        from sklearn.metrics import roc_auc_score

        metrics["heuristicAuc"] = float(roc_auc_score(y_val, ds.baseline[val_idx]))
    metrics.update({
        "trainRows": float(len(train_idx)), "validationRows": float(len(val_idx)), "positiveRate": float(y_val.mean()),
        "trainPositiveRate": float(y_train.mean()),
    })
    for name, (_, _, loss) in fitted.items():
        metrics[f"candidateLogLoss_{name}"] = loss

    trained_at = (now or datetime.now(UTC)).astimezone(UTC)
    version = f"{spec.name}-{algorithm}-{trained_at:%Y%m%d.%H%M}"
    onnx_bytes = to_onnx(model, len(spec.feature_names), {"name": spec.name, "version": version, "featureNames": ",".join(spec.feature_names)})
    parity = onnx_parity(onnx_bytes, model, x_val, positive)
    metadata: dict[str, Any] = {
        "name": spec.name,
        "version": version,
        "featureNames": list(spec.feature_names),
        "inputName": INPUT_NAME,
        "outputName": OUTPUT_NAME,
        "positiveClassIndex": positive,
        "metrics": _clean(metrics),
        "trainedAt": trained_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "algorithm": type(model.steps[-1][1] if hasattr(model, "steps") else model).__name__,
        "calibration": bins,
        "training": {
            "file": spec.data_file, "sha256": ds.sha256, "rows": len(ds.y), "seed": seed, "validation": f"1 of {VALIDATION_FOLDS} stratified group folds",
            "selection": f"lowest validation log loss; GradientBoosting only when at least {GBM_MIN_GAIN:.0%} better" if len(spec.algorithms) > 1 else "fixed",
            "sampleWeights": f"1/propensity clipped at {ips_clip:g}" if spec.ips_weighted else "none",
            "onnxParityMaxAbsDiff": parity, "onnxOpset": ONNX_OPSET,
        },
    }
    return TrainedModel(spec, algorithm, model, metadata, onnx_bytes, parity, (len(train_idx), len(val_idx)), {k: v[2] for k, v in fitted.items()})


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def write_model(trained: TrainedModel, out_dir: str | Path) -> tuple[Path, Path]:
    """Writes <name>.onnx and <name>.meta.json to `out_dir`."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    onnx_path = out / f"{trained.spec.name}.onnx"
    meta_path = out / f"{trained.spec.name}.meta.json"
    _atomic_write(onnx_path, trained.onnx)
    _atomic_write(meta_path, (json.dumps(trained.metadata, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    return onnx_path, meta_path


def gate_auc(trained: TrainedModel) -> float:
    """The validation AUC that decides installation (the ranker uses the IPS-weighted AUC)."""
    metrics = trained.metadata["metrics"]
    return float(metrics["aucIps"] if trained.spec.ips_weighted else metrics["auc"])


def install(paths: Sequence[Path], directory: Path) -> None:
    """Copies the model files into the served models directory (the .onnx first, each replaced atomically)."""
    directory.mkdir(parents=True, exist_ok=True)
    for src in paths:
        tmp = directory / f".{src.name}.{os.getpid()}.tmp"
        shutil.copyfile(src, tmp)
        os.replace(tmp, directory / src.name)


# ---------------------------------------------------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------------------------------------------------


def _report(t: TrainedModel, paths: tuple[Path, Path]) -> str:
    m = t.metadata["metrics"]
    lines = [
        f"{t.spec.name}: {t.rows[0] + t.rows[1]} rows from {t.spec.data_file} (train {t.rows[0]}, validation {t.rows[1]}, "
        f"validation positive rate {m['positiveRate']:.3f})",
        "  candidates (validation log loss): " + ", ".join(f"{k} {v:.4f}" for k, v in t.candidates.items()) + f" -> {t.algorithm}",
        f"  validation: AUC {m['auc']:.4f}  log loss {m['logLoss']:.4f}  accuracy {m['accuracy']:.4f}  Brier {m['brier']:.4f}  ECE {m['ece']:.4f}"
        + (f"  (heuristic AUC {m['heuristicAuc']:.4f})" if "heuristicAuc" in m else ""),
    ]
    if t.spec.ips_weighted:
        lines.append(f"  IPS-weighted: AUC {m['aucIps']:.4f}  log loss {m['logLossIps']:.4f}  accept rate {m['acceptRateIps']:.4f} "
                     f"(predicted {m['meanPredictedIps']:.4f})  effective sample size {m['effectiveSampleSize']:.0f}")
    lines.append("  calibration: " + "  ".join(
        f"[{b['lower']:.1f},{b['upper']:.1f}) n={b['count']}" + (f" {b['meanPredicted']:.2f}/{b['fractionPositive']:.2f}" if b["count"] else "")
        for b in t.metadata["calibration"]
    ) + "   (n predicted/observed)")
    lines.append(f"  ONNX parity max |diff| {t.parity:.1e}; wrote {paths[0]} + {paths[1].name} (version {t.metadata['version']})")
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ml/train.py", description="Train the ONNX models served by the HealthSwap API (ModelRegistry).")
    p.add_argument("model", choices=(*COMMANDS, "all"), help="model to train (er = entity_resolution)")
    p.add_argument("--data", type=Path, default=DEFAULT_DATA, help="hs ml export / simulate output (default: ml/data)")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output directory (default: ml/out/models; not the served models/)")
    p.add_argument("--install", action="store_true", help="copy the models into the served models directory (settings models.directory)")
    p.add_argument("--force", action="store_true", help=f"install even when the validation AUC is below {MIN_INSTALL_AUC}")
    p.add_argument("--seed", type=int, default=7, help="split and model seed (default: 7)")
    p.add_argument("--ips-clip", type=float, default=10.0, help="ranker: largest inverse-propensity weight (default: 10)")
    p.add_argument("--env", help="configuration environment for --install (default: HS_ENV, else Production)")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.ips_clip < 1:
        _parser().error("--ips-clip must be at least 1")
    commands = COMMANDS if args.model == "all" else (args.model,)
    trained: list[tuple[TrainedModel, tuple[Path, Path]]] = []
    failed = 0
    for command in commands:
        try:
            t = train_model(command, args.data, seed=args.seed, ips_clip=args.ips_clip)
            paths = write_model(t, args.out)
        except TrainingError as ex:
            print(f"train {command}: error: {ex}", file=sys.stderr)
            failed += 1
            continue
        print(_report(t, paths), flush=True)
        trained.append((t, paths))

    refused = 0
    if args.install and trained:
        from healthswap.platform.settings import load_settings

        directory = Path(load_settings(args.env).models.directory)
        installed: list[str] = []
        for t, paths in trained:
            auc = gate_auc(t)
            if auc < MIN_INSTALL_AUC and not args.force:
                print(f"install {t.spec.name}: refused, validation AUC {auc:.4f} < {MIN_INSTALL_AUC} (use --force to install anyway)", file=sys.stderr)
                refused += 1
                continue
            install(paths, directory)
            installed.append(f"{t.spec.name} {t.metadata['version']}")
        if installed:
            print(f"\nInstalled into {directory}: {', '.join(installed)}")
            print("The API loads models at startup. To switch a running API, call POST /admin/models/reload with an admin token")
            print("(scope hs.admin; development: POST /oauth2/token with the hs-admin client), for example")
            print('  curl -X POST http://localhost:8080/admin/models/reload -H "Authorization: Bearer <admin token>"')
            print("or restart it. Delete the files (or restore the previous ones) and reload to return to the heuristics.")
    return 1 if failed or refused else 0


if __name__ == "__main__":
    sys.exit(main())
