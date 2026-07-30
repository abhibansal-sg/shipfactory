"""Read-only legacy-run drain and deletion-gate reporting."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import store


_TERMINAL_LEGACY_STATES = ("done", "cancelled", "failed")


def _backup_root() -> Path:
    return store._db_path().parent / "backups"


def _backup_archive_status() -> dict[str, Any]:
    root = _backup_root()
    candidates: list[dict[str, Any]] = []
    if root.is_dir():
        for database in sorted(root.rglob("shipfactory.db")):
            directory = database.parent
            manifests = sorted(
                path for path in directory.iterdir()
                if path.is_file() and path.name.lower().startswith("readme")
            )
            candidates.append({
                "database_path": str(database),
                "archive_manifest_paths": [str(path) for path in manifests],
                "ready": database.is_file() and database.stat().st_size > 0 and bool(manifests),
            })
    ready = [item for item in candidates if item["ready"]]
    return {
        "ready": bool(ready),
        "latest_ready": ready[-1] if ready else None,
        "candidate_count": len(candidates),
    }


def build_report() -> dict[str, Any]:
    """Project live legacy dependencies without initializing or mutating state."""
    with store._connect_readonly() as db:
        active = [
            {
                "id": row["id"],
                "project_id": row["project_id"],
                "board": row["board"],
                "recipe": f"{row['recipe_id']}@{row['recipe_version']}",
                "status": row["status"],
                "updated_at": row["updated_at"],
            }
            for row in db.execute(
                """SELECT id,project_id,board,recipe_id,recipe_version,status,updated_at
                   FROM recipe_instances
                   WHERE status NOT IN (?,?,?)
                   ORDER BY updated_at DESC,id""",
                _TERMINAL_LEGACY_STATES,
            )
        ]
        defaults = [
            {
                "project_id": row["project_id"],
                "recipe": row["default_recipe_key"],
                "updated_at": row["updated_at"],
            }
            for row in db.execute(
                """SELECT project_id,default_recipe_key,updated_at
                   FROM project_recipe_policies
                   WHERE default_recipe_key IS NOT NULL
                   ORDER BY project_id"""
            )
        ]
        activity = db.execute(
            "SELECT MAX(updated_at) FROM recipe_instances"
        ).fetchone()[0]

    api_clients = {
        "observable": False,
        "observed_count": None,
        "reason": "legacy API access telemetry is not available",
    }
    backup = _backup_archive_status()
    conditions = {
        "no_active_legacy_runs": {
            "passed": not active,
            "detail": f"{len(active)} active legacy run(s)",
        },
        "no_legacy_project_defaults": {
            "passed": not defaults,
            "detail": f"{len(defaults)} Project default(s) still select legacy recipes",
        },
        "no_observed_legacy_api_clients": {
            "passed": False,
            "detail": api_clients["reason"],
        },
        "backup_and_readonly_archive_ready": {
            "passed": bool(backup["ready"]),
            "detail": "verified database backup plus archive manifest" if backup["ready"] else "no verified backup/archive pair",
        },
        "graph_capability_journeys_complete": {
            "passed": False,
            "detail": "no durable live capability-attestation record exists for every required journey",
        },
        "dashboard_and_cli_legacy_calls_retired": {
            "passed": False,
            "detail": "legacy compatibility routes and CLI commands are still present",
        },
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "active_legacy_runs": active,
        "legacy_project_defaults": defaults,
        "legacy_api_clients": api_clients,
        "last_legacy_activity_at": activity,
        "backup_archive": backup,
        "conditions": conditions,
        "deletion_eligible": all(item["passed"] for item in conditions.values()),
    }


__all__ = ["build_report"]
