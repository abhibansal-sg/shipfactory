"""Isolated entry point for runner-controlled pytest evidence."""

from __future__ import annotations

import os
import sys
import importlib.util
from pathlib import Path

import pytest

_PLUGIN_PATH = Path(__file__).with_name("pytest_evidence.py")
_PLUGIN_SPEC = importlib.util.spec_from_file_location("_shipfactory_pytest_evidence", _PLUGIN_PATH)
if _PLUGIN_SPEC is None or _PLUGIN_SPEC.loader is None:
    raise RuntimeError("runner-owned pytest evidence plugin is unavailable")
_PLUGIN = importlib.util.module_from_spec(_PLUGIN_SPEC)
_PLUGIN_SPEC.loader.exec_module(_PLUGIN)
_EvidencePlugin = _PLUGIN._EvidencePlugin


def main() -> int:
    # The interpreter is started with -I, so candidate sitecustomize/modules
    # cannot run before the trusted plugin is imported. Add the candidate cwd
    # only after those imports so normal test-module imports still work.
    sys.path.insert(0, os.getcwd())
    sys.dont_write_bytecode = True
    return int(pytest.main(sys.argv[1:], plugins=[_EvidencePlugin()]))


if __name__ == "__main__":
    raise SystemExit(main())
