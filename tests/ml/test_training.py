"""ml/train.py: training on synthetic files with the real feature names, ONNX export, and loading through the serving ModelRegistry."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("sklearn")
pytest.importorskip("skl2onnx")
pytest.importorskip("onnxruntime")

import train  # noqa: E402  (ml/train.py, put on sys.path by tests/ml/conftest.py)

from healthswap.domain.knowledge.quality import EntityResolution  # noqa: E402
from healthswap.domain.recommendation.ranking import RankerFeatures, Substitutability  # noqa: E402
from healthswap.platform.ml.registry import ModelRegistry  # noqa: E402
from healthswap.platform.settings import ModelOptions  # noqa: E402


def _f32(values: Any) -> list[float]:
    return [float(np.float32(v)) for v in values]


def _sigmoid(z: float) -> float:
    return 1.0 / (1.0 + math.exp(-z))


def _write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def substitutability_rows(n: int = 800, seed: int = 1) -> list[dict[str, Any]]:
    """Rows like `hs ml export` writes, labelled with the gold rule's AND logic (non-linear: trees should win)."""
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n):
        f = _f32([
            rng.integers(0, 4), rng.random(), rng.choice([0.0, 0.5, 1.0]), rng.choice([0.0, 0.5, 1.0]), rng.choice([0.0, 0.5, 1.0]),
            rng.choice([0.0, 0.7, 1.0]), rng.random() * 1.4, rng.integers(0, 2), rng.integers(0, 2), float(rng.random() < 0.9),
        ])
        label = int(f[0] <= 2 and f[9] == 1 and f[5] > 0 and f[6] <= 0.69 and f[4] != 0 and (f[0] == 2 or f[2] != 0))
        rows.append({"market": "IN", "from": f"a{i % 60}", "to": f"b{i}", "fromName": "A", "toName": "B", "features": f, "label": label,
                     "heuristic": float(Substitutability.heuristic(f))})
    return rows


def er_rows(n: int = 480, seed: int = 2, random_labels: bool = False) -> list[dict[str, Any]]:
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n):
        f = _f32([rng.random(), rng.random(), rng.choice([0.0, 0.5, 0.8, 1.0]), rng.integers(0, 2), rng.random(), rng.random(), rng.random(),
                  float(rng.random() < 0.1)])
        z = -5.0 + 4.0 * f[0] + 3.0 * f[1] + 1.5 * f[2] + 1.0 * f[3] - 1.0 * f[4] + 1.0 * f[5] - 3.0 * f[7]
        label = int(rng.random() < (0.5 if random_labels else _sigmoid(z)))
        rows.append({"a": f"product {i // 8}", "b": f"listing {i}", "features": f, "label": label})
    return rows


def ranker_rows(n: int = 900, seed: int = 3) -> list[dict[str, Any]]:
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n):
        placement = int(rng.integers(0, 3))
        f = _f32([rng.random() * 40, 0.6 + rng.random() * 0.4, 0.7 + rng.random(), rng.integers(0, 2), rng.integers(0, 2), rng.random(),
                  placement == 0, placement == 1, 0, placement == 2, rng.integers(0, 2), rng.integers(0, 2), rng.random(), rng.integers(0, 2),
                  rng.integers(0, 3), rng.integers(0, 2)])
        z = -1.2 + 2.2 * f[1] + 0.035 * f[0] - 1.6 * max(0.0, f[2] - 1) + 0.4 * f[15] - 0.2 * f[7] + 0.3 * f[12]
        rows.append({"suggestionId": f"s{i // 3}", "rank": i % 3 + 1, "placement": "CART", "features": f, "label": int(rng.random() < _sigmoid(z)),
                     "propensity": float(0.2 + 0.8 * rng.random())})
    return rows


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    _write(data / "substitutability_pairs.jsonl", substitutability_rows())
    _write(data / "er_pairs.jsonl", er_rows())
    _write(data / "ranker_events.jsonl", ranker_rows())
    return data


def test_train_all_loads_through_the_model_registry(data_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "models"
    assert train.main(["all", "--data", str(data_dir), "--out", str(out), "--seed", "7"]) == 0
    printed = capsys.readouterr().out
    for name in ("substitutability", "ranker", "entity_resolution"):
        assert (out / f"{name}.onnx").is_file() and (out / f"{name}.meta.json").is_file()
        assert f"{name}: " in printed

    registry = ModelRegistry(ModelOptions(directory=out))
    versions = (registry.ranker_version, registry.substitutability_version, registry.entity_resolution_version)
    assert all(re.fullmatch(rf"{name}-(lr|gbm)-\d{{8}}\.\d{{4}}", v) for name, v in zip(("ranker", "substitutability", "entity_resolution"), versions))

    sub = [r["features"] for r in substitutability_rows(40, seed=11)]
    scores = registry.score_substitutability(sub)
    assert len(scores) == 40 and all(0 <= s <= 1 for s in scores) and scores != [Substitutability.heuristic(f) for f in sub]
    er = [r["features"] for r in er_rows(20, seed=12)]
    assert all(0 <= registry.score_entity_match(f) <= 1 for f in er)
    acceptance = registry.predict_acceptance([r["features"] for r in ranker_rows(30, seed=13)])
    assert acceptance is not None and len(acceptance) == 30 and all(0 <= a <= 1 for a in acceptance)
    registry.close()


def test_metadata_matches_the_registry_contract(data_dir: Path, tmp_path: Path) -> None:
    trained = train.train_model("er", data_dir, seed=7)
    _, meta_path = train.write_model(trained, tmp_path / "out")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["name"] == "entity_resolution" and meta["featureNames"] == list(EntityResolution.FEATURE_NAMES)
    assert (meta["inputName"], meta["outputName"], meta["positiveClassIndex"]) == ("input", "probabilities", 1)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", meta["trainedAt"])
    metrics = meta["metrics"]
    assert {"auc", "logLoss", "accuracy", "brier", "ece", "heuristicAuc", "trainRows", "validationRows"} <= set(metrics)
    assert all(isinstance(v, (int, float)) and math.isfinite(v) for v in metrics.values())
    assert metrics["auc"] > 0.75 and metrics["trainRows"] + metrics["validationRows"] == 480
    assert len(meta["calibration"]) == train.CALIBRATION_BINS and sum(b["count"] for b in meta["calibration"]) == metrics["validationRows"]
    assert trained.parity <= train.PARITY_TOLERANCE and meta["training"]["sha256"] == train.load_dataset(train.SPECS["er"], data_dir).sha256

    # The served code reads the same file: ModelMetadata.read, and predictions equal scikit-learn's.
    registry = ModelRegistry(ModelOptions(directory=tmp_path / "out"))
    assert registry.entity_resolution_version == meta["version"] and registry.entity_resolution is not None
    rows = np.asarray([r["features"] for r in er_rows(25, seed=5)], dtype=np.float32)
    rows[:, 7] = 0.0  # a GTIN conflict short-circuits to 0.05 before the model
    expected = trained.estimator.predict_proba(rows)[:, 1]
    assert all(abs(float(registry.score_entity_match(r.tolist())) - p) < 1e-4 for r, p in zip(rows, expected))


def test_model_selection_and_ips_metrics(data_dir: Path) -> None:
    sub = train.train_model("substitutability", data_dir, seed=7)
    assert sub.algorithm == "gbm" and sub.candidates["gbm"] <= sub.candidates["lr"] * (1 - train.GBM_MIN_GAIN)
    assert sub.metadata["algorithm"] == "GradientBoostingClassifier" and sub.metadata["metrics"]["auc"] > 0.9

    ranker = train.train_model("ranker", data_dir, seed=7, ips_clip=4.0)
    m = ranker.metadata["metrics"]
    assert ranker.algorithm == "lr" and ranker.metadata["featureNames"] == list(RankerFeatures.FEATURE_NAMES)
    assert {"aucIps", "logLossIps", "acceptRateIps", "meanPredictedIps", "effectiveSampleSize"} <= set(m) and m["ipsClip"] == 4.0
    assert 0 < m["effectiveSampleSize"] < m["validationRows"]  # unequal propensities -> unequal weights
    ds = train.load_dataset(train.SPECS["ranker"], data_dir, ips_clip=4.0)
    assert ds.weights.max() <= 4.0 and np.allclose(ds.weights, np.minimum(1 / ds.propensities, 4.0))

    # The same seed gives the same split and model.
    again = train.train_model("substitutability", data_dir, seed=7)
    assert again.metadata["metrics"] == sub.metadata["metrics"]


def test_registry_refuses_altered_feature_names(data_dir: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    out = tmp_path / "out"
    train.write_model(train.train_model("substitutability", data_dir, seed=7), out)
    registry = ModelRegistry(ModelOptions(directory=out))
    assert registry.substitutability_version.startswith("substitutability-")
    meta_path = out / "substitutability.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["featureNames"] = list(reversed(meta["featureNames"]))
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    registry.reload()
    assert registry.substitutability is None and registry.substitutability_version == "heuristic"
    assert "train/serve skew" in caplog.text
    registry.close()


def test_install_gate_and_force(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    data = tmp_path / "data"
    _write(data / "er_pairs.jsonl", er_rows(480, seed=4, random_labels=True))
    served = tmp_path / "served"
    monkeypatch.setenv("HS_MODELS__DIRECTORY", str(served))
    trained = train.train_model("er", data, seed=7)
    assert train.gate_auc(trained) < train.MIN_INSTALL_AUC  # labels carry no signal

    assert train.main(["er", "--data", str(data), "--out", str(tmp_path / "out"), "--install"]) == 1
    assert "refused, validation AUC" in capsys.readouterr().err and not served.exists()

    assert train.main(["er", "--data", str(data), "--out", str(tmp_path / "out"), "--install", "--force"]) == 0
    assert (served / "entity_resolution.onnx").is_file() and (served / "entity_resolution.meta.json").is_file()
    assert "POST /admin/models/reload" in capsys.readouterr().out


def test_bad_inputs_are_reported(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = tmp_path / "data"
    assert train.main(["ranker", "--data", str(data), "--out", str(tmp_path / "out")]) == 1
    assert "hs ml simulate" in capsys.readouterr().err

    rows = substitutability_rows(50)
    rows[3]["features"] = rows[3]["features"][:9]
    _write(data / "substitutability_pairs.jsonl", rows)
    with pytest.raises(train.TrainingError, match=r"substitutability_pairs.jsonl:4: expected 10 features \(category_distance, embedding_cos"):
        train.train_model("substitutability", data)

    bad = ranker_rows(30)
    bad[0]["propensity"] = 0
    _write(data / "ranker_events.jsonl", bad)
    with pytest.raises(train.TrainingError, match="propensity"):
        train.train_model("ranker", data)
