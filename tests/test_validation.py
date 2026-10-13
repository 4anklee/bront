import pytest
from pydantic import JsonValue, ValidationError

from inference_guard.validation import JsonCheck, at_pointer, parse_json, validate_json


@pytest.mark.parametrize(
    ("actual", "expected", "tolerance", "passed"),
    [
        (1.0, 1, 0, True),
        (True, 1, 0, False),
        ("1", 1, 0, False),
        ([1, 2], [[1, 2]], 0, False),
        ([1, 2], [1], 0, False),
        ({"a": 1, "b": 2}, {"a": 1}, 0, False),
        ({"a": [0.1 + 0.2]}, {"a": [0.3]}, 0.000001, True),
        ([2.1], [2.0], 0.001, False),
        (None, None, 0, True),
    ],
)
def test_shape_types_and_tolerances(actual, expected, tolerance, passed):
    check = JsonCheck(pointer="", equals=expected, abs_tol=tolerance)
    assert validate_json(actual, [check]).passed is passed


def test_missing_required_and_explicit_null_are_distinct():
    required = JsonCheck(pointer="/value")
    null = JsonCheck(pointer="/value", equals=None)
    assert validate_json({"value": 7}, [required]).passed
    assert validate_json({"value": None}, [null]).passed
    assert not validate_json({"value": 7}, [null]).passed
    assert validate_json({}, [required]).reason == "missing_path"
    assert validate_json({}, [null]).reason == "missing_path"


def test_json_pointer_escapes_and_array_indices():
    data: JsonValue = {"a/b": {"~": ["result"]}}
    assert at_pointer(data, "/a~1b/~0/0") == "result"
    for pointer in ("/a~1b/~0/00", "/a~1b/~0/-", "/a~1b/~0/١"):
        with pytest.raises(KeyError):
            at_pointer(data, pointer)
    with pytest.raises(ValidationError):
        JsonCheck(pointer="/bad~2escape")


@pytest.mark.parametrize("content", ['{"x":NaN}', "[1e999]", '{"x":1,"x":2}', b"\xff"])
def test_non_json_values_are_rejected(content):
    with pytest.raises(ValueError):
        parse_json(content)


def test_relative_tolerance_and_diagnostics():
    check = JsonCheck(pointer="/value", equals=100, rel_tol=0.01)
    assert validate_json({"value": 100.5}, [check]).passed
    result = validate_json({"value": "secret-output"}, [check])
    assert result.reason == "value_mismatch"
    assert result.check_index == 0
    assert "secret-output" not in result.model_dump_json()
