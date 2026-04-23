"""Household task tracking MCP server."""

import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# FastMCP constructor
# ---------------------------------------------------------------------------
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

VALID_CADENCES = list(CADENCE_DAYS.keys()) + ["once"]


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
            cadence TEXT CHECK(cadence IN ('weekly', 'monthly', 'quarterly') OR cadence IS NULL),
            notes TEXT,
            last_completed TEXT,
            sort_order INTEGER NOT NULL DEFAULT 0,
            due_date TEXT,
            created_at TEXT NOT NULL
        )
    """)
    # Migration: add sort_order if missing (existing DBs)
    try:
        conn.execute("ALTER TABLE tasks ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # column already exists
    # Migration: add due_date if missing (existing DBs)
    try:
        conn.execute("ALTER TABLE tasks ADD COLUMN due_date TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.commit()
    conn.close()


_init_db()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _task_status(row: sqlite3.Row) -> str:
    if row["cadence"] is None:
        # One-time task: complete once done, to do otherwise
        return "Complete" if row["last_completed"] else "To Do"
    if row["last_completed"] is None:
        return "To Do"
    last = datetime.fromisoformat(row["last_completed"])
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    threshold = datetime.now(timezone.utc) - timedelta(days=CADENCE_DAYS[row["cadence"]])
    return "Complete" if last >= threshold else "To Do"


def _is_recurring(row) -> bool:
    """Check if a task (row or dict) is recurring."""
    cadence = row["cadence"] if hasattr(row, "keys") else row.get("cadence")
    return cadence is not None


def _format_task(row: sqlite3.Row) -> dict:
    status = _task_status(row)
    return {
        "id": row["id"],
        "title": row["title"],
        "cadence": row["cadence"] or "once",
        "notes": row["notes"] or "",
        "status": status,
        "last_completed": row["last_completed"],
        "sort_order": row["sort_order"],
        "due_date": row["due_date"],
        "created_at": row["created_at"],
    }


def _sort_tasks(tasks: list[dict]) -> list[dict]:
    """Sort: To Do before Complete, recurring before one-time.
    Recurring sorted by title; one-time sorted by sort_order."""
    def sort_key(t):
        status_ord = 0 if t["status"] == "To Do" else 1
        recurring_ord = 0 if t["cadence"] != "once" else 1
        # Recurring: alphabetical. One-time: by sort_order.
        order = t["title"].lower() if t["cadence"] != "once" else str(t["sort_order"]).zfill(10)
        return (status_ord, recurring_ord, order)
    return sorted(tasks, key=sort_key)


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------
@mcp.tool()
def list_tasks() -> str:
    """List all household tasks with their current status.

    Returns tasks sorted: To Do first (recurring above one-time), then Complete.
    Status is computed from last_completed date and cadence.
    """
    conn = _get_db()
    rows = conn.execute("SELECT * FROM tasks ORDER BY title").fetchall()
    conn.close()

    if not rows:
        return "No tasks yet. Use add_task to create one."

    tasks = _sort_tasks([_format_task(r) for r in rows])

    lines = []
    current_status = None
    for t in tasks:
        if t["status"] != current_status:
            current_status = t["status"]
            lines.append(f"\n## {current_status}")
        notes_bit = f" — {t['notes']}" if t["notes"] else ""
        completed_bit = f" (last: {t['last_completed'][:10]})" if t["last_completed"] else ""
        due_bit = f" (due: {t['due_date']})" if t.get("due_date") else ""
        lines.append(f"- **{t['title']}** [{t['cadence']}]{completed_bit}{due_bit}{notes_bit}")
        lines.append(f"  id: `{t['id']}`")

    return "\n".join(lines)


@mcp.tool()
def add_task(title: str, cadence: str = "once", notes: Optional[str] = None, due_date: Optional[str] = None) -> str:
    """Add a new household task.

    Args:
        title: Name of the task (e.g. "Clean gutters")
        cadence: How often — one of: weekly, monthly, quarterly, once. Defaults to once.
        notes: Optional free text notes about the task
        due_date: Optional due date for one-time tasks (YYYY-MM-DD format)
    """
    cadence = cadence.lower().strip()
    if cadence not in VALID_CADENCES:
        return f"Invalid cadence '{cadence}'. Must be one of: {', '.join(VALID_CADENCES)}."

    db_cadence = None if cadence == "once" else cadence
    # due_date only applies to one-time tasks
    db_due_date = due_date if db_cadence is None else None
    task_id = str(uuid.uuid4())[:8]
    conn = _get_db()
    # For one-time tasks, put at end of sort order
    max_order = conn.execute("SELECT COALESCE(MAX(sort_order), 0) FROM tasks WHERE cadence IS NULL").fetchone()[0]
    sort_order = max_order + 1 if db_cadence is None else 0
    conn.execute(
        "INSERT INTO tasks (id, title, cadence, notes, sort_order, due_date, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (task_id, title.strip(), db_cadence, notes, sort_order, db_due_date, _now_iso()),
    )
    conn.commit()
    conn.close()
    due_bit = f", due: {db_due_date}" if db_due_date else ""
    return f"Added task '{title}' ({cadence}{due_bit}). ID: {task_id}"


@mcp.tool()
def edit_task(
    task_id: str,
    title: Optional[str] = None,
    cadence: Optional[str] = None,
    notes: Optional[str] = None,
    due_date: Optional[str] = None,
) -> str:
    """Edit an existing task. Only provided fields are updated.

    Args:
        task_id: The task ID (use list_tasks to find it)
        title: New title
        cadence: New cadence — one of: weekly, monthly, quarterly, once
        notes: New notes (free text)
        due_date: Due date for one-time tasks (YYYY-MM-DD format, or empty string to clear)
    """
    if cadence is not None:
        cadence = cadence.lower().strip()
        if cadence not in VALID_CADENCES:
            return f"Invalid cadence '{cadence}'. Must be one of: {', '.join(VALID_CADENCES)}."

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
        values.append(None if cadence == "once" else cadence)
    if notes is not None:
        updates.append("notes = ?")
        values.append(notes)
    if due_date is not None:
        updates.append("due_date = ?")
        # Empty string clears the due date
        values.append(due_date if due_date else None)

    if not updates:
        conn.close()
        return "Nothing to update — provide at least one of: title, cadence, notes, due_date."

    values.append(task_id)
    conn.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?", values)
    conn.commit()

    updated = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    t = _format_task(updated)
    due_bit = f", due: {t['due_date']}" if t['due_date'] else ""
    return f"Updated '{t['title']}' — cadence: {t['cadence']}, status: {t['status']}{due_bit}"


@mcp.tool()
def complete_task(task_id: str) -> str:
    """Mark a task as complete. Sets last_completed to now.

    For recurring tasks, it will move back to To Do after the cadence period.
    For one-time tasks, it stays complete permanently.

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

    if row["cadence"] is None:
        return f"Completed '{row['title']}' (one-time task — done!)."
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
