"""Repository-root Hermes plugin entry point.

The installed plugin directory is this repository root.  Keep the package
implementation under :mod:`shipfactory` while exposing the loader contract Hermes
expects at ``plugins/shipfactory/__init__.py``.
"""

from pathlib import Path
import sys


_ROOT = str(Path(__file__).resolve().parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from shipfactory import register

__all__ = ["register"]
