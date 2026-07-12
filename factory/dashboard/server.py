"""Localhost-only HTTP server for the Hermes Factory dashboard."""

from __future__ import annotations

import os
import secrets
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import templates


def _root() -> Path:
    """Return the Factory state root."""
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "factory"


def token_file() -> Path:
    """Return the dashboard token file below Factory state."""
    return _root() / "dashboard.token"


def load_token() -> str:
    """Read or securely create the persistent dashboard bearer token."""
    path = token_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        token = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        token = ""
    if not token:
        token = secrets.token_urlsafe(24)
        path.write_text(token + "\n", encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
    return token


def _call(store, names: tuple[str, ...], *args, default=None):
    """Call the first optional store read accessor available."""
    for name in names:
        function = getattr(store, name, None)
        if function:
            return function(*args)
    return default


def _handler(store, token: str):
    class Handler(BaseHTTPRequestHandler):
        def _authorized(self) -> bool:
            return parse_qs(urlsplit(self.path).query).get("token", [""])[0] == token

        def _send(self, content: str, status: HTTPStatus = HTTPStatus.OK) -> None:
            data = content.encode("utf-8")
            self.send_response(status); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(data))); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(data)

        def _guard(self) -> bool:
            if self._authorized(): return True
            self._send("Unauthorized", HTTPStatus.UNAUTHORIZED); return False

        def do_GET(self) -> None:
            if not self._guard(): return
            path = urlsplit(self.path).path
            if path == "/":
                html = templates.board(_call(store, ("board_tasks", "list_tasks", "tasks"), default=[]), token)
            elif path == "/seats":
                rows = _call(store, ("seats_dashboard", "seats_snapshot", "seat_stats"), default=[])
                if not rows:
                    try:
                        from factory.config import load_seats
                        rows = [vars(seat) | {"paused": store.seat_paused(seat.name)} for seat in load_seats().seats.values()]
                    except Exception:
                        rows = []
                html = templates.table_page("Seats", rows, token)
            elif path == "/costs":
                html = templates.table_page("Costs", store.costs_rollup("seat", 1), token)
            elif path.startswith("/runs/"):
                run_id = path.removeprefix("/runs/")
                try: key = int(run_id)
                except ValueError: key = run_id
                run = _call(store, ("get_run", "run_by_id"), key, default=None)
                log = _call(store, ("run_log_tail",), key, default=(run or {}).get("log", ""))
                html = templates.run_page(run, log, token)
            else:
                self._send("Not found", HTTPStatus.NOT_FOUND); return
            self._send(html)

        def do_POST(self) -> None:
            if not self._guard(): return
            parsed = urlsplit(self.path)
            if parsed.path != "/pause": self._send("Not found", HTTPStatus.NOT_FOUND); return
            seat = parse_qs(parsed.query).get("seat", [""])[0]
            if not seat: self._send("Missing seat", HTTPStatus.BAD_REQUEST); return
            store.set_seat_paused(seat, True)
            self._send(templates.page("Seat paused", f"<p>Paused {escape(seat)}</p>", token))

        def log_message(self, _format: str, *_args) -> None:
            pass
    return Handler


def create_server(port: int = 18820, *, store=None, token: str | None = None) -> ThreadingHTTPServer:
    """Create a localhost-only dashboard server; port zero selects an ephemeral port."""
    if store is None:
        from factory import store
    return ThreadingHTTPServer(("127.0.0.1", port), _handler(store, token or load_token()))


def serve(port: int = 18820) -> None:
    """Serve the dashboard until interrupted."""
    token = load_token(); server = create_server(port, token=token)
    print(f"Factory dashboard: http://127.0.0.1:{server.server_port}/?token={token}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


__all__ = ["create_server", "load_token", "serve", "token_file"]
