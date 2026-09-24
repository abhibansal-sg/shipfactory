"""GraphRunner v1 Project/operator API contracts (Milestone D Task 15)."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from shipfactory import cli, graph_runner, graph_runtime, store


PLUGIN_API = Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"


def _recipe_text(instructions: str = "Ask the operator") -> str:
    return f"""name: approval-flow
start: approve
boxes:
  - id: approve
    name: Approve
    who: human
    instructions: {instructions}
  - id: finish
    name: Finish
    who: human
    instructions: Confirm delivery
    end: true
arrows:
  - from: approve
    result: approved
    to: [finish]
"""


def _client() -> TestClient:
    spec = importlib.util.spec_from_file_location("graph_v1_dashboard_api", PLUGIN_API)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/shipfactory")
    return TestClient(app)


@pytest.fixture
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    subprocess.run(["git", "-C", str(workspace), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(workspace), "config", "user.email", "test@example.com"], check=True)
    (workspace / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(workspace), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(workspace), "commit", "-qm", "fixture"], check=True)

    library = tmp_path / "recipes"
    v1 = library / "v1"
    v1.mkdir(parents=True)
    recipe_path = v1 / "approval-flow.yaml"
    recipe_path.write_text(_recipe_text(), encoding="utf-8")

    import shipfactory.config
    monkeypatch.setattr(
        shipfactory.config,
        "load_seats",
        lambda: SimpleNamespace(
            seats={},
            recipes={
                "enabled": True,
                "library_path": str(library),
                "projects_visual_recipes": {},
            },
        ),
    )

    from hermes_cli import projects_db
    with projects_db.connect_closing() as conn:
        conn.execute(
            "INSERT INTO projects(id,slug,name,board_slug,primary_path,created_at,archived) "
            "VALUES(?,?,?,?,?,?,0)",
            ("project-1", "project-1", "Project One", "board-one", str(workspace), 1),
        )
        conn.commit()
    store.init_db()
    return _client(), workspace, recipe_path


def _attach(client: TestClient, *, enabled: bool = True, is_default: bool = True):
    return client.put(
        "/api/plugins/shipfactory/v1/projects/project-1/recipes/approval-flow",
        json={"enabled": enabled, "is_default": is_default},
    )


def _launch(client: TestClient, key: str, request: str = "Ship this"):
    return client.post(
        "/api/plugins/shipfactory/v1/projects/project-1/runs",
        json={"recipe": "approval-flow", "request": request, "launch_key": key},
    )


def test_v1_api_route_set_is_complete(api):
    client, _workspace, _recipe_path = api
    routes = {
        (method, route.path)
        for route in client.app.routes
        for method in getattr(route, "methods", set())
        if route.path.startswith("/api/plugins/shipfactory/v1/")
    }
    assert routes == {
        ("GET", "/api/plugins/shipfactory/v1/recipes"),
        ("POST", "/api/plugins/shipfactory/v1/recipes"),
        ("GET", "/api/plugins/shipfactory/v1/recipes/{name}"),
        ("GET", "/api/plugins/shipfactory/v1/projects/{project_id}/recipes"),
        ("PUT", "/api/plugins/shipfactory/v1/projects/{project_id}/recipes/{name}"),
        ("POST", "/api/plugins/shipfactory/v1/projects/{project_id}/runs"),
        ("GET", "/api/plugins/shipfactory/v1/runs"),
        ("GET", "/api/plugins/shipfactory/v1/runs/{run_id}"),
        ("GET", "/api/plugins/shipfactory/v1/runs/{run_id}/graph"),
        ("POST", "/api/plugins/shipfactory/v1/runs/{run_id}/cancel"),
        ("POST", "/api/plugins/shipfactory/v1/human-boxes/{attempt_id}/decision"),
        ("GET", "/api/plugins/shipfactory/v1/legacy-drain-report"),
    }


def test_v1_recipe_and_project_gets_are_readonly(api, monkeypatch):
    client, _workspace, _recipe_path = api
    assert _attach(client).status_code == 200

    def forbidden(*_args, **_kwargs):
        raise AssertionError("read-only GET initialized or migrated a database")

    monkeypatch.setattr(store, "init_db", forbidden)
    listed = client.get("/api/plugins/shipfactory/v1/recipes")
    shown = client.get("/api/plugins/shipfactory/v1/recipes/approval-flow")
    attached = client.get("/api/plugins/shipfactory/v1/projects/project-1/recipes")

    assert listed.status_code == shown.status_code == attached.status_code == 200
    assert [item["name"] for item in listed.json()["recipes"]] == ["approval-flow"]
    assert shown.json()["recipe"]["hash"] == listed.json()["recipes"][0]["hash"]
    assert attached.json()["recipes"] == [{
        "name": "approval-flow", "enabled": True, "is_default": True,
    }]


def test_project_recipe_allowlist_and_default_rules(api):
    client, _workspace, _recipe_path = api
    missing = client.put(
        "/api/plugins/shipfactory/v1/projects/project-1/recipes/missing",
        json={"enabled": True, "is_default": False},
    )
    invalid_default = _attach(client, enabled=False, is_default=True)
    assert missing.status_code == 404
    assert invalid_default.status_code == 422

    assert _attach(client).status_code == 200
    disabled = _attach(client, enabled=False, is_default=False)
    assert disabled.status_code == 200
    assert _launch(client, "disabled-key").status_code == 409


def test_launch_replay_freezes_recipe_and_uses_project_workspace(api, monkeypatch):
    client, workspace, recipe_path = api
    assert _attach(client).status_code == 200
    unrelated_cwd = workspace.parent / "not-the-project"
    unrelated_cwd.mkdir()
    monkeypatch.chdir(unrelated_cwd)

    first = _launch(client, "launch-1")
    replay = _launch(client, "launch-1")
    conflict = _launch(client, "launch-1", request="different")
    assert first.status_code == replay.status_code == 200
    assert replay.json()["run"]["id"] == first.json()["run"]["id"]
    assert conflict.status_code == 409
    assert first.json()["run"]["workspace_path"] == str(workspace.resolve())

    listed = client.get(
        "/api/plugins/shipfactory/v1/runs",
        params={"project_id": "project-1", "state": "running"},
    )
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["runs"]] == [first.json()["run"]["id"]]

    first_hash = first.json()["run"]["recipe_hash"]
    recipe_path.write_text(_recipe_text("Ask again after edit"), encoding="utf-8")
    second = _launch(client, "launch-2")
    assert second.status_code == 200
    assert second.json()["run"]["recipe_hash"] != first_hash

    shown = client.get(f"/api/plugins/shipfactory/v1/runs/{first.json()['run']['id']}")
    assert shown.status_code == 200
    frozen = json.loads(shown.json()["run"]["recipe_snapshot_json"])
    assert frozen["boxes"][0]["instructions"] == "Ask the operator"


def test_run_graph_and_human_decision_boundaries(api):
    client, _workspace, _recipe_path = api
    assert _attach(client).status_code == 200
    launched = _launch(client, "decision-run").json()["run"]
    with store._connect() as db:
        graph_runner.reconcile_run(db, launched["id"])
    graph_runtime.spawn_ready(1, board="board-one")

    graph = client.get(f"/api/plugins/shipfactory/v1/runs/{launched['id']}/graph")
    assert graph.status_code == 200
    waiting = graph.json()["run"]["waiting_human"]
    assert len(waiting) == 1
    attempt_id = waiting[0]["id"]

    invalid = client.post(
        f"/api/plugins/shipfactory/v1/human-boxes/{attempt_id}/decision",
        json={
            "result": "not-declared", "nonce": "nonce-invalid",
            "actor_kind": "human", "actor_id": "abhi", "channel": "dashboard",
        },
    )
    assert invalid.status_code == 422

    accepted = client.post(
        f"/api/plugins/shipfactory/v1/human-boxes/{attempt_id}/decision",
        json={
            "result": "approved", "nonce": "nonce-1",
            "actor_kind": "human", "actor_id": "abhi", "channel": "dashboard",
        },
    )
    replay = client.post(
        f"/api/plugins/shipfactory/v1/human-boxes/{attempt_id}/decision",
        json={
            "result": "approved", "nonce": "nonce-1",
            "actor_kind": "human", "actor_id": "abhi", "channel": "dashboard",
        },
    )
    conflict = client.post(
        f"/api/plugins/shipfactory/v1/human-boxes/{attempt_id}/decision",
        json={
            "result": "approved", "nonce": "nonce-2",
            "actor_kind": "human", "actor_id": "abhi", "channel": "dashboard",
        },
    )
    assert accepted.status_code == replay.status_code == 200
    assert accepted.json()["decision"]["id"] == replay.json()["decision"]["id"]
    assert conflict.status_code == 409


def test_v1_cli_recipe_and_run_commands_preserve_idempotency(api):
    client, workspace, _recipe_path = api
    assert _attach(client).status_code == 200

    recipes = cli.main(["recipe", "list", "--v1"])
    shown_recipe = cli.main(["recipe", "show", "approval-flow", "--v1"])
    assert [item["name"] for item in recipes] == ["approval-flow"]
    assert shown_recipe["hash"] == recipes[0]["hash"]

    started = cli.main([
        "run", "start", "--project", "project-1",
        "--recipe", "approval-flow", "--request", "From CLI",
        "--launch-key", "cli-launch",
    ])
    replay = cli.main([
        "run", "start", "--project", "project-1",
        "--recipe", "approval-flow", "--request", "From CLI",
        "--launch-key", "cli-launch",
    ])
    listed = cli.main(["run", "list", "--project", "project-1"])
    shown = cli.main(["run", "show", started["id"]])

    assert replay["id"] == started["id"]
    assert [item["id"] for item in listed] == [started["id"]]
    assert shown["workspace_path"] == str(workspace.resolve())


def _publish_document(name: str = "publish-flow",
                      instructions: str = "Do the work") -> dict:
    return {
        "name": name,
        "start": "work",
        "boxes": [
            {
                "id": "work",
                "name": "Work",
                "who": "worker",
                "instructions": instructions,
            },
            {
                "id": "finish",
                "name": "Finish",
                "who": "human",
                "instructions": "Confirm delivery",
                "end": True,
            },
        ],
        "arrows": [
            {"from": "work", "result": "done", "to": ["finish"]},
        ],
    }


def _publish(client: TestClient, name: str, document: dict):
    return client.post(
        "/api/plugins/shipfactory/v1/recipes",
        json={"name": name, "document": document},
    )


def test_publish_graph_recipe_v1_writes_new_recipe(api):
    client, _workspace, _recipe_path = api
    response = _publish(client, "publish-flow", _publish_document())
    assert response.status_code == 201
    assert response.json()["recipe"]["name"] == "publish-flow"
    assert response.json()["recipe"]["start"] == "work"

    shown = client.get("/api/plugins/shipfactory/v1/recipes/publish-flow")
    assert shown.status_code == 200
    assert shown.json()["recipe"]["hash"] == response.json()["recipe"]["hash"]
    listed = client.get("/api/plugins/shipfactory/v1/recipes")
    assert [item["name"] for item in listed.json()["recipes"]] == [
        "approval-flow", "publish-flow",
    ]


def test_publish_graph_recipe_v1_rejects_invalid_name(api):
    client, _workspace, _recipe_path = api
    response = _publish(client, "Bad_Name", _publish_document(name="Bad_Name"))
    assert response.status_code == 422
    assert response.json()["field"] == "name"


def test_publish_graph_recipe_v1_rejects_invalid_document(api):
    client, _workspace, _recipe_path = api
    document = _publish_document()
    document["boxes"][0].pop("who")
    response = _publish(client, "publish-flow", document)
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_recipe"


def test_publish_graph_recipe_v1_rejects_name_mismatch(api):
    client, _workspace, _recipe_path = api
    response = _publish(client, "publish-flow", _publish_document(name="other-flow"))
    assert response.status_code == 422


def test_publish_graph_recipe_v1_rejects_v2_document(api):
    client, _workspace, _recipe_path = api
    document = _publish_document(name="v2-flow")
    document["version"] = 2
    document["capability_sets"] = {
        "basic": {"skills": [], "toolsets": [], "plugins": []},
    }
    for box in document["boxes"]:
        box["workspace"] = {"lane": "build", "access": "read"}
        box["capabilities"] = "basic"
    response = _publish(client, "v2-flow", document)
    assert response.status_code == 422
    assert response.json()["message"] == (
        "v2 recipes are not yet executable (SF-28..31)"
    )


def test_publish_graph_recipe_v1_conflict_and_idempotent_replay(api):
    client, _workspace, recipe_path = api
    document = _publish_document()
    first = _publish(client, "publish-flow", document)
    assert first.status_code == 201
    target = recipe_path.parent / "publish-flow.yaml"
    original = target.read_text(encoding="utf-8")

    conflict = _publish(
        client, "publish-flow", _publish_document(instructions="Changed"),
    )
    assert conflict.status_code == 409
    assert target.read_text(encoding="utf-8") == original

    replay = _publish(client, "publish-flow", document)
    assert replay.status_code == 200
    assert replay.json()["recipe"]["hash"] == first.json()["recipe"]["hash"]


# ---------------------------------------------------------------------------
# Same-origin Factory Workflow designer (served as a static plugin asset).
#
# The Hermes dashboard host serves any file under a plugin's dashboard/
# directory at GET /dashboard-plugins/<name>/<path> with a browser-asset
# suffix allowlist.  The standalone designer build lives in
# dashboard/designer/ and the dashboard bundle embeds it in an iframe pane.
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
DESIGNER_DIR = REPO_ROOT / "dashboard" / "designer"


@pytest.fixture
def host_client(monkeypatch: pytest.MonkeyPatch):
    """TestClient against the real Hermes dashboard app.

    Points the hermetic HERMES_HOME (created by the autouse conftest
    fixture) at this repository via a user-plugin symlink, enables the
    plugin in config.yaml, and forces a fresh plugin scan.
    """
    home = Path(os.environ["HERMES_HOME"])
    plugins_root = home / "plugins"
    plugins_root.mkdir(parents=True, exist_ok=True)
    link = plugins_root / "shipfactory"
    if not link.exists():
        link.symlink_to(REPO_ROOT)
    (home / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - shipfactory\n", encoding="utf-8"
    )
    from hermes_cli import web_server

    web_server._get_dashboard_plugins(force_rescan=True)
    from fastapi.testclient import TestClient

    client = TestClient(web_server.app)
    try:
        yield client
    finally:
        web_server._dashboard_plugins_cache = None


def test_designer_page_served_same_origin(host_client: TestClient):
    response = host_client.get(
        "/dashboard-plugins/shipfactory/designer/index.html"
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert '<div id="root">' in response.text
    # Assets must be referenced relatively so the page keeps working under
    # the /dashboard-plugins/shipfactory/designer/ prefix (and any
    # X-Forwarded-Prefix rewrite).
    assert "./assets/" in response.text


def test_designer_js_asset_served_with_javascript_content_type(
    host_client: TestClient,
):
    scripts = sorted(DESIGNER_DIR.glob("assets/*.js"))
    assert scripts, "designer build must ship a bundled JS asset"
    asset = scripts[0]
    response = host_client.get(
        f"/dashboard-plugins/shipfactory/designer/assets/{asset.name}"
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/javascript")
    assert response.content == asset.read_bytes()


def test_designer_unknown_asset_404(host_client: TestClient):
    response = host_client.get(
        "/dashboard-plugins/shipfactory/designer/assets/missing-asset.js"
    )
    assert response.status_code == 404


def test_designer_asset_traversal_is_blocked(host_client: TestClient):
    # plugin_api.py sits next to designer/ — traversal must never serve it
    # (and .py is outside the browser-asset allowlist regardless).
    response = host_client.get(
        "/dashboard-plugins/shipfactory/designer/../plugin_api.py"
    )
    assert response.status_code in (403, 404)


def test_dashboard_bundle_registers_workflows_tab():
    bundle = (REPO_ROOT / "dashboard" / "dist" / "index.js").read_text(
        encoding="utf-8"
    )
    assert re.search(r'id:\s*"workflows"', bundle)
    assert 'label: "Workflows"' in bundle
    assert all(f'label: "{label}"' in bundle for label in (
        "Projects", "Workflows", "Runs", "Settings",
    ))
    assert "/dashboard-plugins/shipfactory/designer/index.html" in bundle
    assert "iframe" in bundle


def test_project_screen_opens_designer_with_project_identity():
    bundle = (REPO_ROOT / "dashboard" / "dist" / "index.js").read_text(
        encoding="utf-8"
    )
    assert 'data-project-new-workflow' in bundle
    assert 'props.onCreateWorkflow(project)' in bundle
    assert '"sf-project-id"' in bundle
    assert '"sf-project-name"' in bundle
    assert 'h(DesignerView, { project: designerProject' in bundle
