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
    _format_packing_item,
    _get_db,
    _list_bags,
    _ensure_bag,
    _now_iso,
    _sort_tasks,
    _canonical_status,
    mcp,
    CADENCE_DAYS,
    VALID_CADENCES,
    PACKING_STATUSES,
    PACKING_NEXT_STATUS,
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
    due_date = body.get("due_date")

    if not title:
        return JSONResponse({"error": "title is required"}, status_code=400)
    if cadence not in VALID_CADENCES:
        return JSONResponse({"error": f"cadence must be one of: {', '.join(VALID_CADENCES)}"}, status_code=400)

    db_cadence = None if cadence == "once" else cadence
    # due_date only applies to one-time tasks
    db_due_date = due_date if db_cadence is None else None
    task_id = str(uuid.uuid4())[:8]
    conn = _get_db()
    max_order = conn.execute("SELECT COALESCE(MAX(sort_order), 0) FROM tasks WHERE cadence IS NULL").fetchone()[0]
    sort_order = max_order + 1 if db_cadence is None else 0
    conn.execute(
        "INSERT INTO tasks (id, title, cadence, notes, sort_order, due_date, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (task_id, title, db_cadence, notes, sort_order, db_due_date, _now_iso()),
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

    # Handle due_date separately (can be null/empty to clear)
    if "due_date" in body:
        updates.append("due_date = ?")
        values.append(body["due_date"] if body["due_date"] else None)

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


# ---------------------------------------------------------------------------
# Packing list REST API
# ---------------------------------------------------------------------------
async def api_list_packing_items(request: Request) -> JSONResponse:
    conn = _get_db()
    rows = conn.execute("SELECT * FROM packing_items").fetchall()
    items = [_format_packing_item(r) for r in rows]
    conn.close()
    return JSONResponse(items)


async def api_add_packing_item(request: Request) -> JSONResponse:
    body = await request.json()
    title = (body.get("title") or "").strip()
    bag_raw = body.get("bag")
    bag = bag_raw.strip() if isinstance(bag_raw, str) and bag_raw.strip() else None
    status_in = body.get("status") or "Have"
    status = _canonical_status(status_in)
    priority = body.get("priority")

    if not title:
        return JSONResponse({"error": "title is required"}, status_code=400)
    if status is None:
        return JSONResponse(
            {"error": f"status must be one of: {', '.join(PACKING_STATUSES)}"}, status_code=400
        )
    if priority not in (None, 1, 2, 3):
        return JSONResponse({"error": "priority must be 1, 2, 3, or null"}, status_code=400)

    item_id = str(uuid.uuid4())[:8]
    conn = _get_db()
    if bag:
        _ensure_bag(conn, bag)
    max_order = conn.execute("SELECT COALESCE(MAX(sort_order), 0) FROM packing_items").fetchone()[0]
    conn.execute(
        "INSERT INTO packing_items (id, title, status, bag, priority, sort_order, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (item_id, title, status, bag, priority, max_order + 1, _now_iso()),
    )
    conn.commit()
    conn.close()
    return JSONResponse({"id": item_id, "title": title}, status_code=201)


async def api_edit_packing_item(request: Request) -> JSONResponse:
    item_id = request.path_params["item_id"]
    body = await request.json()

    conn = _get_db()
    row = conn.execute("SELECT * FROM packing_items WHERE id = ?", (item_id,)).fetchone()
    if not row:
        conn.close()
        return JSONResponse({"error": "not found"}, status_code=404)

    updates, values = [], []
    if "title" in body and body["title"] is not None:
        title = body["title"].strip()
        if not title:
            conn.close()
            return JSONResponse({"error": "title cannot be empty"}, status_code=400)
        updates.append("title = ?")
        values.append(title)
    if "status" in body and body["status"] is not None:
        canonical = _canonical_status(body["status"])
        if canonical is None:
            conn.close()
            return JSONResponse(
                {"error": f"status must be one of: {', '.join(PACKING_STATUSES)}"}, status_code=400
            )
        updates.append("status = ?")
        values.append(canonical)
    if "bag" in body:
        bag_raw = body["bag"]
        bag = bag_raw.strip() if isinstance(bag_raw, str) and bag_raw.strip() else None
        if bag:
            _ensure_bag(conn, bag)
        updates.append("bag = ?")
        values.append(bag)
    if "priority" in body:
        p = body["priority"]
        if p in ("", None):
            updates.append("priority = ?")
            values.append(None)
        elif p in (1, 2, 3, "1", "2", "3"):
            updates.append("priority = ?")
            values.append(int(p))
        else:
            conn.close()
            return JSONResponse({"error": "priority must be 1, 2, 3, or null"}, status_code=400)

    if not updates:
        conn.close()
        return JSONResponse({"error": "nothing to update"}, status_code=400)

    values.append(item_id)
    conn.execute(f"UPDATE packing_items SET {', '.join(updates)} WHERE id = ?", values)
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


async def api_delete_packing_item(request: Request) -> JSONResponse:
    item_id = request.path_params["item_id"]
    conn = _get_db()
    row = conn.execute("SELECT * FROM packing_items WHERE id = ?", (item_id,)).fetchone()
    if not row:
        conn.close()
        return JSONResponse({"error": "not found"}, status_code=404)
    conn.execute("DELETE FROM packing_items WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


async def api_bulk_add_packing_items(request: Request) -> JSONResponse:
    """Bulk add packing items. Body: {"items": [{title, status, bag, priority}, ...]}.

    Partial success: each row is validated independently. Returns counts
    plus per-row errors so the user knows what to fix.
    """
    body = await request.json()
    items = body.get("items")
    if not isinstance(items, list):
        return JSONResponse({"error": "items must be a list"}, status_code=400)

    added = 0
    errors = []
    conn = _get_db()
    try:
        max_order = conn.execute(
            "SELECT COALESCE(MAX(sort_order), 0) FROM packing_items"
        ).fetchone()[0]
        for idx, raw in enumerate(items, start=1):
            try:
                if not isinstance(raw, dict):
                    raise ValueError("row must be an object")
                title = (raw.get("title") or "").strip() if isinstance(raw.get("title"), str) else ""
                bag_raw = raw.get("bag")
                bag = bag_raw.strip() if isinstance(bag_raw, str) and bag_raw.strip() else None
                status_in = raw.get("status")
                if status_in in (None, ""):
                    status = "Have"
                else:
                    status = _canonical_status(status_in)
                p_raw = raw.get("priority")
                if p_raw in (None, "", "None"):
                    priority = None
                else:
                    try:
                        priority = int(p_raw)
                    except (ValueError, TypeError):
                        raise ValueError(f"invalid priority {p_raw!r}")
                    if priority not in (1, 2, 3):
                        raise ValueError("priority must be 1, 2, or 3")

                if not title:
                    raise ValueError("title is required")
                if status is None:
                    raise ValueError(f"unknown status {status_in!r}")

                if bag:
                    _ensure_bag(conn, bag)
                max_order += 1
                item_id = str(uuid.uuid4())[:8]
                conn.execute(
                    "INSERT INTO packing_items (id, title, status, bag, priority, sort_order, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (item_id, title, status, bag, priority, max_order, _now_iso()),
                )
                added += 1
            except Exception as e:
                errors.append({"row": idx, "error": str(e)})
        conn.commit()
    finally:
        conn.close()
    return JSONResponse({"added": added, "skipped": len(errors), "errors": errors})


async def api_advance_packing_item(request: Request) -> JSONResponse:
    item_id = request.path_params["item_id"]
    conn = _get_db()
    row = conn.execute("SELECT * FROM packing_items WHERE id = ?", (item_id,)).fetchone()
    if not row:
        conn.close()
        return JSONResponse({"error": "not found"}, status_code=404)
    nxt = PACKING_NEXT_STATUS.get(row["status"])
    if nxt is None:
        conn.close()
        return JSONResponse({"error": "already packed"}, status_code=400)
    conn.execute("UPDATE packing_items SET status = ? WHERE id = ?", (nxt, item_id))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "status": nxt})


async def api_list_packing_bags(request: Request) -> JSONResponse:
    conn = _get_db()
    bags = _list_bags(conn)
    conn.close()
    return JSONResponse(bags)


async def api_add_packing_bag(request: Request) -> JSONResponse:
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    conn = _get_db()
    _ensure_bag(conn, name)
    conn.commit()
    bags = _list_bags(conn)
    conn.close()
    return JSONResponse({"ok": True, "bags": bags}, status_code=201)


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
    # Packing list
    Route("/api/packing/items", api_list_packing_items, methods=["GET"]),
    Route("/api/packing/items", api_add_packing_item, methods=["POST"]),
    Route("/api/packing/items/bulk", api_bulk_add_packing_items, methods=["POST"]),
    Route("/api/packing/items/{item_id}", api_edit_packing_item, methods=["PUT"]),
    Route("/api/packing/items/{item_id}", api_delete_packing_item, methods=["DELETE"]),
    Route("/api/packing/items/{item_id}/advance", api_advance_packing_item, methods=["POST"]),
    Route("/api/packing/bags", api_list_packing_bags, methods=["GET"]),
    Route("/api/packing/bags", api_add_packing_bag, methods=["POST"]),
]

for i, route in enumerate(custom_routes):
    app.router.routes.insert(i, route)
