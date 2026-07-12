"""Inline HTML templates for the dependency-free Factory dashboard."""

from __future__ import annotations

from html import escape
from typing import Any

STYLE = """body{background:#0b1020;color:#dbe5ff;font:14px system-ui;margin:0}header{padding:18px 28px;background:#111936}nav a{color:#8fc7ff;margin-right:18px}main{padding:24px}table{border-collapse:collapse;width:100%}th,td{padding:10px;border-bottom:1px solid #293453;text-align:left}.board{display:grid;grid-template-columns:repeat(5,1fr);gap:12px}.column{background:#121a30;border:1px solid #293453;border-radius:8px;padding:10px;min-height:120px}.card{background:#1a2541;margin:8px 0;padding:9px;border-radius:6px}pre{white-space:pre-wrap;background:#050914;padding:16px;border-radius:8px}.muted{color:#8390ad}button{background:#d66b6b;color:#fff;border:0;padding:7px 12px;border-radius:5px}@media(max-width:850px){.board{grid-template-columns:1fr}}"""


def page(title: str, body: str, token: str) -> str:
    """Wrap a dashboard fragment in the common dark-themed shell."""
    query = "?token=" + escape(token, quote=True)
    nav = " ".join(f'<a href="{path}{query}">{label}</a>' for path, label in (("/", "Board"), ("/seats", "Seats"), ("/costs", "Costs")))
    return f'<!doctype html><html><head><meta charset="utf-8"><title>{escape(title)}</title><style>{STYLE}</style></head><body><header><b>Hermes Factory</b><nav>{nav}</nav></header><main>{body}</main></body></html>'


def board(tasks: list[dict[str, Any]], token: str) -> str:
    """Render tasks in the five specified workflow columns."""
    columns = []
    for status in ("todo", "ready", "in_progress", "review", "done"):
        cards = "".join(f'<div class="card"><b>{escape(str(t.get("title", t.get("id", "Untitled"))))}</b><br><span class="muted">{escape(str(t.get("assignee", "unassigned")))}</span></div>' for t in tasks if t.get("status") == status)
        columns.append(f'<section class="column"><h2>{escape(status.replace("_", " ").title())}</h2>{cards}</section>')
    return page("Factory Board", '<div class="board">' + "".join(columns) + "</div>", token)


def table_page(title: str, rows: list[dict[str, Any]], token: str) -> str:
    """Render arbitrary accessor rows as an escaped table."""
    keys = list(dict.fromkeys(key for row in rows for key in row))
    head = "".join(f"<th>{escape(str(key))}</th>" for key in keys)
    body = "".join("<tr>" + "".join(f"<td>{escape(str(row.get(key, '')))}</td>" for key in keys) + "</tr>" for row in rows)
    empty = '<p class="muted">No data yet.</p>' if not rows else ""
    return page(title, f"<h1>{escape(title)}</h1>{empty}<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>", token)


def run_page(run: dict[str, Any] | None, log_tail: str, token: str) -> str:
    """Render one run's metadata, usage, task link, and escaped log tail."""
    if not run:
        return page("Run not found", "<h1>Run not found</h1>", token)
    task = escape(str(run.get("task_id", "")))
    meta = " · ".join(escape(f"{key}: {value}") for key, value in run.items() if key != "log")
    return page(f"Run {run.get('id', '')}", f"<h1>Run {escape(str(run.get('id', '')))}</h1><p>{meta}</p><p>Task: {task}</p><pre>{escape(log_tail)}</pre>", token)


__all__ = ["board", "page", "run_page", "table_page"]
