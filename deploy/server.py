"""HTTP wrapper for household MCP server.

Follows the Railway deployment pattern:
1. PathRewriteMiddleware rewrites POST/GET/DELETE on / to /mcp
2. HEAD / returns MCP-Protocol-Version header for Claude discovery
3. streamable_http_app() is the BASE app; custom routes prepended
4. JSON REST API at /api/* for the web UI
5. Static file serving for the web UI
"""

import json
import os
import sys

from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

# Add parent dir so we can import server module
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import (
    _format_task,
    _get_db,
    _now_iso,
    mcp,
    CADENCE_DAYS,
)

import uuid

# ---------------------------------------------------------------------------
# MCP path rewrite middleware
# ---------------------------------------------------------------------------
class PathRewriteMiddleware:
    """Rewrite / to /mcp for MCP JSON-RPC traffic.

    Claude's connector sends POST / but the SDK serves at /mcp.
    Only rewrites POST, GET, DELETE on / — leaves other paths alone.
    """
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] == "/" and scope["method"] in ("POST", "GET", "DELETE"):
            scope["path"] = "/mcp"
        await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# HEAD / — MCP protocol discovery
# ---------------------------------------------------------------------------
async def head_root(request: Request) -> Response:
    return Response(
        status_code=200,
        headers={"MCP-Protocol-Version": "2025-06-18"},
    )


# ---------------------------------------------------------------------------
# JSON REST API for web UI
# ---------------------------------------------------------------------------
async def api_list_tasks(request: Request) -> JSONResponse:
    conn = _get_db()
    rows = conn.execute("SELECT * FROM tasks ORDER BY title").fetchall()
    conn.close()
    tasks = [_format_task(r) for r in rows]
    tasks.sort(key=lambda t: (0 if t["status"] == "To Do" else 1, t["title"]))
    return JSONResponse(tasks)


async def api_add_task(request: Request) -> JSONResponse:
    body = await request.json()
    title = body.get("title", "").strip()
    cadence = body.get("cadence", "").lower().strip()
    notes = body.get("notes")

    if not title:
        return JSONResponse({"error": "title is required"}, status_code=400)
    if cadence not in CADENCE_DAYS:
        return JSONResponse({"error": f"cadence must be one of: {', '.join(CADENCE_DAYS)}"}, status_code=400)

    task_id = str(uuid.uuid4())[:8]
    conn = _get_db()
    conn.execute(
        "INSERT INTO tasks (id, title, cadence, notes, created_at) VALUES (?, ?, ?, ?, ?)",
        (task_id, title, cadence, notes, _now_iso()),
    )
    conn.commit()
    conn.close()
    return JSONResponse({"id": task_id, "title": title}, status_code=201)


async def api_edit_task(request: Request) -> JSONResponse:
    task_id = request.path_params["task_id"]
    body = await request.json()

    conn = _get_db()
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        conn.close()
        return JSONResponse({"error": "not found"}, status_code=404)

    updates = []
    values = []
    for field in ("title", "cadence", "notes"):
        if field in body and body[field] is not None:
            val = body[field].strip() if isinstance(body[field], str) else body[field]
            if field == "cadence" and val not in CADENCE_DAYS:
                conn.close()
                return JSONResponse({"error": f"cadence must be one of: {', '.join(CADENCE_DAYS)}"}, status_code=400)
            updates.append(f"{field} = ?")
            values.append(val)

    if not updates:
        conn.close()
        return JSONResponse({"error": "nothing to update"}, status_code=400)

    values.append(task_id)
    conn.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?", values)
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


async def api_complete_task(request: Request) -> JSONResponse:
    task_id = request.path_params["task_id"]
    conn = _get_db()
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        conn.close()
        return JSONResponse({"error": "not found"}, status_code=404)

    conn.execute("UPDATE tasks SET last_completed = ? WHERE id = ?", (_now_iso(), task_id))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


async def api_delete_task(request: Request) -> JSONResponse:
    task_id = request.path_params["task_id"]
    conn = _get_db()
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        conn.close()
        return JSONResponse({"error": "not found"}, status_code=404)

    conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


async def serve_index(request: Request) -> HTMLResponse:
    static_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
    with open(os.path.join(static_dir, "index.html")) as f:
        return HTMLResponse(f.read())


# ---------------------------------------------------------------------------
# Build the app
# ---------------------------------------------------------------------------
app = mcp.streamable_http_app()

# Prepend custom routes (order matters — checked before MCP routes)
custom_routes = [
    Route("/", head_root, methods=["HEAD"]),
    Route("/ui", serve_index, methods=["GET"]),
    Route("/api/tasks", api_list_tasks, methods=["GET"]),
    Route("/api/tasks", api_add_task, methods=["POST"]),
    Route("/api/tasks/{task_id}", api_edit_task, methods=["PUT"]),
    Route("/api/tasks/{task_id}/complete", api_complete_task, methods=["POST"]),
    Route("/api/tasks/{task_id}", api_delete_task, methods=["DELETE"]),
]

for i, route in enumerate(custom_routes):
    app.router.routes.insert(i, route)

# Add path rewrite middleware (for MCP traffic)
app.add_middleware(PathRewriteMiddleware)
