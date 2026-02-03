"""Shared exception types for Nate's tool implementations."""

from __future__ import annotations


class ToolExecutionError(RuntimeError):
    """Raised when a tool cannot fulfil the requested action."""


__all__ = ["ToolExecutionError"]
