"""HTTP-level tests for the localhost Factory dashboard."""

from __future__ import annotations

import http.client
import threading

import pytest

from factory.dashboard.server import create_server, load_token, token_file


class Store:
    def __init__(self):
        self.paused = []

    def board_tasks(self):
        return [{"id": "T1", "title": "Build <safe>", "status": "ready", "assignee": "dev"}]

    def seats_snapshot(self):
        return [{"seat": "dev", "executor": "codex", "model": "gpt", "running": 1, "today_tokens": 42}]

    def costs_rollup(self, by, since):
        assert (by, since) == ("seat", 1)
        return [{"seat": "dev", "runs": 2, "tokens_total": 42}]

    def get_run(self, run_id):
        return {"id": run_id, "task_id": "T1", "tokens_total": 42}

    def run_log_tail(self, run_id):
        return "hello <world>"

    def set_seat_paused(self, seat, paused):
        self.paused.append((seat, paused))


def request(server, method, path):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    connection.request(method, path)
    response = connection.getresponse(); body = response.read().decode(); connection.close()
    return response.status, body


def running_server(store):
    try:
        server = create_server(0, store=store, token="secret")
    except PermissionError:
        pytest.skip("environment forbids localhost socket binding")
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    return server, thread


def test_token_is_persisted_under_factory_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    first = load_token(); second = load_token()
    assert first == second and len(first) >= 24
    assert token_file() == tmp_path / "factory" / "dashboard.token"


def test_auth_and_board_columns():
    server, thread = running_server(Store())
    try:
        assert request(server, "GET", "/")[0] == 401
        status, body = request(server, "GET", "/?token=secret")
        assert status == 200
        assert all(name in body for name in ("Todo", "Ready", "In Progress", "Review", "Done"))
        assert "Build &lt;safe&gt;" in body
    finally:
        server.shutdown(); server.server_close(); thread.join(2)


def test_seats_costs_and_run_are_rendered_and_escaped():
    server, thread = running_server(Store())
    try:
        assert "codex" in request(server, "GET", "/seats?token=secret")[1]
        assert "tokens_total" in request(server, "GET", "/costs?token=secret")[1]
        run = request(server, "GET", "/runs/7?token=secret")[1]
        assert "Run 7" in run and "hello &lt;world&gt;" in run
    finally:
        server.shutdown(); server.server_close(); thread.join(2)


def test_pause_post_uses_store_accessor():
    store = Store(); server, thread = running_server(store)
    try:
        assert request(server, "POST", "/pause?seat=dev&token=secret")[0] == 200
        assert store.paused == [("dev", True)]
    finally:
        server.shutdown(); server.server_close(); thread.join(2)
