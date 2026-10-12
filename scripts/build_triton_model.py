"""Generate the tiny, deterministic ONNX model served by Triton."""

from pathlib import Path

import onnx
from onnx import TensorProto, helper


def main() -> None:
    graph = helper.make_graph(
        [helper.make_node("Mul", ["INPUT", "SCALE"], ["OUTPUT"])],
        "double",
        [helper.make_tensor_value_info("INPUT", TensorProto.DOUBLE, ["length"])],
        [helper.make_tensor_value_info("OUTPUT", TensorProto.DOUBLE, ["length"])],
        initializer=[helper.make_tensor("SCALE", TensorProto.DOUBLE, [], [2.0])],
    )
    model = helper.make_model(
        graph,
        producer_name="inference-guard",
        opset_imports=[helper.make_opsetid("", 13)],
        ir_version=8,
    )
    onnx.checker.check_model(model)
    destination = Path(__file__).resolve().parents[1] / "deploy/triton/models/double/1/model.onnx"
    destination.parent.mkdir(parents=True, exist_ok=True)
    onnx.save(model, destination)
    print(f"Wrote {destination} ({destination.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
