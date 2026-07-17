"""Independent adversarial corpus attacking the merged verification engine.

Each test constructs the real attack named in the external program review
§2.4.6-§2.4.10 through the production action, case-loop, browser, review-task,
or evidence-verification boundary. Host-only browser/loopback attacks skip
when the host cannot provide that infrastructure; missing infrastructure is
also separately asserted to block the production run.
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
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
        "browser_slots": 1, "surface": "stricter",
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


def _capture_driver(tmp_path: Path, factory):
    """Return a real case-loop driver that emits runner-format capture files."""
    def driver(case, workspace, env, timeout):
        started = store._now()
        capture = factory(case, env, started)
        path = tmp_path / f"{env['SHIPFACTORY_EVIDENCE_BUNDLE_ID']}-{case['id']}.sfev"
        path.write_bytes(capture["bytes"])
        return {
            "classification": "passed", "stdout": b"", "stderr": b"", "exit_code": 0,
            "started_at": started, "ended_at": store._now(),
            "capture_containers": [{
                "kind": capture["kind"], "path": str(path),
                "mime_type": capture.get("mime_type", "application/json"),
            }],
        }
    return driver


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
        recipe_activation=1,
    )
    store.record_run_end(run_id, 0, None, None, 0.1, "done")

    payload = _action_payload(repo_a, head, head, tree, instance="wrong-worktree")
    payload["workspace"] = str(repo_b)  # scheduled against the wrong (decoy) worktree
    payload["workspace_owner_task_id"] = owner_task_id
    payload["workspace_owner_activation"] = 1
    payload["workspace_owner_run_id"] = run_id
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
    run_id = store.record_run_start(
        owner_task_id, "build", "codex", "model", workspace_path=repo,
        recipe_activation=1,
    )
    store.record_run_end(run_id, 0, None, None, 0.1, "done")
    payload = _action_payload(repo, head, head, tree, instance="right-worktree")
    payload["workspace_owner_task_id"] = owner_task_id
    payload["workspace_owner_activation"] = 1
    payload["workspace_owner_run_id"] = run_id
    result = _finish_action(payload)
    assert result["status"] == "done"


@pytest.mark.parametrize("attack", ["missing", "older_activation", "foreign_task"])
def test_workspace_owner_requires_exact_task_activation_and_run(tmp_path, attack):
    repo, head, tree = _repo(tmp_path)
    owner_task_id = "exact-owner"
    if attack == "missing":
        run_id = 999_999
    else:
        run_id = store.record_run_start(
            "foreign-owner" if attack == "foreign_task" else owner_task_id,
            "build", "codex", "model", workspace_path=repo,
            recipe_activation=0 if attack == "older_activation" else 1,
        )
        store.record_run_end(run_id, 0, None, None, 0.1, "done")
    payload = _action_payload(repo, head, head, tree, instance=f"owner-{attack}")
    payload.update({
        "workspace_owner_task_id": owner_task_id,
        "workspace_owner_activation": 1,
        "workspace_owner_run_id": run_id,
    })
    result = verify.run_action(payload)
    assert result["status"] == "failed"
    with store._connect() as db:
        reason = db.execute(
            "SELECT invalid_reason FROM evidence_bundles WHERE id=?", (result["bundle_id"],),
        ).fetchone()["invalid_reason"]
    assert "exact workspace producer run is missing" in reason


# ---------------------------------------------------------------------------
# §2.4.10 #2 -- old video/trace copied into the new evidence directory.
# §2.4.10 #19 -- manifest references an item whose bytes changed after hashing.
# ---------------------------------------------------------------------------

def test_stale_capture_copied_into_a_fresh_bundle_is_rejected(tmp_path):
    repo, head, tree = _repo(tmp_path)
    manifest = verify.load_verification_manifest(repo, head)
    def source(case, env, started):
        return {"kind": "trace", "bytes": verify.build_capture_container(
            "trace", b'{"events":["real trace for run A"]}',
            instance_id=env["SHIPFACTORY_INSTANCE_ID"],
            head_sha=env["SHIPFACTORY_HEAD_SHA"],
            bundle_id=env["SHIPFACTORY_EVIDENCE_BUNDLE_ID"], case_id=case["id"],
            attempt=int(env["SHIPFACTORY_CASE_ATTEMPT"]), captured_at=started,
            redaction_state="clean",
        )}
    first = _run(
        repo, head, tree, manifest, instance_id="capture-a",
        drivers={"command": _capture_driver(tmp_path, source)},
    )
    assert first["state"] == "done"
    with store._connect() as db:
        item = db.execute(
            "SELECT path FROM evidence_items WHERE bundle_id=? AND kind='trace'", (first["id"],),
        ).fetchone()
    stale_bytes = Path(item["path"]).read_bytes()

    second = _run(
        repo, head, tree, manifest, instance_id="capture-b",
        drivers={"command": _capture_driver(
            tmp_path, lambda case, env, started: {"kind": "trace", "bytes": stale_bytes},
        )},
    )
    assert second["state"] == "failed"
    assert "capture container identity does not match" in second["invalid_reason"]


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


@pytest.mark.parametrize("field,value", [
    ("instance_id", "drifted-instance"), ("step_id", "drifted-step"),
    ("activation", 99), ("input_revision_hash", "drifted-revision"),
    ("base_sha", "0" * 40), ("head_sha", "1" * 40), ("tree_sha", "2" * 40),
    ("manifest_relpath", ".shipfactory/other.yaml"), ("manifest_blob_sha", "3" * 40),
    ("environment_session_id", "foreign-env"),
    ("environment_identity_json", '{"app_session_id":"foreign"}'),
    ("workspace_path", "/tmp/foreign-workspace"),
    ("workspace_owner_task_id", "foreign-task"), ("workspace_owner_activation", 7),
    ("workspace_owner_run_id", 700), ("required_surface", "api"),
    ("redaction_state", "redacted"), ("phase_b_eligible", 0),
    ("state", "blocked"), ("invalid_reason", "forged reason"),
])
def test_every_sealed_bundle_security_field_rejects_db_drift(tmp_path, field, value):
    repo, head, tree = _repo(tmp_path)
    manifest = verify.load_verification_manifest(repo, head)
    bundle = _run(repo, head, tree, manifest, instance_id=f"drift-{field}")
    assert bundle["state"] == "done"
    with store._connect() as db:
        db.execute(f"UPDATE evidence_bundles SET {field}=? WHERE id=?", (value, bundle["id"]))
    with pytest.raises(verify.EvidenceInvariantError):
        verify.verify_evidence_bundle(bundle["id"])


def test_sealed_item_manifest_rejects_legitimate_db_rehash_or_metadata_drift(tmp_path):
    repo, head, tree = _repo(tmp_path)
    manifest = verify.load_verification_manifest(repo, head)
    bundle = _run(repo, head, tree, manifest, instance_id="item-manifest-drift")
    with store._connect() as db:
        db.execute(
            "UPDATE evidence_items SET producer='foreign-runner' WHERE bundle_id=?",
            (bundle["id"],),
        )
    with pytest.raises(verify.EvidenceInvariantError, match="item DB fields drifted"):
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


@pytest.mark.parametrize("stale_field", ["base_sha", "candidate_sha", "workspace"])
def test_run_action_rejects_real_healthy_app_from_stale_environment(tmp_path, stale_field):
    from shipfactory import spawn

    repo, head, tree = _repo(tmp_path)
    payload = _action_payload(
        repo, head, head, tree, instance=f"stale-app-{stale_field}",
        environment="app", environment_config={"healthcheck_timeout_seconds": 1},
    )
    app_key = (
        f"verification/{payload['instance_id']}/{payload['step_id']}/{payload['activation']}/"
        f"{hashlib.sha256((head + '|' + head + '|' + str(repo.resolve())).encode()).hexdigest()[:20]}"
    )
    stale_workspace = tmp_path / "foreign-workspace"
    stale_workspace.mkdir()
    env_values = {
        "base_sha": "0" * 40 if stale_field == "base_sha" else head,
        "candidate_sha": "1" * 40 if stale_field == "candidate_sha" else head,
        "workspace_path": str(stale_workspace if stale_field == "workspace" else repo),
    }
    env_id = f"env-{stale_field}"
    store.insert_env_session(
        env_id, key=f"key-{stale_field}", manifest_path=".shipfactory/runtime.yaml",
        manifest_blob_sha="2" * 40, tracked_input_hash="3" * 64,
        control_plane_risk=False, control_plane_paths=[], lease_key=None,
        stdout_path=None, stderr_path=None, **env_values,
    )
    store.update_env_session_state(env_id, "ready")
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True,
    )
    token = spawn._capture_start_token(proc.pid, proc)
    app_id = f"app-{stale_field}"
    store.insert_app_session(
        app_id, env_session_id=env_id, request_key=app_key,
        workspace_path=str(repo), stdout_path=None, stderr_path=None,
    )
    store.mark_app_session_bound(
        app_id, port=9, port_lease_key=f"port-{stale_field}", app_url="http://127.0.0.1:9",
    )
    store.mark_app_session_pid(app_id, proc.pid)
    store.mark_app_session_token(app_id, token)
    store.update_app_session_state(app_id, "healthy", health_status="200")
    try:
        result = verify.run_action(payload)
    finally:
        spawn.verified_killpg(proc.pid, token, signal.SIGKILL)
        proc.wait(timeout=5)
    assert result["status"] == "blocked"
    with store._connect() as db:
        bundle = db.execute(
            "SELECT invalid_reason FROM evidence_bundles WHERE id=?", (result["bundle_id"],),
        ).fetchone()
    assert "environment_identity_mismatch" in bundle["invalid_reason"]
    assert "environment identity is stale" in bundle["invalid_reason"]


def test_run_action_rejects_live_app_reporting_stale_instance_and_head(tmp_path):
    from shipfactory import spawn

    repo, head, tree = _repo(tmp_path)
    server = tmp_path / "identity_server.py"
    port_file = tmp_path / "identity.port"
    server.write_text(
        "import http.server, json, pathlib, sys\n"
        "class H(http.server.BaseHTTPRequestHandler):\n"
        " def do_GET(self):\n"
        "  body=json.dumps({'instance_id':'prior-instance','head_sha':'0'*40}).encode()\n"
        "  self.send_response(200); self.send_header('Content-Length',str(len(body)))\n"
        "  self.end_headers(); self.wfile.write(body)\n"
        " def log_message(self,*args): pass\n"
        "s=http.server.ThreadingHTTPServer(('127.0.0.1',0),H)\n"
        "pathlib.Path(sys.argv[1]).write_text(str(s.server_port))\n"
        "s.serve_forever()\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [sys.executable, str(server), str(port_file)], start_new_session=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 5
    while not port_file.exists() and proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    if not port_file.exists():
        _out, err = proc.communicate(timeout=5)
        pytest.skip(f"host loopback unavailable: {err.decode(errors='replace').strip()}")
    token = spawn._capture_start_token(proc.pid, proc)
    port = int(port_file.read_text())
    payload = _action_payload(
        repo, head, head, tree, instance="current-instance",
        environment="app", environment_config={"healthcheck_timeout_seconds": 1},
    )
    app_key = (
        f"verification/current-instance/verify/1/"
        f"{hashlib.sha256((head + '|' + head + '|' + str(repo.resolve())).encode()).hexdigest()[:20]}"
    )
    store.insert_env_session(
        "live-env", key="live-key", base_sha=head, candidate_sha=head,
        manifest_path=".shipfactory/runtime.yaml", manifest_blob_sha="2" * 40,
        tracked_input_hash="3" * 64, workspace_path=str(repo), control_plane_risk=False,
        control_plane_paths=[], lease_key=None, stdout_path=None, stderr_path=None,
    )
    store.update_env_session_state("live-env", "ready")
    store.insert_app_session(
        "live-app", env_session_id="live-env", request_key=app_key,
        workspace_path=str(repo), expected_instance_id="current-instance",
        expected_head_sha=head, stdout_path=None, stderr_path=None,
    )
    store.mark_app_session_bound(
        "live-app", port=port, port_lease_key="identity-port",
        app_url=f"http://127.0.0.1:{port}",
    )
    store.mark_app_session_pid("live-app", proc.pid)
    store.mark_app_session_token("live-app", token)
    store.update_app_session_state("live-app", "healthy", health_status="200")
    try:
        result = verify.run_action(payload)
    finally:
        spawn.verified_killpg(proc.pid, token, signal.SIGKILL)
        proc.wait(timeout=5)
    assert result["status"] == "blocked"
    with store._connect() as db:
        reason = db.execute(
            "SELECT invalid_reason FROM evidence_bundles WHERE id=?", (result["bundle_id"],),
        ).fetchone()["invalid_reason"]
    assert "live instance/head identity is stale" in reason


# ---------------------------------------------------------------------------
# §2.4.10 #4 -- command prints "125 passed" but exits nonzero.
# §2.4.10 #5 -- tests skip/deselect everything and exit zero.
# ---------------------------------------------------------------------------

def test_fabricated_pass_text_and_exit_zero_without_real_pytest_evidence_fails_closed(tmp_path):
    fake_pytest = tmp_path / "pytest"
    fake_pytest.write_text(
        "#!/usr/bin/env python3\nprint('125 passed in 0.01s')\n",
        encoding="utf-8",
    )
    fake_pytest.chmod(0o755)
    document = _manifest(
        argv=[str(fake_pytest)],
        oracle={"type": "pytest_summary", "min_passed": 1},
    )
    repo, head, tree = _repo(tmp_path, document)
    manifest = verify.load_verification_manifest(repo, head)
    bundle = _run(repo, head, tree, manifest)
    assert bundle["state"] == "blocked"
    assert bundle["invalid_reason"] == "test_failed"


def test_candidate_owned_relative_python_cannot_forge_pytest_evidence_via_run_action(tmp_path):
    repo, base, _base_tree = _repo(tmp_path)
    fake_python = repo / "python3"
    fake_python.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib\n"
        "pathlib.Path(os.environ['SHIPFACTORY_PYTEST_EVIDENCE_PATH']).write_text(\n"
        "    json.dumps({'schema': 'shipfactory.pytest-evidence/v1', "
        "'nonce': os.environ['SHIPFACTORY_PYTEST_EVIDENCE_NONCE'], "
        "'exitstatus': 0, 'collected': 999, 'deselected': 0, "
        "'passed': 999, 'failed': 0, 'errors': 0, 'skipped': 0})\n"
        ")\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    import yaml
    (repo / ".shipfactory" / "verification.yaml").write_text(
        yaml.safe_dump(_manifest(
            argv=["./python3", "-m", "pytest", "-q"],
            oracle={"type": "pytest_summary", "min_passed": 1},
        ), sort_keys=False),
        encoding="utf-8",
    )
    head, tree = _commit(repo, "candidate-owned pytest interpreter")

    result = _finish_action(_action_payload(
        repo, base, head, tree, instance="relative-pytest-interpreter",
    ))

    assert result["status"] == "blocked"
    with store._connect() as db:
        reason = db.execute(
            "SELECT invalid_reason FROM evidence_bundles WHERE id=?", (result["bundle_id"],),
        ).fetchone()["invalid_reason"]
    assert reason == "test_failed"


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
        argv=[sys.executable, "-m", "pytest", "tests/test_real.py", "-q", "-k", "does-not-match"],
        oracle={"type": "pytest_summary", "min_passed": 1},
    )
    repo, head, tree = _repo(tmp_path, document)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_real.py").write_text("def test_real(): assert True\n")
    head, tree = _commit(repo, "add a real deselected test")
    manifest = verify.load_verification_manifest(repo, head)
    bundle = _run(repo, head, tree, manifest)
    assert bundle["state"] == "blocked"


def test_no_tests_ran_exits_zero_fails_closed_with_pytest_summary(tmp_path):
    document = _manifest(
        argv=[sys.executable, "-m", "pytest", "empty-tests", "-q"],
        oracle={"type": "pytest_summary", "min_passed": 1},
    )
    repo, head, tree = _repo(tmp_path, document)
    (repo / "empty-tests").mkdir()
    (repo / "empty-tests" / ".keep").write_text("")
    head, tree = _commit(repo, "add empty test directory")
    manifest = verify.load_verification_manifest(repo, head)
    bundle = _run(repo, head, tree, manifest)
    assert bundle["state"] == "blocked"


def test_pytest_summary_requires_zero_failures_even_with_passes(tmp_path):
    document = _manifest(
        argv=[sys.executable, "-m", "pytest", "tests/test_real.py", "-q"],
        oracle={"type": "pytest_summary", "min_passed": 1},
    )
    repo, head, tree = _repo(tmp_path, document)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_real.py").write_text(
        "def test_pass(): assert True\ndef test_fail(): assert False\n"
    )
    head, tree = _commit(repo, "add real pass and failure")
    manifest = verify.load_verification_manifest(repo, head)
    bundle = _run(repo, head, tree, manifest)
    assert bundle["state"] == "blocked"


def test_pytest_summary_honest_pass_seals_done(tmp_path):
    document = _manifest(
        argv=[sys.executable, "-m", "pytest", "tests/test_real.py", "-q"],
        oracle={"type": "pytest_summary", "min_passed": 3},
    )
    repo, head, tree = _repo(tmp_path, document)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_real.py").write_text(
        "\n".join(f"def test_{index}(): assert True" for index in range(4)) + "\n"
    )
    head, tree = _commit(repo, "add real passing pytest cases")
    manifest = verify.load_verification_manifest(repo, head)
    bundle = _run(repo, head, tree, manifest)
    assert bundle["state"] == "done"


# ---------------------------------------------------------------------------
# §2.4.10 #6/#7/#8 -- real backend, reload, and stale-cache attacks through
# the production browser subprocess.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scenario,case_id,assertions", [
    ("backend", "ui-renders-without-backend-effect", [
        {"type": "visible", "selector": "#success-banner"},
        {"type": "api-status", "request": "/api/orders", "status": 201},
    ]),
    ("reload", "state-before-and-after-reload", [
        {"type": "visible", "selector": "#saved-banner"},
    ]),
    ("cache", "stale-cache-serves-old-assets", [
        {"type": "visible", "selector": "#fresh-version"},
    ]),
])
def test_real_browser_catches_backend_reload_and_cache_attacks(
    tmp_path, scenario, case_id, assertions,
):
    server = tmp_path / f"fixture-{scenario}.py"
    port_file = tmp_path / f"fixture-{scenario}.port"
    server.write_text(
        "import http.server, pathlib, sys\n"
        f"scenario={scenario!r}; count=0\n"
        "class H(http.server.BaseHTTPRequestHandler):\n"
        " def do_GET(self):\n"
        "  global count; count += 1\n"
        "  if scenario=='backend' and self.path.startswith('/api/orders'):\n"
        "   self.send_response(500); self.end_headers(); return\n"
        "  if scenario=='reload': body=(b'<div id=\"saved-banner\">saved</div>' if count==1 else b'<div>lost</div>')\n"
        "  elif scenario=='cache': body=(b'<div id=\"fresh-version\">fresh</div>' if count==1 else b'<div id=\"stale-version\">stale</div>')\n"
        "  else: body=b'<div id=\"success-banner\">rendered</div>'\n"
        "  self.send_response(200); self.send_header('Content-Type','text/html')\n"
        "  self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(body)))\n"
        "  self.end_headers(); self.wfile.write(body)\n"
        " def log_message(self,*args): pass\n"
        "s=http.server.ThreadingHTTPServer(('127.0.0.1',0),H)\n"
        "pathlib.Path(sys.argv[1]).write_text(str(s.server_port)); s.serve_forever()\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [sys.executable, str(server), str(port_file)], start_new_session=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 5
    while not port_file.exists() and proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    if not port_file.exists():
        _out, err = proc.communicate(timeout=5)
        pytest.skip(f"host loopback unavailable: {err.decode(errors='replace').strip()}")
    from shipfactory import spawn
    token = spawn._capture_start_token(proc.pid, proc)
    app_url = f"http://127.0.0.1:{int(port_file.read_text())}"
    document = {
        "schema": verify.VERIFICATION_SCHEMA,
        "cases": [{
            "id": case_id, "requirement_ids": ["REQ-UI"], "driver": "playwright",
            "script": "e2e/reload.spec.ts", "assertions": assertions,
        }],
        "capture": {"video": False, "trace": False, "screenshots": "never"},
    }
    repo, head, tree = _repo(tmp_path, document)
    (repo / "e2e").mkdir()
    (repo / "e2e" / "reload.spec.ts").write_text("// declarative browser case\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add spec"], cwd=repo, env=_GIT_ENV, check=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=repo, text=True).strip()
    manifest = verify.load_verification_manifest(repo, head)
    instance_id = f"pw-{case_id}"
    try:
        bundle = _run(
            repo, head, tree, manifest, instance_id=instance_id,
            environment_identity={"app_url": app_url},
            # A real Chromium cold start can exceed the generic ten-second
            # unit-driver budget after the process-heavy full suite.  Keep the
            # assertion fail-closed, but give this real-browser control enough
            # time to reach the intended backend/reload/cache oracle instead
            # of misclassifying host load as the attack outcome (finding #53).
            profile=_profile(surface="browser", max_runtime_seconds=30),
        )
    finally:
        spawn.verified_killpg(proc.pid, token, signal.SIGKILL)
        proc.wait(timeout=5)
    assert bundle["state"] == "blocked"
    assert bundle["invalid_reason"] == "test_failed"
    assert json.loads(
        (Path(store._db_path()).parent / "runs" / instance_id / "verify" / "1"
         / "evidence" / "bundle.json").read_text()
    )["phase_b_eligible"] is False


def test_missing_required_browser_infrastructure_fails_closed(tmp_path):
    document = {
        "schema": verify.VERIFICATION_SCHEMA,
        "cases": [{
            "id": "browser-required", "requirement_ids": ["REQ-UI"], "driver": "playwright",
            "script": "e2e/required.spec.ts",
            "assertions": [{"type": "visible", "selector": "#ok"}],
        }],
        "capture": {"video": False, "trace": False, "screenshots": "never"},
    }
    repo, head, tree = _repo(tmp_path, document)
    manifest = verify.load_verification_manifest(repo, head)
    bundle = _run(repo, head, tree, manifest, instance_id="browser-missing")
    assert bundle["state"] == "blocked"
    assert bundle["invalid_reason"] == "test_infrastructure_error"


# ---------------------------------------------------------------------------
# §2.4.10 #9 -- candidate changes after verification but before review.
# §2.4.10 #18 -- model approves without opening evidence.
# §2.4.10 #17 -- reviewer and builder share a provider despite different seats.
# ---------------------------------------------------------------------------

def _seed_review_instance(
    conn, tmp_path, repo, head, tree, *, instance_id, bind_review_inputs=True,
    reviewer_executor="claude", reviewer_provider=None, reviewer_activation=1,
    create_reviewer_run=True, reviewer_result="done", reviewer_exit_code=0,
    review_input_kind="change-set", include_verification=True, verdict_contract=None,
):
    """Activate a real v2 review task with Factory-opened transitive evidence."""
    from hermes_cli import kanban_db
    from shipfactory.recipes import primitives

    store.init_db()
    seats = Path(os.environ["HERMES_HOME"]) / "shipfactory" / "seats.yaml"
    seats.parent.mkdir(parents=True, exist_ok=True)
    seats.write_text(
        "company: test\nseats:\n"
        "  dev-backend: {profile: default, executor: codex, model: gpt, role: engineer}\n"
        "  qa: {profile: default, executor: claude, model: sonnet, role: qa}\n",
        encoding="utf-8",
    )
    definition = {
        "id": "review", "title": "review", "primitive": "review_gate",
        "needs": ["verify"] if include_verification else ["build"],
        "inputs": [{"from": "build", "kind": review_input_kind, "required": False}],
        "params": {"seat": "qa", "workspace": "dir", "instructions": "review exact inputs"},
    }
    steps = [
        {"id": "build", "title": "build", "primitive": "agent_task", "needs": [],
         "inputs": [], "params": {"seat": "dev-backend", "workspace": "worktree"}},
        *([{"id": "verify", "title": "verify", "primitive": "verification", "needs": ["build"],
            "inputs": [], "params": {}}] if include_verification else []),
        definition,
    ]
    recipe = {"schema": "shipfactory.recipe/v2", "id": "fake", "version": 1, "steps": steps}
    if verdict_contract is not None:
        recipe["verdict_contract"] = verdict_contract
    normalized_recipe = json.dumps(
        recipe, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    recipe_hash = hashlib.sha256(normalized_recipe.encode("utf-8")).hexdigest()
    with store._connect() as db:
        db.execute(
            "INSERT INTO recipe_versions(id,version,hash,status,normalized_yaml,created_at) "
            "VALUES('fake',1,?,'published',?,?)",
            (recipe_hash, normalized_recipe, store._now()),
        )
        db.execute(
            "INSERT INTO recipe_instances(id,board,collector_task_id,recipe_id,recipe_version,"
            "recipe_hash,status,parameters_json,activation_count,tokens_charged,blocked_reason,"
            "created_at,updated_at,base_sha) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (instance_id, "test", "collector", "fake", 1, recipe_hash,
             "running", "{}", 0, 0, None,
             store._now(), store._now(), head),
        )
    build_task_id = kanban_db.create_task(
        conn, title="build", body="build", assignee="dev-backend",
        workspace_kind="worktree", board="test", workspace_path=str(repo),
    )
    producer_run_id = store.record_run_start(
        build_task_id, "dev-backend", "codex", "gpt", workspace_path=repo,
        recipe_activation=1,
    )
    store.record_run_end(producer_run_id, 0, 1, 1, 0.1, "done")
    defs = {item["id"]: item for item in steps}
    now = store._now()
    with store._connect() as db:
        for step_id, primitive, task_id in (
            ("build", "agent_task", build_task_id),
            *([("verify", "verification", None)] if include_verification else []),
            ("review", "review_gate", None),
        ):
            db.execute(
                "INSERT INTO recipe_steps(instance_id,step_id,activation,primitive,state,"
                "kanban_task_id,producer_run_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (instance_id, step_id, 1, primitive, "active", task_id,
                 producer_run_id if step_id == "build" else None, now, now),
            )
        instance = dict(db.execute(
            "SELECT * FROM recipe_instances WHERE id=?", (instance_id,),
        ).fetchone())
        if bind_review_inputs:
            review_task_id = primitives.activate(
                conn, instance, recipe, definition,
                {"step_id": "review", "activation": 1}, {}, [], db=db,
            )
        else:
            review_task_id = kanban_db.create_task(
                conn, title="review", body="unbound review", assignee="qa", board="test",
            )
        db.execute(
            "UPDATE recipe_steps SET kanban_task_id=? WHERE instance_id=? AND step_id='review'",
            (review_task_id, instance_id),
        )
    if create_reviewer_run:
        reviewer_run_id = store.record_run_start(
            review_task_id, "qa", reviewer_executor, "review-model",
            workspace_path=repo, provider=reviewer_provider,
            recipe_activation=reviewer_activation,
        )
        store.record_run_end(
            reviewer_run_id, reviewer_exit_code, 1, 1, 0.1, reviewer_result,
        )
    latest = {
        "build": {"step_id": "build", "kanban_task_id": build_task_id},
        **({"verify": {"step_id": "verify", "kanban_task_id": None}}
           if include_verification else {}),
        "review": {"step_id": "review", "kanban_task_id": review_task_id},
    }
    return definition, defs, latest, recipe


def test_review_task_without_factory_opened_sealed_inputs_is_blocked(tmp_path, kanban_conn):
    repo, head, tree = _repo(tmp_path)
    manifest = verify.load_verification_manifest(repo, head)
    bundle = _run(repo, head, tree, manifest, instance_id="rvw-nocite", step_id="verify")
    assert bundle["state"] == "done"
    definition, defs, latest, recipe = _seed_review_instance(
        kanban_conn, tmp_path, repo, head, tree, instance_id="rvw-nocite",
        bind_review_inputs=False,
    )
    with store._connect() as db:
        blocker = advancer._review_approval_blocker(
            db, "rvw-nocite", definition, verdict_body="APPROVE clean pass",
            recipe=recipe, defs=defs, conn=kanban_conn, latest=latest,
        )
    assert blocker == "review_inputs_not_bound"


def test_reconcile_blocks_v2_review_approval_without_factory_opened_inputs(
    tmp_path, kanban_conn,
):
    from hermes_cli import kanban_db

    repo, head, tree = _repo(tmp_path)
    manifest = verify.load_verification_manifest(repo, head)
    bundle = _run(repo, head, tree, manifest, instance_id="rvw-reconcile", step_id="verify")
    assert bundle["state"] == "done"
    _definition, _defs, latest, _recipe = _seed_review_instance(
        kanban_conn, tmp_path, repo, head, tree, instance_id="rvw-reconcile",
        bind_review_inputs=False,
    )
    review_task_id = latest["review"]["kanban_task_id"]
    with store._connect() as db:
        db.execute(
            "UPDATE recipe_steps SET state=CASE step_id "
            "WHEN 'review' THEN 'running' ELSE 'done' END "
            "WHERE instance_id='rvw-reconcile'"
        )
    result = "SHIPFACTORY_VERDICT: " + json.dumps({
        "outcome": "approve", "body": "APPROVE clean pass",
    }, separators=(",", ":"))
    assert kanban_db.complete_task(kanban_conn, review_task_id, result=result)

    reconciled = advancer.reconcile(kanban_conn, "rvw-reconcile")

    assert reconciled["status"] == "blocked"
    with store._connect() as db:
        review = db.execute(
            "SELECT state,blocked_reason FROM recipe_steps "
            "WHERE instance_id='rvw-reconcile' AND step_id='review'",
        ).fetchone()
        instance = db.execute(
            "SELECT status,blocked_reason FROM recipe_instances WHERE id='rvw-reconcile'",
        ).fetchone()
    assert dict(review) == {
        "state": "blocked", "blocked_reason": "review_inputs_not_bound",
    }
    assert dict(instance) == {
        "status": "blocked", "blocked_reason": "review_inputs_not_bound",
    }


def test_approval_uses_factory_opened_bundle_bytes_not_hash_testimony(tmp_path, kanban_conn):
    repo, head, tree = _repo(tmp_path)
    manifest = verify.load_verification_manifest(repo, head)
    bundle = _run(repo, head, tree, manifest, instance_id="rvw-cite", step_id="verify")
    assert bundle["state"] == "done"
    definition, defs, latest, recipe = _seed_review_instance(
        kanban_conn, tmp_path, repo, head, tree, instance_id="rvw-cite",
    )
    with store._connect() as db:
        blocker = advancer._review_approval_blocker(
            db, "rvw-cite", definition,
            verdict_body="APPROVE after opening Factory-supplied inputs",
            recipe=recipe, defs=defs, conn=kanban_conn, latest=latest,
        )
    assert blocker is None
    from hermes_cli import kanban_db
    task = kanban_db.get_task(kanban_conn, latest["review"]["kanban_task_id"])
    assert "SHIPFACTORY_REVIEW_INPUT_SHA256:" in task.body
    assert bundle["bundle_sha256"] in task.body


def test_adversarial_review_receives_transitive_bundle_and_retry_history(tmp_path, kanban_conn):
    from hermes_cli import kanban_db
    from shipfactory.recipes import primitives

    repo, head, tree = _repo(tmp_path)
    manifest = verify.load_verification_manifest(repo, head)
    def fail(case, workspace, env, timeout):
        now = store._now()
        return {"classification": "failed", "stdout": b"", "stderr": b"",
                "exit_code": 1, "started_at": now, "ended_at": now}
    first = _run(
        repo, head, tree, manifest, instance_id="rvw-chain", step_id="verify",
        activation=1, drivers={"command": fail},
    )
    second = _run(
        repo, head, tree, manifest, instance_id="rvw-chain", step_id="verify", activation=2,
    )
    assert first["state"] == "blocked" and second["state"] == "done"
    _definition, _defs, latest, recipe = _seed_review_instance(
        kanban_conn, tmp_path, repo, head, tree, instance_id="rvw-chain",
    )
    adversarial = {
        "id": "adversarial-review", "title": "adversarial review",
        "primitive": "review_gate", "needs": ["review"],
        "inputs": [{"from": "build", "kind": "change-set", "required": False}],
        "params": {"seat": "qa", "workspace": "dir", "instructions": "attack exact inputs"},
    }
    recipe["steps"].append(adversarial)
    defs = {item["id"]: item for item in recipe["steps"]}
    now = store._now()
    with store._connect() as db:
        db.execute(
            "INSERT INTO recipe_steps(instance_id,step_id,activation,primitive,state,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            ("rvw-chain", "adversarial-review", 1, "review_gate", "active", now, now),
        )
        instance = dict(db.execute(
            "SELECT * FROM recipe_instances WHERE id='rvw-chain'",
        ).fetchone())
        task_id = primitives.activate(
            kanban_conn, instance, recipe, adversarial,
            {"step_id": "adversarial-review", "activation": 1}, {}, [], db=db,
        )
        db.execute(
            "UPDATE recipe_steps SET kanban_task_id=? WHERE instance_id='rvw-chain' "
            "AND step_id='adversarial-review'",
            (task_id,),
        )
    latest["adversarial-review"] = {
        "step_id": "adversarial-review", "kanban_task_id": task_id,
    }
    reviewer_run_id = store.record_run_start(
        task_id, "qa", "claude", "review-model", workspace_path=repo,
        recipe_activation=1,
    )
    store.record_run_end(reviewer_run_id, 0, 1, 1, 0.1, "done")
    task = kanban_db.get_task(kanban_conn, task_id)
    assert first["bundle_sha256"] in task.body
    assert second["bundle_sha256"] in task.body
    assert '"activation":1' in task.body and '"activation":2' in task.body
    with store._connect() as db:
        blocker = advancer._review_approval_blocker(
            db, "rvw-chain", adversarial,
            verdict_body="APPROVE after Factory supplied the transitive history",
            recipe=recipe, defs=defs, conn=kanban_conn, latest=latest,
        )
    assert blocker is None


def test_review_task_gets_exact_sealed_spec_plan_change_set_diff_and_bundle(
    tmp_path, kanban_conn,
):
    from hermes_cli import kanban_db
    from shipfactory import artifacts
    from shipfactory.recipes import primitives

    repo, head, tree = _repo(tmp_path)
    manifest = verify.load_verification_manifest(repo, head)
    bundle = _run(repo, head, tree, manifest, instance_id="rvw-full-inputs", step_id="verify")
    _definition, _defs, latest, recipe = _seed_review_instance(
        kanban_conn, tmp_path, repo, head, tree, instance_id="rvw-full-inputs",
        bind_review_inputs=False,
    )
    output_dir = repo / ".shipfactory-output"
    output_dir.mkdir(exist_ok=True)
    spec_document = {
        "schema": "shipfactory.task-spec/v1", "intent_artifact_id": "a" * 64,
        "problem": "Review the exact candidate bytes.", "non_goals": [],
        "requirements": [{"id": "REQ-1", "behavior": "Bind every review input.",
                          "oracle": "Factory opens sealed bytes.", "risk": "security"}],
        "target_files": ["tracked.txt"], "forbidden_paths": [], "risk_tags": ["security"],
        "acceptance_cases": ["unit-suite"], "rollback_notes": "Revert.",
        "assumptions": [], "clarifications": [],
    }
    (output_dir / "spec.json").write_text(json.dumps(spec_document), encoding="utf-8")
    spec = artifacts.seal_artifact(
        instance_id="rvw-full-inputs", step_id="spec", activation=1, run_id=101,
        output={"kind": "task-spec", "schema": "shipfactory.task-spec/v1",
                "path": ".shipfactory-output/spec.json"},
        workspace=repo, producer="test",
    )
    (output_dir / "spec.json").unlink()
    plan_document = {
        "schema": "shipfactory.plan/v1", "task_spec_sha256": spec["sha256"],
        "base_sha": head,
        "nodes": [{"id": "build", "title": "Build", "needs": [], "kind": "implementation",
                   "requirements": ["REQ-1"], "allowed_paths": ["tracked.txt"],
                   "expected_outputs": ["change-set"], "test_cases": ["TEST-REQ-1"],
                   "risk_tags": ["security"]}],
        "integration_order": ["build"], "shared_file_overlaps": [], "residual_risks": [],
    }
    (output_dir / "plan.json").write_text(json.dumps(plan_document), encoding="utf-8")
    plan = artifacts.seal_artifact(
        instance_id="rvw-full-inputs", step_id="plan", activation=1, run_id=102,
        output={"kind": "plan", "schema": "shipfactory.plan/v1",
                "path": ".shipfactory-output/plan.json"},
        workspace=repo, producer="test",
    )
    (output_dir / "plan.json").unlink()
    build = next(item for item in recipe["steps"] if item["id"] == "build")
    build["needs"] = ["plan"]
    recipe["steps"][:0] = [
        {"id": "spec", "title": "spec", "primitive": "agent_task", "needs": [],
         "inputs": [], "params": {"seat": "dev-backend", "workspace": "dir"}},
        {"id": "plan", "title": "plan", "primitive": "agent_task", "needs": ["spec"],
         "inputs": [{"from": "spec", "kind": "task-spec", "required": True}],
         "params": {"seat": "dev-backend", "workspace": "dir"}},
    ]
    full_review = {
        "id": "full-review", "title": "full review", "primitive": "review_gate",
        "needs": ["review"],
        "inputs": [{"from": "build", "kind": "change-set", "required": True}],
        "params": {"seat": "qa", "workspace": "dir", "instructions": "review everything"},
    }
    recipe["steps"].append(full_review)
    now = store._now()
    with store._connect() as db:
        for step_id in ("spec", "plan"):
            db.execute(
                "INSERT INTO recipe_steps(instance_id,step_id,activation,primitive,state,"
                "created_at,updated_at) VALUES(?,?,1,'agent_task','done',?,?)",
                ("rvw-full-inputs", step_id, now, now),
            )
        db.execute(
            "INSERT INTO recipe_steps(instance_id,step_id,activation,primitive,state,created_at,updated_at) "
            "VALUES(?,?,1,'review_gate','active',?,?)",
            ("rvw-full-inputs", "full-review", now, now),
        )
        instance = dict(db.execute(
            "SELECT * FROM recipe_instances WHERE id='rvw-full-inputs'",
        ).fetchone())
        task_id = primitives.activate(
            kanban_conn, instance, recipe, full_review,
            {"step_id": "full-review", "activation": 1}, {}, [], db=db,
        )
    task = kanban_db.get_task(kanban_conn, task_id)
    assert spec["sha256"] in task.body and plan["sha256"] in task.body
    assert bundle["bundle_sha256"] in task.body
    assert '"producer_run_id":' in task.body and '"bytes_b64":' in task.body
    assert '"sealed_bytes_b64":' in task.body


def test_approval_after_candidate_mutates_workspace_post_verification_is_blocked(tmp_path, kanban_conn):
    repo, head, tree = _repo(tmp_path)
    manifest = verify.load_verification_manifest(repo, head)
    bundle = _run(repo, head, tree, manifest, instance_id="rvw-mutate", step_id="verify")
    assert bundle["state"] == "done"
    definition, defs, latest, recipe = _seed_review_instance(
        kanban_conn, tmp_path, repo, head, tree, instance_id="rvw-mutate",
    )
    # Candidate silently edits the reviewed worktree after verification sealed
    # its bundle but before the review decision is applied.
    (repo / "tracked.txt").write_text("mutated after verification\n", encoding="utf-8")
    with store._connect() as db:
        blocker = advancer._review_approval_blocker(
            db, "rvw-mutate", definition,
            verdict_body=f"APPROVE reviewed sealed bundle {bundle['bundle_sha256']}",
            recipe=recipe, defs=defs, conn=kanban_conn, latest=latest,
        )
    assert blocker is not None and blocker.startswith("candidate_mutated_after_verification")


def test_approval_when_verification_never_passed_is_blocked(tmp_path, kanban_conn):
    document = _manifest(argv=["python3", "-c", "raise SystemExit(1)"])
    repo, head, tree = _repo(tmp_path, document)
    manifest = verify.load_verification_manifest(repo, head)
    bundle = _run(repo, head, tree, manifest, instance_id="rvw-failed", step_id="verify")
    assert bundle["state"] == "blocked"
    definition, defs, latest, recipe = _seed_review_instance(
        kanban_conn, tmp_path, repo, head, tree, instance_id="rvw-failed",
    )
    with store._connect() as db:
        blocker = advancer._review_approval_blocker(
            db, "rvw-failed", definition,
            verdict_body=f"APPROVE reviewed sealed bundle {bundle['bundle_sha256']}",
            recipe=recipe, defs=defs, conn=kanban_conn, latest=latest,
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
                name="qa", profile="claude-review", executor="claude", model="sonnet",
            ),
            "architect": config.Seat(
                name="architect", profile="codex-default", executor="codex", model="gpt",
            ),
        },
        hierarchy_gates={},
    )
    assert config.reviewer_shares_builder_provider(cfg, "dev-backend", "qa") is True
    assert config.reviewer_shares_builder_provider(cfg, "dev-backend", "architect") is False
    assert config.reviewer_shares_builder_provider(cfg, "dev-backend", "dev-backend") is True
    with pytest.raises(config.FactoryConfigError):
        config.reviewer_shares_builder_provider(cfg, "dev-backend", "missing-seat")


def test_review_approval_blocked_when_reviewer_and_builder_collude_on_provider(
    tmp_path, kanban_conn, monkeypatch,
):
    from hermes_cli import profiles as hermes_profiles
    monkeypatch.setattr(hermes_profiles, "profile_exists", lambda name: True)
    repo, head, tree = _repo(tmp_path)
    manifest = verify.load_verification_manifest(repo, head)
    bundle = _run(repo, head, tree, manifest, instance_id="rvw-collude", step_id="verify")
    assert bundle["state"] == "done"
    definition, defs, latest, recipe = _seed_review_instance(
        kanban_conn, tmp_path, repo, head, tree, instance_id="rvw-collude",
        reviewer_executor="codex",
    )
    home = Path(os.environ["HERMES_HOME"]) / "shipfactory"
    (home / "seats.yaml").write_text(
        "company: acme\nseats:\n"
        "  dev-backend: {profile: build-profile, executor: claude, model: opus, role: engineer}\n"
        "  qa: {profile: review-profile, executor: claude, model: sonnet, role: qa}\n",
        encoding="utf-8",
    )
    with store._connect() as db:
        blocker = advancer._review_approval_blocker(
            db, "rvw-collude", definition,
            verdict_body=f"APPROVE reviewed sealed bundle {bundle['bundle_sha256']}",
            recipe=recipe, defs=defs, conn=kanban_conn, latest=latest,
        )
    assert blocker == "reviewer_shares_builder_provider"


@pytest.mark.parametrize(("reviewer_executor", "expected"), [
    ("codex", "reviewer_shares_builder_provider"),
    ("claude", None),
])
def test_plan_spec_gate_provider_independence_under_the_v2_verdict_contract(
    tmp_path, kanban_conn, reviewer_executor, expected,
):
    """Amendment F judgment call: plan/spec attacks have no change-set
    ancestry, so under the verdict_contract marker the producer derives from
    the gate's single declared agent_task input and the same exact-run
    provider comparison applies (with no worktree binding — the reviewed
    artifacts are sealed files)."""
    repo, head, tree = _repo(tmp_path)
    definition, defs, latest, recipe = _seed_review_instance(
        kanban_conn, tmp_path, repo, head, tree, instance_id=f"rvw-spec-{reviewer_executor}",
        review_input_kind="task-spec", include_verification=False,
        verdict_contract="shipfactory.verdict/v2", reviewer_executor=reviewer_executor,
    )
    with store._connect() as db:
        blocker = advancer._review_approval_blocker(
            db, f"rvw-spec-{reviewer_executor}", definition,
            verdict_body="Clean pass; no findings.",
            recipe=recipe, defs=defs, conn=kanban_conn, latest=latest,
        )
    assert blocker == expected


def test_plan_spec_gate_without_the_marker_keeps_the_pinned_v8_behavior(
    tmp_path, kanban_conn,
):
    """Pinned in-flight recipes carry no verdict_contract key, so a same-family
    plan/spec approval stays unenforced there — the extension must not
    retro-block instances published before dev-pipeline@9."""
    repo, head, tree = _repo(tmp_path)
    definition, defs, latest, recipe = _seed_review_instance(
        kanban_conn, tmp_path, repo, head, tree, instance_id="rvw-spec-unmarked",
        review_input_kind="task-spec", include_verification=False,
        reviewer_executor="codex",
    )
    with store._connect() as db:
        blocker = advancer._review_approval_blocker(
            db, "rvw-spec-unmarked", definition,
            verdict_body="APPROVE clean pass",
            recipe=recipe, defs=defs, conn=kanban_conn, latest=latest,
        )
    assert blocker is None


def test_plan_spec_gate_with_two_agent_inputs_fails_closed_as_ambiguous(
    tmp_path, kanban_conn,
):
    """A marker gate with two candidate producers must refuse, not silently
    skip the independence check (adversarial-review finding, Amendment F)."""
    repo, head, tree = _repo(tmp_path)
    definition, defs, latest, recipe = _seed_review_instance(
        kanban_conn, tmp_path, repo, head, tree, instance_id="rvw-spec-ambiguous",
        review_input_kind="task-spec", include_verification=False,
        verdict_contract="shipfactory.verdict/v2", reviewer_executor="claude",
    )
    defs["plan"] = {"id": "plan", "title": "plan", "primitive": "agent_task",
                    "needs": [], "inputs": [], "params": {}}
    definition["inputs"].append({"from": "plan", "kind": "plan", "required": False})
    with store._connect() as db:
        blocker = advancer._review_approval_blocker(
            db, "rvw-spec-ambiguous", definition,
            verdict_body="Clean pass; no findings.",
            recipe=recipe, defs=defs, conn=kanban_conn, latest=latest,
        )
    assert blocker == "review_producer_ambiguous:build,plan"


def test_review_provider_identity_does_not_read_mutable_seats(tmp_path, kanban_conn):
    repo, head, tree = _repo(tmp_path)
    manifest = verify.load_verification_manifest(repo, head)
    _run(repo, head, tree, manifest, instance_id="rvw-provider-missing", step_id="verify")
    definition, defs, latest, recipe = _seed_review_instance(
        kanban_conn, tmp_path, repo, head, tree, instance_id="rvw-provider-missing",
    )
    (Path(os.environ["HERMES_HOME"]) / "shipfactory" / "seats.yaml").unlink()
    with store._connect() as db:
        blocker = advancer._review_approval_blocker(
            db, "rvw-provider-missing", definition,
            verdict_body="APPROVE after opening exact inputs", recipe=recipe,
            defs=defs, conn=kanban_conn, latest=latest,
        )
    assert blocker is None


def test_review_approval_blocks_when_exact_reviewer_run_is_missing(tmp_path, kanban_conn):
    repo, head, tree = _repo(tmp_path)
    manifest = verify.load_verification_manifest(repo, head)
    bundle = _run(repo, head, tree, manifest, instance_id="rvw-reviewer-missing", step_id="verify")
    assert bundle["state"] == "done"
    definition, defs, latest, recipe = _seed_review_instance(
        kanban_conn, tmp_path, repo, head, tree, instance_id="rvw-reviewer-missing",
        create_reviewer_run=False,
    )
    with store._connect() as db:
        blocker = advancer._review_approval_blocker(
            db, "rvw-reviewer-missing", definition,
            verdict_body="APPROVE after opening exact inputs", recipe=recipe,
            defs=defs, conn=kanban_conn, latest=latest,
        )
    assert blocker == "review_reviewer_run_missing:exact_task_activation"


def test_review_approval_blocks_stale_reviewer_run_activation(tmp_path, kanban_conn):
    repo, head, tree = _repo(tmp_path)
    manifest = verify.load_verification_manifest(repo, head)
    bundle = _run(repo, head, tree, manifest, instance_id="rvw-reviewer-stale", step_id="verify")
    assert bundle["state"] == "done"
    definition, defs, latest, recipe = _seed_review_instance(
        kanban_conn, tmp_path, repo, head, tree, instance_id="rvw-reviewer-stale",
        reviewer_activation=0,
    )
    with store._connect() as db:
        blocker = advancer._review_approval_blocker(
            db, "rvw-reviewer-stale", definition,
            verdict_body="APPROVE after opening exact inputs", recipe=recipe,
            defs=defs, conn=kanban_conn, latest=latest,
        )
    assert blocker.startswith("review_reviewer_run_stale_activation:")


def test_reconcile_blocks_collusion_from_durable_run_provider_identity(
    tmp_path, kanban_conn,
):
    from hermes_cli import kanban_db

    repo, head, tree = _repo(tmp_path)
    manifest = verify.load_verification_manifest(repo, head)
    bundle = _run(repo, head, tree, manifest, instance_id="rvw-reconcile-collude", step_id="verify")
    assert bundle["state"] == "done"
    definition, defs, latest, recipe = _seed_review_instance(
        kanban_conn, tmp_path, repo, head, tree, instance_id="rvw-reconcile-collude",
        reviewer_executor="codex",
    )
    # The run rows say Codex performed both tasks.  A post-spawn seat edit
    # must not turn that fact into an independent Claude review.
    (Path(os.environ["HERMES_HOME"]) / "shipfactory" / "seats.yaml").write_text(
        "company: acme\nseats:\n"
        "  dev-backend: {profile: build-profile, executor: claude, model: opus, role: engineer}\n"
        "  qa: {profile: review-profile, executor: claude, model: sonnet, role: qa}\n",
        encoding="utf-8",
    )
    review_task_id = latest["review"]["kanban_task_id"]
    with store._connect() as db:
        db.execute(
            "UPDATE recipe_steps SET state=CASE step_id "
            "WHEN 'review' THEN 'running' ELSE 'done' END "
            "WHERE instance_id='rvw-reconcile-collude'"
        )
    result = "SHIPFACTORY_VERDICT: " + json.dumps({
        "outcome": "approve", "body": "APPROVE clean pass; no findings",
    }, separators=(",", ":"))
    assert kanban_db.complete_task(kanban_conn, review_task_id, result=result)

    reconciled = advancer.reconcile(kanban_conn, "rvw-reconcile-collude")

    assert reconciled["status"] == "blocked"
    with store._connect() as db:
        review = db.execute(
            "SELECT state,blocked_reason FROM recipe_steps "
            "WHERE instance_id='rvw-reconcile-collude' AND step_id='review'",
        ).fetchone()
    assert dict(review) == {
        "state": "blocked", "blocked_reason": "reviewer_shares_builder_provider",
    }


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
    raw_secret = json.dumps({
        "headers": {"authorization": "Bearer sk-supersecrettoken1234567890"},
    }).encode()
    def unredacted(case, env, started):
        return {"kind": "trace", "bytes": verify.build_capture_container(
            "trace", raw_secret, instance_id=env["SHIPFACTORY_INSTANCE_ID"],
            head_sha=env["SHIPFACTORY_HEAD_SHA"],
            bundle_id=env["SHIPFACTORY_EVIDENCE_BUNDLE_ID"], case_id=case["id"],
            attempt=int(env["SHIPFACTORY_CASE_ATTEMPT"]), captured_at=started,
            redaction_state="clean",
        )}
    bundle = _run(
        repo, head, tree, manifest, instance_id="trace-secret",
        drivers={"command": _capture_driver(tmp_path, unredacted)},
    )
    assert bundle["state"] == "failed"
    assert "not structurally redacted" in bundle["invalid_reason"]


def test_screenshot_capture_always_blocks_sealing_as_uncertain(tmp_path):
    repo, head, tree = _repo(tmp_path)
    manifest = verify.load_verification_manifest(repo, head)
    fake_png = b"\x89PNG\r\n\x1a\n" + b"pixels-that-might-contain-a-visible-api-key" * 4
    def screenshot(case, env, started):
        return {"kind": "screenshot", "mime_type": "image/png", "bytes": verify.build_capture_container(
            "screenshot", fake_png, instance_id=env["SHIPFACTORY_INSTANCE_ID"],
            head_sha=env["SHIPFACTORY_HEAD_SHA"],
            bundle_id=env["SHIPFACTORY_EVIDENCE_BUNDLE_ID"], case_id=case["id"],
            attempt=int(env["SHIPFACTORY_CASE_ATTEMPT"]), captured_at=started,
            redaction_state="uncertain",
        )}
    sealed = _run(
        repo, head, tree, manifest, instance_id="screenshot-secret",
        drivers={"command": _capture_driver(tmp_path, screenshot)},
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
    # Reordered name/value keys and nested HAR cookie objects are the attack;
    # the production parent independently rejects a runner that labels these
    # bytes clean instead of preserving structurally valid redacted JSON.
    har_payload = json.dumps({
        "log": {"entries": [{"request": {"headers": [
            {"value": "session=abc123secret; csrftoken=xyz789", "name": "Cookie"},
            {"value": "Bearer sk-realtoken1234567890abcdef", "name": "Authorization"},
            {"name": "Accept", "value": "application/json"},
        ], "cookies": [{"value": "nested-cookie-secret", "name": "session"}]}}]},
    }).encode()
    def dishonest_runner(case, env, started):
        return {"kind": "har", "bytes": verify.build_capture_container(
            "har", har_payload, instance_id=env["SHIPFACTORY_INSTANCE_ID"],
            head_sha=env["SHIPFACTORY_HEAD_SHA"],
            bundle_id=env["SHIPFACTORY_EVIDENCE_BUNDLE_ID"], case_id=case["id"],
            attempt=int(env["SHIPFACTORY_CASE_ATTEMPT"]), captured_at=started,
            redaction_state="clean",
        )}
    rejected = _run(
        repo, head, tree, manifest, instance_id="har-secret",
        drivers={"command": _capture_driver(tmp_path, dishonest_runner)},
    )
    assert rejected["state"] == "failed"
    assert "not structurally redacted" in rejected["invalid_reason"]

    redacted_document = json.loads(har_payload)
    headers = redacted_document["log"]["entries"][0]["request"]["headers"]
    headers[0]["value"] = headers[1]["value"] = "[REDACTED]"
    redacted_document["log"]["entries"][0]["request"]["cookies"][0]["value"] = "[REDACTED]"
    redacted_payload = json.dumps(
        redacted_document, sort_keys=True, separators=(",", ":"),
    ).encode()
    def honest_runner(case, env, started):
        return {"kind": "har", "bytes": verify.build_capture_container(
            "har", redacted_payload, instance_id=env["SHIPFACTORY_INSTANCE_ID"],
            head_sha=env["SHIPFACTORY_HEAD_SHA"],
            bundle_id=env["SHIPFACTORY_EVIDENCE_BUNDLE_ID"], case_id=case["id"],
            attempt=int(env["SHIPFACTORY_CASE_ATTEMPT"]), captured_at=started,
            redaction_state="redacted",
        )}
    sealed = _run(
        repo, head, tree, manifest, instance_id="har-redacted",
        drivers={"command": _capture_driver(tmp_path, honest_runner)},
    )
    assert sealed["state"] == "done"
    with store._connect() as db:
        item = db.execute(
            "SELECT path FROM evidence_items WHERE bundle_id=? AND kind='har'", (sealed["id"],),
        ).fetchone()
    _header, payload = verify._parse_capture_container(Path(item["path"]).read_bytes())
    assert json.loads(payload)["log"]["entries"]
    assert b"nested-cookie-secret" not in payload and b"application/json" in payload


def test_binary_trace_is_never_rewritten_or_reported_clean(tmp_path):
    repo, head, tree = _repo(tmp_path)
    manifest = verify.load_verification_manifest(repo, head)
    binary = b"PK\x03\x04\x00\xff\xfeAUTHORIZATION=binary-secret"
    def binary_runner(case, env, started):
        return {"kind": "trace", "bytes": verify.build_capture_container(
            "trace", binary, instance_id=env["SHIPFACTORY_INSTANCE_ID"],
            head_sha=env["SHIPFACTORY_HEAD_SHA"],
            bundle_id=env["SHIPFACTORY_EVIDENCE_BUNDLE_ID"], case_id=case["id"],
            attempt=int(env["SHIPFACTORY_CASE_ATTEMPT"]), captured_at=started,
            redaction_state="uncertain",
        )}
    bundle = _run(
        repo, head, tree, manifest, instance_id="binary-trace",
        drivers={"command": _capture_driver(tmp_path, binary_runner)},
    )
    assert bundle["state"] == "blocked"
    assert "redaction is uncertain" in bundle["invalid_reason"]
    with store._connect() as db:
        item = db.execute(
            "SELECT path FROM evidence_items WHERE bundle_id=? AND kind='trace'", (bundle["id"],),
        ).fetchone()
    _header, payload = verify._parse_capture_container(Path(item["path"]).read_bytes())
    assert payload == binary


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
    definition, defs, latest, recipe = _seed_review_instance(
        kanban_conn, tmp_path, repo, head, tree, instance_id="rvw-tampered",
    )
    # Tamper with the sealed evidence bytes after sealing, without touching
    # the evidence_bundles row itself.
    Path(item["path"]).write_bytes(b"[stdout]\nforged after seal\n[stderr]\n")
    with store._connect() as db:
        blocker = advancer._review_approval_blocker(
            db, "rvw-tampered", definition,
            verdict_body=f"APPROVE reviewed sealed bundle {bundle['bundle_sha256']}",
            recipe=recipe, defs=defs, conn=kanban_conn, latest=latest,
        )
    assert blocker is not None and blocker.startswith("evidence_invariant:")


# ---------------------------------------------------------------------------
# §2.4.10 #12 -- ffmpeg hangs after tests finish.
# ---------------------------------------------------------------------------

def test_production_browser_sidecar_that_ignores_sigterm_is_forcibly_reaped(
    tmp_path, monkeypatch,
):
    wrapper = tmp_path / "hung-capture-provider.py"
    wrapper.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, signal, sys, time\n"
        "request = json.loads(pathlib.Path(sys.argv[-1]).read_text())\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "pathlib.Path(request['ready_path']).write_text('ready\\n')\n"
        "while True: time.sleep(1)\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    monkeypatch.setattr(verify, "_playwright_python", lambda: str(wrapper))
    repo, head, _tree = _repo(tmp_path)
    case = {
        "id": "hung-capture", "requirement_ids": ["REQ-UI"], "driver": "playwright",
        "script": "e2e/hung.spec.ts", "assertions": [{"type": "visible", "selector": "#ok"}],
    }
    env = {
        "SHIPFACTORY_INSTANCE_ID": "hung-sidecar", "SHIPFACTORY_HEAD_SHA": head,
        "SHIPFACTORY_EVIDENCE_BUNDLE_ID": "hung-sidecar:verify:1",
        "SHIPFACTORY_CASE_ID": "hung-capture", "SHIPFACTORY_CASE_ATTEMPT": "1",
        "SHIPFACTORY_CAPTURE_POLICY": "{}", "SHIPFACTORY_ENV_APP_URL": "http://127.0.0.1:9",
    }
    started = time.monotonic()
    result = verify._playwright_driver(case, repo, env, 1)
    elapsed = time.monotonic() - started
    assert elapsed < 10, "sidecar cleanup must not hang evidence collection"
    assert result["classification"] == "timeout"
    assert verify._SIDECAR_TRACKERS == {}


def test_process_scope_enumeration_system_error_fails_closed():
    """A transient macOS proc_environ failure cannot be labelled complete."""

    class FakePsutilError(Exception):
        pass

    class RootProcess:
        def children(self, recursive=False):
            return []

    class BrokenCandidate:
        pid = 987_654

        def environ(self):
            raise SystemError("proc_environ returned a result with an exception set")

    class FakePsutil:
        Error = FakePsutilError

        def Process(self, pid):
            return RootProcess()

        def process_iter(self, attrs):
            return [BrokenCandidate()]

    tracker = object.__new__(verify._ProcessTreeTracker)
    tracker.__dict__["_psutil"] = FakePsutil()
    tracker.__dict__["proc"] = type("Leader", (), {"pid": 123_456})()
    tracker.scope = "scope"
    tracker.identities = {}
    tracker.available = True

    tracker._scan()

    assert tracker.available is False
    assert tracker.identities == {}


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
        "    start_new_session=True,\n"
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
    with store._connect() as db:
        metadata = json.loads(db.execute(
            "SELECT metadata_json FROM evidence_items WHERE bundle_id=? AND kind='log'",
            (bundle["id"],),
        ).fetchone()["metadata_json"])
    if metadata["process_tree_supervision"] != "complete":
        from shipfactory import spawn
        token = spawn._process_start_token(grandchild_pid)
        spawn.verified_killpg(grandchild_pid, token, signal.SIGKILL)
        pytest.skip("host process-scope enumeration unavailable in this sandbox")

    deadline = time.monotonic() + 5
    alive = True
    while time.monotonic() < deadline:
        try:
            import psutil
            descendant = psutil.Process(grandchild_pid)
            alive = descendant.status() != psutil.STATUS_ZOMBIE
        except (psutil.NoSuchProcess, ProcessLookupError):
            alive = False
        if not alive:
            break
        time.sleep(0.05)
    assert not alive, "the launcher exited, but its grandchild ('the app') was left running"


# ---------------------------------------------------------------------------
# §2.4.10 #14 -- truncated video has a valid container header.
# ---------------------------------------------------------------------------

def test_truncated_capture_with_a_valid_header_is_rejected(tmp_path):
    repo, head, tree = _repo(tmp_path)
    manifest = verify.load_verification_manifest(repo, head)
    full_payload = b'{"frames": "' + b"X" * 2000 + b'"}'
    def truncated_runner(case, env, started):
        container = verify.build_capture_container(
            "trace", full_payload, instance_id=env["SHIPFACTORY_INSTANCE_ID"],
            head_sha=env["SHIPFACTORY_HEAD_SHA"],
            bundle_id=env["SHIPFACTORY_EVIDENCE_BUNDLE_ID"], case_id=case["id"],
            attempt=int(env["SHIPFACTORY_CASE_ATTEMPT"]), captured_at=started,
            redaction_state="clean",
        )
        return {"kind": "trace", "bytes": container[:-500]}
    bundle = _run(
        repo, head, tree, manifest, instance_id="truncated-capture",
        drivers={"command": _capture_driver(tmp_path, truncated_runner)},
    )
    assert bundle["state"] == "failed"
    assert "payload was truncated or replaced" in bundle["invalid_reason"]


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


def test_profile_without_a_declared_surface_fails_closed(tmp_path):
    repo, base, base_tree = _repo(tmp_path)
    (repo / "dashboard").mkdir()
    (repo / "dashboard" / "widget.tsx").write_text("export const x = 1;\n", encoding="utf-8")
    head, tree = _commit(repo, "touch a UI path")
    manifest = verify.load_verification_manifest(repo, base, verify_worktree_copy=False)
    profile = _profile()
    profile.pop("surface")
    bundle = verify.run_verification(
        instance_id="surface-optional", step_id="verify", activation=1,
        input_revision_hash="revision", base_sha=base, head_sha=head, tree_sha=tree,
        workspace=repo, manifest=manifest, profile=profile, run_candidate_cases=False,
        protected_manifest=verify.load_verification_manifest(repo, base, verify_worktree_copy=False),
    )
    assert bundle["state"] == "failed"
    assert "profile must declare a surface" in bundle["invalid_reason"]


@pytest.mark.parametrize("path,surface,missing", [
    ("dashboard/widget.tsx", "browser", "browser"),
    ("server/api/routes.py", "api", "api"),
    ("db/migrations/001.sql", "migration", "rollback"),
])
def test_surface_floor_requires_executed_behavior_not_only_a_profile_label(
    tmp_path, path, surface, missing,
):
    repo, base, _tree = _repo(tmp_path)
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("changed\n", encoding="utf-8")
    head, tree = _commit(repo, f"touch {surface}")
    protected = verify.load_verification_manifest(repo, base, verify_worktree_copy=False)
    bundle = verify.run_verification(
        instance_id=f"surface-behavior-{surface}", step_id="verify", activation=1,
        input_revision_hash="revision", base_sha=base, head_sha=head, tree_sha=tree,
        workspace=repo, manifest=protected, profile=_profile(surface=surface),
        run_candidate_cases=False, protected_manifest=protected,
        required_surface=surface,
    )
    assert bundle["state"] == "failed"
    assert f"required surface behaviors are missing: {missing}" in bundle["invalid_reason"]


def test_migration_surface_rejects_a_passing_command_with_only_a_rollback_label(tmp_path):
    document = _manifest(
        argv=["true"], case_id="rollback-noop",
        oracle={"type": "exit_code", "equals": 0},
    )
    repo, base, _tree = _repo(tmp_path, document)
    migration = repo / "db" / "migrations" / "001.sql"
    migration.parent.mkdir(parents=True)
    migration.write_text("ALTER TABLE example ADD COLUMN value TEXT;\n", encoding="utf-8")
    head, tree = _commit(repo, "add migration")
    protected = verify.load_verification_manifest(repo, base, verify_worktree_copy=False)

    bundle = verify.run_verification(
        instance_id="rollback-noop", step_id="verify", activation=1,
        input_revision_hash="revision", base_sha=base, head_sha=head, tree_sha=tree,
        workspace=repo, manifest=protected, profile=_profile(surface="migration"),
        run_candidate_cases=False, protected_manifest=protected,
        required_surface="migration",
    )

    assert bundle["state"] == "failed"
    assert "required surface behaviors are missing: rollback" in bundle["invalid_reason"]


def test_migration_behavior_rejects_direction_word_only_inside_an_inert_flag():
    document = {
        "schema": verify.VERIFICATION_SCHEMA,
        "cases": [
            {
                "id": "migration-down", "requirement_ids": ["REQ-1"],
                "driver": "command", "surface_behavior": "migration_down",
                "argv": ["do_stuff.sh", "--reason=rollback-please"],
                "oracle": {"type": "exit_code", "equals": 0},
            },
            {
                "id": "migration-up", "requirement_ids": ["REQ-1"],
                "driver": "command", "surface_behavior": "migration_up",
                "argv": ["do_stuff.sh", "--reason=upgrade-please"],
                "oracle": {"type": "exit_code", "equals": 0},
            },
        ],
        "capture": {"video": False, "trace": False, "screenshots": "never"},
    }

    with pytest.raises(
        verify.VerificationManifestError, match="primary migration subcommand",
    ):
        verify.validate_verification_manifest(document)


def test_migration_behavior_rejects_option_flag_as_primary_subcommand():
    document = {
        "schema": verify.VERIFICATION_SCHEMA,
        "cases": [
            {
                "id": "migration-down", "requirement_ids": ["REQ-1"],
                "driver": "command", "surface_behavior": "migration_down",
                "argv": ["do_stuff.sh", "--rollback"],
                "oracle": {"type": "exit_code", "equals": 0},
            },
            {
                "id": "migration-up", "requirement_ids": ["REQ-1"],
                "driver": "command", "surface_behavior": "migration_up",
                "argv": ["do_stuff.sh", "--upgrade"],
                "oracle": {"type": "exit_code", "equals": 0},
            },
        ],
        "capture": {"video": False, "trace": False, "screenshots": "never"},
    }

    with pytest.raises(
        verify.VerificationManifestError, match="bare primary migration subcommand",
    ):
        verify.validate_verification_manifest(document)


def test_python_prefix_tool_is_not_misclassified_as_a_python_interpreter():
    document = {
        "schema": verify.VERIFICATION_SCHEMA,
        "cases": [
            {
                "id": "migration-down", "requirement_ids": ["REQ-1"],
                "driver": "command", "surface_behavior": "migration_down",
                "argv": ["pythonic-migrate", "decoy", "rollback"],
                "oracle": {"type": "exit_code", "equals": 0},
            },
            {
                "id": "migration-up", "requirement_ids": ["REQ-1"],
                "driver": "command", "surface_behavior": "migration_up",
                "argv": ["pythonic-migrate", "decoy", "upgrade"],
                "oracle": {"type": "exit_code", "equals": 0},
            },
        ],
        "capture": {"video": False, "trace": False, "screenshots": "never"},
    }

    with pytest.raises(
        verify.VerificationManifestError, match="primary migration subcommand",
    ):
        verify.validate_verification_manifest(document)


def test_migration_surface_executes_protected_down_and_up_behavior_pair(tmp_path):
    document = {
        "schema": verify.VERIFICATION_SCHEMA,
        "cases": [
            {
                "id": "migration-down", "requirement_ids": ["REQ-1"],
                "driver": "command", "surface_behavior": "migration_down",
                "argv": [sys.executable, "migration_tool.py", "rollback"],
                "oracle": {"type": "exit_code", "equals": 0},
            },
            {
                "id": "migration-up", "requirement_ids": ["REQ-1"],
                "driver": "command", "surface_behavior": "migration_up",
                "argv": [sys.executable, "migration_tool.py", "upgrade"],
                "oracle": {"type": "exit_code", "equals": 0},
            },
        ],
        "capture": {"video": False, "trace": False, "screenshots": "never"},
    }
    repo, _initial, _tree = _repo(tmp_path, document)
    migration_state = tmp_path / "migration-state"
    (repo / "migration_tool.py").write_text(
        "import pathlib, sys\n"
        f"state = pathlib.Path({str(migration_state)!r})\n"
        "if sys.argv[1] == 'rollback': state.write_text('down')\n"
        "elif sys.argv[1] == 'upgrade':\n"
        "    assert state.read_text() == 'down'\n"
        "    state.write_text('up')\n",
        encoding="utf-8",
    )
    base, _base_tree = _commit(repo, "trusted migration verification")
    migration = repo / "db" / "migrations" / "001.sql"
    migration.parent.mkdir(parents=True)
    migration.write_text("ALTER TABLE example ADD COLUMN value TEXT;\n", encoding="utf-8")
    head, tree = _commit(repo, "add migration")
    protected = verify.load_verification_manifest(repo, base, verify_worktree_copy=False)

    bundle = verify.run_verification(
        instance_id="rollback-roundtrip", step_id="verify", activation=1,
        input_revision_hash="revision", base_sha=base, head_sha=head, tree_sha=tree,
        workspace=repo, manifest=protected, profile=_profile(surface="migration"),
        run_candidate_cases=False, protected_manifest=protected,
        required_surface="migration",
    )

    assert bundle["state"] == "done"
    assert migration_state.read_text() == "up"


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
