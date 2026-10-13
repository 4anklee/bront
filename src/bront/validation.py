"""Strict JSON response assertions; diagnostics never include response values."""

import json
import math
import re
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


def finite_json(value: JsonValue) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("JSON numbers must be finite")
    if isinstance(value, list):
        for item in value:
            finite_json(item)
    if isinstance(value, dict):
        for item in value.values():
            finite_json(item)


def parse_json(content: str | bytes) -> JsonValue:
    def unique_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("Duplicate JSON object member")
            result[key] = item
        return result

    value = json.loads(content, object_pairs_hook=unique_object)
    finite_json(value)
    return value


def pointer_tokens(pointer: str) -> list[str]:
    if pointer == "":
        return []
    if not pointer.startswith("/") or re.search(r"~(?![01])", pointer):
        raise ValueError(
            "Expected a JSON Pointer: empty or slash-prefixed, using ~0 and ~1 escapes"
        )
    return [token.replace("~1", "/").replace("~0", "~") for token in pointer[1:].split("/")]


def at_pointer(value: JsonValue, pointer: str) -> JsonValue:
    for token in pointer_tokens(pointer):
        if isinstance(value, dict):
            value = value[token]
        elif isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]*", token):
            value = value[int(token)]
        else:
            raise KeyError("Missing JSON Pointer")
    return value


def equivalent(actual: JsonValue, expected: JsonValue, absolute: float, relative: float) -> bool:
    # bool is a Python int subclass, but true must never pass an expected numeric 1.
    if (
        isinstance(actual, (int, float))
        and not isinstance(actual, bool)
        and isinstance(expected, (int, float))
        and not isinstance(expected, bool)
    ):
        if actual == expected:
            return True
        try:
            return math.isclose(actual, expected, abs_tol=absolute, rel_tol=relative)
        except OverflowError:
            return False
    if type(actual) is not type(expected):
        return False
    if isinstance(actual, list) and isinstance(expected, list):
        return len(actual) == len(expected) and all(
            equivalent(a, e, absolute, relative) for a, e in zip(actual, expected, strict=True)
        )
    if isinstance(actual, dict) and isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(
            equivalent(actual[key], expected[key], absolute, relative) for key in actual
        )
    return actual == expected


class JsonCheck(StrictModel):
    pointer: str
    equals: JsonValue = None
    abs_tol: float = Field(default=0, ge=0)
    rel_tol: float = Field(default=0, ge=0)

    @field_validator("pointer")
    @classmethod
    def valid_pointer(cls, value: str) -> str:
        pointer_tokens(value)
        return value

    @model_validator(mode="after")
    def valid_expected(self) -> Self:
        finite_json(self.equals)
        if (self.abs_tol or self.rel_tol) and "equals" not in self.model_fields_set:
            raise ValueError("Tolerance requires an expected value")
        return self


class ValidationResult(StrictModel):
    passed: bool
    reason: str | None = None
    check_index: int | None = None


def validate_json(value: JsonValue, checks: list[JsonCheck]) -> ValidationResult:
    for index, check in enumerate(checks):
        try:
            actual = at_pointer(value, check.pointer)
        except KeyError, IndexError, ValueError:
            return ValidationResult(passed=False, reason="missing_path", check_index=index)
        if "equals" in check.model_fields_set and not equivalent(
            actual, check.equals, check.abs_tol, check.rel_tol
        ):
            return ValidationResult(passed=False, reason="value_mismatch", check_index=index)
    return ValidationResult(passed=True)
