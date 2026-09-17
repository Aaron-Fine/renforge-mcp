"""Strict minimal-play support.

This package is intentionally independent of legacy project-local live state.
Public MCP registration is added only as each vertical slice becomes usable.
"""

from .contracts import SUPPORTED_RENPY_VERSION, Identity

__all__ = ["SUPPORTED_RENPY_VERSION", "Identity"]
