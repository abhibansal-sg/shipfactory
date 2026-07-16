"""Runner-owned pytest plugin emitting structured verification evidence."""

from __future__ import annotations

import json
import os
from pathlib import Path


class _EvidencePlugin:
    def __init__(self) -> None:
        self.counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
        self.collected = 0
        self.deselected = 0

    def pytest_collection_finish(self, session) -> None:
        self.collected = len(session.items)

    def pytest_deselected(self, items) -> None:
        self.deselected += len(items)

    def pytest_runtest_logreport(self, report) -> None:
        if report.when == "call":
            if report.passed:
                self.counts["passed"] += 1
            elif report.failed:
                self.counts["failed"] += 1
            elif report.skipped:
                self.counts["skipped"] += 1
        elif report.failed:
            self.counts["errors"] += 1

    def pytest_sessionfinish(self, session, exitstatus) -> None:
        path = os.environ.get("SHIPFACTORY_PYTEST_EVIDENCE_PATH")
        nonce = os.environ.get("SHIPFACTORY_PYTEST_EVIDENCE_NONCE")
        if not path or not nonce:
            return
        payload = {
            "schema": "shipfactory.pytest-evidence/v1",
            "nonce": nonce,
            "exitstatus": int(exitstatus),
            "collected": int(self.collected),
            "deselected": int(self.deselected),
            **self.counts,
        }
        target = Path(path)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        os.replace(temporary, target)


def pytest_configure(config) -> None:
    config.pluginmanager.register(_EvidencePlugin(), "shipfactory-structured-evidence")
