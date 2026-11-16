"""
Datamanager public package interface.

Typical usage
-------------
>>> from datamanager import app            # Typer CLI
>>> from datamanager import core           # Core helpers
>>> from datamanager import manifest       # Manifest helpers
"""

from __future__ import annotations

from importlib.metadata import version as _dist_version

from . import core as core
from . import manifest as manifest
from .__main__ import app as app  # keeps `python -m datamanager` handy

__all__ = ["__version__", "app", "core", "manifest"]
__version__: str = _dist_version("datamanager")
