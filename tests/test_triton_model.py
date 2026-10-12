import sys
from pathlib import Path

import numpy as np
import onnx
from onnx.reference import ReferenceEvaluator


def test_committed_model_matches_fallback_arithmetic():
    path = Path(__file__).resolve().parents[1] / "deploy/triton/models/double/1/model.onnx"
    model = onnx.load(path)
    onnx.checker.check_model(model)
    evaluator = ReferenceEvaluator(model)
    for values in (
        [0.1, -2.25, 0.0, 1e100, 1e-100],
        [3.5],
        list(range(1024)),
        [5e-324, 1e-310, sys.float_info.max / 2],
    ):
        (outputs,) = evaluator.run(None, {"INPUT": np.array(values, dtype=np.float64)})
        assert isinstance(outputs, np.ndarray)
        assert outputs.dtype == np.float64
        assert outputs.tolist() == [value * 2 for value in values]
