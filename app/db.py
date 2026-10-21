"""Postgres-backed state: analyses, history, chat, settings.

Files (.tex, compiled .pdf, extracted .txt) live on the uploads volume —
the agent tooling needs real files — but everything you'd want to survive a
re-analyze or page refresh is stored here.

Requires Postgres (docker compose provides it). Connects per-operation with
autocommit; this app is single-process with low write volume, so no pooling.
"""
from __future__ import annotations

import json
import os
import time

import psycopg
from psycopg.types.json import Jsonb

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://resume:resume@localhost:5432/resume_analyzer"
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS analyses (
    name        TEXT PRIMARY KEY,
    status      TEXT NOT NULL DEFAULT 'new',
    updated_at  DOUBLE PRECISION,
    model       TEXT,
    instruction TEXT,
    analysis    JSONB,
    error       TEXT
);
CREATE TABLE IF NOT EXISTS analysis_history (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    finished_at DOUBLE PRECISION,
    model       TEXT,
    instruction TEXT,
    analysis    JSONB
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id      BIGSERIAL PRIMARY KEY,
    name    TEXT NOT NULL,
    role    TEXT NOT NULL,
    content TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_settings (
    key    TEXT PRIMARY KEY,
    value  JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS chat_messages_name_idx ON chat_messages (name, id);
CREATE INDEX IF NOT EXISTS analysis_history_name_idx ON analysis_history (name, id);
"""


def _conn():
    return psycopg.connect(DATABASE_URL, autocommit=True)


def init_db() -> None:
    with _conn() as conn:
        conn.execute(_SCHEMA)


def sweep_interrupted() -> None:
    """Analyses stuck in 'running' can never finish after a restart."""
    with _conn() as conn:
        conn.execute(
            "UPDATE analyses SET status='error',"
            " error='Interrupted by restart; analyze again.' WHERE status='running'"
        )


# --- current analysis --------------------------------------------------------

def read_analysis(name: str) -> dict:
    with _conn() as conn:
        row = conn.execute(
            "SELECT status, updated_at, model, instruction, analysis, error"
            " FROM analyses WHERE name = %s", (name,)).fetchone()
    if row is None:
        return {"status": "new"}
    return {
        "status": row[0] or "new",
        "updated_at": row[1],
        "model": row[2],
        "instruction": row[3],
        "analysis": row[4],
        "error": row[5],
    }


def write_analysis(name: str, fields: dict) -> None:
    """Merge `fields` into the current analysis row (like dict.update)."""
    row = read_analysis(name)
    row.update(fields)
    row["updated_at"] = time.time()
    with _conn() as conn:
        conn.execute(
            """INSERT INTO analyses (name, status, updated_at, model, instruction, analysis, error)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (name) DO UPDATE SET
                 status = excluded.status, updated_at = excluded.updated_at,
                 model = excluded.model, instruction = excluded.instruction,
                 analysis = excluded.analysis, error = excluded.error""",
            (name, row.get("status", "new"), row["updated_at"], row.get("model"),
             row.get("instruction"),
             Jsonb(row["analysis"]) if row.get("analysis") is not None else None,
             row.get("error")))


# --- history -----------------------------------------------------------------

def add_history(name: str, run: dict) -> str:
    with _conn() as conn:
        hid = conn.execute(
            """INSERT INTO analysis_history (name, finished_at, model, instruction, analysis)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (name, run.get("finished_at", time.time()), run.get("model"),
             run.get("instruction", ""),
             Jsonb(run.get("analysis") or {}))).fetchone()[0]
    return str(hid)


def get_history(name: str) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, finished_at, model, instruction, analysis"
            " FROM analysis_history WHERE name = %s ORDER BY id", (name,)).fetchall()
    return [{"id": str(r[0]), "finished_at": r[1], "model": r[2],
             "instruction": r[3], "analysis": r[4]} for r in rows]


# --- chat ----------------------------------------------------------------------

def get_chat(name: str) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT role, content FROM chat_messages WHERE name = %s ORDER BY id",
            (name,)).fetchall()
    return [{"role": r[0], "content": r[1]} for r in rows]


def add_chat(name: str, role: str, content: str) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO chat_messages (name, role, content) VALUES (%s, %s, %s)",
            (name, role, content))


def clear_chat(name: str) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM chat_messages WHERE name = %s", (name,))


# --- settings ------------------------------------------------------------------

def get_setting(key: str, default: dict | None = None) -> dict:
    with _conn() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key = %s", (key,)).fetchone()
    if row is None:
        return default if default is not None else {}
    value = row[0]
    return value if isinstance(value, dict) else json.loads(value)


def set_setting(key: str, value: dict) -> None:
    with _conn() as conn:
        conn.execute(
            """INSERT INTO app_settings (key, value) VALUES (%s, %s)
               ON CONFLICT (key) DO UPDATE SET value = excluded.value""",
            (key, Jsonb(value)))


# --- cleanup --------------------------------------------------------------------

def delete_resume(name: str) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM analyses WHERE name = %s", (name,))
        conn.execute("DELETE FROM analysis_history WHERE name = %s", (name,))
        conn.execute("DELETE FROM chat_messages WHERE name = %s", (name,))
