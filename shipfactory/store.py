"""SQLite persistence for Hermes Factory state."""

from __future__ import annotations

import json
import hashlib
import errno
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


DAEMON_RUN_TASK_ID = "__shipfactory_daemon__"


_BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, seat TEXT NOT NULL,
  executor TEXT NOT NULL, model TEXT NOT NULL, pid INTEGER, started_at TEXT NOT NULL,
  ended_at TEXT, exit_code INTEGER, tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0,
  tokens_total INTEGER DEFAULT 0, duration_s REAL, result TEXT);
CREATE TABLE IF NOT EXISTS policies (task_id TEXT PRIMARY KEY, policy_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, stage_id TEXT NOT NULL,
  stage_type TEXT NOT NULL, seat TEXT NOT NULL, outcome TEXT NOT NULL, body TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS monitors (
  task_id TEXT PRIMARY KEY, next_check_at TEXT NOT NULL, timeout_at TEXT,
  max_attempts INTEGER NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0,
  recovery_policy TEXT NOT NULL, notes TEXT, scheduled_by TEXT,
  interval_seconds INTEGER NOT NULL DEFAULT 300);
CREATE TABLE IF NOT EXISTS watchdogs (
  root_task_id TEXT PRIMARY KEY, agent TEXT NOT NULL, instructions TEXT NOT NULL, last_fingerprint TEXT);
CREATE TABLE IF NOT EXISTS seat_state (seat TEXT PRIMARY KEY, paused INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS sync (
  gh_number INTEGER PRIMARY KEY, task_id TEXT NOT NULL, gh_updated TEXT, k_updated TEXT, last_synced_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS recipe_versions (
  id TEXT NOT NULL, version INTEGER NOT NULL, hash TEXT NOT NULL, status TEXT NOT NULL,
  normalized_yaml TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(id, version));
CREATE TABLE IF NOT EXISTS recipe_instances (
  id TEXT PRIMARY KEY, board TEXT NOT NULL, collector_task_id TEXT NOT NULL,
  recipe_id TEXT NOT NULL, recipe_version INTEGER NOT NULL, recipe_hash TEXT NOT NULL,
  status TEXT NOT NULL, parameters_json TEXT NOT NULL, activation_count INTEGER NOT NULL DEFAULT 0,
  tokens_charged INTEGER NOT NULL DEFAULT 0, blocked_reason TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS recipe_steps (
  instance_id TEXT NOT NULL, step_id TEXT NOT NULL, activation INTEGER NOT NULL,
  primitive TEXT NOT NULL, state TEXT NOT NULL, kanban_task_id TEXT UNIQUE,
  input_revision_hash TEXT, output_revision INTEGER, finding_count INTEGER, blocked_reason TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY(instance_id, step_id, activation),
  FOREIGN KEY(instance_id) REFERENCES recipe_instances(id));
CREATE TABLE IF NOT EXISTS advance_events (
  key TEXT PRIMARY KEY, instance_id TEXT, source TEXT NOT NULL, payload_json TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL, applied_at TEXT);
CREATE INDEX IF NOT EXISTS idx_advance_events_pending ON advance_events(state, created_at);
CREATE TABLE IF NOT EXISTS budget_charges (
  key TEXT PRIMARY KEY, board TEXT NOT NULL, utc_day TEXT NOT NULL, instance_id TEXT NOT NULL,
  step_id TEXT NOT NULL, activation INTEGER NOT NULL, tokens INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_budget_charges_day ON budget_charges(board, utc_day);
CREATE TABLE IF NOT EXISTS outbox (
  key TEXT PRIMARY KEY, target TEXT NOT NULL, message TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT NOT NULL, delivered_at TEXT, last_error TEXT);
CREATE TABLE IF NOT EXISTS triage_selections (
  id TEXT PRIMARY KEY, source_task_id TEXT NOT NULL UNIQUE, board TEXT NOT NULL, lease_until TEXT,
  ranked_json TEXT NOT NULL, chosen_recipe TEXT, parameters_json TEXT, skip_steps_json TEXT,
  outcome TEXT, root_collector_task_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
"""


_A0_MIGRATION_STATEMENTS = (
    "ALTER TABLE advance_events ADD COLUMN lease_owner TEXT",
    "ALTER TABLE advance_events ADD COLUMN lease_until TEXT",
    "ALTER TABLE advance_events ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE advance_events ADD COLUMN expected_activation INTEGER",
    "ALTER TABLE advance_events ADD COLUMN expected_state TEXT",
    "ALTER TABLE advance_events ADD COLUMN outcome TEXT",
    "ALTER TABLE advance_events ADD COLUMN last_error TEXT",
    "ALTER TABLE outbox ADD COLUMN lease_owner TEXT",
    "ALTER TABLE outbox ADD COLUMN lease_until TEXT",
    """CREATE TABLE action_intents (
    key             TEXT PRIMARY KEY,
    logical_key     TEXT NOT NULL,
    attempt         INTEGER NOT NULL,
    instance_id     TEXT,
    step_id         TEXT,
    activation      INTEGER,
    kind            TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    state           TEXT NOT NULL,
    lease_owner     TEXT,
    lease_until     TEXT,
    started_at      TEXT,
    finished_at     TEXT,
    result_json     TEXT,
    last_error      TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE(logical_key, attempt)
)""",
    """CREATE INDEX idx_action_intents_ready
ON action_intents(state, lease_until, created_at)""",
    """CREATE TABLE resource_leases (
    key               TEXT PRIMARY KEY,
    kind              TEXT NOT NULL,
    units             INTEGER NOT NULL,
    instance_id       TEXT,
    step_id           TEXT,
    activation        INTEGER,
    state             TEXT NOT NULL,
    lease_until       TEXT,
    metadata_json     TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    released_at       TEXT
)""",
)
_A0_MIGRATION_TEXT = ";\n".join(_A0_MIGRATION_STATEMENTS) + ";\n"
_A1_MIGRATION_STATEMENTS = (
    "ALTER TABLE runs ADD COLUMN board TEXT",
    "ALTER TABLE runs ADD COLUMN workspace_path TEXT",
    "ALTER TABLE runs ADD COLUMN log_path TEXT",
    "ALTER TABLE runs ADD COLUMN prompt_path TEXT",
    "ALTER TABLE runs ADD COLUMN provider TEXT",
    "ALTER TABLE runs ADD COLUMN resolved_model TEXT",
    "ALTER TABLE runs ADD COLUMN executor_version TEXT",
    "ALTER TABLE runs ADD COLUMN process_start_token TEXT",
    "ALTER TABLE monitors ADD COLUMN state TEXT NOT NULL DEFAULT 'active'",
    "ALTER TABLE monitors ADD COLUMN last_outcome TEXT",
    "ALTER TABLE monitors ADD COLUMN last_error TEXT",
    "ALTER TABLE monitors ADD COLUMN last_checked_at TEXT",
    "CREATE INDEX idx_resource_leases_active ON resource_leases(kind,state,lease_until)",
)
_A1_MIGRATION_TEXT = ";\n".join(_A1_MIGRATION_STATEMENTS) + ";\n"
_A1_FENCING_MIGRATION_STATEMENTS = (
    "ALTER TABLE runs ADD COLUMN task_attempt_id INTEGER",
)
_A1_FENCING_MIGRATION_TEXT = ";\n".join(_A1_FENCING_MIGRATION_STATEMENTS) + ";\n"
_ARTIFACT_MIGRATION_STATEMENTS = (
    """CREATE TABLE artifacts (
    id                    TEXT PRIMARY KEY,
    instance_id           TEXT NOT NULL,
    step_id               TEXT NOT NULL,
    activation            INTEGER NOT NULL,
    run_id                INTEGER,
    kind                  TEXT NOT NULL,
    schema_version        INTEGER NOT NULL,
    state                 TEXT NOT NULL,
    candidate_path        TEXT,
    sealed_path           TEXT,
    sha256                TEXT,
    size_bytes            INTEGER,
    producer              TEXT NOT NULL,
    trust_domain          TEXT,
    base_sha              TEXT NOT NULL,
    head_sha              TEXT,
    repo_tree_sha         TEXT NOT NULL,
    validation_error      TEXT,
    created_at            TEXT NOT NULL,
    sealed_at             TEXT,
    UNIQUE(instance_id, step_id, activation, kind)
)""",
    """CREATE TABLE artifact_edges (
    parent_artifact_id  TEXT NOT NULL,
    child_artifact_id   TEXT NOT NULL,
    relation            TEXT NOT NULL,
    PRIMARY KEY(parent_artifact_id, child_artifact_id, relation)
)""",
    "ALTER TABLE recipe_steps ADD COLUMN input_artifact_set_hash TEXT",
    "ALTER TABLE recipe_steps ADD COLUMN output_artifact_set_hash TEXT",
)
_ARTIFACT_MIGRATION_TEXT = ";\n".join(_ARTIFACT_MIGRATION_STATEMENTS) + ";\n"
_INSTANCE_BASE_MIGRATION_STATEMENTS = (
    "ALTER TABLE recipe_instances ADD COLUMN base_sha TEXT",
    "ALTER TABLE recipe_instances ADD COLUMN updated_base_at TEXT",
    """UPDATE recipe_instances
SET base_sha=(
        SELECT a.base_sha FROM artifacts a
        WHERE a.instance_id=recipe_instances.id AND a.state='sealed'
        ORDER BY a.sealed_at DESC,a.created_at DESC LIMIT 1
    ),
    updated_base_at=(
        SELECT COALESCE(a.sealed_at,a.created_at) FROM artifacts a
        WHERE a.instance_id=recipe_instances.id AND a.state='sealed'
        ORDER BY a.sealed_at DESC,a.created_at DESC LIMIT 1
    )""",
)
_INSTANCE_BASE_MIGRATION_TEXT = ";\n".join(_INSTANCE_BASE_MIGRATION_STATEMENTS) + ";\n"
_PLANNING_BUDGET_MIGRATION_STATEMENTS = (
    "ALTER TABLE budget_charges ADD COLUMN token_pool TEXT",
    "CREATE INDEX idx_budget_charges_pool ON budget_charges(instance_id,token_pool)",
)
_PLANNING_BUDGET_MIGRATION_TEXT = ";\n".join(
    _PLANNING_BUDGET_MIGRATION_STATEMENTS
) + ";\n"

_ENVIRONMENT_SESSION_MIGRATION_STATEMENTS = (
    """CREATE TABLE env_sessions (
    id                    TEXT PRIMARY KEY,
    key                   TEXT NOT NULL,
    base_sha              TEXT NOT NULL,
    candidate_sha         TEXT,
    manifest_path         TEXT NOT NULL,
    manifest_blob_sha     TEXT NOT NULL,
    tracked_input_hash    TEXT NOT NULL,
    workspace_path        TEXT NOT NULL,
    state                 TEXT NOT NULL,
    pid                   INTEGER,
    process_start_token   TEXT,
    control_plane_risk    INTEGER NOT NULL DEFAULT 0,
    control_plane_paths   TEXT,
    lease_key             TEXT,
    stdout_path           TEXT,
    stderr_path           TEXT,
    created_at            TEXT NOT NULL,
    started_at            TEXT,
    finished_at           TEXT,
    last_error            TEXT
)""",
    "CREATE INDEX idx_env_sessions_key ON env_sessions(key, created_at)",
    """CREATE TABLE app_sessions (
    id                    TEXT PRIMARY KEY,
    env_session_id        TEXT NOT NULL,
    request_key           TEXT NOT NULL,
    workspace_path        TEXT NOT NULL,
    state                 TEXT NOT NULL,
    pid                   INTEGER,
    process_start_token   TEXT,
    port                  INTEGER,
    port_lease_key        TEXT,
    app_url               TEXT,
    health_status         TEXT,
    stdout_path           TEXT,
    stderr_path           TEXT,
    created_at            TEXT NOT NULL,
    started_at            TEXT,
    healthy_at            TEXT,
    stopping_at           TEXT,
    stopped_at            TEXT,
    last_error            TEXT,
    UNIQUE(request_key)
)""",
    "CREATE INDEX idx_app_sessions_state ON app_sessions(state)",
)
_ENVIRONMENT_SESSION_MIGRATION_TEXT = ";\n".join(_ENVIRONMENT_SESSION_MIGRATION_STATEMENTS) + ";\n"
_ENVIRONMENT_ENFORCEMENT_MIGRATION_STATEMENTS = (
    "ALTER TABLE env_sessions ADD COLUMN network_enforcement_level TEXT",
    "ALTER TABLE env_sessions ADD COLUMN output_cap_exceeded INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE app_sessions ADD COLUMN network_enforcement_level TEXT",
    "ALTER TABLE app_sessions ADD COLUMN output_cap_exceeded INTEGER NOT NULL DEFAULT 0",
)
_ENVIRONMENT_ENFORCEMENT_MIGRATION_TEXT = (
    ";\n".join(_ENVIRONMENT_ENFORCEMENT_MIGRATION_STATEMENTS) + ";\n"
)
_RUN_ACCESS_ENFORCEMENT_MIGRATION_STATEMENTS = (
    "ALTER TABLE runs ADD COLUMN access_enforcement_level TEXT",
)
_RUN_ACCESS_ENFORCEMENT_MIGRATION_TEXT = (
    ";\n".join(_RUN_ACCESS_ENFORCEMENT_MIGRATION_STATEMENTS) + ";\n"
)
_VERIFICATION_MIGRATION_STATEMENTS = (
    """CREATE TABLE evidence_bundles (
    id                    TEXT PRIMARY KEY,
    instance_id           TEXT NOT NULL,
    step_id               TEXT NOT NULL,
    activation            INTEGER NOT NULL,
    input_revision_hash   TEXT NOT NULL,
    base_sha              TEXT NOT NULL,
    head_sha              TEXT NOT NULL,
    tree_sha              TEXT NOT NULL,
    environment_session_id TEXT,
    manifest_relpath      TEXT NOT NULL,
    manifest_blob_sha     TEXT NOT NULL,
    state                 TEXT NOT NULL,
    bundle_sha256         TEXT,
    redaction_state       TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    sealed_at             TEXT,
    invalid_reason        TEXT,
    UNIQUE(instance_id, step_id, activation)
)""",
    """CREATE TABLE evidence_items (
    id               TEXT PRIMARY KEY,
    bundle_id        TEXT NOT NULL,
    case_id          TEXT,
    kind             TEXT NOT NULL,
    path             TEXT NOT NULL,
    sha256           TEXT NOT NULL,
    size_bytes       INTEGER NOT NULL,
    mime_type        TEXT,
    producer         TEXT NOT NULL,
    command_json     TEXT,
    cwd_relpath      TEXT,
    env_digest       TEXT,
    exit_code        INTEGER,
    started_at       TEXT,
    ended_at         TEXT,
    metadata_json    TEXT NOT NULL
)""",
    """CREATE TABLE verification_cases (
    bundle_id                TEXT NOT NULL,
    case_id                  TEXT NOT NULL,
    attempt                  INTEGER NOT NULL,
    requirement_ids_json     TEXT NOT NULL,
    oracle_type              TEXT NOT NULL,
    oracle_json              TEXT NOT NULL,
    status                   TEXT NOT NULL,
    evidence_item_ids_json   TEXT NOT NULL,
    started_at               TEXT NOT NULL,
    ended_at                 TEXT,
    PRIMARY KEY(bundle_id, case_id, attempt)
)""",
)
_VERIFICATION_MIGRATION_TEXT = ";\n".join(_VERIFICATION_MIGRATION_STATEMENTS) + ";\n"
_GATE_DECISION_MIGRATION_STATEMENTS = (
    """CREATE TABLE gate_decisions (
    id                   TEXT PRIMARY KEY,
    instance_id          TEXT NOT NULL,
    step_id              TEXT NOT NULL,
    activation           INTEGER NOT NULL,
    revision_hash        TEXT NOT NULL,
    evidence_bundle_id   TEXT,
    evidence_bundle_hash TEXT,
    actor_kind           TEXT NOT NULL,
    actor_id             TEXT NOT NULL,
    channel              TEXT NOT NULL,
    decision             TEXT NOT NULL,
    reason               TEXT,
    nonce_hash           TEXT,
    policy_hash          TEXT,
    created_at           TEXT NOT NULL,
    consumed_at          TEXT,
    advance_event_key    TEXT UNIQUE
)""",
)
_GATE_DECISION_MIGRATION_TEXT = (
    ";\n".join(_GATE_DECISION_MIGRATION_STATEMENTS) + ";\n"
)
_VERIFICATION_HARDENING_MIGRATION_STATEMENTS = (
    "ALTER TABLE evidence_bundles ADD COLUMN phase_b_eligible INTEGER",
)
_VERIFICATION_HARDENING_MIGRATION_TEXT = (
    ";\n".join(_VERIFICATION_HARDENING_MIGRATION_STATEMENTS) + ";\n"
)
_VERIFICATION_PRODUCTION_BINDING_MIGRATION_STATEMENTS = (
    "ALTER TABLE runs ADD COLUMN recipe_activation INTEGER",
    "ALTER TABLE recipe_steps ADD COLUMN producer_run_id INTEGER",
    "ALTER TABLE evidence_bundles ADD COLUMN workspace_path TEXT",
    "ALTER TABLE evidence_bundles ADD COLUMN workspace_owner_task_id TEXT",
    "ALTER TABLE evidence_bundles ADD COLUMN workspace_owner_activation INTEGER",
    "ALTER TABLE evidence_bundles ADD COLUMN workspace_owner_run_id INTEGER",
    "ALTER TABLE evidence_bundles ADD COLUMN required_surface TEXT",
    "ALTER TABLE evidence_bundles ADD COLUMN environment_identity_json TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE evidence_items ADD COLUMN attempt INTEGER",
)
_VERIFICATION_PRODUCTION_BINDING_MIGRATION_TEXT = (
    ";\n".join(_VERIFICATION_PRODUCTION_BINDING_MIGRATION_STATEMENTS) + ";\n"
)
_APP_SESSION_IDENTITY_MIGRATION_STATEMENTS = (
    "ALTER TABLE app_sessions ADD COLUMN expected_instance_id TEXT",
    "ALTER TABLE app_sessions ADD COLUMN expected_head_sha TEXT",
)
_APP_SESSION_IDENTITY_MIGRATION_TEXT = (
    ";\n".join(_APP_SESSION_IDENTITY_MIGRATION_STATEMENTS) + ";\n"
)
_CONTAINMENT_OVERLAY_MIGRATION_STATEMENTS = (
    "ALTER TABLE recipe_instances ADD COLUMN parent_tasks_json TEXT",
    "ALTER TABLE recipe_steps ADD COLUMN rejected_by_step_id TEXT",
    "ALTER TABLE recipe_steps ADD COLUMN rejected_by_activation INTEGER",
    "ALTER TABLE recipe_steps ADD COLUMN verdict_json TEXT",
)
_CONTAINMENT_OVERLAY_MIGRATION_TEXT = (
    ";\n".join(_CONTAINMENT_OVERLAY_MIGRATION_STATEMENTS) + ";\n"
)
_PROJECT_RECIPE_POLICY_MIGRATION_STATEMENTS = (
    """CREATE TABLE project_recipe_policies (
  project_id TEXT PRIMARY KEY,
  allowed_recipe_keys_json TEXT NOT NULL,
  default_recipe_key TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
)""",
    "ALTER TABLE recipe_instances ADD COLUMN project_id TEXT",
    "ALTER TABLE recipe_instances ADD COLUMN linear_issue_id TEXT",
    "ALTER TABLE recipe_instances ADD COLUMN launch_idempotency_key TEXT",
    "CREATE INDEX idx_project_recipe_policies_updated ON project_recipe_policies(updated_at)",
    "CREATE INDEX idx_recipe_instances_project_updated ON recipe_instances(project_id, updated_at DESC)",
    "CREATE UNIQUE INDEX uq_recipe_instances_linear_issue ON recipe_instances(linear_issue_id) WHERE linear_issue_id IS NOT NULL",
    "CREATE UNIQUE INDEX uq_recipe_instances_launch_key ON recipe_instances(project_id, launch_idempotency_key) WHERE project_id IS NOT NULL AND launch_idempotency_key IS NOT NULL",
)
_PROJECT_RECIPE_POLICY_MIGRATION_TEXT = (
    ";\n".join(_PROJECT_RECIPE_POLICY_MIGRATION_STATEMENTS) + ";\n"
)
_GRAPH_RUNNER_V1_MIGRATION_STATEMENTS = (
    """CREATE TABLE recipe_runs_v1 (
  id TEXT PRIMARY KEY NOT NULL,
  project_id TEXT NOT NULL,
  board TEXT NOT NULL,
  recipe_name TEXT NOT NULL,
  recipe_hash TEXT NOT NULL CHECK(
    typeof(recipe_hash)='text'
    AND length(recipe_hash)=64
    AND recipe_hash NOT GLOB '*[^0-9a-f]*'
  ),
  recipe_snapshot_json TEXT NOT NULL CHECK(json_valid(recipe_snapshot_json)),
  request_text TEXT NOT NULL,
  workspace_path TEXT,
  launch_key TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL CHECK(state IN ('running','paused','completed','failed')),
  blocked_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT,
  CHECK(
    (state IN ('completed','failed') AND completed_at IS NOT NULL)
    OR (state IN ('running','paused') AND completed_at IS NULL)
  )
)""",
    "CREATE INDEX idx_recipe_runs_v1_active ON recipe_runs_v1(state,updated_at DESC)",
    """CREATE TABLE box_attempts_v1 (
  id TEXT PRIMARY KEY NOT NULL,
  run_id TEXT NOT NULL REFERENCES recipe_runs_v1(id),
  box_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL CHECK(typeof(ordinal)='integer' AND ordinal>=1),
  state TEXT NOT NULL CHECK(state IN ('pending','ready','running','waiting_human','completed','failed','cancelled')),
  executor_run_id INTEGER REFERENCES runs(id),
  input_work_json TEXT NOT NULL CHECK(json_valid(input_work_json)),
  output_work TEXT,
  result TEXT,
  technical_failure TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  finished_at TEXT,
  UNIQUE(run_id,box_id,ordinal),
  UNIQUE(id,run_id),
  CHECK(
    (state IN ('completed','failed','cancelled') AND finished_at IS NOT NULL)
    OR (state IN ('pending','ready','running','waiting_human') AND finished_at IS NULL)
  )
)""",
    "CREATE INDEX idx_box_attempts_v1_ready ON box_attempts_v1(state,run_id,created_at)",
    """CREATE TABLE route_tokens_v1 (
  id TEXT PRIMARY KEY NOT NULL,
  run_id TEXT NOT NULL REFERENCES recipe_runs_v1(id),
  source_attempt_id TEXT,
  arrow_index INTEGER CHECK(arrow_index IS NULL OR (typeof(arrow_index)='integer' AND arrow_index>=0)),
  destination_box_id TEXT NOT NULL,
  lineage_json TEXT NOT NULL CHECK(
    json_valid(lineage_json) AND json_type(lineage_json)='array'
  ),
  work_refs_json TEXT NOT NULL CHECK(json_valid(work_refs_json)),
  state TEXT NOT NULL CHECK(state IN ('pending','consumed','cancelled')),
  created_at TEXT NOT NULL,
  consumed_at TEXT,
  UNIQUE(run_id,source_attempt_id,arrow_index,destination_box_id),
  FOREIGN KEY(source_attempt_id,run_id) REFERENCES box_attempts_v1(id,run_id),
  CHECK(
    (source_attempt_id IS NULL AND arrow_index IS NULL)
    OR (source_attempt_id IS NOT NULL AND arrow_index IS NOT NULL)
  ),
  CHECK(
    (state IN ('consumed','cancelled') AND consumed_at IS NOT NULL)
    OR (state='pending' AND consumed_at IS NULL)
  )
)""",
    """CREATE TRIGGER trg_route_tokens_v1_logical_unique_insert
BEFORE INSERT ON route_tokens_v1
WHEN EXISTS (
  SELECT 1 FROM route_tokens_v1 existing
  WHERE existing.run_id=NEW.run_id
    AND existing.source_attempt_id IS NEW.source_attempt_id
    AND existing.arrow_index IS NEW.arrow_index
    AND existing.destination_box_id=NEW.destination_box_id
)
BEGIN
  SELECT RAISE(ABORT,'duplicate route token logical identity');
END""",
    """CREATE TRIGGER trg_route_tokens_v1_logical_unique_update
BEFORE UPDATE OF run_id,source_attempt_id,arrow_index,destination_box_id
ON route_tokens_v1
WHEN EXISTS (
  SELECT 1 FROM route_tokens_v1 existing
  WHERE existing.id<>OLD.id
    AND existing.run_id=NEW.run_id
    AND existing.source_attempt_id IS NEW.source_attempt_id
    AND existing.arrow_index IS NEW.arrow_index
    AND existing.destination_box_id=NEW.destination_box_id
)
BEGIN
  SELECT RAISE(ABORT,'duplicate route token logical identity');
END""",
    "CREATE INDEX idx_route_tokens_v1_active ON route_tokens_v1(state,run_id,destination_box_id)",
    """CREATE TABLE split_groups_v1 (
  id TEXT PRIMARY KEY NOT NULL,
  run_id TEXT NOT NULL REFERENCES recipe_runs_v1(id),
  parent_lineage_json TEXT NOT NULL CHECK(
    json_valid(parent_lineage_json) AND json_type(parent_lineage_json)='array'
  ),
  branch_ids_json TEXT NOT NULL CHECK(
    json_valid(branch_ids_json)
    AND json_type(branch_ids_json)='array'
    AND json_array_length(branch_ids_json)>1
  ),
  state TEXT NOT NULL CHECK(state IN ('open','closed','cancelled')),
  created_at TEXT NOT NULL,
  closed_at TEXT,
  CHECK(
    (state IN ('closed','cancelled') AND closed_at IS NOT NULL)
    OR (state='open' AND closed_at IS NULL)
  )
)""",
    """CREATE TABLE run_events_v1 (
  key TEXT PRIMARY KEY NOT NULL,
  run_id TEXT NOT NULL REFERENCES recipe_runs_v1(id),
  source TEXT NOT NULL,
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
  state TEXT NOT NULL CHECK(state IN ('pending','leased','applied','discarded','failed')),
  lease_owner TEXT,
  lease_until TEXT,
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(typeof(attempt_count)='integer' AND attempt_count>=0),
  outcome TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL,
  applied_at TEXT,
  UNIQUE(key,run_id),
  CHECK(
    (state='leased' AND lease_owner IS NOT NULL AND lease_until IS NOT NULL)
    OR (state<>'leased' AND lease_owner IS NULL AND lease_until IS NULL)
  ),
  CHECK(
    (state IN ('applied','discarded','failed') AND applied_at IS NOT NULL)
    OR (state IN ('pending','leased') AND applied_at IS NULL)
  )
)""",
    "CREATE INDEX idx_run_events_v1_pending ON run_events_v1(state,lease_until,created_at)",
    """CREATE TABLE human_box_decisions_v1 (
  id TEXT PRIMARY KEY NOT NULL,
  attempt_id TEXT NOT NULL UNIQUE REFERENCES box_attempts_v1(id),
  result TEXT NOT NULL,
  actor_kind TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  channel TEXT NOT NULL,
  nonce_hash TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  event_key TEXT NOT NULL UNIQUE
)""",
    """CREATE TABLE project_recipes_v1 (
  project_id TEXT NOT NULL,
  recipe_name TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
  is_default INTEGER NOT NULL DEFAULT 0 CHECK(is_default IN (0,1)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(project_id,recipe_name)
)""",
)
_GRAPH_RUNNER_V1_MIGRATION_TEXT = (
    ";\n".join(_GRAPH_RUNNER_V1_MIGRATION_STATEMENTS) + ";\n"
)
_GRAPH_RUNNER_ESCALATED_MIGRATION_STATEMENTS = (
    "PRAGMA defer_foreign_keys=ON",
    """CREATE TABLE recipe_runs_v1_next (
  id TEXT PRIMARY KEY NOT NULL,
  project_id TEXT NOT NULL,
  board TEXT NOT NULL,
  recipe_name TEXT NOT NULL,
  recipe_hash TEXT NOT NULL CHECK(
    typeof(recipe_hash)='text'
    AND length(recipe_hash)=64
    AND recipe_hash NOT GLOB '*[^0-9a-f]*'
  ),
  recipe_snapshot_json TEXT NOT NULL CHECK(json_valid(recipe_snapshot_json)),
  request_text TEXT NOT NULL,
  workspace_path TEXT,
  launch_key TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL CHECK(state IN ('running','paused','escalated','completed','failed')),
  blocked_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT,
  CHECK(
    (state IN ('completed','failed') AND completed_at IS NOT NULL)
    OR (state IN ('running','paused','escalated') AND completed_at IS NULL)
  )
)""",
    """INSERT INTO recipe_runs_v1_next(
  id,project_id,board,recipe_name,recipe_hash,recipe_snapshot_json,request_text,
  workspace_path,launch_key,state,blocked_reason,created_at,updated_at,completed_at
)
SELECT
  id,project_id,board,recipe_name,recipe_hash,recipe_snapshot_json,request_text,
  workspace_path,launch_key,state,blocked_reason,created_at,updated_at,completed_at
FROM recipe_runs_v1""",
    "DROP TABLE recipe_runs_v1",
    "ALTER TABLE recipe_runs_v1_next RENAME TO recipe_runs_v1",
    "CREATE INDEX idx_recipe_runs_v1_active ON recipe_runs_v1(state,updated_at DESC)",
    "PRAGMA defer_foreign_keys=OFF",
)
_GRAPH_RUNNER_ESCALATED_MIGRATION_TEXT = (
    ";\n".join(_GRAPH_RUNNER_ESCALATED_MIGRATION_STATEMENTS) + ";\n"
)
_MIGRATIONS = (
    (1, "a0_single_writer_recoverable_actions", _A0_MIGRATION_TEXT),
    (2, "a1_durable_runs_resource_governor", _A1_MIGRATION_TEXT),
    (3, "a1_worker_transition_attempt_fencing", _A1_FENCING_MIGRATION_TEXT),
    (4, "sf5_artifact_revision_identity", _ARTIFACT_MIGRATION_TEXT),
    (5, "sf5_instance_base_identity", _INSTANCE_BASE_MIGRATION_TEXT),
    (6, "sf6_named_token_pool_charges", _PLANNING_BUDGET_MIGRATION_TEXT),
    (7, "sf8_environment_sessions", _ENVIRONMENT_SESSION_MIGRATION_TEXT),
    (8, "sf8_environment_enforcement_and_caps", _ENVIRONMENT_ENFORCEMENT_MIGRATION_TEXT),
    (9, "sf7_run_access_enforcement_level", _RUN_ACCESS_ENFORCEMENT_MIGRATION_TEXT),
    (10, "sf9_verification_evidence", _VERIFICATION_MIGRATION_TEXT),
    (11, "sf11_bound_gate_decisions", _GATE_DECISION_MIGRATION_TEXT),
    (12, "verification_adversarial_hardening", _VERIFICATION_HARDENING_MIGRATION_TEXT),
    (13, "verification_production_identity_binding", _VERIFICATION_PRODUCTION_BINDING_MIGRATION_TEXT),
    (14, "app_session_expected_candidate_identity", _APP_SESSION_IDENTITY_MIGRATION_TEXT),
    (15, "sf17_containment_overlay", _CONTAINMENT_OVERLAY_MIGRATION_TEXT),
    (16, "sf18_project_recipe_policy_and_flight_identity", _PROJECT_RECIPE_POLICY_MIGRATION_TEXT),
    (17, "graphrunner_v1_durable_state", _GRAPH_RUNNER_V1_MIGRATION_TEXT),
    (18, "graphrunner_v1_escalated_runs", _GRAPH_RUNNER_ESCALATED_MIGRATION_TEXT),
)
_MIGRATION_STATEMENTS = {
    1: _A0_MIGRATION_STATEMENTS,
    2: _A1_MIGRATION_STATEMENTS,
    3: _A1_FENCING_MIGRATION_STATEMENTS,
    4: _ARTIFACT_MIGRATION_STATEMENTS,
    5: _INSTANCE_BASE_MIGRATION_STATEMENTS,
    6: _PLANNING_BUDGET_MIGRATION_STATEMENTS,
    7: _ENVIRONMENT_SESSION_MIGRATION_STATEMENTS,
    8: _ENVIRONMENT_ENFORCEMENT_MIGRATION_STATEMENTS,
    9: _RUN_ACCESS_ENFORCEMENT_MIGRATION_STATEMENTS,
    10: _VERIFICATION_MIGRATION_STATEMENTS,
    11: _GATE_DECISION_MIGRATION_STATEMENTS,
    12: _VERIFICATION_HARDENING_MIGRATION_STATEMENTS,
    13: _VERIFICATION_PRODUCTION_BINDING_MIGRATION_STATEMENTS,
    14: _APP_SESSION_IDENTITY_MIGRATION_STATEMENTS,
    15: _CONTAINMENT_OVERLAY_MIGRATION_STATEMENTS,
    16: _PROJECT_RECIPE_POLICY_MIGRATION_STATEMENTS,
    17: _GRAPH_RUNNER_V1_MIGRATION_STATEMENTS,
    18: _GRAPH_RUNNER_ESCALATED_MIGRATION_STATEMENTS,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_path() -> Path:
    home = os.environ.get("HERMES_HOME")
    if not home:
        from hermes_constants import get_hermes_home
        home = str(get_hermes_home())
    return Path(home) / "shipfactory" / "shipfactory.db"


class _ClosingConnection(sqlite3.Connection):
    """sqlite3.Connection whose ``with`` block commits AND closes.

    Finding #27 (2026-07-14): stock ``with sqlite3.connect(...)`` is a
    TRANSACTION scope — it commits/rolls back on exit but never closes the
    handle. Every ``with _connect()`` in this package therefore leaked one
    fd per call; the daemon leaked ~13/hour against macOS's default 256
    soft limit, and EMFILE surfaces as SQLite "disk I/O error" + index
    corruption (finding #21 was this leak's downstream symptom).
    """

    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0, factory=_ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")  # #16-V2: wait out concurrent writers.
    conn.execute("PRAGMA journal_mode = WAL")  # #16-V2: readers do not block writers.
    return conn


def _rows(cursor: sqlite3.Cursor) -> list[dict]:
    return [dict(row) for row in cursor.fetchall()]


def init_db() -> None:
    """Create the base schema and transactionally apply verified migrations."""
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # executescript commits implicitly, so execute the bootstrap DDL one
            # statement at a time inside our explicit transaction.
            for statement in _BASE_SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)
            # Normalize the two pre-migration legacy schemas that shipped
            # before schema_migrations existed. These are bootstrap upgrades,
            # not A0 migrations; all subsequent changes are numbered below.
            monitor_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(monitors)")
            }
            if "interval_seconds" not in monitor_columns:
                conn.execute(
                    "ALTER TABLE monitors ADD COLUMN interval_seconds INTEGER NOT NULL DEFAULT 300"
                )
            step_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(recipe_steps)")
            }
            if "finding_count" not in step_columns:
                conn.execute("ALTER TABLE recipe_steps ADD COLUMN finding_count INTEGER")
            conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                checksum TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )""")
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        for version, name, migration in _MIGRATIONS:
            checksum = hashlib.sha256(migration.encode("utf-8")).hexdigest()
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
                    "SELECT version,name,checksum FROM schema_migrations ORDER BY version"
                ).fetchall()
                existing = next((row for row in rows if row["version"] == version), None)
                if existing is not None:
                    if existing["name"] != name or existing["checksum"] != checksum:
                        raise RuntimeError(f"schema migration {version} checksum mismatch")
                    conn.commit()
                    continue
                if any(int(row["version"]) > version for row in rows):
                    raise RuntimeError(f"schema migration {version} is partially applied")
                prior = max((int(row["version"]) for row in rows), default=0)
                if prior != version - 1:
                    raise RuntimeError(
                        f"schema migration {version} requires prior version {version - 1}, found {prior}"
                    )
                existing_tables = {
                    row[0] for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if version == 1:
                    event_columns = {row["name"] for row in conn.execute(
                        "PRAGMA table_info(advance_events)"
                    )}
                    outbox_columns = {row["name"] for row in conn.execute(
                        "PRAGMA table_info(outbox)"
                    )}
                    migration_artifacts = (
                        "lease_owner" in event_columns
                        or "lease_owner" in outbox_columns
                        or bool({"action_intents", "resource_leases"} & existing_tables)
                    )
                elif version == 2:
                    run_columns = {row["name"] for row in conn.execute(
                        "PRAGMA table_info(runs)"
                    )}
                    monitor_columns = {row["name"] for row in conn.execute(
                        "PRAGMA table_info(monitors)"
                    )}
                    indexes = {row[0] for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='index'"
                    )}
                    migration_artifacts = bool(
                        {"board", "workspace_path", "process_start_token"} & run_columns
                        or {"state", "last_outcome"} & monitor_columns
                        or "idx_resource_leases_active" in indexes
                    )
                elif version == 3:
                    run_columns = {row["name"] for row in conn.execute(
                        "PRAGMA table_info(runs)"
                    )}
                    migration_artifacts = "task_attempt_id" in run_columns
                elif version == 4:
                    step_columns = {row["name"] for row in conn.execute(
                        "PRAGMA table_info(recipe_steps)"
                    )}
                    migration_artifacts = bool(
                        {"artifacts", "artifact_edges"} & existing_tables
                        or {"input_artifact_set_hash", "output_artifact_set_hash"}
                        & step_columns
                    )
                elif version == 5:
                    instance_columns = {row["name"] for row in conn.execute(
                        "PRAGMA table_info(recipe_instances)"
                    )}
                    migration_artifacts = bool(
                        {"base_sha", "updated_base_at"} & instance_columns
                    )
                elif version == 6:
                    charge_columns = {row["name"] for row in conn.execute(
                        "PRAGMA table_info(budget_charges)"
                    )}
                    indexes = {row[0] for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='index'"
                    )}
                    migration_artifacts = bool(
                        "token_pool" in charge_columns
                        or "idx_budget_charges_pool" in indexes
                    )
                elif version == 7:
                    migration_artifacts = bool(
                        {"env_sessions", "app_sessions"} & existing_tables
                    )
                elif version == 8:
                    env_columns = {row["name"] for row in conn.execute(
                        "PRAGMA table_info(env_sessions)"
                    )}
                    app_columns = {row["name"] for row in conn.execute(
                        "PRAGMA table_info(app_sessions)"
                    )}
                    migration_artifacts = bool(
                        {"network_enforcement_level", "output_cap_exceeded"}
                        & (env_columns | app_columns)
                    )
                elif version == 9:
                    run_columns = {row["name"] for row in conn.execute(
                        "PRAGMA table_info(runs)"
                    )}
                    migration_artifacts = "access_enforcement_level" in run_columns
                elif version == 10:
                    migration_artifacts = bool(
                        {"evidence_bundles", "evidence_items", "verification_cases"}
                        & existing_tables
                    )
                elif version == 11:
                    migration_artifacts = "gate_decisions" in existing_tables
                elif version == 12:
                    bundle_columns = {row["name"] for row in conn.execute(
                        "PRAGMA table_info(evidence_bundles)"
                    )}
                    migration_artifacts = "phase_b_eligible" in bundle_columns
                elif version == 13:
                    run_columns = {row["name"] for row in conn.execute(
                        "PRAGMA table_info(runs)"
                    )}
                    step_columns = {row["name"] for row in conn.execute(
                        "PRAGMA table_info(recipe_steps)"
                    )}
                    bundle_columns = {row["name"] for row in conn.execute(
                        "PRAGMA table_info(evidence_bundles)"
                    )}
                    item_columns = {row["name"] for row in conn.execute(
                        "PRAGMA table_info(evidence_items)"
                    )}
                    migration_artifacts = bool(
                        "recipe_activation" in run_columns
                        or "producer_run_id" in step_columns
                        or {"workspace_path", "environment_identity_json"} & bundle_columns
                        or "attempt" in item_columns
                    )
                elif version == 14:
                    app_columns = {row["name"] for row in conn.execute(
                        "PRAGMA table_info(app_sessions)"
                    )}
                    migration_artifacts = bool(
                        {"expected_instance_id", "expected_head_sha"} & app_columns
                    )
                elif version == 15:
                    instance_columns = {row["name"] for row in conn.execute(
                        "PRAGMA table_info(recipe_instances)"
                    )}
                    step_columns = {row["name"] for row in conn.execute(
                        "PRAGMA table_info(recipe_steps)"
                    )}
                    migration_artifacts = bool(
                        "parent_tasks_json" in instance_columns
                        or {"rejected_by_step_id", "rejected_by_activation", "verdict_json"}
                        & step_columns
                    )
                elif version == 16:
                    instance_columns = {row["name"] for row in conn.execute(
                        "PRAGMA table_info(recipe_instances)"
                    )}
                    indexes = {
                        row[0] for row in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='index'"
                        )
                    }
                    migration_artifacts = bool(
                        "project_recipe_policies" in existing_tables
                        or {"project_id", "linear_issue_id", "launch_idempotency_key"}
                        & instance_columns
                        or {
                            "idx_project_recipe_policies_updated",
                            "idx_recipe_instances_project_updated",
                            "uq_recipe_instances_linear_issue",
                            "uq_recipe_instances_launch_key",
                        }
                        & indexes
                    )
                elif version == 17:
                    graph_tables = {
                        "recipe_runs_v1", "box_attempts_v1", "route_tokens_v1",
                        "split_groups_v1", "run_events_v1", "human_box_decisions_v1",
                        "project_recipes_v1",
                    }
                    graph_indexes = {
                        "idx_recipe_runs_v1_active", "idx_box_attempts_v1_ready",
                        "idx_route_tokens_v1_active", "idx_run_events_v1_pending",
                    }
                    graph_triggers = {
                        "trg_route_tokens_v1_logical_unique_insert",
                        "trg_route_tokens_v1_logical_unique_update",
                    }
                    indexes = {
                        row[0] for row in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='index'"
                        )
                    }
                    triggers = {
                        row[0] for row in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='trigger'"
                        )
                    }
                    migration_artifacts = bool(
                        graph_tables & existing_tables
                        or graph_indexes & indexes
                        or graph_triggers & triggers
                    )
                elif version == 18:
                    run_schema = conn.execute(
                        """SELECT sql FROM sqlite_master
                           WHERE type='table' AND name='recipe_runs_v1'"""
                    ).fetchone()
                    migration_artifacts = bool(
                        "recipe_runs_v1_next" in existing_tables
                        or (
                            run_schema is not None
                            and "'escalated'" in str(run_schema["sql"])
                        )
                    )
                if migration_artifacts:
                    raise RuntimeError(f"schema migration {version} is partially applied")
                for statement in _MIGRATION_STATEMENTS[version]:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_migrations(version,name,checksum,applied_at) VALUES(?,?,?,?)",
                    (version, name, checksum, _now()),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise


def record_run_start(task_id, seat, executor, model, pid=None, *, board=None,
                     workspace_path=None, log_path=None, prompt_path=None,
                     provider=None, resolved_model=None, executor_version=None,
                     process_start_token=None, task_attempt_id=None,
                     access_enforcement_level=None, recipe_activation=None) -> int:
    """Insert a running harness execution and return its run id."""
    init_db()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO runs(task_id,seat,executor,model,pid,started_at,tokens_in,tokens_out,"
            "tokens_total,board,workspace_path,log_path,prompt_path,provider,resolved_model,"
            "executor_version,process_start_token,task_attempt_id,access_enforcement_level,"
            "recipe_activation) VALUES(?,?,?,?,?,?,NULL,NULL,NULL,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, seat, executor, model or "", pid, _now(), board,
             str(workspace_path) if workspace_path is not None else None,
             str(log_path) if log_path is not None else None,
             str(prompt_path) if prompt_path is not None else None,
             provider, resolved_model, executor_version, process_start_token,
             int(task_attempt_id) if task_attempt_id is not None else None,
             access_enforcement_level,
             int(recipe_activation) if recipe_activation is not None else None),
        )
        return int(cur.lastrowid)


def record_run_spawned(run_id: int, pid: int, process_start_token: str | None) -> None:
    """Attach the OS identity only after a pre-spawn run row is durable."""
    with _connect() as conn:
        changed = conn.execute(
            "UPDATE runs SET pid=?,process_start_token=? WHERE id=? AND ended_at IS NULL",
            (int(pid), process_start_token, int(run_id)),
        ).rowcount
        if changed != 1:
            raise ValueError(f"unknown or terminal run {run_id}")


def nonterminal_runs() -> list[dict[str, Any]]:
    """Return durable worker runs which still need process reconciliation."""
    init_db()
    with _connect() as conn:
        return _rows(conn.execute(
            "SELECT * FROM runs WHERE ended_at IS NULL AND task_id<>? "
            "AND executor NOT IN ('verification','verification-runner') ORDER BY id",
            (DAEMON_RUN_TASK_ID,),
        ))


def nonterminal_verification_runs() -> list[dict[str, Any]]:
    """Return verification children requiring restart-time identity fencing."""
    init_db()
    with _connect() as conn:
        return _rows(conn.execute(
            "SELECT * FROM runs WHERE ended_at IS NULL "
            "AND executor IN ('verification','verification-runner') ORDER BY id"
        ))


def nonterminal_daemon_runs() -> list[dict[str, Any]]:
    """Return daemon rows which still need singleton-start reconciliation."""
    init_db()
    with _connect() as conn:
        return _rows(conn.execute(
            "SELECT * FROM runs WHERE ended_at IS NULL AND task_id=? ORDER BY id",
            (DAEMON_RUN_TASK_ID,),
        ))


def _pid_liveness(pid: int) -> bool | None:
    """Return PID liveness, preserving an indeterminate OS probe as ``None``."""
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        return None
    return True


def reconcile_daemon_runs(start_token_for_pid) -> list[dict[str, Any]]:
    """Close stale daemon rows, returning rows whose exact identity is live.

    Tokenless legacy rows are stale under the exclusive daemon lock. A
    token-bearing row is recoverable only when its PID is absent/dead or its
    persisted start token no longer matches a non-null observation. An exact
    match, or an unavailable identity probe for a PID that cannot be proven
    dead, is deliberately left untouched so the caller can fail closed rather
    than adopting or silently closing a still-live daemon.
    """
    live: list[dict[str, Any]] = []
    for row in nonterminal_daemon_runs():
        pid = int(row["pid"]) if row.get("pid") is not None else 0
        token = row.get("process_start_token")
        if pid <= 0:
            reason = "pid missing"
        elif not token:
            reason = "start token missing"
        else:
            try:
                observed = start_token_for_pid(pid)
            except Exception:
                observed = None
            if observed == token:
                live.append(row)
                continue
            if observed is None:
                liveness = _pid_liveness(pid)
                if liveness is not False:
                    protected = dict(row)
                    protected["_identity_probe_unavailable"] = True
                    live.append(protected)
                    continue
                reason = "pid dead; start token probe unavailable"
            else:
                reason = "pid reused: start token mismatched"
        record_run_crashed(int(row["id"]), reason)
    return live


def workspace_path_for_task(task_id: str) -> str | None:
    """Return the most recently recorded workspace for a real shipfactory run.

    Used to cross-check a verification action's claimed ``workspace`` against
    the worktree shipfactory itself actually spawned for the candidate's
    owning task, independent of git content (finding #1, verification
    adversarial lane): two different worktrees can share identical head/tree
    SHAs, so SHA equality alone cannot prove "this is the right worktree."
    """
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT workspace_path FROM runs WHERE task_id=? AND workspace_path IS NOT NULL "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
    return row["workspace_path"] if row else None


def exact_workspace_run(
    task_id: str, run_id: int, activation: int | None = None,
) -> dict[str, Any] | None:
    """Return the exact producer run only when task and activation match."""
    init_db()
    with _connect() as conn:
        if activation is None:
            row = conn.execute(
                "SELECT * FROM runs WHERE id=? AND task_id=?",
                (int(run_id), str(task_id)),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM runs WHERE id=? AND task_id=? AND recipe_activation=?",
                (int(run_id), str(task_id), int(activation)),
            ).fetchone()
    return dict(row) if row else None


def run_row(run_id: int) -> dict[str, Any] | None:
    """Return one durable run row."""
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id=?", (int(run_id),)).fetchone()
    return dict(row) if row else None


def record_run_end(run_id, exit_code, tokens_in, tokens_out, duration_s, result) -> None:
    """Finalize a harness execution with usage and outcome."""
    init_db()
    tokens_in = int(tokens_in) if tokens_in is not None else None
    tokens_out = int(tokens_out) if tokens_out is not None else None
    tokens_total = (
        tokens_in + tokens_out
        if tokens_in is not None and tokens_out is not None else None
    )
    with _connect() as conn:
        conn.execute("UPDATE runs SET ended_at=?,exit_code=?,tokens_in=?,tokens_out=?,tokens_total=?,duration_s=?,result=? WHERE id=?",
                     (_now(), exit_code, tokens_in, tokens_out, tokens_total, duration_s, result, run_id))


def record_run_crashed(run_id: int, reason: str = "process identity unavailable") -> None:
    """Durably terminate a run whose recorded OS identity cannot be adopted."""
    with _connect() as conn:
        conn.execute(
            "UPDATE runs SET ended_at=?,exit_code=-1,result=? WHERE id=? AND ended_at IS NULL",
            (_now(), f"crashed: {reason}"[:500], int(run_id)),
        )


def _daemon_payload(
    boards: list[str],
    last_tick_at: dict[str, str | None],
    *,
    tick_interval: float,
) -> dict[str, Any]:
    """Build the one-release-compatible daemon liveness payload."""
    return {
        "kind": "shipfactory_daemon",
        "board": boards[0],
        "board_deprecation": "board is retained for one release; use boards",
        "boards": boards,
        "last_tick_at": last_tick_at,
        "tick_interval_seconds": tick_interval,
    }


def record_daemon_start(
    board: str,
    pid: int,
    *,
    boards: list[str] | None = None,
    tick_interval: float = 5.0,
    process_start_token: str | None = None,
) -> int:
    """Insert a durable Factory-daemon run record for all served boards."""
    names = list(dict.fromkeys(boards or [board]))
    init_db()
    with _connect() as conn:
        # Keep the run insert and initial liveness payload in one transaction.
        # A payload/serialization/update failure must not leave a live row
        # behind that blocks the next singleton startup.
        cur = conn.execute(
            "INSERT INTO runs(task_id,seat,executor,model,pid,started_at,tokens_in,tokens_out,"
            "tokens_total,board,workspace_path,log_path,prompt_path,provider,resolved_model,"
            "executor_version,process_start_token,task_attempt_id,access_enforcement_level,"
            "recipe_activation) VALUES(?,?,?,?,?,?,NULL,NULL,NULL,?,?,?,?,?,?,?,?,?,?,?)",
            (DAEMON_RUN_TASK_ID, names[0], "shipfactory-daemon", "", pid, _now(),
             names[0], None, None, None, None, None, None, process_start_token,
             None, None, None),
        )
        run_id = int(cur.lastrowid)
        payload = _daemon_payload(
            names,
            {name: None for name in names},
            tick_interval=float(tick_interval),
        )
        conn.execute(
            "UPDATE runs SET result=? WHERE id=?",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")), run_id),
        )
    return run_id


def record_daemon_tick(run_id: int, board: str) -> str:
    """Persist one board's latest completed tick on its daemon run record."""
    ticked_at = _now()
    with _connect() as conn:
        row = conn.execute("SELECT seat,result FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown daemon run {run_id}")
        try:
            payload = json.loads(row["result"] or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        names = payload.get("boards")
        if not isinstance(names, list) or not names:
            names = [str(payload.get("board") or row["seat"])]
        if board not in names:
            names.append(board)
        ticks = payload.get("last_tick_at")
        if not isinstance(ticks, dict):
            ticks = {names[0]: ticks}
        ticks = {name: ticks.get(name) for name in names}
        ticks[board] = ticked_at
        result = json.dumps(
            _daemon_payload(
                names,
                ticks,
                tick_interval=float(payload.get("tick_interval_seconds") or 5.0),
            ),
            sort_keys=True,
            separators=(",", ":"),
        )
        conn.execute("UPDATE runs SET result=? WHERE id=?", (result, run_id))
    return ticked_at


def record_daemon_end(run_id: int) -> None:
    """Mark a daemon run cleanly stopped without changing its last tick."""
    with _connect() as conn:
        conn.execute(
            "UPDATE runs SET ended_at=?,exit_code=0 WHERE id=? AND ended_at IS NULL",
            (_now(), run_id),
        )


def latest_daemon_run(board: str | None = None) -> dict[str, Any] | None:
    """Return the latest durable daemon record, optionally serving ``board``."""
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM runs WHERE task_id=? ORDER BY id DESC",
            (DAEMON_RUN_TASK_ID,),
        ).fetchall()
    row = None
    payload: dict[str, Any] = {}
    for candidate in rows:
        try:
            candidate_payload = json.loads(candidate["result"] or "{}")
        except (TypeError, json.JSONDecodeError):
            candidate_payload = {}
        names = candidate_payload.get("boards")
        if not isinstance(names, list) or not names:
            names = [str(candidate_payload.get("board") or candidate["seat"])]
        if board is None or board in names:
            row = candidate
            payload = candidate_payload
            break
    if row is None:
        return None
    value = dict(row)
    names = payload.get("boards")
    if not isinstance(names, list) or not names:
        names = [str(payload.get("board") or value["seat"])]
    ticks = payload.get("last_tick_at")
    if not isinstance(ticks, dict):
        ticks = {names[0]: ticks}
    value["board"] = payload.get("board") or names[0]
    value["boards"] = names
    value["last_tick_at"] = {name: ticks.get(name) for name in names}
    value["tick_interval_seconds"] = float(payload.get("tick_interval_seconds") or 5.0)
    value["board_deprecation"] = payload.get("board_deprecation")
    return value


def get_policy(task_id) -> dict | None:
    """Return a task's execution policy, if present."""
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT policy_json FROM policies WHERE task_id=?", (task_id,)).fetchone()
    return json.loads(row[0]) if row else None


def set_policy(task_id, policy: dict) -> None:
    """Create or replace a task execution policy."""
    init_db()
    value = json.dumps(policy, sort_keys=True, separators=(",", ":"))
    with _connect() as conn:
        conn.execute("INSERT INTO policies VALUES(?,?) ON CONFLICT(task_id) DO UPDATE SET policy_json=excluded.policy_json", (task_id, value))


def _policy_project_id(project_id: str) -> str:
    if not isinstance(project_id, str) or not project_id.strip():
        raise TypeError("project_id must be a non-empty string")
    return project_id


def _canonical_recipe_keys(
    allowed_recipe_keys: list[str], default_recipe_key: str | None,
) -> tuple[list[str], str]:
    if not isinstance(allowed_recipe_keys, list):
        raise TypeError("allowed_recipe_keys must be a list")
    if any(not isinstance(key, str) or not key for key in allowed_recipe_keys):
        raise TypeError("allowed_recipe_keys must contain non-empty strings")
    if len(set(allowed_recipe_keys)) != len(allowed_recipe_keys):
        raise ValueError("allowed_recipe_keys must not contain duplicates")
    keys = sorted(allowed_recipe_keys)
    if default_recipe_key is not None and not isinstance(default_recipe_key, str):
        raise TypeError("default_recipe_key must be a string or null")
    if default_recipe_key is not None and default_recipe_key not in keys:
        raise ValueError("default_recipe_key must be an allowed recipe key")
    encoded = json.dumps(keys, ensure_ascii=False, separators=(",", ":"))
    return keys, encoded


def _policy_row_value(row: Any, name: str, index: int) -> Any:
    try:
        return row[name]
    except (IndexError, KeyError, TypeError):
        return row[index]


def load_project_recipe_policy(db: Any, project_id: str) -> dict[str, Any] | None:
    """Load one project policy and fail closed on malformed persisted data."""
    project_id = _policy_project_id(project_id)
    row = db.execute(
        "SELECT project_id,allowed_recipe_keys_json,default_recipe_key,updated_at "
        "FROM project_recipe_policies WHERE project_id=?",
        (project_id,),
    ).fetchone()
    if row is None:
        return None
    stored_project_id = _policy_row_value(row, "project_id", 0)
    raw_keys = _policy_row_value(row, "allowed_recipe_keys_json", 1)
    default_recipe_key = _policy_row_value(row, "default_recipe_key", 2)
    updated_at = _policy_row_value(row, "updated_at", 3)
    if stored_project_id != project_id or not isinstance(raw_keys, str):
        raise ValueError("stored project recipe policy has invalid shape")
    try:
        stored_keys = json.loads(raw_keys)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("stored project recipe policy has invalid JSON") from exc
    keys, canonical_json = _canonical_recipe_keys(stored_keys, default_recipe_key)
    if raw_keys != canonical_json or not isinstance(updated_at, str) or not updated_at:
        raise ValueError("stored project recipe policy is not canonical")
    return {
        "project_id": project_id,
        "allowed_recipe_keys": keys,
        "default_recipe_key": default_recipe_key,
        "updated_at": updated_at,
    }


def save_project_recipe_policy(
    db: Any, project_id: str, allowed_recipe_keys: list[str],
    default_recipe_key: str | None,
) -> dict[str, Any]:
    """Atomically replace a project's canonical recipe attachment policy."""
    project_id = _policy_project_id(project_id)
    _, encoded = _canonical_recipe_keys(allowed_recipe_keys, default_recipe_key)
    now = _now()
    db.execute(
        "INSERT INTO project_recipe_policies("
        "project_id,allowed_recipe_keys_json,default_recipe_key,created_at,updated_at) "
        "VALUES(?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET "
        "allowed_recipe_keys_json=excluded.allowed_recipe_keys_json,"
        "default_recipe_key=excluded.default_recipe_key,updated_at=excluded.updated_at",
        (project_id, encoded, default_recipe_key, now, now),
    )
    result = load_project_recipe_policy(db, project_id)
    if result is None:
        raise RuntimeError("project recipe policy write did not persist")
    return result


def _project_flight_row(db: Any, where: str, parameters: tuple[Any, ...]) -> dict[str, Any] | None:
    cursor = db.execute(f"SELECT * FROM recipe_instances WHERE {where}", parameters)
    row = cursor.fetchone()
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return dict(row)
    return dict(zip((column[0] for column in cursor.description), row))


def project_flight(db: Any, instance_id: str) -> dict[str, Any] | None:
    """Return one immutable flight identity by Factory instance id."""
    if not isinstance(instance_id, str) or not instance_id:
        raise TypeError("instance_id must be a non-empty string")
    return _project_flight_row(db, "id=?", (instance_id,))


def project_flight_by_idempotency_key(
    db: Any, project_id: str, launch_idempotency_key: str,
) -> dict[str, Any] | None:
    """Return the project-scoped flight bound to a launch idempotency key."""
    project_id = _policy_project_id(project_id)
    if not isinstance(launch_idempotency_key, str) or not launch_idempotency_key:
        raise TypeError("launch_idempotency_key must be a non-empty string")
    return _project_flight_row(
        db, "project_id=? AND launch_idempotency_key=?",
        (project_id, launch_idempotency_key),
    )


def project_flight_by_linear_issue_id(
    db: Any, linear_issue_id: str,
) -> dict[str, Any] | None:
    """Return the globally unique flight bound to a Linear issue."""
    if not isinstance(linear_issue_id, str) or not linear_issue_id:
        raise TypeError("linear_issue_id must be a non-empty string")
    return _project_flight_row(db, "linear_issue_id=?", (linear_issue_id,))


def project_rollup(
    db: Any, project_id: str | None, *, recent_limit: int,
) -> dict[str, Any]:
    """Return bounded project flight counts and stable recent summaries."""
    if not isinstance(recent_limit, int) or isinstance(recent_limit, bool):
        raise TypeError("recent_limit must be a positive integer")
    if recent_limit < 1:
        raise ValueError("recent_limit must be a positive integer")
    if project_id is not None:
        project_id = _policy_project_id(project_id)
        rows = db.execute(
            "SELECT id,recipe_id,recipe_version,status,updated_at,linear_issue_id "
            "FROM recipe_instances WHERE project_id=? ORDER BY updated_at DESC,id DESC LIMIT ?",
            (project_id, recent_limit),
        ).fetchall()
        count_rows = db.execute(
            "SELECT status,COUNT(*) FROM recipe_instances WHERE project_id=? GROUP BY status",
            (project_id,),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT id,recipe_id,recipe_version,status,updated_at,linear_issue_id "
            "FROM recipe_instances WHERE project_id IS NULL ORDER BY updated_at DESC,id DESC LIMIT ?",
            (recent_limit,),
        ).fetchall()
        count_rows = db.execute(
            "SELECT status,COUNT(*) FROM recipe_instances WHERE project_id IS NULL GROUP BY status"
        ).fetchall()
    waiting_states = {"waiting_gate", "waiting_event"}
    active = sum(int(count[1]) for count in count_rows if count[0] not in {
        "done", "failed", "cancelled", *waiting_states,
    })
    waiting = sum(int(count[1]) for count in count_rows if count[0] in waiting_states)
    recent = [
        {
            "instance_id": row[0],
            "recipe": f"{row[1]}@{row[2]}",
            "status": row[3],
            "updated_at": row[4],
            "linear_issue_id": row[5],
        }
        for row in rows
    ]
    return {"active": active, "waiting": waiting, "recent": recent}


def record_decision(task_id, stage_id, stage_type, seat, outcome, body) -> None:
    """Append an immutable policy-stage decision."""
    init_db()
    with _connect() as conn:
        conn.execute("INSERT INTO decisions(task_id,stage_id,stage_type,seat,outcome,body,at) VALUES(?,?,?,?,?,?,?)",
                     (task_id, stage_id, stage_type, seat, outcome, body, _now()))


def decisions_for(task_id) -> list[dict]:
    """Return decisions for a task in insertion order."""
    init_db()
    with _connect() as conn:
        return _rows(conn.execute("SELECT task_id,stage_id,stage_type,seat,outcome,body,at FROM decisions WHERE task_id=? ORDER BY id", (task_id,)))


def add_monitor(
    task_id,
    next_check_at,
    timeout_at,
    max_attempts,
    recovery_policy,
    notes,
    scheduled_by,
    interval_seconds=300,
) -> None:
    """Create or replace a task monitor, resetting its attempts."""
    init_db()
    interval_seconds = int(interval_seconds)
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    with _connect() as conn:
        conn.execute("""INSERT INTO monitors(
          task_id,next_check_at,timeout_at,max_attempts,attempt_count,recovery_policy,notes,scheduled_by,interval_seconds
        ) VALUES(?,?,?,?,0,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET
          next_check_at=excluded.next_check_at,timeout_at=excluded.timeout_at,max_attempts=excluded.max_attempts,
          attempt_count=0,recovery_policy=excluded.recovery_policy,notes=excluded.notes,
          scheduled_by=excluded.scheduled_by,interval_seconds=excluded.interval_seconds,
          state='active',last_outcome=NULL,last_error=NULL,last_checked_at=NULL""",
                     (task_id, next_check_at, timeout_at, max_attempts, recovery_policy, notes,
                      scheduled_by, interval_seconds))


def due_monitors(now_iso) -> list[dict]:
    """Return monitors whose next check or terminal timeout has arrived."""
    init_db()
    with _connect() as conn:
        return _rows(conn.execute(
            """SELECT * FROM monitors
               WHERE state='active' AND (next_check_at<=? OR (timeout_at IS NOT NULL AND timeout_at<=?))
               ORDER BY next_check_at,task_id""",
            (now_iso, now_iso),
        ))


def advance_monitor(task_id, now_iso, *, close=False) -> bool:
    """Atomically advance one recovery attempt and reschedule or close it."""

    init_db()
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT interval_seconds FROM monitors WHERE task_id=?", (task_id,)
        ).fetchone()
        if row is None:
            return False
        conn.execute(
            "UPDATE monitors SET attempt_count=attempt_count+1 WHERE task_id=?", (task_id,)
        )
        if close:
            conn.execute("UPDATE monitors SET state='closed' WHERE task_id=?", (task_id,))
        else:
            now = datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
            next_check_at = (now.astimezone(timezone.utc) + timedelta(
                seconds=int(row["interval_seconds"])
            )).isoformat()
            conn.execute(
                "UPDATE monitors SET next_check_at=? WHERE task_id=?",
                (next_check_at, task_id),
            )
        return True


def record_monitor_outcome(task_id: str, outcome: str, error: str | None = None) -> None:
    """Persist the latest bounded watchdog attempt outcome for operators."""
    init_db()
    with _connect() as conn:
        conn.execute(
            "UPDATE monitors SET last_outcome=?,last_error=?,last_checked_at=? WHERE task_id=?",
            (outcome, error, _now(), task_id),
        )


def clear_monitor(task_id) -> None:
    """Delete a task monitor."""
    init_db()
    with _connect() as conn:
        conn.execute("DELETE FROM monitors WHERE task_id=?", (task_id,))


def add_watchdog(root_task_id, agent, instructions) -> None:
    """Create or update a subtree watchdog without losing its fingerprint."""
    init_db()
    with _connect() as conn:
        conn.execute("""INSERT INTO watchdogs(root_task_id,agent,instructions) VALUES(?,?,?)
          ON CONFLICT(root_task_id) DO UPDATE SET agent=excluded.agent,instructions=excluded.instructions""",
                     (root_task_id, agent, instructions))


def watchdogs() -> list[dict]:
    """Return all subtree watchdog definitions."""
    init_db()
    with _connect() as conn:
        return _rows(conn.execute("SELECT * FROM watchdogs ORDER BY root_task_id"))


def set_watchdog_fingerprint(root_task_id, fp) -> None:
    """Persist the last reviewed subtree fingerprint."""
    init_db()
    with _connect() as conn:
        conn.execute("UPDATE watchdogs SET last_fingerprint=? WHERE root_task_id=?", (fp, root_task_id))


def seat_paused(seat) -> bool:
    """Return whether spawning is paused for a seat."""
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT paused FROM seat_state WHERE seat=?", (seat,)).fetchone()
    return bool(row[0]) if row else False


def set_seat_paused(seat, paused: bool) -> None:
    """Set a seat's durable spawning pause flag."""
    init_db()
    with _connect() as conn:
        conn.execute("INSERT INTO seat_state VALUES(?,?) ON CONFLICT(seat) DO UPDATE SET paused=excluded.paused", (seat, int(bool(paused))))


def costs_rollup(by: str, since_days: int) -> list[dict]:
    """Aggregate completed run counts and token usage by seat, executor, or task."""
    columns = {"seat": "seat", "executor": "executor", "task": "task_id"}
    if by not in columns:
        raise ValueError("by must be seat, executor, or task")
    if int(since_days) < 0:
        raise ValueError("since_days must be non-negative")
    init_db()
    since = (datetime.now(timezone.utc) - timedelta(days=int(since_days))).isoformat()
    column = columns[by]
    with _connect() as conn:
        return _rows(conn.execute(f"""SELECT {column} AS {by}, COUNT(*) AS runs,
          COALESCE(SUM(tokens_in),0) AS tokens_in, COALESCE(SUM(tokens_out),0) AS tokens_out,
          COALESCE(SUM(tokens_total),0) AS tokens_total,
          SUM(CASE WHEN tokens_total IS NULL THEN 1 ELSE 0 END) AS usage_unknown_runs,
          SUM(CASE WHEN tokens_total IS NULL THEN 0 ELSE 1 END) AS usage_known_runs,
          COALESCE(SUM(duration_s),0) AS duration_s
          FROM runs WHERE started_at>=? AND task_id<>?
          GROUP BY {column} ORDER BY {column}""", (since, DAEMON_RUN_TASK_ID)))


def reap_resource_leases(now: str | None = None) -> int:
    """Expire elapsed resource leases without deleting their audit rows."""
    init_db()
    now = now or _now()
    with _connect() as conn:
        return conn.execute(
            "UPDATE resource_leases SET state='expired',released_at=? "
            "WHERE state='active' AND lease_until IS NOT NULL AND lease_until<=?",
            (now, now),
        ).rowcount


def active_resource_units(kind: str, *, now: str | None = None) -> int:
    reap_resource_leases(now)
    with _connect() as conn:
        return int(conn.execute(
            "SELECT COALESCE(SUM(units),0) FROM resource_leases WHERE kind=? AND state='active'",
            (kind,),
        ).fetchone()[0])


def available_resource_units(kind: str, capacity: int) -> int:
    """Return operator-configured capacity remaining after active leases."""
    return max(0, int(capacity) - active_resource_units(kind))


def acquire_resource_lease(kind: str, capacity: int, *, units: int = 1,
                           lease_seconds: int = 300, key: str | None = None,
                           instance_id: str | None = None, step_id: str | None = None,
                           activation: int | None = None,
                           metadata: dict[str, Any] | None = None) -> str | None:
    """Atomically acquire bounded capacity, or return ``None`` without spawning."""
    init_db()
    units, capacity = int(units), int(capacity)
    if units < 1 or capacity < 1:
        return None
    key = key or f"{kind}:{uuid.uuid4().hex}"
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    lease_until = (now_dt + timedelta(seconds=int(lease_seconds))).isoformat()
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE resource_leases SET state='expired',released_at=? "
            "WHERE state='active' AND lease_until IS NOT NULL AND lease_until<=?",
            (now, now),
        )
        used = int(conn.execute(
            "SELECT COALESCE(SUM(units),0) FROM resource_leases WHERE kind=? AND state='active'",
            (kind,),
        ).fetchone()[0])
        existing = conn.execute(
            "SELECT state,units FROM resource_leases WHERE key=?", (key,),
        ).fetchone()
        if existing and existing["state"] == "active":
            conn.execute(
                "UPDATE resource_leases SET lease_until=?,metadata_json=? WHERE key=?",
                (lease_until, json.dumps(metadata or {}, sort_keys=True), key),
            )
            return key
        if used + units > capacity:
            return None
        if existing:
            conn.execute(
                "UPDATE resource_leases SET kind=?,units=?,instance_id=?,step_id=?,activation=?,"
                "state='active',lease_until=?,metadata_json=?,released_at=NULL WHERE key=?",
                (kind, units, instance_id, step_id, activation, lease_until,
                 json.dumps(metadata or {}, sort_keys=True), key),
            )
            return key
        conn.execute(
            "INSERT INTO resource_leases(key,kind,units,instance_id,step_id,activation,state,"
            "lease_until,metadata_json,created_at,released_at) VALUES(?,?,?,?,?,?,'active',?,?,?,NULL)",
            (key, kind, units, instance_id, step_id, activation, lease_until,
             json.dumps(metadata or {}, sort_keys=True), now),
        )
    return key


def renew_resource_lease(key: str, *, lease_seconds: int = 300) -> bool:
    lease_until = (datetime.now(timezone.utc) + timedelta(seconds=int(lease_seconds))).isoformat()
    with _connect() as conn:
        return conn.execute(
            "UPDATE resource_leases SET lease_until=? WHERE key=? AND state='active'",
            (lease_until, key),
        ).rowcount == 1


def release_resource_lease(key: str) -> bool:
    with _connect() as conn:
        return conn.execute(
            "UPDATE resource_leases SET state='released',released_at=?,lease_until=NULL "
            "WHERE key=? AND state='active'",
            (_now(), key),
        ).rowcount == 1


def acquire_port_lease(port_min: int, port_max: int, *, key: str, lease_seconds: int = 300,
                       instance_id: str | None = None, step_id: str | None = None,
                       activation: int | None = None,
                       metadata: dict[str, Any] | None = None) -> int | None:
    """Atomically bind one free port in ``[port_min, port_max]`` as a lease.

    Reuses ``resource_leases`` (kind='port') so expiry/renewal/release share
    the A1 governor rather than a parallel bookkeeping table. Unlike
    ``acquire_resource_lease`` this must pick a specific port number, so the
    scan-and-insert happens under the same ``BEGIN IMMEDIATE`` writer lock
    that already serializes concurrent lease acquisition.
    """
    init_db()
    port_min, port_max = int(port_min), int(port_max)
    if port_min < 1 or port_max < port_min:
        return None
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    lease_until = (now_dt + timedelta(seconds=int(lease_seconds))).isoformat()
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE resource_leases SET state='expired',released_at=? "
            "WHERE state='active' AND lease_until IS NOT NULL AND lease_until<=?",
            (now, now),
        )
        existing = conn.execute(
            "SELECT state,metadata_json FROM resource_leases WHERE key=?", (key,),
        ).fetchone()
        if existing and existing["state"] == "active":
            try:
                port = int(json.loads(existing["metadata_json"])["port"])
            except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                return None
            conn.execute(
                "UPDATE resource_leases SET lease_until=? WHERE key=?",
                (lease_until, key),
            )
            return port
        used_ports: set[int] = set()
        for row in conn.execute(
            "SELECT metadata_json FROM resource_leases WHERE kind='port' AND state='active'"
        ):
            try:
                used_ports.add(int(json.loads(row["metadata_json"])["port"]))
            except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                continue
        port = next((p for p in range(port_min, port_max + 1) if p not in used_ports), None)
        if port is None:
            return None
        meta = dict(metadata or {})
        meta["port"] = port
        conn.execute(
            "INSERT INTO resource_leases(key,kind,units,instance_id,step_id,activation,state,"
            "lease_until,metadata_json,created_at,released_at) VALUES(?,'port',1,?,?,?,'active',?,?,?,NULL)",
            (key, instance_id, step_id, activation, lease_until,
             json.dumps(meta, sort_keys=True), now),
        )
        return port


def insert_env_session(id: str, *, key: str, base_sha: str, candidate_sha: str | None,
                       manifest_path: str, manifest_blob_sha: str, tracked_input_hash: str,
                       workspace_path: str, control_plane_risk: bool,
                       control_plane_paths: list[str], lease_key: str | None,
                       stdout_path: str | None, stderr_path: str | None) -> None:
    """Persist a new materialization row before any bootstrap child spawns."""
    init_db()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO env_sessions(id,key,base_sha,candidate_sha,manifest_path,"
            "manifest_blob_sha,tracked_input_hash,workspace_path,state,control_plane_risk,"
            "control_plane_paths,lease_key,stdout_path,stderr_path,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,'materializing',?,?,?,?,?,?)",
            (id, key, base_sha, candidate_sha, manifest_path, manifest_blob_sha,
             tracked_input_hash, workspace_path, int(bool(control_plane_risk)),
             json.dumps(sorted(control_plane_paths), sort_keys=True), lease_key,
             stdout_path, stderr_path, _now()),
        )


def env_session_row(id: str) -> dict[str, Any] | None:
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM env_sessions WHERE id=?", (id,)).fetchone()
    return dict(row) if row else None


def latest_env_session_for_key(key: str) -> dict[str, Any] | None:
    """Return the most recent materialization row for a content-addressed key."""
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM env_sessions WHERE key=? ORDER BY created_at DESC,id DESC LIMIT 1",
            (key,),
        ).fetchone()
    return dict(row) if row else None


def mark_env_session_pid(id: str, pid: int) -> None:
    """Persist the child pid the instant ``Popen`` returns.

    Split from the start-token write (``mark_env_session_token``) so a
    daemon crash during the up-to-two-second OS start-token observation
    window still leaves the pid durable — recovery can then verify/kill the
    real child instead of leaking an untracked orphan (review finding #2).
    """
    with _connect() as conn:
        changed = conn.execute(
            "UPDATE env_sessions SET pid=?,started_at=? WHERE id=? AND state='materializing'",
            (int(pid), _now(), id),
        ).rowcount
        if changed != 1:
            raise ValueError(f"unknown or terminal env_session {id}")


def mark_env_session_token(id: str, token: str | None) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE env_sessions SET process_start_token=? WHERE id=?", (token, id),
        )


def update_env_session_network_enforcement(id: str, level: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE env_sessions SET network_enforcement_level=? WHERE id=?", (level, id),
        )


def mark_env_session_output_capped(id: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE env_sessions SET output_cap_exceeded=1 WHERE id=?", (id,))


def update_env_session_state(id: str, state: str, *, last_error: str | None = None) -> None:
    terminal = state in {"ready", "failed"}
    with _connect() as conn:
        conn.execute(
            "UPDATE env_sessions SET state=?,last_error=?,finished_at=CASE WHEN ? THEN ? ELSE finished_at END "
            "WHERE id=?",
            (state, last_error, terminal, _now() if terminal else None, id),
        )


def nonterminal_env_sessions() -> list[dict[str, Any]]:
    init_db()
    with _connect() as conn:
        return _rows(conn.execute("SELECT * FROM env_sessions WHERE state='materializing'"))


def insert_app_session(id: str, *, env_session_id: str, request_key: str, workspace_path: str,
                       expected_instance_id: str | None = None,
                       expected_head_sha: str | None = None,
                       stdout_path: str | None = None,
                       stderr_path: str | None = None) -> dict[str, Any]:
    """Idempotently persist an app-session request keyed by ``request_key``."""
    init_db()
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO app_sessions(id,env_session_id,request_key,workspace_path,"
            "expected_instance_id,expected_head_sha,state,stdout_path,stderr_path,created_at) "
            "VALUES(?,?,?,?,?,?,'starting',?,?,?)",
            (id, env_session_id, request_key, workspace_path, expected_instance_id,
             expected_head_sha, stdout_path, stderr_path, _now()),
        )
        row = conn.execute(
            "SELECT * FROM app_sessions WHERE request_key=?", (request_key,),
        ).fetchone()
    result = dict(row)
    requested = (str(env_session_id), str(Path(workspace_path).resolve()))
    persisted = (str(result["env_session_id"]), str(Path(result["workspace_path"]).resolve()))
    if persisted != requested or (
        expected_instance_id is not None
        and result.get("expected_instance_id") != expected_instance_id
    ) or (
        expected_head_sha is not None
        and result.get("expected_head_sha") != expected_head_sha
    ):
        raise ValueError("app-session request key is already bound to another candidate identity")
    return result


def app_session_row(id: str) -> dict[str, Any] | None:
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM app_sessions WHERE id=?", (id,)).fetchone()
    return dict(row) if row else None


def app_session_by_request_key(request_key: str) -> dict[str, Any] | None:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM app_sessions WHERE request_key=?", (request_key,),
        ).fetchone()
    return dict(row) if row else None


def mark_app_session_bound(id: str, *, port: int, port_lease_key: str, app_url: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE app_sessions SET port=?,port_lease_key=?,app_url=? WHERE id=?",
            (int(port), port_lease_key, app_url, id),
        )


def mark_app_session_pid(id: str, pid: int) -> None:
    """Persist the child pid the instant ``Popen`` returns (see finding #2)."""
    with _connect() as conn:
        changed = conn.execute(
            "UPDATE app_sessions SET pid=?,started_at=? "
            "WHERE id=? AND state IN ('starting','stopping')",
            (int(pid), _now(), id),
        ).rowcount
        if changed != 1:
            raise ValueError(f"unknown or terminal app_session {id}")


def mark_app_session_token(id: str, token: str | None) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE app_sessions SET process_start_token=? WHERE id=?", (token, id),
        )


def update_app_session_network_enforcement(id: str, level: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE app_sessions SET network_enforcement_level=? WHERE id=?", (level, id),
        )


def mark_app_session_output_capped(id: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE app_sessions SET output_cap_exceeded=1 WHERE id=?", (id,))


def update_app_session_state(id: str, state: str, *, health_status: str | None = None,
                             last_error: str | None = None) -> None:
    now = _now()
    stopping_at = now if state == "stopping" else None
    stopped_at = now if state in {"stopped", "crashed"} else None
    healthy_at = now if state == "healthy" else None
    with _connect() as conn:
        conn.execute(
            "UPDATE app_sessions SET state=?,"
            "health_status=COALESCE(?,health_status),"
            "last_error=COALESCE(?,last_error),"
            "healthy_at=COALESCE(?,healthy_at),"
            "stopping_at=COALESCE(?,stopping_at),"
            "stopped_at=COALESCE(?,stopped_at) "
            "WHERE id=?",
            (state, health_status, last_error, healthy_at, stopping_at, stopped_at, id),
        )


def nonterminal_app_sessions() -> list[dict[str, Any]]:
    init_db()
    with _connect() as conn:
        return _rows(conn.execute(
            "SELECT * FROM app_sessions WHERE state IN ('starting','healthy','stopping')"
        ))


def sync_get(gh_number) -> dict | None:
    """Return the synchronization mapping for a GitHub issue."""
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM sync WHERE gh_number=?", (gh_number,)).fetchone()
    return dict(row) if row else None


def sync_upsert(gh_number, task_id, gh_updated, k_updated) -> None:
    """Create or update a GitHub issue to kanban task mapping."""
    init_db()
    with _connect() as conn:
        conn.execute("""INSERT INTO sync VALUES(?,?,?,?,?) ON CONFLICT(gh_number) DO UPDATE SET
          task_id=excluded.task_id,gh_updated=excluded.gh_updated,k_updated=excluded.k_updated,last_synced_at=excluded.last_synced_at""",
                     (gh_number, task_id, gh_updated, k_updated, _now()))


class GraphStoreConflict(RuntimeError):
    """A GraphRunner write conflicts with already-persisted durable state."""


class GraphStoreIntegrityError(RuntimeError):
    """A GraphRunner write violates the durable v1 schema or its bindings."""


_GRAPH_UNSET = object()
_BOX_TERMINAL_STATES = {"completed", "failed", "cancelled"}
_EVENT_TERMINAL_STATES = {"applied", "discarded", "failed"}


def _graph_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _validate_graph_recipe_identity(
    *, recipe_name: str, recipe_hash: str, recipe_snapshot_json: str,
):
    from .graph_recipe import GraphRecipeError, validate

    try:
        document = json.loads(recipe_snapshot_json)
        recipe = validate(document)
    except (json.JSONDecodeError, GraphRecipeError) as exc:
        raise GraphStoreIntegrityError("invalid frozen GraphRecipe snapshot") from exc
    if recipe.canonical_json != recipe_snapshot_json:
        raise GraphStoreIntegrityError("GraphRecipe snapshot is not canonical JSON")
    if recipe.name != recipe_name or recipe.hash != recipe_hash:
        raise GraphStoreIntegrityError("GraphRecipe name or hash does not match snapshot")
    return recipe


def _graph_recipe_for_run(conn: sqlite3.Connection, run_id: str):
    row = conn.execute(
        """SELECT recipe_name,recipe_hash,recipe_snapshot_json
           FROM recipe_runs_v1 WHERE id=?""",
        (run_id,),
    ).fetchone()
    if row is None:
        raise GraphStoreIntegrityError(f"GraphRecipe run does not exist: {run_id}")
    return _validate_graph_recipe_identity(
        recipe_name=row["recipe_name"], recipe_hash=row["recipe_hash"],
        recipe_snapshot_json=row["recipe_snapshot_json"],
    )


def _graph_row(conn: sqlite3.Connection, table: str, column: str, value: Any) -> dict[str, Any] | None:
    if table not in {
        "recipe_runs_v1", "box_attempts_v1", "route_tokens_v1",
        "run_events_v1", "human_box_decisions_v1",
    } or column not in {"id", "key", "launch_key"}:
        raise ValueError("unsupported GraphRunner row lookup")
    row = conn.execute(f"SELECT * FROM {table} WHERE {column}=?", (value,)).fetchone()
    return dict(row) if row else None


def create_recipe_run_v1(
    *, run_id: str, project_id: str, board: str, recipe_name: str,
    recipe_hash: str, recipe_snapshot_json: str, request_text: str,
    workspace_path: str | None, launch_key: str,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Create a frozen GraphRunner run, idempotently keyed by ``launch_key``."""
    _validate_graph_recipe_identity(
        recipe_name=recipe_name, recipe_hash=recipe_hash,
        recipe_snapshot_json=recipe_snapshot_json,
    )
    if conn is None:
        init_db()
        with _connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return create_recipe_run_v1(
                run_id=run_id, project_id=project_id, board=board,
                recipe_name=recipe_name, recipe_hash=recipe_hash,
                recipe_snapshot_json=recipe_snapshot_json, request_text=request_text,
                workspace_path=workspace_path, launch_key=launch_key, conn=db,
            )
    existing = _graph_row(conn, "recipe_runs_v1", "launch_key", launch_key)
    identity = {
        "project_id": project_id, "board": board, "recipe_name": recipe_name,
        "recipe_hash": recipe_hash, "recipe_snapshot_json": recipe_snapshot_json,
        "request_text": request_text, "workspace_path": workspace_path,
        "launch_key": launch_key,
    }
    if existing:
        if all(existing[key] == value for key, value in identity.items()):
            return existing
        raise GraphStoreConflict(f"launch key {launch_key!r} already identifies another run")
    now = _now()
    try:
        conn.execute(
            """INSERT INTO recipe_runs_v1(
                id,project_id,board,recipe_name,recipe_hash,recipe_snapshot_json,
                request_text,workspace_path,launch_key,state,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,'running',?,?)""",
            (run_id, project_id, board, recipe_name, recipe_hash,
             recipe_snapshot_json, request_text, workspace_path, launch_key, now, now),
        )
    except sqlite3.IntegrityError as exc:
        row = conn.execute(
            "SELECT * FROM recipe_runs_v1 WHERE id=? OR launch_key=?",
            (run_id, launch_key),
        ).fetchone()
        if row:
            existing = dict(row)
            if all(existing[key] == value for key, value in identity.items()):
                return existing
            raise GraphStoreConflict(
                f"run or launch key already exists with different content: {run_id}"
            ) from exc
        raise GraphStoreIntegrityError(f"invalid recipe run: {run_id}") from exc
    return _graph_row(conn, "recipe_runs_v1", "id", run_id)  # type: ignore[return-value]


def get_recipe_run_v1(
    run_id: str, *, conn: sqlite3.Connection | None = None,
) -> dict[str, Any] | None:
    if conn is None:
        init_db()
        with _connect() as db:
            return get_recipe_run_v1(run_id, conn=db)
    return _graph_row(conn, "recipe_runs_v1", "id", run_id)


def list_recipe_runs_v1(
    *, project_id: str | None = None, state: str | None = None,
    limit: int = 100, conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    if conn is None:
        init_db()
        with _connect() as db:
            return list_recipe_runs_v1(
                project_id=project_id, state=state, limit=limit, conn=db,
            )
    clauses: list[str] = []
    params: list[Any] = []
    if project_id is not None:
        clauses.append("project_id=?")
        params.append(project_id)
    if state is not None:
        clauses.append("state=?")
        params.append(state)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(int(limit), 1000)))
    return _rows(conn.execute(
        f"SELECT * FROM recipe_runs_v1{where} ORDER BY created_at,id LIMIT ?", params,
    ))


def insert_box_attempt_v1(
    *, attempt_id: str, run_id: str, box_id: str, ordinal: int,
    state: str, input_work: Any, executor_run_id: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    if conn is None:
        init_db()
        with _connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return insert_box_attempt_v1(
                attempt_id=attempt_id, run_id=run_id, box_id=box_id,
                ordinal=ordinal, state=state, input_work=input_work,
                executor_run_id=executor_run_id, conn=db,
            )
    input_json = _graph_json(input_work)
    recipe = _graph_recipe_for_run(conn, run_id)
    try:
        recipe.box(box_id)
    except ValueError as exc:
        raise GraphStoreIntegrityError(f"unknown box in attempt: {box_id}") from exc
    expected = {
        "run_id": run_id, "box_id": box_id, "ordinal": int(ordinal),
        "input_work_json": input_json,
    }
    row = conn.execute(
        "SELECT * FROM box_attempts_v1 WHERE run_id=? AND box_id=? AND ordinal=?",
        (run_id, box_id, int(ordinal)),
    ).fetchone()
    if row:
        existing = dict(row)
        if all(existing[key] == value for key, value in expected.items()):
            return existing
        raise GraphStoreConflict("box attempt identity already exists with different content")
    now = _now()
    try:
        conn.execute(
            """INSERT INTO box_attempts_v1(
                id,run_id,box_id,ordinal,state,executor_run_id,input_work_json,
                created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (attempt_id, run_id, box_id, int(ordinal), state, executor_run_id,
             input_json, now, now),
        )
    except sqlite3.IntegrityError as exc:
        row = conn.execute(
            """SELECT * FROM box_attempts_v1
               WHERE id=? OR (run_id=? AND box_id=? AND ordinal=?)""",
            (attempt_id, run_id, box_id, int(ordinal)),
        ).fetchone()
        if row:
            existing = dict(row)
            if all(existing[key] == value for key, value in expected.items()):
                return existing
            raise GraphStoreConflict(
                "box attempt identity already exists with different content"
            ) from exc
        raise GraphStoreIntegrityError(f"invalid box attempt: {attempt_id}") from exc
    return _graph_row(conn, "box_attempts_v1", "id", attempt_id)  # type: ignore[return-value]


def update_box_attempt_v1(
    attempt_id: str, *, expected_state: str, state: str,
    executor_run_id: int | None | object = _GRAPH_UNSET,
    output_work: str | None | object = _GRAPH_UNSET,
    result: str | None | object = _GRAPH_UNSET,
    technical_failure: str | None | object = _GRAPH_UNSET,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    if conn is None:
        init_db()
        with _connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return update_box_attempt_v1(
                attempt_id, expected_state=expected_state, state=state,
                executor_run_id=executor_run_id, output_work=output_work,
                result=result, technical_failure=technical_failure, conn=db,
            )
    current = _graph_row(conn, "box_attempts_v1", "id", attempt_id)
    if current is None or current["state"] != expected_state:
        actual = current["state"] if current else "missing"
        raise GraphStoreConflict(
            f"box attempt state mismatch: expected {expected_state}, found {actual}"
        )
    values = {
        "executor_run_id": current["executor_run_id"] if executor_run_id is _GRAPH_UNSET else executor_run_id,
        "output_work": current["output_work"] if output_work is _GRAPH_UNSET else output_work,
        "result": current["result"] if result is _GRAPH_UNSET else result,
        "technical_failure": (
            current["technical_failure"]
            if technical_failure is _GRAPH_UNSET else technical_failure
        ),
    }
    if state == "completed":
        completion_result = values["result"]
        if not isinstance(completion_result, str) or not completion_result:
            raise GraphStoreIntegrityError("completed box attempt requires a result")
        recipe = _graph_recipe_for_run(conn, current["run_id"])
        if (
            not recipe.is_end(current["box_id"])
            and not recipe.destinations(current["box_id"], completion_result)
        ):
            raise GraphStoreIntegrityError(
                "box attempt result is not declared by frozen recipe"
            )
    now = _now()
    finished_at = now if state in _BOX_TERMINAL_STATES else None
    updated = conn.execute(
        """UPDATE box_attempts_v1
           SET state=?,executor_run_id=?,output_work=?,result=?,technical_failure=?,
               updated_at=?,finished_at=?
           WHERE id=? AND state=?""",
        (state, values["executor_run_id"], values["output_work"], values["result"],
         values["technical_failure"], now, finished_at, attempt_id, expected_state),
    ).rowcount
    if updated != 1:
        raise GraphStoreConflict("box attempt state changed concurrently")
    return _graph_row(conn, "box_attempts_v1", "id", attempt_id)  # type: ignore[return-value]


def insert_route_token_v1(
    *, token_id: str, run_id: str, source_attempt_id: str | None,
    arrow_index: int | None, destination_box_id: str, lineage: Any,
    work_refs: Any, conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    if conn is None:
        init_db()
        with _connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return insert_route_token_v1(
                token_id=token_id, run_id=run_id,
                source_attempt_id=source_attempt_id, arrow_index=arrow_index,
                destination_box_id=destination_box_id, lineage=lineage,
                work_refs=work_refs, conn=db,
            )
    lineage_json = _graph_json(lineage)
    work_refs_json = _graph_json(work_refs)
    recipe = _graph_recipe_for_run(conn, run_id)
    if (source_attempt_id is None) != (arrow_index is None):
        raise GraphStoreIntegrityError(
            "route token source_attempt_id and arrow_index must both be null or non-null"
        )
    if source_attempt_id is None:
        if destination_box_id != recipe.start:
            raise GraphStoreIntegrityError("root route token must target the recipe start box")
    else:
        source = conn.execute(
            """SELECT box_id,result,state FROM box_attempts_v1
               WHERE id=? AND run_id=?""",
            (source_attempt_id, run_id),
        ).fetchone()
        if source is None:
            raise GraphStoreIntegrityError("route token source attempt does not exist in run")
        assert arrow_index is not None
        if arrow_index < 0 or arrow_index >= len(recipe.arrows):
            raise GraphStoreIntegrityError("route token arrow_index is outside recipe")
        arrow = recipe.arrows[arrow_index]
        destinations = arrow["to"]
        assert isinstance(destinations, tuple)
        if (
            source["state"] != "completed"
            or arrow["from"] != source["box_id"]
            or arrow["result"] != source["result"]
            or destination_box_id not in destinations
        ):
            raise GraphStoreIntegrityError(
                "route token source, arrow, and destination do not match recipe"
            )
    existing = _graph_row(conn, "route_tokens_v1", "id", token_id)
    if existing is None:
        row = conn.execute(
            """SELECT * FROM route_tokens_v1
               WHERE run_id=? AND source_attempt_id IS ? AND arrow_index IS ?
                 AND destination_box_id=?""",
            (run_id, source_attempt_id, arrow_index, destination_box_id),
        ).fetchone()
        existing = dict(row) if row else None
    expected = {
        "run_id": run_id, "source_attempt_id": source_attempt_id,
        "arrow_index": arrow_index, "destination_box_id": destination_box_id,
        "lineage_json": lineage_json, "work_refs_json": work_refs_json,
    }
    if existing:
        if all(existing[key] == value for key, value in expected.items()):
            return existing
        raise GraphStoreConflict("route token already exists with different content")
    now = _now()
    try:
        conn.execute(
            """INSERT INTO route_tokens_v1(
                id,run_id,source_attempt_id,arrow_index,destination_box_id,
                lineage_json,work_refs_json,state,created_at
            ) VALUES(?,?,?,?,?,?,?,'pending',?)""",
            (token_id, run_id, source_attempt_id, arrow_index, destination_box_id,
             lineage_json, work_refs_json, now),
        )
    except sqlite3.IntegrityError as exc:
        row = conn.execute(
            """SELECT * FROM route_tokens_v1
               WHERE id=? OR (
                   run_id=? AND source_attempt_id IS ? AND arrow_index IS ?
                   AND destination_box_id=?
               )""",
            (token_id, run_id, source_attempt_id, arrow_index, destination_box_id),
        ).fetchone()
        if row:
            existing = dict(row)
            if all(existing[key] == value for key, value in expected.items()):
                return existing
            raise GraphStoreConflict(
                "route token already exists with different content"
            ) from exc
        raise GraphStoreIntegrityError(f"invalid route token: {token_id}") from exc
    return _graph_row(conn, "route_tokens_v1", "id", token_id)  # type: ignore[return-value]


def consume_route_tokens_v1(
    *, run_id: str, destination_box_id: str, token_ids: list[str] | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    if conn is None:
        init_db()
        with _connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return consume_route_tokens_v1(
                run_id=run_id, destination_box_id=destination_box_id,
                token_ids=token_ids, conn=db,
            )
    params: list[Any] = [run_id, destination_box_id]
    token_filter = ""
    if token_ids is not None:
        if not token_ids:
            return []
        unique_ids = list(dict.fromkeys(token_ids))
        if len(unique_ids) != len(token_ids):
            raise GraphStoreConflict("route token request contains duplicate ids")
        requested = _rows(conn.execute(
            f"SELECT * FROM route_tokens_v1 WHERE id IN "
            f"({','.join('?' for _ in unique_ids)})",
            unique_ids,
        ))
        if len(requested) != len(unique_ids) or any(
            row["run_id"] != run_id
            or row["destination_box_id"] != destination_box_id
            or row["state"] != "pending"
            for row in requested
        ):
            raise GraphStoreConflict(
                "requested route tokens are missing, consumed, or bound elsewhere"
            )
        token_filter = f" AND id IN ({','.join('?' for _ in unique_ids)})"
        params.extend(unique_ids)
    rows = _rows(conn.execute(
        "SELECT * FROM route_tokens_v1 WHERE run_id=? AND destination_box_id=? "
        f"AND state='pending'{token_filter} ORDER BY created_at,id",
        params,
    ))
    if not rows:
        return []
    now = _now()
    ids = [row["id"] for row in rows]
    updated = conn.execute(
        f"UPDATE route_tokens_v1 SET state='consumed',consumed_at=? "
        f"WHERE state='pending' AND id IN ({','.join('?' for _ in ids)})",
        [now, *ids],
    ).rowcount
    if updated != len(ids):
        raise GraphStoreConflict("route tokens changed concurrently")
    return _rows(conn.execute(
        f"SELECT * FROM route_tokens_v1 WHERE id IN ({','.join('?' for _ in ids)}) "
        "ORDER BY created_at,id",
        ids,
    ))


def enqueue_run_event_v1(
    *, key: str, run_id: str, source: str, payload: Any,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    if conn is None:
        init_db()
        with _connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return enqueue_run_event_v1(
                key=key, run_id=run_id, source=source, payload=payload, conn=db,
            )
    payload_json = _graph_json(payload)
    existing = _graph_row(conn, "run_events_v1", "key", key)
    if existing:
        if (
            existing["run_id"] == run_id
            and existing["source"] == source
            and existing["payload_json"] == payload_json
        ):
            return existing
        raise GraphStoreConflict("run event key already exists with different content")
    try:
        conn.execute(
            """INSERT INTO run_events_v1(
                key,run_id,source,payload_json,state,created_at
            ) VALUES(?,?,?,?,'pending',?)""",
            (key, run_id, source, payload_json, _now()),
        )
    except sqlite3.IntegrityError as exc:
        existing = _graph_row(conn, "run_events_v1", "key", key)
        if existing:
            if (
                existing["run_id"] == run_id
                and existing["source"] == source
                and existing["payload_json"] == payload_json
            ):
                return existing
            raise GraphStoreConflict(
                "run event key already exists with different content"
            ) from exc
        raise GraphStoreIntegrityError(f"invalid run event: {key}") from exc
    return _graph_row(conn, "run_events_v1", "key", key)  # type: ignore[return-value]


def _graph_normalize_time(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _graph_lease_deadline(now: str, lease_seconds: int) -> str:
    parsed = datetime.fromisoformat(now.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (
        parsed.astimezone(timezone.utc)
        + timedelta(seconds=max(1, int(lease_seconds)))
    ).isoformat()


def lease_run_events_v1(
    *, owner: str, limit: int = 10, lease_seconds: int = 60,
    now: str | None = None, run_id: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    if conn is None:
        init_db()
        with _connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return lease_run_events_v1(
                owner=owner, limit=limit, lease_seconds=lease_seconds,
                now=now, run_id=run_id, conn=db,
            )
    now = _graph_normalize_time(now or _now())
    lease_until = _graph_lease_deadline(now, lease_seconds)
    conn.execute(
        """UPDATE run_events_v1
           SET state='pending',lease_owner=NULL,lease_until=NULL
           WHERE state='leased' AND lease_until<=?""",
        (now,),
    )
    where = "state='pending'"
    params: list[Any] = []
    if run_id is not None:
        where += " AND run_id=?"
        params.append(run_id)
    params.append(max(1, min(int(limit), 1000)))
    keys = [
        row["key"] for row in conn.execute(
            f"SELECT key FROM run_events_v1 WHERE {where} ORDER BY created_at,key LIMIT ?",
            params,
        )
    ]
    if not keys:
        return []
    updated = conn.execute(
        f"""UPDATE run_events_v1
            SET state='leased',lease_owner=?,lease_until=?,attempt_count=attempt_count+1
            WHERE state='pending' AND key IN ({','.join('?' for _ in keys)})""",
        [owner, lease_until, *keys],
    ).rowcount
    if updated != len(keys):
        raise GraphStoreConflict("run event lease changed concurrently")
    return _rows(conn.execute(
        f"SELECT * FROM run_events_v1 WHERE key IN ({','.join('?' for _ in keys)}) "
        "ORDER BY created_at,key",
        keys,
    ))


def finish_run_event_v1(
    key: str, *, owner: str, expected_attempt_count: int, state: str,
    outcome: str | None = None, error: str | None = None, now: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    if state not in _EVENT_TERMINAL_STATES:
        raise ValueError(f"invalid terminal run event state: {state}")
    if conn is None:
        init_db()
        with _connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return finish_run_event_v1(
                key, owner=owner, expected_attempt_count=expected_attempt_count,
                state=state, outcome=outcome, error=error, now=now, conn=db,
            )
    applied_at = _graph_normalize_time(now or _now())
    updated = conn.execute(
        """UPDATE run_events_v1
           SET state=?,lease_owner=NULL,lease_until=NULL,outcome=?,last_error=?,applied_at=?
           WHERE key=? AND state='leased' AND lease_owner=?
             AND attempt_count=? AND lease_until>?""",
        (state, outcome, error, applied_at, key, owner,
         int(expected_attempt_count), applied_at),
    ).rowcount
    if updated != 1:
        raise GraphStoreConflict(f"run event lease is not owned by {owner!r}: {key}")
    return _graph_row(conn, "run_events_v1", "key", key)  # type: ignore[return-value]


def record_human_box_decision_v1(
    *, decision_id: str, attempt_id: str, result: str, actor_kind: str,
    actor_id: str, channel: str, nonce_hash: str, event_key: str,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    if conn is None:
        init_db()
        with _connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return record_human_box_decision_v1(
                decision_id=decision_id, attempt_id=attempt_id, result=result,
                actor_kind=actor_kind, actor_id=actor_id, channel=channel,
                nonce_hash=nonce_hash, event_key=event_key, conn=db,
            )
    binding = conn.execute(
        """SELECT a.run_id AS attempt_run_id,a.box_id AS box_id,
                  e.run_id AS event_run_id
           FROM box_attempts_v1 a CROSS JOIN run_events_v1 e
           WHERE a.id=? AND e.key=?""",
        (attempt_id, event_key),
    ).fetchone()
    if binding is None:
        raise GraphStoreIntegrityError("human decision attempt or event does not exist")
    if binding["attempt_run_id"] != binding["event_run_id"]:
        raise GraphStoreIntegrityError(
            "human decision attempt and event are not in the same run"
        )
    recipe = _graph_recipe_for_run(conn, binding["attempt_run_id"])
    box = recipe.box(binding["box_id"])
    if box["who"] != "human":
        raise GraphStoreIntegrityError("human decision attempt is not a human box")
    if not recipe.destinations(binding["box_id"], result):
        raise GraphStoreIntegrityError("human decision result is not declared by recipe")
    rows = _rows(conn.execute(
        """SELECT * FROM human_box_decisions_v1
           WHERE attempt_id=? OR nonce_hash=? OR event_key=?""",
        (attempt_id, nonce_hash, event_key),
    ))
    expected = {
        "attempt_id": attempt_id, "result": result, "actor_kind": actor_kind,
        "actor_id": actor_id, "channel": channel, "nonce_hash": nonce_hash,
        "event_key": event_key,
    }
    if rows:
        if len(rows) == 1 and all(rows[0][key] == value for key, value in expected.items()):
            return rows[0]
        raise GraphStoreConflict("human decision replay conflicts with durable decision")
    try:
        conn.execute(
            """INSERT INTO human_box_decisions_v1(
                id,attempt_id,result,actor_kind,actor_id,channel,nonce_hash,created_at,event_key
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (decision_id, attempt_id, result, actor_kind, actor_id, channel,
             nonce_hash, _now(), event_key),
        )
    except sqlite3.IntegrityError as exc:
        rows = _rows(conn.execute(
            """SELECT * FROM human_box_decisions_v1
               WHERE id=? OR attempt_id=? OR nonce_hash=? OR event_key=?""",
            (decision_id, attempt_id, nonce_hash, event_key),
        ))
        if len(rows) == 1 and all(
            rows[0][key] == value for key, value in expected.items()
        ):
            return rows[0]
        if rows:
            raise GraphStoreConflict(
                "human decision replay conflicts with durable decision"
            ) from exc
        raise GraphStoreIntegrityError("invalid human decision") from exc
    return _graph_row(conn, "human_box_decisions_v1", "id", decision_id)  # type: ignore[return-value]


__all__ = ["init_db", "record_run_start", "record_run_spawned", "record_run_end", "record_run_crashed", "nonterminal_runs", "nonterminal_verification_runs", "nonterminal_daemon_runs", "reconcile_daemon_runs", "run_row", "exact_workspace_run", "record_daemon_start", "record_daemon_tick", "record_daemon_end", "latest_daemon_run", "get_policy", "set_policy", "load_project_recipe_policy", "save_project_recipe_policy", "project_flight", "project_flight_by_idempotency_key", "project_flight_by_linear_issue_id", "project_rollup", "record_decision", "decisions_for", "add_monitor", "due_monitors", "advance_monitor", "record_monitor_outcome", "clear_monitor", "add_watchdog", "watchdogs", "set_watchdog_fingerprint", "seat_paused", "set_seat_paused", "costs_rollup", "reap_resource_leases", "active_resource_units", "available_resource_units", "acquire_resource_lease", "renew_resource_lease", "release_resource_lease", "acquire_port_lease", "insert_env_session", "env_session_row", "latest_env_session_for_key", "mark_env_session_spawned", "update_env_session_state", "nonterminal_env_sessions", "insert_app_session", "app_session_row", "app_session_by_request_key", "mark_app_session_bound", "mark_app_session_spawned", "update_app_session_state", "nonterminal_app_sessions", "sync_get", "sync_upsert", "GraphStoreConflict", "GraphStoreIntegrityError", "create_recipe_run_v1", "get_recipe_run_v1", "list_recipe_runs_v1", "insert_box_attempt_v1", "update_box_attempt_v1", "insert_route_token_v1", "consume_route_tokens_v1", "enqueue_run_event_v1", "lease_run_events_v1", "finish_run_event_v1", "record_human_box_decision_v1"]
