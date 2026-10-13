"""Atomic JSON artifact output shared by runs, series, and comparisons."""

import json
import os
import tempfile
from pathlib import Path

from pydantic import BaseModel


def write_artifact(report: BaseModel, path: Path) -> None:
    """Atomic replacement avoids presenting a partially serialized report as evidence."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(report.model_dump(mode="json"), stream, indent=2, allow_nan=False)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
