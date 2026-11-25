"""Trained models with transparent fallbacks (C# Ml/ModelRegistry.cs).

Each model is a binary classifier exported by ml/ as models/<name>.onnx plus models/<name>.meta.json; the metadata's
feature order is the contract between training and serving, so a model whose featureNames differ from the domain
constants is refused (train/serve skew) and the deterministic heuristic is used instead. Absent models degrade the same
way: ranker -> "heuristic-L2", substitutability and entity resolution -> "heuristic".

Scores are float32 probabilities rounded like C#: (decimal)Math.Round(p, 3 or 4), i.e. the double is rounded half-even
by Math.Round(double, digits) and converted with the .NET double -> decimal conversion. Unlike C#, `reload()` is
thread-safe: models are loaded under a lock and published by reference, and in-flight predictions keep using the
session they started with.
"""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import onnxruntime as ort
from onnxruntime.capi import onnxruntime_pybind11_state as _ort_state

from healthswap.domain.knowledge.quality import EntityResolution
from healthswap.domain.recommendation.ranking import RankerFeatures, Substitutability
from healthswap.domain.scoring.models import _json_int, _json_list, _json_map, _json_object, _json_props, _json_str, _json_str_or_none, _Reader
from healthswap.domain.text import double_to_decimal, round_double
from healthswap.platform import jsonutil
from healthswap.platform.settings import ModelOptions

log = logging.getLogger(__name__)

# OnnxRuntimeException: every error type of the native runtime (corrupt protobuf, invalid graph, missing file, ...).
_ORT_ERRORS: tuple[type[BaseException], ...] = tuple(
    v for v in vars(_ort_state).values() if isinstance(v, type) and issubclass(v, Exception)
)


def _json_double(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)) or not math.isfinite(float(value)):
        raise ValueError(f"$.{path}: the JSON value {value!r} could not be converted to a Double")
    return float(value)


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelMetadata:
    """Metadata written next to every ONNX artifact by ml/ (feature order is the contract between training and serving)."""

    name: str
    version: str
    feature_names: tuple[str, ...]
    input_name: str = "input"
    output_name: str = "probabilities"
    positive_class_index: int = 1
    metrics: Mapping[str, float] = field(default_factory=dict)
    trained_at: str | None = None

    @staticmethod
    def from_dict(data: Any, path: str = "") -> ModelMetadata:
        """JsonSerializer.Deserialize<ModelMetadata>(json, HsJson.Options): case-insensitive, name/version/featureNames required."""
        if data is None:
            raise ValueError("Model metadata is empty (null)")
        values = _json_object(data, path, "ModelMetadata", ("name", "version", "featureNames"))
        return ModelMetadata(**_json_props(values, path, _MODEL_METADATA))

    @staticmethod
    def read(path: str | Path) -> ModelMetadata:
        with open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
            return ModelMetadata.from_dict(jsonutil.loads(f.read()))


_MODEL_METADATA: tuple[tuple[str, str, _Reader], ...] = (
    ("name", "name", _json_str),
    ("version", "version", _json_str),
    ("featureNames", "feature_names", lambda v, p: _json_list(v, p, _json_str)),
    ("inputName", "input_name", _json_str),
    ("outputName", "output_name", _json_str),
    ("positiveClassIndex", "positive_class_index", _json_int),
    ("metrics", "metrics", lambda v, p: _json_map(v, p, _json_double)),
    ("trainedAt", "trained_at", _json_str_or_none),
)


class OnnxBinaryModel:
    """Binary classifier evaluated in-process with ONNX Runtime (HLD: "Ranker - in-process model")."""

    def __init__(self, session: ort.InferenceSession, metadata: ModelMetadata) -> None:
        self._session: ort.InferenceSession | None = session
        self.metadata = metadata

    @staticmethod
    def try_load(directory: str | Path, name: str, expected_features: Sequence[str]) -> OnnxBinaryModel | None:
        """The model, or None when the files are missing or the feature order differs from `expected_features`.
        A malformed meta.json raises ValueError; ONNX Runtime errors propagate (ModelRegistry.reload handles them)."""
        onnx = Path(directory) / f"{name}.onnx"
        meta = Path(directory) / f"{name}.meta.json"
        if not onnx.is_file() or not meta.is_file():
            return None
        metadata = ModelMetadata.read(meta)
        if tuple(metadata.feature_names) != tuple(expected_features):
            log.error("Refusing to load %s %s: feature order differs from the serving code (train/serve skew)", name, metadata.version)
            return None
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        return OnnxBinaryModel(ort.InferenceSession(str(onnx), sess_options=options, providers=["CPUExecutionProvider"]), metadata)

    def predict(self, rows: Sequence[Sequence[float]]) -> list[float]:
        """Probability of the positive class per row (float32 values); rows hold at least len(feature_names) features."""
        if len(rows) == 0:
            return []
        session = self._session
        if session is None:
            raise ValueError("Cannot access a disposed object (OnnxBinaryModel).")
        width = len(self.metadata.feature_names)
        tensor = np.array([[row[c] for c in range(width)] for row in rows], dtype=np.float32).reshape(len(rows), width)
        outputs = session.run(None, {self.metadata.input_name: tensor})
        names = [o.name for o in session.get_outputs()]
        if self.metadata.output_name not in names:
            raise ValueError(f"The model has no output named '{self.metadata.output_name}'")
        probabilities = np.asarray(outputs[names.index(self.metadata.output_name)], dtype=np.float32)
        return [float(probabilities[r, self.metadata.positive_class_index]) for r in range(len(rows))]

    def close(self) -> None:
        """Releases the session (C# Dispose)."""
        self._session = None


_MODELS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ranker", tuple(RankerFeatures.FEATURE_NAMES)),
    ("substitutability", tuple(Substitutability.FEATURE_NAMES)),
    ("entity_resolution", tuple(EntityResolution.FEATURE_NAMES)),
)


class ModelRegistry:
    """Trained models with transparent fallbacks: absent or skewed models degrade to deterministic heuristics (L2)."""

    def __init__(self, options: ModelOptions) -> None:
        self.options = options
        self._lock = threading.RLock()
        self._ranker: OnnxBinaryModel | None = None
        self._substitutability: OnnxBinaryModel | None = None
        self._entity_resolution: OnnxBinaryModel | None = None
        self.reload()

    @property
    def ranker(self) -> OnnxBinaryModel | None:
        return self._ranker

    @property
    def substitutability(self) -> OnnxBinaryModel | None:
        return self._substitutability

    @property
    def entity_resolution(self) -> OnnxBinaryModel | None:
        return self._entity_resolution

    @property
    def ranker_version(self) -> str:
        m = self._ranker
        return "heuristic-L2" if m is None else m.metadata.version

    @property
    def substitutability_version(self) -> str:
        m = self._substitutability
        return "heuristic" if m is None else m.metadata.version

    @property
    def entity_resolution_version(self) -> str:
        m = self._entity_resolution
        return "heuristic" if m is None else m.metadata.version

    def reload(self) -> None:
        """(Re)loads ranker, substitutability and entity_resolution in that order. An ONNX Runtime error is logged and the
        remaining models stay on heuristics; a malformed meta.json propagates (with the models loaded so far published)."""
        with self._lock:
            loaded: dict[str, OnnxBinaryModel | None] = {name: None for name, _ in _MODELS}
            try:
                for name, features in _MODELS:
                    loaded[name] = OnnxBinaryModel.try_load(self.options.directory, name, features)
            except _ORT_ERRORS as ex:
                log.error("Failed to load ONNX models from %s; using heuristics", self.options.directory, exc_info=ex)
            finally:
                # Published by reference; the previous sessions are released once in-flight predictions finish.
                self._ranker = loaded["ranker"]
                self._substitutability = loaded["substitutability"]
                self._entity_resolution = loaded["entity_resolution"]
            log.info("Models: ranker=%s substitutability=%s er=%s", self.ranker_version, self.substitutability_version, self.entity_resolution_version)

    def score_substitutability(self, features: Sequence[Sequence[float]]) -> list[Decimal]:
        """Substitutability per candidate pair (model probability rounded to 3 places, else the heuristic)."""
        m = self._substitutability
        if m is not None:
            return [double_to_decimal(round_double(p, 3)) for p in m.predict(features)]
        return [Substitutability.heuristic(f) for f in features]

    def score_entity_match(self, features: Sequence[float]) -> Decimal:
        """Entity-resolution match score; different GTINs (feature 7) never merge, whatever the model says."""
        if features[7] > 0:
            return Decimal("0.05")
        m = self._entity_resolution
        return double_to_decimal(round_double(m.predict([features])[0], 4)) if m is not None else EntityResolution.heuristic_score(features)

    def predict_acceptance(self, features: Sequence[Sequence[float]]) -> list[Decimal] | None:
        """Ranker acceptance probabilities rounded to 4 places, or None when no ranker model is loaded (heuristic-L2)."""
        m = self._ranker
        return None if m is None else [double_to_decimal(round_double(p, 4)) for p in m.predict(features)]

    def close(self) -> None:
        """C# Dispose: releases the sessions; the registry falls back to heuristics until reload()."""
        with self._lock:
            models = (self._ranker, self._substitutability, self._entity_resolution)
            self._ranker = self._substitutability = self._entity_resolution = None
        for m in models:
            if m is not None:
                m.close()

    dispose = close
