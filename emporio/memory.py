"""SQLite persistence: conversation history per session and handoffs to the human team.

A session id is whatever identifies the customer on the channel (the phone number on
WhatsApp, a cookie on the web). Only user/assistant text is replayed to the model; tool
results are stored for auditing but never replayed, so prices and stock are always re-read
from the data instead of trusted from an old turn, and the 8k context is not wasted.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from .tools import ToolCall

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role       TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content    TEXT NOT NULL,
    tool_calls TEXT,            -- JSON audit trail of the tools used to produce this answer
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages (session_id, id);

CREATE TABLE IF NOT EXISTS handoffs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    protocol   TEXT UNIQUE,
    session_id TEXT,
    reason     TEXT NOT NULL,
    summary    TEXT NOT NULL,
    contact    TEXT,
    status     TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()


class ConversationStore:
    def __init__(self, db: Database):
        self.db = db

    def history(self, session_id: str, turns: int) -> list[dict]:
        """The last `turns` user/assistant exchanges, oldest first."""
        rows = self.db.conn.execute(
            "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, 2 * turns),
        ).fetchall()
        messages = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
        while messages and messages[0]["role"] != "user":  # never start mid-exchange
            messages.pop(0)
        return messages

    def tool_trail(self, session_id: str, turns: int) -> list[dict]:
        """Tool calls of the last `turns` answers, oldest first (for session facts)."""
        rows = self.db.conn.execute(
            "SELECT tool_calls FROM messages WHERE session_id = ? AND role = 'assistant' ORDER BY id DESC LIMIT ?",
            (session_id, turns),
        ).fetchall()
        return [call for r in reversed(rows) if r["tool_calls"] for call in json.loads(r["tool_calls"])]

    def count(self, session_id: str) -> int:
        return self.db.conn.execute("SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)).fetchone()[0]

    def save_exchange(self, session_id: str, user_text: str, answer: str, tool_calls: list[ToolCall]) -> None:
        trail = json.dumps(
            [{"name": c.name, "arguments": c.arguments, "result": c.result} for c in tool_calls], ensure_ascii=False
        ) if tool_calls else None
        with self.db.conn:
            self.db.conn.execute(
                "INSERT INTO messages (session_id, role, content, created_at) VALUES (?, 'user', ?, ?)",
                (session_id, user_text, _now()),
            )
            self.db.conn.execute(
                "INSERT INTO messages (session_id, role, content, tool_calls, created_at) VALUES (?, 'assistant', ?, ?, ?)",
                (session_id, answer, trail, _now()),
            )


class SqliteHandoffDesk:
    """Same interface as tools.HandoffDesk, persisted so the team can pick tickets up."""

    def __init__(self, db: Database):
        self.db = db

    def open(self, reason: str, summary: str, contact: str | None, session_id: str | None, today: date) -> str:
        with self.db.conn:
            cursor = self.db.conn.execute(
                "INSERT INTO handoffs (session_id, reason, summary, contact, created_at) VALUES (?, ?, ?, ?, ?)",
                (session_id, reason, summary, contact, _now()),
            )
            protocol = f"EM-{today:%Y%m%d}-{cursor.lastrowid:04d}"
            self.db.conn.execute("UPDATE handoffs SET protocol = ? WHERE id = ?", (protocol, cursor.lastrowid))
        return protocol

    def list_open(self) -> list[dict]:
        rows = self.db.conn.execute("SELECT * FROM handoffs WHERE status = 'open' ORDER BY id").fetchall()
        return [dict(r) for r in rows]
