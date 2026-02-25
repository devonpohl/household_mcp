"""Household task tracking MCP server."""

import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from mcp.server.transport_security import TransportSecuritySettings
from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# FastMCP constructor — allow remote host when deployed
# ---------------------------------------------------------------------------
_allowed_host = os.environ.get("MCP_ALLOWED_HOST")
if _allowed_host:
    mcp = FastMCP(
        "household",
        transport_security=TransportSecuritySettings(
            allowed_hosts=[_allowed_host, f"{_allowed_host}:*"],
            allowed_origins=[f"https://{_allowed_host}"],
        ),
    )
else:
    mcp = FastMCP("household")

# ---------------------------------------------------------------------------
# Database setup
# ---------------------------------------------------------------------------
DB_PATH = os.environ.get("HOUSEHOLD_DB_PATH", "household.db")

CADENCE_DAYS = {
    "weekly": 7,
    "monthly": 30,
    "quarterly": 90,
}


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _init_db() -> None:
    conn = _get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            cadence TEXT NOT NULL CHECK(cadence IN ('weekly', 'monthly', 'quarterly')),
            notes TEXT,
            last_completed TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


_init_db()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _task_status(row: sqlite3.Row) -> str:
    if row["last_completed"] is None:
        return "To Do"
    last = datetime.fromisoformat(row["last_completed"])
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    threshold = datetime.now(timezone.utc) - timedelta(days=CADENCE_DAYS[row["cadence"]])
    return "Complete" if last >= threshold else "To Do"


def _format_task(row: sqlite3.Row) -> dict:
    status = _task_status(row)
    return {
        "id": row["id"],
        "title": row["title"],
        "cadence": row["cadence"],
        "notes": row["notes"] or "",
        "status": status,
        "last_completed": row["last_completed"],
        "created_at": row["created_at"],
    }


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------
@mcp.tool()
def list_tasks() -> str:
    """List all household tasks with their current status.

    Returns tasks sorted with 'To Do' first, then 'Complete'.
    Status is computed from last_completed date and cadence.
    """
    conn = _get_db()
    rows = conn.execute("SELECT * FROM tasks ORDER BY title").fetchall()
    conn.close()

    if not rows:
        return "No tasks yet. Use add_task to create one."

    tasks = [_format_task(r) for r in rows]
    # Sort: To Do first
    tasks.sort(key=lambda t: (0 if t["status"] == "To Do" else 1, t["title"]))

    lines = []
    current_status = None
    for t in tasks:
        if t["status"] != current_status:
            current_status = t["status"]
            lines.append(f"\n## {current_status}")
        notes_bit = f" — {t['notes']}" if t["notes"] else ""
        completed_bit = f" (last: {t['last_completed'][:10]})" if t["last_completed"] else ""
        lines.append(f"- **{t['title']}** [{t['cadence']}]{completed_bit}{notes_bit}")
        lines.append(f"  id: `{t['id']}`")

    return "\n".join(lines)


@mcp.tool()
def add_task(title: str, cadence: str, notes: Optional[str] = None) -> str:
    """Add a new recurring household task.

    Args:
        title: Name of the task (e.g. "Clean gutters")
        cadence: How often — one of: weekly, monthly, quarterly
        notes: Optional free text notes about the task
    """
    cadence = cadence.lower().strip()
    if cadence not in CADENCE_DAYS:
        return f"Invalid cadence '{cadence}'. Must be one of: weekly, monthly, quarterly."

    task_id = str(uuid.uuid4())[:8]
    conn = _get_db()
    conn.execute(
        "INSERT INTO tasks (id, title, cadence, notes, created_at) VALUES (?, ?, ?, ?, ?)",
        (task_id, title.strip(), cadence, notes, _now_iso()),
    )
    conn.commit()
    conn.close()
    return f"Added task '{title}' ({cadence}). ID: {task_id}"


@mcp.tool()
def edit_task(
    task_id: str,
    title: Optional[str] = None,
    cadence: Optional[str] = None,
    notes: Optional[str] = None,
) -> str:
    """Edit an existing task. Only provided fields are updated.

    Args:
        task_id: The task ID (use list_tasks to find it)
        title: New title
        cadence: New cadence — one of: weekly, monthly, quarterly
        notes: New notes (free text)
    """
    if cadence is not None:
        cadence = cadence.lower().strip()
        if cadence not in CADENCE_DAYS:
            return f"Invalid cadence '{cadence}'. Must be one of: weekly, monthly, quarterly."

    conn = _get_db()
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        conn.close()
        return f"No task found with ID '{task_id}'."

    updates = []
    values = []
    if title is not None:
        updates.append("title = ?")
        values.append(title.strip())
    if cadence is not None:
        updates.append("cadence = ?")
        values.append(cadence)
    if notes is not None:
        updates.append("notes = ?")
        values.append(notes)

    if not updates:
        conn.close()
        return "Nothing to update — provide at least one of: title, cadence, notes."

    values.append(task_id)
    conn.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?", values)
    conn.commit()

    updated = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    t = _format_task(updated)
    return f"Updated '{t['title']}' — cadence: {t['cadence']}, status: {t['status']}"


@mcp.tool()
def complete_task(task_id: str) -> str:
    """Mark a task as complete. Sets last_completed to now.

    Args:
        task_id: The task ID (use list_tasks to find it)
    """
    conn = _get_db()
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        conn.close()
        return f"No task found with ID '{task_id}'."

    now = _now_iso()
    conn.execute("UPDATE tasks SET last_completed = ? WHERE id = ?", (now, task_id))
    conn.commit()
    conn.close()
    return f"Completed '{row['title']}'. It'll move back to To Do after one {row['cadence']} cycle."


@mcp.tool()
def delete_task(task_id: str, confirm: bool = False) -> str:
    """Delete a task permanently.

    Args:
        task_id: The task ID (use list_tasks to find it)
        confirm: Must be True to proceed
    """
    if not confirm:
        return "Set confirm=True to delete this task."

    conn = _get_db()
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        conn.close()
        return f"No task found with ID '{task_id}'."

    conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    return f"Deleted '{row['title']}'."


@mcp.tool()
def get_summary() -> str:
    """Quick dashboard: how many tasks to do vs. complete, and what's most overdue."""
    conn = _get_db()
    rows = conn.execute("SELECT * FROM tasks ORDER BY title").fetchall()
    conn.close()

    if not rows:
        return "No tasks tracked yet."

    tasks = [_format_task(r) for r in rows]
    todo = [t for t in tasks if t["status"] == "To Do"]
    done = [t for t in tasks if t["status"] == "Complete"]

    lines = [f"**{len(todo)}** to do, **{len(done)}** complete ({len(tasks)} total)"]

    if todo:
        # Find most overdue: longest since last_completed, or never completed
        def overdue_sort(t):
            if t["last_completed"] is None:
                return datetime.min
            return datetime.fromisoformat(t["last_completed"])

        most_overdue = sorted(todo, key=overdue_sort)[0]
        if most_overdue["last_completed"]:
            lines.append(f"Most overdue: **{most_overdue['title']}** (last done {most_overdue['last_completed'][:10]})")
        else:
            lines.append(f"Most overdue: **{most_overdue['title']}** (never completed)")

    return "\n".join(lines)
