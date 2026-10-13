"""Atomic UTF-8 artifact output shared by JSON evidence and HTML reports."""

import json
import os
import tempfile
from pathlib import Path

from pydantic import BaseModel


def write_artifact(report: BaseModel, path: Path) -> None:
    write_text_artifact(
        json.dumps(report.model_dump(mode="json"), indent=2, allow_nan=False) + "\n", path
    )


def write_text_artifact(content: str, path: Path) -> None:
    """Atomic replacement avoids presenting a partially written artifact as evidence."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
