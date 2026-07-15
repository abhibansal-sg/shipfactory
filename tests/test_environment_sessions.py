"""SF-8 environment-session regressions: manifests, materialization, app-up."""

from __future__ import annotations

import itertools
import os
import signal
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest

from shipfactory import config, environments as env, store

# Real OS ports, so each test gets its own slice — a leftover process from a
# failed test must never bleed a "healthy" answer into an unrelated test.
_PORT_COUNTER = itertools.count(19100, 5)


@pytest.fixture(autouse=True)
def _clean_environment_processes():
    yield
    for record in list(env._APP_RUNNING.values()) + list(env._MATERIALIZING.values()):
        env._kill_group(record.get("pid"), signal.SIGKILL)
    env._APP_RUNNING.clear()
    env._MATERIALIZING.clear()


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Env Test", "GIT_AUTHOR_EMAIL": "env@example.invalid",
    "GIT_COMMITTER_NAME": "Env Test", "GIT_COMMITTER_EMAIL": "env@example.invalid",
}

_MANIFEST = """\
schema: shipfactory.runtime/v1
bootstrap:
  argv: ["scripts/bootstrap.sh"]
  tracked_inputs: {tracked_inputs}
  network: deny
app:
  start_argv: ["scripts/app-start.sh", "--port", "${{PORT}}"]
  healthcheck:
    path: /health
    expected_status: 200
  stop_signal: TERM
seed:
  argv: ["scripts/seed.sh"]
"""

_APP_SERVER = """\
#!/bin/sh
PORT_VALUE="$2"
exec python3 -c "
import sys, http.server
port = int('$PORT_VALUE')
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b'ok')
    def log_message(self, *a): pass
http.server.HTTPServer(('127.0.0.1', port), H).serve_forever()
"
"""

_NEVER_BINDS = """\
#!/bin/sh
sleep 60
"""

_IGNORES_TERM = """\
#!/bin/sh
trap '' TERM
sleep 60
"""


def _write_repo(tmp_path: Path, *, bootstrap="#!/bin/sh\nexit 0\n", seed="#!/bin/sh\nexit 0\n",
                app_start=_APP_SERVER, tracked_inputs=None, extra=None) -> Path:
    repo = tmp_path / f"repo-{len(list(tmp_path.iterdir())) if tmp_path.exists() else 0}-{os.urandom(4).hex()}"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / ".shipfactory").mkdir()
    (repo / ".shipfactory" / "runtime.yaml").write_text(
        _MANIFEST.format(tracked_inputs=tracked_inputs or []), encoding="utf-8",
    )
    (repo / "scripts").mkdir()
    (repo / "scripts" / "bootstrap.sh").write_text(bootstrap, encoding="utf-8")
    (repo / "scripts" / "seed.sh").write_text(seed, encoding="utf-8")
    (repo / "scripts" / "app-start.sh").write_text(app_start, encoding="utf-8")
    for name, content in (extra or {}).items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    for script in ("bootstrap.sh", "seed.sh", "app-start.sh"):
        os.chmod(repo / "scripts" / script, 0o755)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, env=_GIT_ENV, check=True)
    return repo


def _base_sha(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


def _cfg(**overrides) -> dict:
    return config.environment_runtime_config({"runtime": overrides})


def _materialize(repo: Path, base_sha: str, cfg: dict, *, candidate_sha=None) -> dict:
    row = env.request_materialization(
        repo_root=repo, workspace=repo, base_sha=base_sha, candidate_sha=candidate_sha, cfg=cfg,
    )
    assert row is not None
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if env.reap_materializations(cfg):
            break
        time.sleep(0.05)
    return store.env_session_row(row["id"])


# --- Manifest parsing / path safety -----------------------------------------


def test_unknown_top_level_key_is_rejected():
    doc = {
        "schema": env.RUNTIME_SCHEMA,
        "bootstrap": {"argv": ["x"], "tracked_inputs": [], "network": "deny"},
        "app": {
            "start_argv": ["x"], "healthcheck": {"path": "/h", "expected_status": 200},
            "stop_signal": "TERM",
        },
        "seed": {"argv": ["x"]},
        "extra": True,
    }
    with pytest.raises(env.ManifestError):
        env.validate_runtime_manifest(doc)


def test_bad_stop_signal_is_rejected():
    doc = {
        "schema": env.RUNTIME_SCHEMA,
        "bootstrap": {"argv": ["x"], "tracked_inputs": [], "network": "deny"},
        "app": {
            "start_argv": ["x"], "healthcheck": {"path": "/h", "expected_status": 200},
            "stop_signal": "USR1",
        },
        "seed": {"argv": ["x"]},
    }
    with pytest.raises(env.ManifestError):
        env.validate_runtime_manifest(doc)


def test_symlinked_script_is_rejected(tmp_path):
    repo = _write_repo(tmp_path)
    os.symlink("/etc/passwd", repo / "scripts" / "evil.sh")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "symlink"], cwd=repo, env=_GIT_ENV, check=True)
    with pytest.raises(env.PathSafetyError):
        env._ls_tree_blob(repo, _base_sha(repo), "scripts/evil.sh")


def test_path_escaping_repo_is_rejected():
    with pytest.raises(env.PathSafetyError):
        env._repo_relative_path("../../etc/passwd", "bootstrap.argv")


def test_manifest_from_candidate_tree_attack_uses_trusted_base(tmp_path):
    """A candidate that rewrites its own bootstrap script never gets to run it."""
    repo = _write_repo(tmp_path, bootstrap="#!/bin/sh\necho BASE > out.txt\nexit 0\n")
    base_sha = _base_sha(repo)
    (repo / "scripts" / "bootstrap.sh").write_text(
        "#!/bin/sh\necho MALICIOUS > out.txt\nexit 0\n", encoding="utf-8",
    )
    os.chmod(repo / "scripts" / "bootstrap.sh", 0o755)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "candidate rewrite"], cwd=repo, env=_GIT_ENV, check=True)
    candidate_sha = _base_sha(repo)
    # The working tree now has the malicious script checked out; the daemon
    # must still resolve/execute the pinned base_sha bytes.
    cfg = _cfg()
    row = _materialize(repo, base_sha, cfg, candidate_sha=candidate_sha)
    assert row["state"] == "ready"
    assert row["control_plane_risk"] == 1
    assert "scripts/bootstrap.sh" in row["control_plane_paths"]
    assert (repo / "out.txt").read_text().strip() == "BASE"


# --- Materialization / staleness --------------------------------------------


def test_tracked_input_change_invalidates_materialization(tmp_path):
    repo = _write_repo(tmp_path, tracked_inputs=["deps.txt"], extra={"deps.txt": "v1\n"})
    base_sha = _base_sha(repo)
    cfg = _cfg()
    first = _materialize(repo, base_sha, cfg)
    assert first["state"] == "ready"

    (repo / "deps.txt").write_text("v2\n", encoding="utf-8")
    second = _materialize(repo, base_sha, cfg)
    assert second["state"] == "ready"
    assert second["key"] != first["key"]
    assert second["id"] != first["id"]


def test_repeat_request_reuses_ready_materialization(tmp_path):
    repo = _write_repo(tmp_path)
    base_sha = _base_sha(repo)
    cfg = _cfg()
    first = _materialize(repo, base_sha, cfg)
    second = env.request_materialization(repo_root=repo, workspace=repo, base_sha=base_sha, cfg=cfg)
    assert second["id"] == first["id"]


def test_bootstrap_timeout_fails_with_persisted_log(tmp_path):
    repo = _write_repo(tmp_path, bootstrap="#!/bin/sh\necho starting\nsleep 30\nexit 0\n")
    base_sha = _base_sha(repo)
    cfg = _cfg(bootstrap_timeout_seconds=1)
    row = _materialize(repo, base_sha, cfg)
    assert row["state"] == "failed"
    assert row["last_error"] == "bootstrap_timeout"
    assert "starting" in Path(row["stdout_path"]).read_text()
    # Lease released, so a fresh slot is immediately available.
    assert store.acquire_resource_lease("materialization_slot", 1, key="probe") is not None


def test_bootstrap_forked_orphan_is_reaped_via_process_group(tmp_path):
    repo = _write_repo(
        tmp_path,
        bootstrap="#!/bin/sh\n(sleep 30; echo should-not-happen > orphan.txt) &\nexit 0\n",
    )
    base_sha = _base_sha(repo)
    cfg = _cfg()
    row = _materialize(repo, base_sha, cfg)
    assert row["state"] == "ready"
    time.sleep(0.3)
    # The orphaned sleep was in the bootstrap's process group and must have
    # been killed alongside it, so it never reaches the sleep's tail command.
    assert not (repo / "orphan.txt").exists()


def test_cancel_during_seed_fails_and_releases_lease(tmp_path):
    repo = _write_repo(tmp_path, seed="#!/bin/sh\nsleep 30\nexit 0\n")
    base_sha = _base_sha(repo)
    cfg = _cfg(bootstrap_timeout_seconds=60)
    row = env.request_materialization(repo_root=repo, workspace=repo, base_sha=base_sha, cfg=cfg)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        env.reap_materializations(cfg)
        record = env._MATERIALIZING.get(row["id"])
        if record and record.get("phase") == "seed":
            break
        time.sleep(0.05)
    else:
        pytest.fail("seed phase never started")
    assert env.cancel_materialization(row["id"], reason="cancelled") is True
    final = store.env_session_row(row["id"])
    assert final["state"] == "failed"
    assert final["last_error"] == "cancelled"
    assert store.acquire_resource_lease("materialization_slot", 1, key="probe") is not None


def test_daemon_dies_before_spawn_recorded_is_treated_as_crashed(tmp_path):
    """DB-first ordering: a pid-less nonterminal row never actually spawned."""
    repo = _write_repo(tmp_path)
    base_sha = _base_sha(repo)
    store.insert_env_session(
        "orphan-row", key="k", base_sha=base_sha, candidate_sha=None,
        manifest_path=".shipfactory/runtime.yaml", manifest_blob_sha="deadbeef",
        tracked_input_hash="none", workspace_path=str(repo), control_plane_risk=False,
        control_plane_paths=[], lease_key="materialization_slot:orphan",
        stdout_path=None, stderr_path=None,
    )
    store.acquire_resource_lease(
        "materialization_slot", 1, key="materialization_slot:orphan",
    )
    env.restore_materializations()
    row = store.env_session_row("orphan-row")
    assert row["state"] == "failed"
    assert store.acquire_resource_lease("materialization_slot", 1, key="probe") is not None


# --- Port leasing ------------------------------------------------------------


def test_two_sessions_race_for_the_same_port(tmp_path):
    first = store.acquire_port_lease(19000, 19000, key="a")
    second = store.acquire_port_lease(19000, 19000, key="b")
    assert first == 19000
    assert second is None
    assert store.release_resource_lease("a") is True
    third = store.acquire_port_lease(19000, 19000, key="b")
    assert third == 19000


# --- App sessions -------------------------------------------------------------


def _ready_env(tmp_path, *, port_span: int = 1, **app_kwargs) -> tuple[Path, dict]:
    repo = _write_repo(tmp_path, **app_kwargs)
    base_sha = _base_sha(repo)
    port_min = next(_PORT_COUNTER)
    cfg = _cfg(
        port_min=port_min, port_max=port_min + port_span - 1,
        startup_timeout_seconds=3, shutdown_timeout_seconds=1,
    )
    row = _materialize(repo, base_sha, cfg)
    assert row["state"] == "ready"
    return repo, cfg, row


def _wait_for_app_state(app_id: str, cfg: dict, states: set[str], *, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    row = store.app_session_row(app_id)
    while time.monotonic() < deadline and row["state"] not in states:
        # We are the real OS parent of every child this module spawns (Popen
        # is never detached), so a killed-but-unreaped child sits as a
        # zombie that still answers process-identity probes. A real daemon
        # restart reparents live children to init, which reaps zombies
        # immediately; opportunistically reap here to keep the single-
        # process test harness behaviorally equivalent to that.
        try:
            os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            pass
        env.tick(cfg)
        row = store.app_session_row(app_id)
        time.sleep(0.05)
    return row


def test_app_healthcheck_and_stop_happy_path(tmp_path):
    repo, cfg, env_row = _ready_env(tmp_path)
    app = env.request_app_start(env_session_id=env_row["id"], request_key="r1", cfg=cfg)
    healthy = _wait_for_app_state(app["id"], cfg, {"healthy", "crashed"})
    assert healthy["state"] == "healthy"
    body = urllib.request.urlopen(healthy["app_url"], timeout=2).read()
    assert body == b"ok"

    assert env.request_stop(app["id"], cfg) is True
    stopped = _wait_for_app_state(app["id"], cfg, {"stopped", "crashed"})
    assert stopped["state"] == "stopped"
    assert store.acquire_resource_lease("port", 1, key="probe-port") is not None


def test_port_collision_second_session_queues_not_fails(tmp_path):
    repo, cfg, env_row = _ready_env(tmp_path)  # port range has exactly one slot
    first = env.request_app_start(env_session_id=env_row["id"], request_key="p1", cfg=cfg)
    healthy = _wait_for_app_state(first["id"], cfg, {"healthy", "crashed"})
    assert healthy["state"] == "healthy"

    second = env.request_app_start(env_session_id=env_row["id"], request_key="p2", cfg=cfg)
    env.tick(cfg)
    pending = store.app_session_row(second["id"])
    assert pending["state"] == "starting"
    assert pending["pid"] is None  # queued, not failed

    assert env.request_stop(first["id"], cfg) is True
    _wait_for_app_state(first["id"], cfg, {"stopped"})
    second_healthy = _wait_for_app_state(second["id"], cfg, {"healthy", "crashed"})
    assert second_healthy["state"] == "healthy"


def test_healthcheck_never_healthy_fails_and_releases_port(tmp_path):
    repo, cfg, env_row = _ready_env(tmp_path, app_start=_NEVER_BINDS)
    app = env.request_app_start(env_session_id=env_row["id"], request_key="nh1", cfg=cfg)
    crashed = _wait_for_app_state(app["id"], cfg, {"crashed", "healthy"}, timeout=8)
    assert crashed["state"] == "crashed"
    assert "healthcheck" in crashed["last_error"]
    assert store.acquire_resource_lease("port", 1, key="probe-port-2") is not None


def test_stop_escalates_to_kill_after_shutdown_timeout(tmp_path):
    repo, cfg, env_row = _ready_env(tmp_path, app_start=_IGNORES_TERM)
    app = env.request_app_start(env_session_id=env_row["id"], request_key="k1", cfg=cfg)
    started = _wait_for_app_state(app["id"], cfg, {"starting", "healthy", "crashed"}, timeout=3)
    assert app["id"] in env._APP_RUNNING
    assert env.request_stop(app["id"], cfg) is True
    stopped = _wait_for_app_state(app["id"], cfg, {"stopped", "crashed"}, timeout=8)
    assert stopped["state"] == "stopped"


def test_daemon_restart_adopts_live_app_session(tmp_path):
    repo, cfg, env_row = _ready_env(tmp_path)
    app = env.request_app_start(env_session_id=env_row["id"], request_key="ad1", cfg=cfg)
    healthy = _wait_for_app_state(app["id"], cfg, {"healthy", "crashed"})
    assert healthy["state"] == "healthy"

    # Simulate a fresh daemon process: drop all in-memory tracking.
    env._APP_RUNNING.clear()
    env.restore_app_sessions(cfg)
    assert app["id"] in env._APP_RUNNING
    row = store.app_session_row(app["id"])
    assert row["state"] == "healthy"
    # Healthcheck is still enforced post-adoption.
    record = env._APP_RUNNING[app["id"]]
    assert env._poll_health(record["port"], record["health_path"], record["expected_status"]) is True

    assert env.request_stop(app["id"], cfg) is True
    stopped = _wait_for_app_state(app["id"], cfg, {"stopped", "crashed"})
    assert stopped["state"] == "stopped"


def test_daemon_restart_with_dead_session_crashes_and_releases_port(tmp_path):
    repo, cfg, env_row = _ready_env(tmp_path)
    app = env.request_app_start(env_session_id=env_row["id"], request_key="dead1", cfg=cfg)
    healthy = _wait_for_app_state(app["id"], cfg, {"healthy", "crashed"})
    assert healthy["state"] == "healthy"

    record = env._APP_RUNNING.pop(app["id"])
    os.killpg(record["pid"], signal.SIGKILL)
    # Reap it ourselves (we are its parent via Popen) so the OS start-token
    # probe below observes a genuine mismatch rather than a lingering zombie.
    os.waitpid(record["pid"], 0)

    env.restore_app_sessions(cfg)
    row = store.app_session_row(app["id"])
    assert row["state"] == "crashed"
    assert store.acquire_resource_lease("port", 1, key="probe-dead") is not None


def test_stale_pid_is_never_blindly_killed(tmp_path):
    """A reused PID with a mismatched start token must not be signalled."""
    repo, cfg, env_row = _ready_env(tmp_path)
    app = env.request_app_start(env_session_id=env_row["id"], request_key="stale1", cfg=cfg)
    _wait_for_app_state(app["id"], cfg, {"healthy", "crashed"})
    record = env._APP_RUNNING.pop(app["id"])
    real_pid = record["pid"]
    os.killpg(real_pid, signal.SIGKILL)
    os.waitpid(real_pid, 0)

    killed = {"value": False}
    real_killpg = os.killpg

    def spy(pid, sig):
        if pid == real_pid:
            killed["value"] = True
        return real_killpg(pid, sig)

    import shipfactory.spawn as spawn_module
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(spawn_module, "_process_start_token", lambda pid: None)
        mp.setattr(os, "killpg", spy)
        env.restore_app_sessions(cfg)
    assert killed["value"] is False
    row = store.app_session_row(app["id"])
    assert row["state"] == "crashed"
