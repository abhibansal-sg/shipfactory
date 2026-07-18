"""SF-9 deterministic verification and sealed-evidence regressions."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from shipfactory import store, verification as verify
from shipfactory.recipes.loader import RecipeError, validate as validate_recipe


_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Verification Test", "GIT_AUTHOR_EMAIL": "verify@example.invalid",
    "GIT_COMMITTER_NAME": "Verification Test", "GIT_COMMITTER_EMAIL": "verify@example.invalid",
}


def _manifest(*, argv=None, oracle=None, driver="command"):
    case = {
        "id": "unit-suite", "requirement_ids": ["REQ-1"], "driver": driver,
        "argv": argv or ["python3", "-c", "print('ok')"],
        "oracle": oracle or {"type": "exit_code", "equals": 0},
    }
    return {
        "schema": verify.VERIFICATION_SCHEMA, "cases": [case],
        "capture": {"video": False, "trace": False, "screenshots": "on-failure"},
    }


def _repo(tmp_path: Path, document=None):
    repo = tmp_path / "repo"
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


def _commit(repo: Path, message: str) -> tuple[str, str]:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", message], cwd=repo, env=_GIT_ENV, check=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=repo, text=True).strip()
    return head, tree


def _action_payload(repo: Path, base: str, head: str, tree: str, *, instance: str):
    protected = verify.load_verification_manifest(
        repo, base, verify_worktree_copy=False,
    )
    candidate = verify.load_verification_manifest_if_present(repo, head)
    return {
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


def _finish_action(payload, timeout=30):
    # An action can execute protected and candidate cases sequentially.  Each
    # case keeps its own profile runtime limit; this outer poll budget only
    # allows the trusted runner to finish the full case set under host load.
    deadline = time.monotonic() + timeout
    result = verify.run_action(payload)
    while result["status"] == "pending" and time.monotonic() < deadline:
        time.sleep(0.05)
        verify.reap_runs()
        result = verify.run_action(payload)
    assert result["status"] != "pending"
    return result


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


def test_schema_migration_is_normative_and_numbered():
    store.init_db()
    with store._connect() as db:
        assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 15
        assert [row["name"] for row in db.execute("PRAGMA table_info(evidence_bundles)")] == [
            "id", "instance_id", "step_id", "activation", "input_revision_hash",
            "base_sha", "head_sha", "tree_sha", "environment_session_id",
            "manifest_relpath", "manifest_blob_sha", "state", "bundle_sha256",
            "redaction_state", "created_at", "sealed_at", "invalid_reason",
            "phase_b_eligible",
            "workspace_path", "workspace_owner_task_id", "workspace_owner_activation",
            "workspace_owner_run_id", "required_surface", "environment_identity_json",
        ]
        assert [row["name"] for row in db.execute("PRAGMA table_info(evidence_items)")][-5:] == [
            "exit_code", "started_at", "ended_at", "metadata_json", "attempt",
        ]
        assert "recipe_activation" in {
            row["name"] for row in db.execute("PRAGMA table_info(runs)")
        }
        assert "producer_run_id" in {
            row["name"] for row in db.execute("PRAGMA table_info(recipe_steps)")
        }


def test_manifest_is_blob_pinned_covers_requirements_and_rejects_tamper(tmp_path):
    repo, head, _tree = _repo(tmp_path)
    manifest = verify.load_verification_manifest(
        repo, head, required_requirement_ids={"REQ-1"},
    )
    assert manifest.blob_sha
    with pytest.raises(verify.VerificationManifestError, match="uncovered"):
        verify.load_verification_manifest(repo, head, required_requirement_ids={"REQ-2"})
    (repo / ".shipfactory" / "verification.yaml").write_text("tampered\n")
    with pytest.raises(verify.VerificationManifestError, match="differ"):
        verify.load_verification_manifest(repo, head)


def test_unknown_driver_fails_closed():
    document = _manifest()
    document["cases"][0]["driver"] = "shell"
    with pytest.raises(verify.VerificationManifestError, match="unknown verification driver"):
        verify.validate_verification_manifest(document)
    shell = _manifest(argv=["sh", "-c", "echo unsafe"])
    with pytest.raises(verify.VerificationManifestError, match="shell interpolation"):
        verify.validate_verification_manifest(shell)


def test_recipe_v2_accepts_only_the_non_model_verification_shape():
    step = {
        "id": "verify-runtime", "primitive": "verification", "title": "Verify",
        "needs": [], "optional": False, "inputs": [],
        "outputs": [{"kind": "evidence-bundle", "schema": "shipfactory.evidence/v1",
                     "path": ".shipfactory-output/evidence-manifest.json"}],
        "params": {"manifest": ".shipfactory/verification.yaml",
                   "profile": "browser-standard", "environment": "app"},
    }
    recipe = {
        "schema": "shipfactory.recipe/v2", "id": "verification-test", "version": 1,
        "status": "active", "description": "verify", "intent_tags": ["test"],
        "supersedes": None, "parameters": {},
        "budgets": {"max_activations": 1, "max_tokens": 1,
                    "step_activation_caps": {}, "token_pools": {"standard": 1}},
        "steps": [step],
    }
    assert validate_recipe(recipe) is recipe
    step["params"]["seat"] = "qa"
    with pytest.raises(RecipeError, match="params are exact"):
        validate_recipe(recipe)


def test_command_pass_seals_bundle_and_serves_item_only_by_id(tmp_path):
    repo, head, tree = _repo(tmp_path)
    manifest = verify.load_verification_manifest(repo, head)
    bundle = _run(repo, head, tree, manifest)
    assert bundle["state"] == "done"
    verified = verify.verify_evidence_bundle(bundle["id"])
    assert verified["head_sha"] == head
    with store._connect() as db:
        item_id = db.execute(
            "SELECT id FROM evidence_items WHERE bundle_id=?", (bundle["id"],),
        ).fetchone()[0]
        run = dict(db.execute(
            "SELECT * FROM runs WHERE executor='verification' ORDER BY id DESC LIMIT 1"
        ).fetchone())
    item, data = verify.read_evidence_item(item_id)
    assert item["bundle_id"] == bundle["id"]
    assert b"ok" in data
    assert run["ended_at"] and run["pid"] and run["process_start_token"]
    assert run["seat"] == "verification" and run["resolved_model"] == "non-model"


def test_wrong_sha_and_post_test_mutation_are_invalid(tmp_path):
    repo, head, tree = _repo(tmp_path)
    manifest = verify.load_verification_manifest(repo, head)
    wrong = verify.run_verification(
        instance_id="wrong", step_id="verify", activation=1,
        input_revision_hash="revision", base_sha=head, head_sha="f" * 40,
        tree_sha=tree, workspace=repo, manifest=manifest, profile=_profile(),
    )
    assert wrong["state"] == "failed"
    assert "head_sha mismatch" in wrong["invalid_reason"]

    def mutating_driver(case, workspace, env, timeout):
        (workspace / "tracked.txt").write_text("mutated\n")
        now = store._now()
        return {"classification": "passed", "stdout": b"ok", "stderr": b"",
                "exit_code": 0, "started_at": now, "ended_at": now}

    mutated = _run(
        repo, head, tree, manifest, instance_id="mutated",
        drivers={"command": mutating_driver},
    )
    assert mutated["state"] == "failed"
    assert "post-test mutation" in mutated["invalid_reason"]


def test_deterministic_failure_not_retried_but_infra_retries_once(tmp_path):
    repo, head, tree = _repo(tmp_path)
    manifest = verify.load_verification_manifest(repo, head)
    calls = []

    def deterministic(case, workspace, env, timeout):
        calls.append("failed")
        now = store._now()
        return {"classification": "failed", "stdout": b"bad", "stderr": b"",
                "exit_code": 1, "started_at": now, "ended_at": now}

    failed = _run(repo, head, tree, manifest, instance_id="det", drivers={"command": deterministic})
    assert failed["state"] == "blocked"
    assert calls == ["failed"]

    attempts = []
    def flaky(case, workspace, env, timeout):
        attempts.append(len(attempts) + 1)
        now = store._now()
        status = "infrastructure_error" if len(attempts) == 1 else "passed"
        return {"classification": status, "stdout": b"ok", "stderr": b"",
                "exit_code": 0 if status == "passed" else None,
                "started_at": now, "ended_at": now}

    recovered = _run(repo, head, tree, manifest, instance_id="infra", drivers={"command": flaky})
    assert recovered["state"] == "done"
    assert attempts == [1, 2]
    with store._connect() as db:
        history = db.execute(
            "SELECT attempt,status FROM verification_cases WHERE bundle_id=? ORDER BY attempt",
            (recovered["id"],),
        ).fetchall()
    assert [tuple(row) for row in history] == [(1, "infrastructure_error"), (2, "passed")]
    document = json.loads((Path(store._db_path()).parent / "runs" / "infra" / "verify" / "1" / "evidence" / "bundle.json").read_text())
    assert document["phase_b_eligible"] is False


def test_protected_baseline_failure_overrides_candidate_pass(tmp_path):
    repo, head, tree = _repo(tmp_path)
    candidate = verify.load_verification_manifest(repo, head)
    protected_doc = _manifest(argv=["python3", "-c", "raise SystemExit(1)"])
    verify.validate_verification_manifest(protected_doc)
    protected = verify.VerificationManifest(
        protected_doc, "a" * 40, head, ".shipfactory/verification.yaml", b"protected",
    )
    bundle = _run(repo, head, tree, candidate, protected_manifest=protected)
    assert bundle["state"] == "blocked"
    assert bundle["invalid_reason"] == "protected_baseline_test_failed"


def test_production_action_runs_base_manifest_and_blocks_candidate_pass(tmp_path):
    repo, base, _base_tree = _repo(
        tmp_path, _manifest(argv=["python3", "-c", "raise SystemExit(1)"]),
    )
    import yaml
    (repo / verify.DEFAULT_MANIFEST_PATH).write_text(
        yaml.safe_dump(_manifest(), sort_keys=False), encoding="utf-8",
    )
    head, tree = _commit(repo, "candidate passes")
    result = _finish_action(_action_payload(repo, base, head, tree, instance="baseline"))
    assert result["status"] == "blocked"
    with store._connect() as db:
        bundle = dict(db.execute(
            "SELECT * FROM evidence_bundles WHERE id=?", (result["bundle_id"],),
        ).fetchone())
        cases = db.execute(
            "SELECT case_id,status FROM verification_cases WHERE bundle_id=? ORDER BY case_id",
            (result["bundle_id"],),
        ).fetchall()
    assert bundle["invalid_reason"] == "protected_baseline_test_failed"
    assert [tuple(row) for row in cases] == [
        ("protected:unit-suite", "failed"), ("unit-suite", "passed"),
    ]


def test_deleted_candidate_manifest_still_runs_trusted_base_cases(tmp_path):
    repo, base, _base_tree = _repo(tmp_path)
    (repo / verify.DEFAULT_MANIFEST_PATH).unlink()
    head, tree = _commit(repo, "delete candidate manifest")
    payload = _action_payload(repo, base, head, tree, instance="deleted-manifest")
    assert payload["candidate_manifest_blob_sha"] is None
    result = _finish_action(payload)
    assert result["status"] == "done"
    with store._connect() as db:
        cases = db.execute(
            "SELECT case_id,status FROM verification_cases WHERE bundle_id=?",
            (result["bundle_id"],),
        ).fetchall()
    assert [tuple(row) for row in cases] == [("protected:unit-suite", "passed")]


def test_slow_verification_runner_does_not_stall_an_unrelated_action(tmp_path):
    slow_repo, slow_head, slow_tree = _repo(
        tmp_path / "slow", _manifest(argv=["python3", "-c", "import time; time.sleep(20)"]),
    )
    fast_repo, fast_head, fast_tree = _repo(tmp_path / "fast")
    slow = _action_payload(
        slow_repo, slow_head, slow_head, slow_tree, instance="slow-instance",
    )
    slow["profile"]["max_runtime_seconds"] = 30
    fast = _action_payload(
        fast_repo, fast_head, fast_head, fast_tree, instance="fast-instance",
    )
    assert verify.run_action(slow)["status"] == "pending"
    assert verify.run_action(fast)["status"] == "pending"
    fast_result = _finish_action(fast)
    assert fast_result["status"] == "done"
    slow_record = verify._RUNNING[verify._bundle_id("slow-instance", "verify", 1)]
    assert slow_record["proc"].poll() is None


def test_candidate_child_gets_only_explicit_environment(tmp_path, monkeypatch):
    canary = "daemon-only-canary-value"
    monkeypatch.setenv("SHIPFACTORY_TEST_CANARY_SECRET", canary)
    document = _manifest(
        argv=[
            "python3", "-c",
            "import os; print('canary=' + str(os.getenv('SHIPFACTORY_TEST_CANARY_SECRET'))); "
            "print('allowed=' + str(os.getenv('ALLOWED_CASE_VAR'))); "
            "print('home=' + os.environ['HOME'])",
        ],
        oracle={"type": "output_contains", "contains": "canary=None"},
    )
    repo, head, tree = _repo(tmp_path, document)
    manifest = verify.load_verification_manifest(repo, head)
    bundle = _run(
        repo, head, tree, manifest,
        profile=_profile(env={"ALLOWED_CASE_VAR": "yes"}),
    )
    assert bundle["state"] == "done"
    with store._connect() as db:
        item = dict(db.execute(
            "SELECT * FROM evidence_items WHERE bundle_id=?", (bundle["id"],),
        ).fetchone())
    output = Path(item["path"]).read_text(encoding="utf-8")
    assert "canary=None" in output and "allowed=yes" in output
    assert canary not in output
    assert f"home={repo}" not in output


def test_done_bundle_cannot_seal_without_protected_results(tmp_path):
    repo, head, tree = _repo(tmp_path)
    manifest = verify.load_verification_manifest(repo, head)
    bundle_id = verify._bundle_id("missing-protected", "verify", 1)
    verify._insert_bundle(
        bundle_id=bundle_id, instance_id="missing-protected", step_id="verify",
        activation=1, input_revision_hash="revision", base_sha=head,
        head_sha=head, tree_sha=tree, environment_session_id=None, manifest=manifest,
    )
    now = store._now()
    verify._record_case(
        bundle_id=bundle_id, case_id="unit-suite", attempt=1,
        case=manifest.document["cases"][0], status="passed", item_ids=[],
        started_at=now, ended_at=now,
    )
    sealed = verify._seal_bundle(
        bundle_id, final_state="done", reason=None, phase_b_eligible=True,
        required_case_ids=["unit-suite", "protected:unit-suite"],
    )
    assert sealed["state"] == "failed"
    assert "required verification case results are missing" in sealed["invalid_reason"]


def test_output_is_redacted_and_capped_by_profile(tmp_path):
    document = _manifest(
        argv=["python3", "-c", "print('password=supersecret-' + 'x'*1000)"],
    )
    repo, head, tree = _repo(tmp_path, document)
    manifest = verify.load_verification_manifest(repo, head)
    bundle = _run(
        repo, head, tree, manifest,
        profile=_profile(max_evidence_bytes=120, max_log_bytes=80),
    )
    assert bundle["state"] == "done"
    assert bundle["redaction_state"] == "redacted"
    with store._connect() as db:
        item = dict(db.execute(
            "SELECT * FROM evidence_items WHERE bundle_id=?", (bundle["id"],),
        ).fetchone())
    assert item["size_bytes"] <= 80
    data = Path(item["path"]).read_bytes()
    assert b"supersecret" not in data
    assert b"[REDACTED]" in data


def test_foreign_evidence_membership_is_rejected(tmp_path):
    repo, head, tree = _repo(tmp_path)
    manifest = verify.load_verification_manifest(repo, head)
    bundle = _run(repo, head, tree, manifest)
    with store._connect() as db:
        original = dict(db.execute(
            "SELECT * FROM evidence_items WHERE bundle_id=?", (bundle["id"],),
        ).fetchone())
        db.execute(
            "INSERT INTO evidence_items(id,bundle_id,case_id,kind,path,sha256,size_bytes,"
            "producer,metadata_json) VALUES(?,?,?,?,?,?,?,?,?)",
            ("foreign", bundle["id"], "unit-suite", "log", original["path"],
             original["sha256"], original["size_bytes"], "foreign", "{}"),
        )
    with pytest.raises(verify.EvidenceInvariantError, match="outside its sealed set"):
        verify.verify_evidence_bundle(bundle["id"])


def test_browsers_path_resolves_deterministically_without_a_driver_subprocess(monkeypatch, tmp_path):
    """finding #75: the subprocess probe intermittently timed out under
    verification load, silently downgrading every browser case to
    infrastructure_error. Well-known cache locations must win without a race."""
    from shipfactory import verification

    cache = tmp_path / "Library" / "Caches" / "ms-playwright"
    (cache / "chromium_headless_shell-1228").mkdir(parents=True)
    monkeypatch.setattr(verification.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(verification.sys, "platform", "darwin")
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.setattr(
        verification, "_probed_browsers_path",
        lambda interpreter: pytest.fail("deterministic path must not probe"),
    )
    assert verification._playwright_browsers_path("/usr/bin/false") == str(cache)


def test_browsers_path_prefers_explicit_env_and_falls_back_to_probe(monkeypatch, tmp_path):
    from shipfactory import verification

    explicit = tmp_path / "custom-cache"
    (explicit / "chromium-1228").mkdir(parents=True)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(explicit))
    monkeypatch.setattr(
        verification, "_probed_browsers_path",
        lambda interpreter: pytest.fail("explicit env must not probe"),
    )
    assert verification._playwright_browsers_path("/usr/bin/false") == str(explicit)

    empty_home = tmp_path / "empty-home"
    empty_home.mkdir()
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.setattr(verification.Path, "home", classmethod(lambda cls: empty_home))
    monkeypatch.setattr(
        verification, "_probed_browsers_path", lambda interpreter: "probed-result",
    )
    assert verification._playwright_browsers_path("/usr/bin/false") == "probed-result"


def test_runner_env_grants_the_trusted_browser_cache(monkeypatch, tmp_path):
    """finding #76: the v2 runner is spawned with a rebuilt scrubbed env and an
    isolated HOME, so a grant made anywhere else never reaches the browser
    driver — every browser case failed with 'Executable doesn't exist' in the
    isolated cache. The grant must be made where the env is constructed."""
    from shipfactory import verification

    cache = tmp_path / "Library" / "Caches" / "ms-playwright"
    (cache / "chromium-1228").mkdir(parents=True)
    monkeypatch.setattr(verification.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(verification.sys, "platform", "darwin")
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)

    env = verification._runner_env("bundle-a")
    assert env["PLAYWRIGHT_BROWSERS_PATH"] == str(cache)
    assert env["HOME"] != str(tmp_path), "runner HOME must stay isolated"

    # Child-process shape: isolated HOME, but the inherited explicit variable
    # must win so nested sidecars keep the grant.
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(cache))
    monkeypatch.setattr(verification.Path, "home", classmethod(lambda cls: tmp_path / "isolated"))
    child_env = verification._runner_env("bundle-a")
    assert child_env["PLAYWRIGHT_BROWSERS_PATH"] == str(cache)
