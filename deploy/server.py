"""HTTP wrapper for household MCP server.

FastMCP 3.x approach:
1. http_app(path="/") serves MCP at root — no path rewrite middleware needed
2. HEAD / handler returns MCP-Protocol-Version for Claude discovery
3. JSON REST API at /api/* for the web UI
4. Static serving for the web UI at /ui
"""

import os
import sys
import uuid

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

# Add parent dir so we can import server module
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import (
    _format_task,
    _get_db,
    _now_iso,
    _sort_tasks,
    mcp,
    CADENCE_DAYS,
    VALID_CADENCES,
)


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
    tasks = _sort_tasks([_format_task(r) for r in rows])
    return JSONResponse(tasks)


async def api_add_task(request: Request) -> JSONResponse:
    body = await request.json()
    title = body.get("title", "").strip()
    cadence = body.get("cadence", "once").lower().strip()
    notes = body.get("notes")

    if not title:
        return JSONResponse({"error": "title is required"}, status_code=400)
    if cadence not in VALID_CADENCES:
        return JSONResponse({"error": f"cadence must be one of: {', '.join(VALID_CADENCES)}"}, status_code=400)

    db_cadence = None if cadence == "once" else cadence
    task_id = str(uuid.uuid4())[:8]
    conn = _get_db()
    max_order = conn.execute("SELECT COALESCE(MAX(sort_order), 0) FROM tasks WHERE cadence IS NULL").fetchone()[0]
    sort_order = max_order + 1 if db_cadence is None else 0
    conn.execute(
        "INSERT INTO tasks (id, title, cadence, notes, sort_order, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (task_id, title, db_cadence, notes, sort_order, _now_iso()),
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
            if field == "cadence":
                if val not in VALID_CADENCES:
                    conn.close()
                    return JSONResponse({"error": f"cadence must be one of: {', '.join(VALID_CADENCES)}"}, status_code=400)
                val = None if val == "once" else val
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


async def api_reorder_tasks(request: Request) -> JSONResponse:
    """Reorder one-time tasks. Body: {"task_ids": ["id1", "id2", ...]}"""
    body = await request.json()
    task_ids = body.get("task_ids", [])
    if not task_ids:
        return JSONResponse({"error": "task_ids required"}, status_code=400)

    conn = _get_db()
    for i, tid in enumerate(task_ids):
        conn.execute("UPDATE tasks SET sort_order = ? WHERE id = ? AND cadence IS NULL", (i, tid))
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
# http_app(path="/") serves MCP JSON-RPC at / — no rewrite needed
app = mcp.http_app(path="/")

# Prepend custom routes (checked before MCP routes)
custom_routes = [
    Route("/", head_root, methods=["HEAD"]),
    Route("/ui", serve_index, methods=["GET"]),
    Route("/api/tasks", api_list_tasks, methods=["GET"]),
    Route("/api/tasks", api_add_task, methods=["POST"]),
    Route("/api/tasks/reorder", api_reorder_tasks, methods=["POST"]),
    Route("/api/tasks/{task_id}", api_edit_task, methods=["PUT"]),
    Route("/api/tasks/{task_id}/complete", api_complete_task, methods=["POST"]),
    Route("/api/tasks/{task_id}", api_delete_task, methods=["DELETE"]),
]

for i, route in enumerate(custom_routes):
    app.router.routes.insert(i, route)
