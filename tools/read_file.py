"""Implementation of the `read_file` tool surfaced to Nate."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any, Dict, Optional

from .exceptions import ToolExecutionError

# Soft limit to keep responses manageable for the model.
_MAX_CHARS = 16_000


def _clamp_lines(total_lines: int, start_line: Optional[int], end_line: Optional[int]) -> tuple[int, int]:
    start = 1 if start_line is None else max(1, int(start_line))
    end = total_lines if end_line is None else max(start, int(end_line))
    # convert to zero-based slice indices later, but keep 1-indexed values for metadata
    return start, min(end, total_lines)


def _read_text(path: Path) -> list[str]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return handle.readlines()
    except UnicodeDecodeError as exc:
        raise ToolExecutionError(f"File {path} is not UTF-8 text; unable to read") from exc
    except OSError as exc:  # pragma: no cover - defensive
        raise ToolExecutionError(f"Unable to read file {path}: {exc}") from exc


def run(parameters: Dict[str, Any], *, repo_root: Path) -> Dict[str, Any]:
    """Entry point for the read_file tool."""

    relative_path = parameters.get("path")
    if not relative_path or not isinstance(relative_path, str):
        raise ToolExecutionError("`path` must be provided as a string")

    resolved_path = (repo_root / relative_path).resolve()
    if not resolved_path.is_file():
        raise ToolExecutionError(f"Requested path {relative_path} does not exist or is not a file")

    try:
        resolved_path.relative_to(repo_root.resolve())
    except ValueError as exc:  # pragma: no cover - defensive
        raise ToolExecutionError("Attempted to read outside the repository root") from exc

    lines = _read_text(resolved_path)
    start_line, end_line = _clamp_lines(len(lines), parameters.get("start_line"), parameters.get("end_line"))
    # Convert to zero-based indices for slicing (end inclusive in user terms).
    start_idx = start_line - 1
    end_idx = end_line
    selected_lines = lines[start_idx:end_idx]

    buffer = io.StringIO()
    truncated = False
    running_length = 0
    for line in selected_lines:
        if running_length + len(line) > _MAX_CHARS:
            buffer.write(line[: max(0, _MAX_CHARS - running_length)])
            truncated = True
            break
        buffer.write(line)
        running_length += len(line)

    return {
        "path": str(resolved_path.relative_to(repo_root)),
        "start_line": start_line,
        "end_line": end_line,
        "content": buffer.getvalue(),
        "truncated": truncated,
        "total_lines": len(lines),
    }


__all__ = ["run"]
