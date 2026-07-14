"""Repository-root Hermes plugin entry point.

The installed plugin directory is this repository root.  Keep the package
implementation under :mod:`headframe` while exposing the loader contract Hermes
expects at ``plugins/headframe/__init__.py``.
"""

from pathlib import Path
import sys


_ROOT = str(Path(__file__).resolve().parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from headframe import register

__all__ = ["register"]
