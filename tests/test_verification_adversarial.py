"""Independent adversarial corpus attacking the merged verification engine.

Each test constructs the real attack named in the external program review
§2.4.6-§2.4.10 at the strongest feasible boundary given what is actually
implemented (playwright/ffmpeg capture is not wired to a real browser in
this codebase -- those attacks are constructed against the deterministic
capture-container/identity-binding primitives that stand in for a runner-
generated overlay, and against the honest fail-closed behavior of the
playwright stub) and asserts the precise fail-closed outcome, not just a
named pass.
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from shipfactory import config, store, verification as verify
from shipfactory.recipes import advancer


_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Adversarial Test", "GIT_AUTHOR_EMAIL": "adversarial@example.invalid",
    "GIT_COMMITTER_NAME": "Adversarial Test", "GIT_COMMITTER_EMAIL": "adversarial@example.invalid",
}


def _manifest(*, argv=None, oracle=None, driver="command", case_id="unit-suite"):
    case = {
        "id": case_id, "requirement_ids": ["REQ-1"], "driver": driver,
        "argv": argv or ["python3", "-c", "print('ok')"],
        "oracle": oracle or {"type": "exit_code", "equals": 0},
    }
    return {
        "schema": verify.VERIFICATION_SCHEMA, "cases": [case],
        "capture": {"video": False, "trace": False, "screenshots": "on-failure"},
    }


def _repo(tmp_path: Path, document=None, *, name: str = "repo"):
    repo = tmp_path / name
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / ".shipfactory").mkdir()
    import yaml
    (repo / ".shipfactory" / "verification.yaml").write_text(
        yaml.safe_dump(document or _manifest(), sort_keys=False), encoding="utf-8",
    )
    (repo / "tracked.txt").write_text("stable\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, env=_GIT_ENV, check=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=repo, text=True).strip()
    return repo, head, tree


def _commit(repo: Path, message: str) -> tuple[str, str]:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", message], cwd=repo, env=_GIT_ENV, check=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=repo, text=True).strip()
    return head, tree


def _profile(**updates):
    value = {
        "max_runtime_seconds": 10, "infrastructure_retries": 1,
        "max_evidence_bytes": 100_000, "max_log_bytes": 50_000,
        "capture_video": False, "capture_trace": False, "capture_har": False,
        "browser_slots": 1,
    }
    value.update(updates)
    return value


def _run(repo, head, tree, manifest, **kwargs):
    return verify.run_verification(
        instance_id=kwargs.pop("instance_id", "instance"),
        step_id=kwargs.pop("step_id", "verify"), activation=kwargs.pop("activation", 1),
        input_revision_hash="revision", base_sha=head, head_sha=head, tree_sha=tree,
        workspace=repo, manifest=manifest, profile=kwargs.pop("profile", _profile()),
        **kwargs,
    )


def _action_payload(repo: Path, base: str, head: str, tree: str, *, instance: str, **overrides):
    protected = verify.load_verification_manifest(repo, base, verify_worktree_copy=False)
    candidate = verify.load_verification_manifest_if_present(repo, head)
    payload = {
        "instance_id": instance, "step_id": "verify", "activation": 1,
        "input_revision_hash": "revision", "base_sha": base, "head_sha": head,
        "tree_sha": tree, "workspace": str(repo),
        "manifest_relpath": verify.DEFAULT_MANIFEST_PATH,
        "manifest_blob_sha": (candidate or protected).blob_sha,
        "candidate_manifest_blob_sha": candidate.blob_sha if candidate else None,
        "protected_manifest_blob_sha": protected.blob_sha,
        "required_requirement_ids": ["REQ-1"], "profile": _profile(),
        "environment": "source", "environment_config": {},
    }
    payload.update(overrides)
    return payload


def _finish_action(payload, timeout=10):
    deadline = time.monotonic() + timeout
    result = verify.run_action(payload)
    while result["status"] == "pending" and time.monotonic() < deadline:
        time.sleep(0.05)
        verify.reap_runs()
        result = verify.run_action(payload)
    assert result["status"] != "pending"
    return result


def _bare_bundle(repo, head, tree, manifest, *, instance_id, step_id="verify", activation=1):
    """Insert an evidence_bundles row directly, for low-level item/seal control."""
    bundle_id = verify._bundle_id(instance_id, step_id, activation)
    verify._insert_bundle(
        bundle_id=bundle_id, instance_id=instance_id, step_id=step_id, activation=activation,
        input_revision_hash="revision", base_sha=head, head_sha=head, tree_sha=tree,
        environment_session_id=None, manifest=manifest,
    )
    return bundle_id


def _item_path(item_id: str) -> str:
    with store._connect() as db:
        row = db.execute("SELECT path FROM evidence_items WHERE id=?", (item_id,)).fetchone()
    return row["path"]


@pytest.fixture(autouse=True)
def _cleanup_async_verifiers():
    yield
    for record in list(verify._RUNNING.values()):
        verify._kill_child(record["proc"], record.get("token"))
        try:
            record["proc"].wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    verify._RUNNING.clear()
    verify._RESTORED_HOMES.clear()


# ---------------------------------------------------------------------------
# §2.4.10 #1 -- tests execute in the wrong worktree but pass.
# ---------------------------------------------------------------------------

def test_wrong_worktree_with_identical_shas_is_rejected(tmp_path):
    repo_a, head, tree = _repo(tmp_path, name="owned")
    repo_b = tmp_path / "decoy-clone"
    subprocess.run(["git", "clone", "-q", str(repo_a), str(repo_b)], check=True)
    assert subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo_b, text=True,
    ).strip() == head  # the decoy is byte-identical: SHA equality alone cannot tell them apart

    owner_task_id = "owner-task-1"
    run_id = store.record_run_start(
        owner_task_id, "build", "codex", "model", workspace_path=repo_a,
    )
    store.record_run_end(run_id, 0, None, None, 0.1, "done")

    payload = _action_payload(repo_a, head, head, tree, instance="wrong-worktree")
    payload["workspace"] = str(repo_b)  # scheduled against the wrong (decoy) worktree
    payload["workspace_owner_task_id"] = owner_task_id
    result = _finish_action(payload)
    assert result["status"] == "failed"
    with store._connect() as db:
        bundle = dict(db.execute(
            "SELECT * FROM evidence_bundles WHERE id=?", (result["bundle_id"],),
        ).fetchone())
    assert "does not match the worktree recorded" in bundle["invalid_reason"]


def test_correct_worktree_matching_its_recorded_owner_still_passes(tmp_path):
    repo, head, tree = _repo(tmp_path)
    owner_task_id = "owner-task-2"
    run_id = store.record_run_start(owner_task_id, "build", "codex", "model", workspace_path=repo)
    store.record_run_end(run_id, 0, None, None, 0.1, "done")
    payload = _action_payload(repo, head, head, tree, instance="right-worktree")
    payload["workspace_owner_task_id"] = owner_task_id
    result = _finish_action(payload)
    assert result["status"] == "done"


# ---------------------------------------------------------------------------
# §2.4.10 #2 -- old video/trace copied into the new evidence directory.
# §2.4.10 #19 -- manifest references an item whose bytes changed after hashing.
# ---------------------------------------------------------------------------

def test_stale_capture_copied_into_a_fresh_bundle_is_rejected(tmp_path):
    repo, head, tree = _repo(tmp_path)
    manifest = verify.load_verification_manifest(repo, head)
    now = store._now()

    bundle_a = _bare_bundle(repo, head, tree, manifest, instance_id="capture-a")
    root_a = verify._evidence_root("capture-a", "verify", 1)
    verify._record_case(
        bundle_id=bundle_a, case_id="unit-suite", attempt=1,
        case=manifest.document["cases"][0], status="passed", item_ids=[],
        started_at=now, ended_at=now,
    )
    item_a = verify._persist_capture_item(
        bundle_id=bundle_a, instance_id="capture-a", head_sha=head, case_id="unit-suite",
        attempt=1, kind="trace", payload=b'{"events": ["real trace for run A"]}',
        root=root_a, mime_type="application/json", started_at=now, ended_at=now,
    )
    verify._seal_bundle(
        bundle_a, final_state="done", reason=None, phase_b_eligible=True,
        required_case_ids=["unit-suite"],
    )
    stale_bytes = Path(_item_path(item_a["id"])).read_bytes()
    assert stale_bytes.startswith(verify._CAPTURE_MAGIC)

    # Bundle B: an unrelated, fresh verification run. An attacker (or a
    # buggy capture step) copies bundle A's stale, internally-consistent
    # trace bytes wholesale into bundle B's evidence directory.
    bundle_b = _bare_bundle(repo, head, tree, manifest, instance_id="capture-b")
    root_b = verify._evidence_root("capture-b", "verify", 1)
    verify._record_case(
        bundle_id=bundle_b, case_id="unit-suite", attempt=1,
        case=manifest.document["cases"][0], status="passed", item_ids=[],
        started_at=now, ended_at=now,
    )
    ident_b = verify._item_id(bundle_b, "unit-suite", 1, "trace")
    forged_path = root_b / "items" / f"{ident_b}.trace"
    written = verify._copy_once(forged_path, stale_bytes)
    digest = hashlib.sha256(written).hexdigest()
    with store._connect() as db:
        db.execute(
            "INSERT INTO evidence_items"
            "(id,bundle_id,case_id,kind,path,sha256,size_bytes,mime_type,producer,metadata_json)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (ident_b, bundle_b, "unit-suite", "trace", str(forged_path), digest, len(written),
             "application/json", "verification-runner", json.dumps({"redaction_state": "clean"})),
        )
    verify._seal_bundle(
        bundle_b, final_state="done", reason=None, phase_b_eligible=True,
        required_case_ids=["unit-suite"],
    )
    with pytest.raises(verify.CaptureContainerError, match="identity does not match"):
        verify.verify_evidence_bundle(bundle_b)


def test_evidence_item_bytes_replaced_after_hashing_is_rejected(tmp_path):
    repo, head, tree = _repo(tmp_path)
    manifest = verify.load_verification_manifest(repo, head)
    bundle = _run(repo, head, tree, manifest)
    assert bundle["state"] == "done"
    with store._connect() as db:
        item = dict(db.execute(
            "SELECT * FROM evidence_items WHERE bundle_id=?", (bundle["id"],),
        ).fetchone())
    # Real byte-level tamper: mutate the sealed file on disk after the
    # manifest's hash was computed and published.
    Path(item["path"]).write_bytes(b"[stdout]\nforged victory\n[stderr]\n")
    with pytest.raises(verify.EvidenceInvariantError, match="hash/size mismatch"):
        verify.verify_evidence_bundle(bundle["id"])


# ---------------------------------------------------------------------------
# §2.4.10 #3 -- app URL targets a stale prior session; runner identity
# cannot be forged by the app under test.
# ---------------------------------------------------------------------------

def test_environment_identity_in_evidence_is_runner_sourced_not_app_forgeable(tmp_path):
    trusted_identity = {
        "app_session_id": "trusted-session", "env_session_id": "trusted-env",
        "app_url": "http://127.0.0.1:19001", "port": 19001,
    }

    def lying_driver(case, workspace, env, timeout):
        now = store._now()
        # A compromised/buggy driver tries to smuggle a stale prior
        # session's identity back into what gets persisted.
        return {
            "classification": "passed", "stdout": b"ok", "stderr": b"",
            "exit_code": 0, "started_at": now, "ended_at": now,
            "environment_identity": {"app_url": "http://stale-prior-session.invalid"},
        }

    repo, head, tree = _repo(tmp_path)
    manifest = verify.load_verification_manifest(repo, head)
    bundle = _run(
        repo, head, tree, manifest, instance_id="stale-session",
        drivers={"command": lying_driver}, environment_identity=trusted_identity,
    )
    assert bundle["state"] == "done"
    with store._connect() as db:
        item = dict(db.execute(
            "SELECT * FROM evidence_items WHERE bundle_id=?", (bundle["id"],),
        ).fetchone())
    metadata = json.loads(item["metadata_json"])
    assert metadata["environment_identity"] == trusted_identity
    assert metadata["environment_identity"]["app_url"] != "http://stale-prior-session.invalid"


def test_runner_generated_env_identity_cannot_be_overridden_by_profile(tmp_path):
    repo, head, tree = _repo(tmp_path)
    manifest = verify.load_verification_manifest(repo, head)
    with pytest.raises(verify.VerificationManifestError, match="unsafe variable"):
        _run(
            repo, head, tree, manifest,
            profile=_profile(env={"SHIPFACTORY_INSTANCE_ID": "forged-instance"}),
        )


# ---------------------------------------------------------------------------
# §2.4.10 #4 -- command prints "125 passed" but exits nonzero.
# §2.4.10 #5 -- tests skip/deselect everything and exit zero.
# ---------------------------------------------------------------------------

def test_fabricated_pass_text_with_nonzero_exit_fails_closed(tmp_path):
    document = _manifest(
        argv=["python3", "-c", "print('125 passed in 0.4s'); raise SystemExit(1)"],
        oracle={"type": "pytest_summary", "min_passed": 1},
    )
    repo, head, tree = _repo(tmp_path, document)
    manifest = verify.load_verification_manifest(repo, head)
    bundle = _run(repo, head, tree, manifest)
    assert bundle["state"] == "blocked"
    assert bundle["invalid_reason"] == "test_failed"


def test_naive_output_contains_oracle_is_fooled_by_fabricated_text(tmp_path):
    """Documents why pytest_summary exists: a naive oracle is not fail-closed here."""
    document = _manifest(
        argv=["python3", "-c", "print('125 passed'); raise SystemExit(1)"],
        oracle={"type": "output_contains", "contains": "passed"},
    )
    repo, head, tree = _repo(tmp_path, document)
    manifest = verify.load_verification_manifest(repo, head)
    bundle = _run(repo, head, tree, manifest)
    # The manifest author's choice of oracle matters: output_contains alone
    # cannot see the exit code, so this specific (badly-authored) manifest
    # seals done. pytest_summary is the engine-provided, fail-closed answer.
    assert bundle["state"] == "done"


def test_deselected_everything_exits_zero_fails_closed_with_pytest_summary(tmp_path):
    document = _manifest(
        argv=["python3", "-c", "print('1 deselected in 0.01s')"],
        oracle={"type": "pytest_summary", "min_passed": 1},
    )
    repo, head, tree = _repo(tmp_path, document)
    manifest = verify.load_verification_manifest(repo, head)
    bundle = _run(repo, head, tree, manifest)
    assert bundle["state"] == "blocked"


def test_no_tests_ran_exits_zero_fails_closed_with_pytest_summary(tmp_path):
    document = _manifest(
        argv=["python3", "-c", "print('no tests ran in 0.00s')"],
        oracle={"type": "pytest_summary", "min_passed": 1},
    )
    repo, head, tree = _repo(tmp_path, document)
    manifest = verify.load_verification_manifest(repo, head)
    bundle = _run(repo, head, tree, manifest)
    assert bundle["state"] == "blocked"


def test_pytest_summary_requires_zero_failures_even_with_passes(tmp_path):
    document = _manifest(
        argv=["python3", "-c", "print('3 passed, 1 failed in 0.2s'); raise SystemExit(1)"],
        oracle={"type": "pytest_summary", "min_passed": 1},
    )
    repo, head, tree = _repo(tmp_path, document)
    manifest = verify.load_verification_manifest(repo, head)
    bundle = _run(repo, head, tree, manifest)
    assert bundle["state"] == "blocked"


def test_pytest_summary_honest_pass_seals_done(tmp_path):
    document = _manifest(
        argv=["python3", "-c", "print('4 passed in 0.1s')"],
        oracle={"type": "pytest_summary", "min_passed": 3},
    )
    repo, head, tree = _repo(tmp_path, document)
    manifest = verify.load_verification_manifest(repo, head)
    bundle = _run(repo, head, tree, manifest)
    assert bundle["state"] == "done"


# ---------------------------------------------------------------------------
# §2.4.10 #6/#7/#8 -- UI render without backend effect / state before-after
# reload / stale service-worker cache: real browser oracle evaluation is
# unimplemented in this codebase. The honest, fail-closed boundary is that
# the playwright driver NEVER fabricates a pass -- it is unconditionally an
# infrastructure error, so no UI-only claim can ever seal a bundle "done".
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("case_id,assertions", [
    # #6: UI route renders while the required backend side effect never occurs.
    ("ui-renders-without-backend-effect", [
        {"type": "visible", "selector": "#success-banner"},
        {"type": "api-status", "request": "/api/orders", "status": 201},
    ]),
    # #7: state appears correct before refresh and disappears after reload.
    ("state-before-and-after-reload", [
        {"type": "visible", "selector": "#saved-banner"},
        {"type": "visible", "selector": "#saved-banner-after-reload"},
    ]),
    # #8: a service worker / browser cache serves old assets.
    ("service-worker-serves-old-assets", [
        {"type": "api-status", "request": "/assets/app.js?fresh-hash", "status": 200},
    ]),
])
def test_playwright_backed_ui_claims_never_silently_pass(tmp_path, case_id, assertions):
    document = {
        "schema": verify.VERIFICATION_SCHEMA,
        "cases": [{
            "id": case_id, "requirement_ids": ["REQ-UI"], "driver": "playwright",
            "script": "e2e/reload.spec.ts", "assertions": assertions,
        }],
        "capture": {"video": False, "trace": False, "screenshots": "on-failure"},
    }
    repo, head, tree = _repo(tmp_path, document)
    (repo / "e2e").mkdir()
    (repo / "e2e" / "reload.spec.ts").write_text("// stub\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add spec"], cwd=repo, env=_GIT_ENV, check=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=repo, text=True).strip()
    manifest = verify.load_verification_manifest(repo, head)
    instance_id = f"pw-{case_id}"
    bundle = _run(repo, head, tree, manifest, instance_id=instance_id)
    assert bundle["state"] == "blocked"
    assert bundle["invalid_reason"] == "test_infrastructure_error"
    assert json.loads(
        (Path(store._db_path()).parent / "runs" / instance_id / "verify" / "1"
         / "evidence" / "bundle.json").read_text()
    )["phase_b_eligible"] is False


# ---------------------------------------------------------------------------
# §2.4.10 #9 -- candidate changes after verification but before review.
# §2.4.10 #18 -- model approves without opening evidence.
# §2.4.10 #17 -- reviewer and builder share a provider despite different seats.
# ---------------------------------------------------------------------------

def _seed_review_instance(conn, tmp_path, repo, head, tree, *, instance_id):
    """Seed just enough real state (recipe_instances row + real kanban tasks)
    for `_review_approval_blocker` to resolve workspace/seat identity through
    its real dependencies, without needing a full recipe/reconcile harness.
    """
    from hermes_cli import kanban_db
    with store._connect() as db:
        db.execute(
            "INSERT INTO recipe_instances(id,board,collector_task_id,recipe_id,recipe_version,"
            "recipe_hash,status,parameters_json,activation_count,tokens_charged,blocked_reason,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (instance_id, "test", "collector", "fake", 1, "hash", "running", "{}", 0, 0, None,
             store._now(), store._now()),
        )
    build_task_id = kanban_db.create_task(
        conn, title="build", body="build", assignee="dev-backend",
        workspace_kind="worktree", board="test", workspace_path=str(repo),
    )
    review_task_id = kanban_db.create_task(
        conn, title="review", body="review", assignee="qa", board="test",
    )
    definition = {
        "id": "review", "primitive": "review_gate", "needs": ["verify"],
        "inputs": [{"from": "build", "kind": "change-set", "required": False}],
        "params": {"seat": "qa"},
    }
    defs = {
        "build": {"id": "build", "primitive": "agent_task", "params": {"seat": "dev-backend"}},
        "verify": {"id": "verify", "primitive": "verification"},
        "review": definition,
    }
    latest = {
        "build": {"step_id": "build", "kanban_task_id": build_task_id},
        "verify": {"step_id": "verify", "kanban_task_id": None},
        "review": {"step_id": "review", "kanban_task_id": review_task_id},
    }
    return definition, defs, latest


def test_approval_without_citing_the_sealed_bundle_is_blocked(tmp_path, kanban_conn):
    repo, head, tree = _repo(tmp_path)
    manifest = verify.load_verification_manifest(repo, head)
    bundle = _run(repo, head, tree, manifest, instance_id="rvw-nocite", step_id="verify")
    assert bundle["state"] == "done"
    definition, defs, latest = _seed_review_instance(
        kanban_conn, tmp_path, repo, head, tree, instance_id="rvw-nocite",
    )
    with store._connect() as db:
        blocker = advancer._review_approval_blocker(
            db, "rvw-nocite", definition, verdict_body="APPROVE clean pass",
            defs=defs, conn=kanban_conn, latest=latest,
        )
    assert blocker == "evidence_not_cited"


def test_approval_citing_the_exact_sealed_bundle_is_allowed(tmp_path, kanban_conn):
    repo, head, tree = _repo(tmp_path)
    manifest = verify.load_verification_manifest(repo, head)
    bundle = _run(repo, head, tree, manifest, instance_id="rvw-cite", step_id="verify")
    assert bundle["state"] == "done"
    definition, defs, latest = _seed_review_instance(
        kanban_conn, tmp_path, repo, head, tree, instance_id="rvw-cite",
    )
    with store._connect() as db:
        blocker = advancer._review_approval_blocker(
            db, "rvw-cite", definition,
            verdict_body=f"APPROVE reviewed sealed bundle {bundle['bundle_sha256']}",
            defs=defs, conn=kanban_conn, latest=latest,
        )
    assert blocker is None


def test_approval_after_candidate_mutates_workspace_post_verification_is_blocked(tmp_path, kanban_conn):
    repo, head, tree = _repo(tmp_path)
    manifest = verify.load_verification_manifest(repo, head)
    bundle = _run(repo, head, tree, manifest, instance_id="rvw-mutate", step_id="verify")
    assert bundle["state"] == "done"
    definition, defs, latest = _seed_review_instance(
        kanban_conn, tmp_path, repo, head, tree, instance_id="rvw-mutate",
    )
    # Candidate silently edits the reviewed worktree after verification sealed
    # its bundle but before the review decision is applied.
    (repo / "tracked.txt").write_text("mutated after verification\n", encoding="utf-8")
    with store._connect() as db:
        blocker = advancer._review_approval_blocker(
            db, "rvw-mutate", definition,
            verdict_body=f"APPROVE reviewed sealed bundle {bundle['bundle_sha256']}",
            defs=defs, conn=kanban_conn, latest=latest,
        )
    assert blocker is not None and blocker.startswith("candidate_mutated_after_verification")


def test_approval_when_verification_never_passed_is_blocked(tmp_path, kanban_conn):
    document = _manifest(argv=["python3", "-c", "raise SystemExit(1)"])
    repo, head, tree = _repo(tmp_path, document)
    manifest = verify.load_verification_manifest(repo, head)
    bundle = _run(repo, head, tree, manifest, instance_id="rvw-failed", step_id="verify")
    assert bundle["state"] == "blocked"
    definition, defs, latest = _seed_review_instance(
        kanban_conn, tmp_path, repo, head, tree, instance_id="rvw-failed",
    )
    with store._connect() as db:
        blocker = advancer._review_approval_blocker(
            db, "rvw-failed", definition,
            verdict_body=f"APPROVE reviewed sealed bundle {bundle['bundle_sha256']}",
            defs=defs, conn=kanban_conn, latest=latest,
        )
    assert blocker == "verification_not_passed"


def test_reviewer_sharing_builder_provider_despite_distinct_seat_names():
    cfg = config.FactoryConfig(
        company="acme",
        seats={
            "dev-backend": config.Seat(
                name="dev-backend", profile="claude-default", executor="claude", model="opus",
            ),
            "qa": config.Seat(
                name="qa", profile="claude-default", executor="claude", model="opus",
            ),
            "architect": config.Seat(
                name="architect", profile="codex-default", executor="codex", model="gpt",
            ),
        },
        hierarchy_gates={},
    )
    assert config.reviewer_shares_builder_provider(cfg, "dev-backend", "qa") is True
    assert config.reviewer_shares_builder_provider(cfg, "dev-backend", "architect") is False
    assert config.reviewer_shares_builder_provider(cfg, "dev-backend", "dev-backend") is False


def test_review_approval_blocked_when_reviewer_and_builder_collude_on_provider(
    tmp_path, kanban_conn, monkeypatch,
):
    from hermes_cli import profiles as hermes_profiles
    monkeypatch.setattr(hermes_profiles, "profile_exists", lambda name: True)
    home = Path(os.environ["HERMES_HOME"]) / "shipfactory"
    home.mkdir(parents=True, exist_ok=True)
    (home / "seats.yaml").write_text(
        "company: acme\n"
        "seats:\n"
        "  dev-backend: {profile: shared-profile, executor: claude, model: opus, role: engineer}\n"
        "  qa: {profile: shared-profile, executor: claude, model: opus, role: qa}\n",
        encoding="utf-8",
    )
    repo, head, tree = _repo(tmp_path)
    manifest = verify.load_verification_manifest(repo, head)
    bundle = _run(repo, head, tree, manifest, instance_id="rvw-collude", step_id="verify")
    assert bundle["state"] == "done"
    definition, defs, latest = _seed_review_instance(
        kanban_conn, tmp_path, repo, head, tree, instance_id="rvw-collude",
    )
    with store._connect() as db:
        blocker = advancer._review_approval_blocker(
            db, "rvw-collude", definition,
            verdict_body=f"APPROVE reviewed sealed bundle {bundle['bundle_sha256']}",
            defs=defs, conn=kanban_conn, latest=latest,
        )
    assert blocker == "reviewer_shares_builder_provider"


# ---------------------------------------------------------------------------
# §2.4.10 #10 -- candidate alters/deletes the manifest to remove a protected case.
# ---------------------------------------------------------------------------

def test_candidate_cannot_delete_a_protected_case_by_editing_its_own_manifest(tmp_path):
    protected_doc = _manifest(argv=["python3", "-c", "raise SystemExit(1)"], case_id="must-fail")
    repo, base, _base_tree = _repo(tmp_path, protected_doc)
    import yaml
    # Candidate rewrites the manifest, deleting the (protected) failing case
    # and replacing it with one that trivially passes.
    (repo / verify.DEFAULT_MANIFEST_PATH).write_text(
        yaml.safe_dump(_manifest(case_id="always-passes"), sort_keys=False), encoding="utf-8",
    )
    head, tree = _commit(repo, "candidate deletes the protected case")
    result = _finish_action(_action_payload(repo, base, head, tree, instance="delete-protected"))
    assert result["status"] == "blocked"
    with store._connect() as db:
        bundle = dict(db.execute(
            "SELECT * FROM evidence_bundles WHERE id=?", (result["bundle_id"],),
        ).fetchone())
        cases = {
            row["case_id"]: row["status"] for row in db.execute(
                "SELECT case_id,status FROM verification_cases WHERE bundle_id=?",
                (result["bundle_id"],),
            ).fetchall()
        }
    # The protected (trusted-base) case still ran and still failed, even
    # though the candidate's own manifest no longer mentions it.
    assert cases["protected:must-fail"] == "failed"
    assert bundle["invalid_reason"] == "protected_baseline_test_failed"


def test_control_plane_touch_of_the_manifest_is_visible_in_the_sealed_bundle(tmp_path):
    repo, base, _base_tree = _repo(tmp_path)
    import yaml
    (repo / verify.DEFAULT_MANIFEST_PATH).write_text(
        yaml.safe_dump(_manifest(case_id="edited"), sort_keys=False), encoding="utf-8",
    )
    head, tree = _commit(repo, "touch the verification manifest")
    manifest = verify.load_verification_manifest(repo, head)
    bundle = verify.run_verification(
        instance_id="cp-touch", step_id="verify", activation=1,
        input_revision_hash="revision", base_sha=base, head_sha=head, tree_sha=tree,
        workspace=repo, manifest=manifest, profile=_profile(),
        protected_manifest=verify.load_verification_manifest(repo, base, verify_worktree_copy=False),
    )
    document = json.loads(
        (Path(store._db_path()).parent / "runs" / "cp-touch" / "verify" / "1"
         / "evidence" / "bundle.json").read_text()
    )
    assert document["control_plane_touched"] is True


def test_control_plane_not_touched_when_manifest_is_untouched(tmp_path):
    repo, base, base_tree = _repo(tmp_path)
    (repo / "unrelated.txt").write_text("change\n", encoding="utf-8")
    head, tree = _commit(repo, "unrelated change")
    manifest = verify.load_verification_manifest(repo, base)
    bundle = verify.run_verification(
        instance_id="cp-untouched", step_id="verify", activation=1,
        input_revision_hash="revision", base_sha=base, head_sha=head, tree_sha=tree,
        workspace=repo, manifest=manifest, profile=_profile(), run_candidate_cases=False,
        protected_manifest=verify.load_verification_manifest(repo, base, verify_worktree_copy=False),
    )
    document = json.loads(
        (Path(store._db_path()).parent / "runs" / "cp-untouched" / "verify" / "1"
         / "evidence" / "bundle.json").read_text()
    )
    assert document["control_plane_touched"] is False


# ---------------------------------------------------------------------------
# §2.4.10 #11 -- secret appears in screenshot, trace, or HAR.
# ---------------------------------------------------------------------------

def test_secret_in_trace_payload_is_redacted(tmp_path):
    repo, head, tree = _repo(tmp_path)
    manifest = verify.load_verification_manifest(repo, head)
    now = store._now()
    bundle_id = _bare_bundle(repo, head, tree, manifest, instance_id="trace-secret")
    root = verify._evidence_root("trace-secret", "verify", 1)
    verify._record_case(
        bundle_id=bundle_id, case_id="unit-suite", attempt=1,
        case=manifest.document["cases"][0], status="passed", item_ids=[],
        started_at=now, ended_at=now,
    )
    payload = json.dumps({"headers": {"authorization": "Bearer sk-supersecrettoken1234567890"}}).encode()
    item = verify._persist_capture_item(
        bundle_id=bundle_id, instance_id="trace-secret", head_sha=head, case_id="unit-suite",
        attempt=1, kind="trace", payload=payload, root=root, mime_type="application/json",
        started_at=now, ended_at=now,
    )
    assert item["redaction_state"] == "redacted"
    _header, sealed_payload = verify._parse_capture_container(
        Path(_item_path(item["id"])).read_bytes(),
    )
    assert b"supersecrettoken" not in sealed_payload
    assert b"[REDACTED]" in sealed_payload
    sealed = verify._seal_bundle(
        bundle_id, final_state="done", reason=None, phase_b_eligible=True,
        required_case_ids=["unit-suite"],
    )
    assert sealed["state"] == "done"
    verify.verify_evidence_bundle(bundle_id)  # container + redaction both hold up


def test_screenshot_capture_always_blocks_sealing_as_uncertain(tmp_path):
    repo, head, tree = _repo(tmp_path)
    manifest = verify.load_verification_manifest(repo, head)
    now = store._now()
    bundle_id = _bare_bundle(repo, head, tree, manifest, instance_id="screenshot-secret")
    root = verify._evidence_root("screenshot-secret", "verify", 1)
    verify._record_case(
        bundle_id=bundle_id, case_id="unit-suite", attempt=1,
        case=manifest.document["cases"][0], status="passed", item_ids=[],
        started_at=now, ended_at=now,
    )
    fake_png = b"\x89PNG\r\n\x1a\n" + b"pixels-that-might-contain-a-visible-api-key" * 4
    item = verify._persist_capture_item(
        bundle_id=bundle_id, instance_id="screenshot-secret", head_sha=head, case_id="unit-suite",
        attempt=1, kind="screenshot", payload=fake_png, root=root, mime_type="image/png",
        started_at=now, ended_at=now,
    )
    assert item["redaction_state"] == "uncertain"
    sealed = verify._seal_bundle(
        bundle_id, final_state="done", reason=None, phase_b_eligible=True,
        required_case_ids=["unit-suite"],
    )
    assert sealed["state"] == "blocked"
    assert "redaction is uncertain" in sealed["invalid_reason"]
    assert json.loads(
        (Path(store._db_path()).parent / "runs" / "screenshot-secret" / "verify" / "1"
         / "evidence" / "bundle.json").read_text()
    )["phase_b_eligible"] is False


def test_har_cookies_and_auth_headers_are_stripped(tmp_path):
    repo, head, tree = _repo(tmp_path)
    manifest = verify.load_verification_manifest(repo, head)
    now = store._now()
    bundle_id = _bare_bundle(repo, head, tree, manifest, instance_id="har-secret")
    root = verify._evidence_root("har-secret", "verify", 1)
    verify._record_case(
        bundle_id=bundle_id, case_id="unit-suite", attempt=1,
        case=manifest.document["cases"][0], status="passed", item_ids=[],
        started_at=now, ended_at=now,
    )
    har_payload = json.dumps({
        "log": {"entries": [{"request": {"headers": [
            {"name": "Cookie", "value": "session=abc123secret; csrftoken=xyz789"},
            {"name": "Authorization", "value": "Bearer sk-realtoken1234567890abcdef"},
            {"name": "Accept", "value": "application/json"},
        ]}}]},
    }).encode()
    item = verify._persist_capture_item(
        bundle_id=bundle_id, instance_id="har-secret", head_sha=head, case_id="unit-suite",
        attempt=1, kind="har", payload=har_payload, root=root, mime_type="application/json",
        started_at=now, ended_at=now,
    )
    assert item["redaction_state"] == "redacted"
    _header, sealed_payload = verify._parse_capture_container(
        Path(_item_path(item["id"])).read_bytes(),
    )
    assert b"abc123secret" not in sealed_payload
    assert b"sk-realtoken1234567890abcdef" not in sealed_payload
    assert b"[REDACTED]" in sealed_payload
    # A header that was never secret survives untouched -- redaction must be
    # targeted, not a blanket wipe of the evidence.
    assert b"application/json" in sealed_payload
    document = json.loads(sealed_payload)
    assert document["log"]["entries"]  # still valid, parseable JSON after redaction


def test_review_approval_blocker_reverifies_bundle_integrity_not_just_the_db_row(
    tmp_path, kanban_conn,
):
    """§2.4.8: review inputs are the sealed bundle itself, re-verified live --
    not a cached DB row trusted at face value."""
    repo, head, tree = _repo(tmp_path)
    manifest = verify.load_verification_manifest(repo, head)
    bundle = _run(repo, head, tree, manifest, instance_id="rvw-tampered", step_id="verify")
    assert bundle["state"] == "done"
    with store._connect() as db:
        item = dict(db.execute(
            "SELECT * FROM evidence_items WHERE bundle_id=?", (bundle["id"],),
        ).fetchone())
    # Tamper with the sealed evidence bytes after sealing, without touching
    # the evidence_bundles row itself.
    Path(item["path"]).write_bytes(b"[stdout]\nforged after seal\n[stderr]\n")
    definition, defs, latest = _seed_review_instance(
        kanban_conn, tmp_path, repo, head, tree, instance_id="rvw-tampered",
    )
    with store._connect() as db:
        blocker = advancer._review_approval_blocker(
            db, "rvw-tampered", definition,
            verdict_body=f"APPROVE reviewed sealed bundle {bundle['bundle_sha256']}",
            defs=defs, conn=kanban_conn, latest=latest,
        )
    assert blocker is not None and blocker.startswith("evidence_invariant:")


# ---------------------------------------------------------------------------
# §2.4.10 #12 -- ffmpeg hangs after tests finish.
# ---------------------------------------------------------------------------

def test_supervised_sidecar_that_ignores_sigterm_is_forcibly_reaped(tmp_path):
    home = Path(os.environ["HERMES_HOME"]) / "shipfactory" / "sidecar-home"
    home.mkdir(parents=True, exist_ok=True)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home)}
    script = (
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(60)\n"
    )
    proc, token = verify.run_supervised_sidecar(
        ["python3", "-c", script], cwd=tmp_path, env=env,
    )
    # Give the child a moment to install its SIGTERM handler for real.
    deadline = time.monotonic() + 5
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert proc.poll() is None  # still alive, ignoring signals as ffmpeg-that-hung would
    started = time.monotonic()
    exit_code = verify.stop_supervised_sidecar(proc, token, grace_seconds=0.5)
    elapsed = time.monotonic() - started
    assert elapsed < 10, "sidecar cleanup must not hang evidence collection"
    assert exit_code != 0
    try:
        os.kill(proc.pid, 0)
        alive = True
    except OSError:
        alive = False
    assert not alive, "SIGTERM-ignoring sidecar must be SIGKILLed, not left running"


# ---------------------------------------------------------------------------
# §2.4.10 #13 -- browser process exits while the child app remains.
# ---------------------------------------------------------------------------

def test_detached_grandchild_does_not_outlive_a_normally_exiting_case(tmp_path):
    marker = tmp_path / "grandchild-alive.marker"
    pid_file = tmp_path / "grandchild.pid"
    grandchild_script = tmp_path / "grandchild.py"
    grandchild_script.write_text(
        "import pathlib, sys, time\n"
        f"pathlib.Path({str(marker)!r}).write_text('1')\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    launcher_script = tmp_path / "launcher.py"
    launcher_script.write_text(
        "import pathlib, subprocess, sys, time\n"
        "grandchild = subprocess.Popen(\n"
        f"    [sys.executable, {str(grandchild_script)!r}],\n"
        "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n"
        ")\n"  # no pipes inherited -> the launcher's own communicate() can return promptly
        f"pathlib_pid_file = {str(pid_file)!r}\n"
        "open(pathlib_pid_file, 'w').write(str(grandchild.pid))\n"
        # Deterministic sync: wait for the grandchild's OWN readiness signal
        # (not a fixed sleep) before the launcher exits, so cleanup can only
        # ever race against an already-alive grandchild.
        f"deadline = time.monotonic() + 5\n"
        f"marker_path = pathlib.Path({str(marker)!r})\n"
        "while not marker_path.exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.02)\n"
        "print('launcher-exiting')\n",
        encoding="utf-8",
    )
    document = _manifest(argv=["python3", str(launcher_script)])
    repo, head, tree = _repo(tmp_path, document)
    manifest = verify.load_verification_manifest(repo, head)
    bundle = _run(repo, head, tree, manifest, instance_id="orphan-app")
    assert bundle["state"] == "done"

    deadline = time.monotonic() + 5
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert marker.exists(), "grandchild must have actually started for this test to be meaningful"
    grandchild_pid = int(pid_file.read_text())

    deadline = time.monotonic() + 5
    alive = True
    while time.monotonic() < deadline:
        try:
            os.kill(grandchild_pid, 0)
        except OSError:
            alive = False
            break
        # opportunistically reap our own child to avoid a zombie masking liveness
        try:
            os.waitpid(grandchild_pid, os.WNOHANG)
        except ChildProcessError:
            pass
        time.sleep(0.05)
    assert not alive, "the launcher exited, but its grandchild ('the app') was left running"


# ---------------------------------------------------------------------------
# §2.4.10 #14 -- truncated video has a valid container header.
# ---------------------------------------------------------------------------

def test_truncated_capture_with_a_valid_header_is_rejected(tmp_path):
    repo, head, tree = _repo(tmp_path)
    manifest = verify.load_verification_manifest(repo, head)
    now = store._now()
    bundle_id = _bare_bundle(repo, head, tree, manifest, instance_id="truncated-capture")
    root = verify._evidence_root("truncated-capture", "verify", 1)
    verify._record_case(
        bundle_id=bundle_id, case_id="unit-suite", attempt=1,
        case=manifest.document["cases"][0], status="passed", item_ids=[],
        started_at=now, ended_at=now,
    )
    full_payload = b'{"frames": "' + b"X" * 2000 + b'"}'
    container = verify.build_capture_container(
        "trace", full_payload, instance_id="truncated-capture", head_sha=head,
        bundle_id=bundle_id, case_id="unit-suite", attempt=1, captured_at=now,
    )
    # Simulate a capture pipeline that crashed mid-write: the header (written
    # first, declaring the full intended length) survived; only a prefix of
    # the actual payload made it to disk.
    truncated = container[: len(container) - 500]
    ident = verify._item_id(bundle_id, "unit-suite", 1, "trace")
    path = root / "items" / f"{ident}.trace"
    written = verify._copy_once(path, truncated)
    digest = hashlib.sha256(written).hexdigest()
    with store._connect() as db:
        db.execute(
            "INSERT INTO evidence_items"
            "(id,bundle_id,case_id,kind,path,sha256,size_bytes,mime_type,producer,metadata_json)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (ident, bundle_id, "unit-suite", "trace", str(path), digest, len(written),
             "application/json", "verification-runner", json.dumps({"redaction_state": "clean"})),
        )
    verify._seal_bundle(
        bundle_id, final_state="done", reason=None, phase_b_eligible=True,
        required_case_ids=["unit-suite"],
    )
    with pytest.raises(verify.CaptureContainerError, match="truncated or replaced"):
        verify.verify_evidence_bundle(bundle_id)


# ---------------------------------------------------------------------------
# §2.4.10 #15 -- evidence exceeds the disk budget.
# ---------------------------------------------------------------------------

def test_evidence_disk_budget_is_a_cumulative_bundle_wide_cap(tmp_path):
    document = {
        "schema": verify.VERIFICATION_SCHEMA,
        "cases": [
            {"id": "case-a", "requirement_ids": ["REQ-1"], "driver": "command",
             "argv": ["python3", "-c", "print('A' * 5000)"],
             "oracle": {"type": "exit_code", "equals": 0}},
            {"id": "case-b", "requirement_ids": ["REQ-1"], "driver": "command",
             "argv": ["python3", "-c", "print('B' * 5000)"],
             "oracle": {"type": "exit_code", "equals": 0}},
        ],
        "capture": {"video": False, "trace": False, "screenshots": "on-failure"},
    }
    repo, head, tree = _repo(tmp_path, document)
    manifest = verify.load_verification_manifest(repo, head)
    bundle = _run(
        repo, head, tree, manifest,
        profile=_profile(max_evidence_bytes=1000, max_log_bytes=1000),
    )
    assert bundle["state"] == "done"
    with store._connect() as db:
        items = [dict(row) for row in db.execute(
            "SELECT * FROM evidence_items WHERE bundle_id=?", (bundle["id"],),
        ).fetchall()]
    total = sum(item["size_bytes"] for item in items)
    assert total <= 1000, "the cap must bound the WHOLE bundle, not reset per case"
    assert any(json.loads(item["metadata_json"]).get("capped") for item in items)


# ---------------------------------------------------------------------------
# §2.4.10 #16 -- first attempt fails, second passes: history stays visible
# and the step is not autonomous-graduation eligible.
# ---------------------------------------------------------------------------

def test_cross_activation_failure_history_blocks_autonomous_graduation(tmp_path):
    document = _manifest(argv=["python3", "-c", "raise SystemExit(1)"])
    repo, head, tree = _repo(tmp_path, document)
    manifest = verify.load_verification_manifest(repo, head)
    first = _run(
        repo, head, tree, manifest, instance_id="cross-activation",
        step_id="verify", activation=1,
    )
    assert first["state"] == "blocked"

    import yaml
    (repo / verify.DEFAULT_MANIFEST_PATH).write_text(
        yaml.safe_dump(_manifest(), sort_keys=False), encoding="utf-8",
    )
    head2, tree2 = _commit(repo, "fix the failing case")
    manifest2 = verify.load_verification_manifest(repo, head2)
    second = verify.run_verification(
        instance_id="cross-activation", step_id="verify", activation=2,
        input_revision_hash="revision", base_sha=head2, head_sha=head2, tree_sha=tree2,
        workspace=repo, manifest=manifest2, profile=_profile(),
    )
    assert second["state"] == "done"
    document2 = json.loads(
        (Path(store._db_path()).parent / "runs" / "cross-activation" / "verify" / "2"
         / "evidence" / "bundle.json").read_text()
    )
    assert document2["phase_b_eligible"] is False
    assert document2["prior_activation_failures"] == [
        {"activation": 1, "state": "blocked", "phase_b_eligible": False},
    ]


def test_clean_first_activation_is_still_autonomous_eligible(tmp_path):
    repo, head, tree = _repo(tmp_path)
    manifest = verify.load_verification_manifest(repo, head)
    bundle = _run(repo, head, tree, manifest, instance_id="clean-history", activation=1)
    assert bundle["state"] == "done"
    document = json.loads(
        (Path(store._db_path()).parent / "runs" / "clean-history" / "verify" / "1"
         / "evidence" / "bundle.json").read_text()
    )
    assert document["phase_b_eligible"] is True
    assert document["prior_activation_failures"] == []


# ---------------------------------------------------------------------------
# §2.4.6 -- commit binding: dirty tree before/during run, HEAD/tree drift.
# ---------------------------------------------------------------------------

def test_dirty_tree_before_run_is_rejected(tmp_path):
    repo, head, tree = _repo(tmp_path)
    manifest = verify.load_verification_manifest(repo, head)
    (repo / "tracked.txt").write_text("dirtied before the run even starts\n", encoding="utf-8")
    bundle = _run(repo, head, tree, manifest, instance_id="dirty-before")
    assert bundle["state"] == "failed"
    assert "not clean" in bundle["invalid_reason"]


def test_dirty_tree_created_mid_run_across_two_cases_is_rejected(tmp_path):
    document = {
        "schema": verify.VERIFICATION_SCHEMA,
        "cases": [
            {"id": "dirties-tree", "requirement_ids": ["REQ-1"], "driver": "command",
             "argv": ["python3", "-c", "open('tracked.txt', 'w').write('mutated mid-run\\n')"],
             "oracle": {"type": "exit_code", "equals": 0}},
            {"id": "runs-after", "requirement_ids": ["REQ-1"], "driver": "command",
             "argv": ["python3", "-c", "print('ok')"],
             "oracle": {"type": "exit_code", "equals": 0}},
        ],
        "capture": {"video": False, "trace": False, "screenshots": "on-failure"},
    }
    repo, head, tree = _repo(tmp_path, document)
    manifest = verify.load_verification_manifest(repo, head)
    bundle = _run(repo, head, tree, manifest, instance_id="dirty-mid-run")
    assert bundle["state"] == "failed"
    assert "post-test mutation" in bundle["invalid_reason"]


def test_head_advances_mid_run_via_a_real_commit_is_rejected(tmp_path):
    def committing_driver(case, workspace, env, timeout):
        (workspace / "new-file.txt").write_text("sneaky commit\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=workspace, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "mid-run commit"], cwd=workspace, env=_GIT_ENV, check=True,
        )
        now = store._now()
        return {"classification": "passed", "stdout": b"ok", "stderr": b"",
                "exit_code": 0, "started_at": now, "ended_at": now}

    repo, head, tree = _repo(tmp_path)
    manifest = verify.load_verification_manifest(repo, head)
    bundle = _run(
        repo, head, tree, manifest, instance_id="head-drift",
        drivers={"command": committing_driver},
    )
    assert bundle["state"] == "failed"
    assert "post-test mutation" in bundle["invalid_reason"]
    assert "head_sha mismatch" in bundle["invalid_reason"]


# ---------------------------------------------------------------------------
# §2.4.7 -- deterministic surface selection.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path,expected", [
    ("dashboard/components/Button.tsx", "browser"),
    ("frontend/src/App.jsx", "browser"),
    ("shipfactory/api/routes.py", "api"),
    ("server/user_api.py", "api"),
    ("db/migrations/0007_add_column.sql", "migration"),
    ("scripts/migrate/backfill.py", "migration"),
    ("docs/README.md", "stricter"),
])
def test_deterministic_path_surface_classification(path, expected):
    assert verify.classify_path_surface(path) == expected


def test_model_risk_may_raise_the_required_surface_but_never_lower_it():
    assert verify.classify_required_surface(
        ["shipfactory/api/routes.py"], model_risk_surface="browser",
    ) == "browser"
    # A model claiming a lower-risk surface than the deterministic floor
    # cannot lower the requirement.
    assert verify.classify_required_surface(
        ["dashboard/components/Button.tsx"], model_risk_surface="api",
    ) == "browser"
    assert verify.classify_required_surface([], model_risk_surface="api") == "stricter"


def test_profile_below_the_deterministic_surface_floor_fails_closed(tmp_path):
    repo, base, base_tree = _repo(tmp_path)
    (repo / "dashboard").mkdir()
    (repo / "dashboard" / "widget.tsx").write_text("export const x = 1;\n", encoding="utf-8")
    head, tree = _commit(repo, "touch a UI path")
    manifest = verify.load_verification_manifest(repo, base, verify_worktree_copy=False)
    bundle = verify.run_verification(
        instance_id="surface-floor", step_id="verify", activation=1,
        input_revision_hash="revision", base_sha=base, head_sha=head, tree_sha=tree,
        workspace=repo, manifest=manifest, profile=_profile(surface="api"),
        run_candidate_cases=False,
        protected_manifest=verify.load_verification_manifest(repo, base, verify_worktree_copy=False),
    )
    assert bundle["state"] == "failed"
    assert "below the deterministic floor" in bundle["invalid_reason"]


def test_profile_without_a_declared_surface_is_unaffected_by_the_floor(tmp_path):
    """Opt-in: profiles that never declared `surface` keep their prior behavior."""
    repo, base, base_tree = _repo(tmp_path)
    (repo / "dashboard").mkdir()
    (repo / "dashboard" / "widget.tsx").write_text("export const x = 1;\n", encoding="utf-8")
    head, tree = _commit(repo, "touch a UI path")
    manifest = verify.load_verification_manifest(repo, base, verify_worktree_copy=False)
    bundle = verify.run_verification(
        instance_id="surface-optional", step_id="verify", activation=1,
        input_revision_hash="revision", base_sha=base, head_sha=head, tree_sha=tree,
        workspace=repo, manifest=manifest, profile=_profile(), run_candidate_cases=False,
        protected_manifest=verify.load_verification_manifest(repo, base, verify_worktree_copy=False),
    )
    assert bundle["state"] == "done"


# ---------------------------------------------------------------------------
# §2.4.9 -- evidence is never committed to the public repo.
# ---------------------------------------------------------------------------

def test_evidence_root_never_resolves_inside_the_candidate_repository(tmp_path):
    repo, head, tree = _repo(tmp_path)
    manifest = verify.load_verification_manifest(repo, head)
    bundle = _run(repo, head, tree, manifest, instance_id="not-in-repo")
    with store._connect() as db:
        item = dict(db.execute(
            "SELECT * FROM evidence_items WHERE bundle_id=?", (bundle["id"],),
        ).fetchone())
    item_path = Path(item["path"]).resolve()
    repo_resolved = repo.resolve()
    assert repo_resolved not in item_path.parents
    assert item_path != repo_resolved
