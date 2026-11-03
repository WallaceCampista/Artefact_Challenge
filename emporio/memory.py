"""SQLite persistence: conversations, per-turn traces and metrics, feedback, and handoffs.

A session id is whatever identifies the customer on the channel (the phone number on
WhatsApp, a cookie on the web). Only user/assistant text is replayed to the model; tool
results and traces are stored for auditing but never replayed, so prices and stock are always
re-read from the data instead of trusted from an old turn, and the 8k context is not wasted.

One connection is shared by the CLI or by the web server's worker threads, so every access
goes through a lock (SQLite itself serializes writes anyway).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
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

CREATE TABLE IF NOT EXISTS feedback (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL REFERENCES messages (id),
    rating     TEXT NOT NULL CHECK (rating IN ('up', 'down')),
    comment    TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (message_id)
);
"""

# Columns added after the first version; added in place so existing databases keep working.
MIGRATIONS = {
    "messages": {
        "trace": "TEXT",
        "latency_ms": "INTEGER",
        "model_calls": "INTEGER",
        "prompt_tokens": "INTEGER",
        "completion_tokens": "INTEGER",
        "grounding_retries": "INTEGER",
        "redactions": "INTEGER",
        "simulated_now": "TEXT",
    },
    "handoffs": {"resolved_at": "TEXT"},
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            for table, columns in MIGRATIONS.items():
                existing = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
                for column, kind in columns.items():
                    if column not in existing:
                        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
            self.conn.commit()

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    @contextmanager
    def transaction(self):
        with self.lock, self.conn:
            yield self.conn

    def close(self) -> None:
        with self.lock:
            self.conn.close()


class ConversationStore:
    def __init__(self, db: Database):
        self.db = db

    def history(self, session_id: str, turns: int) -> list[dict]:
        """The last `turns` user/assistant exchanges, oldest first."""
        rows = self.db.query(
            "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, 2 * turns),
        )
        messages = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
        while messages and messages[0]["role"] != "user":  # never start mid-exchange
            messages.pop(0)
        return messages

    def tool_trail(self, session_id: str, turns: int) -> list[dict]:
        """Tool calls of the last `turns` answers, oldest first (for session facts)."""
        rows = self.db.query(
            "SELECT tool_calls FROM messages WHERE session_id = ? AND role = 'assistant' ORDER BY id DESC LIMIT ?",
            (session_id, turns),
        )
        return [call for r in reversed(rows) if r["tool_calls"] for call in json.loads(r["tool_calls"])]

    def count(self, session_id: str) -> int:
        return self.db.query("SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,))[0][0]

    def save_exchange(self, session_id: str, user_text: str, answer: str, tool_calls: list[ToolCall],
                      metrics: dict | None = None) -> int:
        """Stores one exchange; returns the id of the assistant message. `metrics` may hold
        trace, latency_ms, model_calls, prompt/completion tokens, grounding_retries, redactions
        and simulated_now."""
        trail = json.dumps(
            [{"name": c.name, "arguments": c.arguments, "result": c.result} for c in tool_calls], ensure_ascii=False
        ) if tool_calls else None
        metrics = dict(metrics or {})
        if "trace" in metrics:
            metrics["trace"] = json.dumps(metrics["trace"], ensure_ascii=False)
        columns = [c for c in MIGRATIONS["messages"] if c in metrics]
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO messages (session_id, role, content, created_at) VALUES (?, 'user', ?, ?)",
                (session_id, user_text, _now()),
            )
            cursor = conn.execute(
                "INSERT INTO messages (session_id, role, content, tool_calls, created_at"
                + "".join(f", {c}" for c in columns) + ") VALUES (?, 'assistant', ?, ?, ?" + ", ?" * len(columns) + ")",
                (session_id, answer, trail, _now(), *(metrics[c] for c in columns)),
            )
            return cursor.lastrowid

    def transcript(self, session_id: str) -> list[dict]:
        """Every message of a session with its trace and metrics (web UI, team inbox)."""
        rows = self.db.query(
            "SELECT m.*, f.rating FROM messages m LEFT JOIN feedback f ON f.message_id = m.id "
            "WHERE m.session_id = ? ORDER BY m.id", (session_id,),
        )
        out = []
        for r in rows:
            item = {"id": r["id"], "role": r["role"], "content": r["content"], "created_at": r["created_at"]}
            if r["role"] == "assistant":
                item.update(
                    trace=json.loads(r["trace"]) if r["trace"] else [],
                    latency_ms=r["latency_ms"], model_calls=r["model_calls"],
                    tokens=(r["prompt_tokens"] or 0) + (r["completion_tokens"] or 0),
                    grounding_retries=r["grounding_retries"] or 0, redactions=r["redactions"] or 0,
                    simulated_now=r["simulated_now"], rating=r["rating"],
                )
            out.append(item)
        return out


class FeedbackStore:
    def __init__(self, db: Database):
        self.db = db

    def rate(self, message_id: int, rating: str, comment: str | None = None) -> bool:
        """Thumbs up/down on an assistant answer (latest rating wins). False if no such answer."""
        if rating not in ("up", "down"):
            raise ValueError("rating must be 'up' or 'down'")
        if not self.db.query("SELECT 1 FROM messages WHERE id = ? AND role = 'assistant'", (message_id,)):
            return False
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO feedback (message_id, rating, comment, created_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (message_id) DO UPDATE SET rating = excluded.rating, comment = excluded.comment, "
                "created_at = excluded.created_at",
                (message_id, rating, comment, _now()),
            )
        return True

    def negatives(self) -> list[dict]:
        """Answers rated down, with the conversation that led to them: candidates for new eval cases.
        `turns` holds every customer message of the session up to the rated answer, so the case
        can replay it; `simulated_now` and `answered_at` give the store time it happened."""
        rows = self.db.query(
            "SELECT a.id, a.session_id, a.content AS answer, a.tool_calls, a.simulated_now, "
            "a.created_at AS answered_at, f.comment, f.created_at AS rated_at "
            "FROM feedback f JOIN messages a ON a.id = f.message_id WHERE f.rating = 'down' ORDER BY f.id"
        )
        items = []
        for r in rows:
            turns = [q["content"] for q in self.db.query(
                "SELECT content FROM messages WHERE session_id = ? AND id < ? AND role = 'user' ORDER BY id",
                (r["session_id"], r["id"]))]
            item = {key: r[key] for key in ("id", "session_id", "answer", "simulated_now", "answered_at",
                                            "comment", "rated_at")}
            item.update(turns=turns, question=turns[-1] if turns else "",
                        tools=[c["name"] for c in json.loads(r["tool_calls"])] if r["tool_calls"] else [])
            items.append(item)
        return items


class SqliteHandoffDesk:
    """Same interface as tools.HandoffDesk, persisted so the team can pick tickets up."""

    def __init__(self, db: Database):
        self.db = db

    def open(self, reason: str, summary: str, contact: str | None, session_id: str | None, today: date) -> str:
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO handoffs (session_id, reason, summary, contact, created_at) VALUES (?, ?, ?, ?, ?)",
                (session_id, reason, summary, contact, _now()),
            )
            protocol = f"EM-{today:%Y%m%d}-{cursor.lastrowid:04d}"
            conn.execute("UPDATE handoffs SET protocol = ? WHERE id = ?", (protocol, cursor.lastrowid))
        return protocol

    def list_open(self) -> list[dict]:
        """Oldest first: the order the team should work in."""
        return self.list("open", newest_first=False)

    def list(self, status: str | None = None, newest_first: bool = True) -> list[dict]:
        order = "DESC" if newest_first else "ASC"
        if status:
            rows = self.db.query(f"SELECT * FROM handoffs WHERE status = ? ORDER BY id {order}", (status,))
        else:
            rows = self.db.query(f"SELECT * FROM handoffs ORDER BY id {order}")
        return [dict(r) for r in rows]

    def resolve(self, protocol: str) -> bool:
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE handoffs SET status = 'resolved', resolved_at = ? WHERE protocol = ? AND status = 'open'",
                (_now(), protocol),
            )
        return cursor.rowcount == 1


def _percentile(values: list[int], q: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


class Analytics:
    """Operational metrics computed from the stored conversations (no extra instrumentation)."""

    def __init__(self, db: Database):
        self.db = db

    def summary(self) -> dict:
        answers = self.db.query(
            "SELECT session_id, latency_ms, model_calls, prompt_tokens, completion_tokens, grounding_retries, "
            "redactions, tool_calls, trace FROM messages WHERE role = 'assistant'"
        )
        latencies = [r["latency_ms"] for r in answers if r["latency_ms"] is not None]
        tools: dict[str, int] = {}
        auto = 0
        guards: dict[str, int] = {}
        for r in answers:
            for call in json.loads(r["tool_calls"]) if r["tool_calls"] else []:
                tools[call["name"]] = tools.get(call["name"], 0) + 1
            for event in json.loads(r["trace"]) if r["trace"] else []:
                if event["type"] == "tool" and event.get("auto"):
                    auto += 1
                if event["type"] == "guard" and event.get("outcome") != "pass":
                    key = f"{event['check']}:{event['outcome']}"
                    guards[key] = guards.get(key, 0) + 1
        handoffs = {row["status"]: row["n"] for row in
                    self.db.query("SELECT status, COUNT(*) AS n FROM handoffs GROUP BY status")}
        ratings = {row["rating"]: row["n"] for row in
                   self.db.query("SELECT rating, COUNT(*) AS n FROM feedback GROUP BY rating")}
        sessions = len({r["session_id"] for r in answers})
        sessions_with_handoff = self.db.query("SELECT COUNT(DISTINCT session_id) FROM handoffs")[0][0]
        n = len(answers)
        return {
            "conversations": sessions,
            "answers": n,
            "latency_ms": {"avg": round(sum(latencies) / len(latencies)) if latencies else None,
                           "p50": _percentile(latencies, 0.5), "p95": _percentile(latencies, 0.95)},
            "tokens_per_answer": round(sum((r["prompt_tokens"] or 0) + (r["completion_tokens"] or 0)
                                           for r in answers) / n) if n else None,
            "model_calls_per_answer": round(sum(r["model_calls"] or 0 for r in answers) / n, 2) if n else None,
            "tool_usage": dict(sorted(tools.items(), key=lambda kv: -kv[1])),
            "pre_routed_tool_calls": auto,
            "answers_with_grounding_retry": sum(1 for r in answers if r["grounding_retries"]),
            "answers_with_redaction": sum(1 for r in answers if r["redactions"]),
            "guards": guards,
            "handoffs": {"open": handoffs.get("open", 0), "resolved": handoffs.get("resolved", 0)},
            "handoff_rate": round(sessions_with_handoff / sessions, 3) if sessions else None,
            "feedback": {"up": ratings.get("up", 0), "down": ratings.get("down", 0)},
        }
