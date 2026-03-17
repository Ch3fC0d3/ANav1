from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .config import settings


JSON_FIELDS = {"glossary_hits", "example_hits", "transcript_words", "warnings"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_data_dirs() -> None:
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)


def get_connection() -> sqlite3.Connection:
    ensure_data_dirs()
    connection = sqlite3.connect(settings.database_path)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    ensure_data_dirs()
    with get_connection() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS glossary_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                navajo_term TEXT NOT NULL,
                english_meaning TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS recordings (
                id TEXT PRIMARY KEY,
                original_filename TEXT NOT NULL,
                audio_path TEXT NOT NULL,
                mime_type TEXT NOT NULL DEFAULT '',
                raw_transcript TEXT NOT NULL DEFAULT '',
                corrected_transcript TEXT NOT NULL DEFAULT '',
                draft_translation TEXT NOT NULL DEFAULT '',
                final_translation TEXT NOT NULL DEFAULT '',
                translation_notes TEXT NOT NULL DEFAULT '',
                topic_tags TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'needs_review',
                confidence TEXT NOT NULL DEFAULT 'low',
                draft_explanation TEXT NOT NULL DEFAULT '',
                glossary_hits_json TEXT NOT NULL DEFAULT '[]',
                example_hits_json TEXT NOT NULL DEFAULT '[]',
                transcript_words_json TEXT NOT NULL DEFAULT '[]',
                warnings_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                approved_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_recordings_status ON recordings (status);
            CREATE INDEX IF NOT EXISTS idx_recordings_created_at ON recordings (created_at DESC);
            """
        )


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None

    item = dict(row)
    item["glossary_hits"] = json.loads(item.pop("glossary_hits_json", "[]"))
    item["example_hits"] = json.loads(item.pop("example_hits_json", "[]"))
    item["transcript_words"] = json.loads(item.pop("transcript_words_json", "[]"))
    item["warnings"] = json.loads(item.pop("warnings_json", "[]"))
    return item


def list_glossary() -> list[dict[str, Any]]:
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT id, navajo_term, english_meaning, notes, created_at
            FROM glossary_entries
            ORDER BY navajo_term COLLATE NOCASE
            """
        ).fetchall()
    return [dict(row) for row in rows]


def create_glossary_entry(navajo_term: str, english_meaning: str, notes: str = "") -> dict[str, Any]:
    timestamp = utc_now()
    with get_connection() as connection:
        cursor = connection.execute(
            """
            INSERT INTO glossary_entries (navajo_term, english_meaning, notes, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (navajo_term.strip(), english_meaning.strip(), notes.strip(), timestamp),
        )
        row = connection.execute(
            """
            SELECT id, navajo_term, english_meaning, notes, created_at
            FROM glossary_entries
            WHERE id = ?
            """,
            (cursor.lastrowid,),
        ).fetchone()
    return dict(row)


def create_recording(payload: dict[str, Any]) -> dict[str, Any]:
    timestamp = utc_now()
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO recordings (
                id,
                original_filename,
                audio_path,
                mime_type,
                raw_transcript,
                corrected_transcript,
                draft_translation,
                final_translation,
                translation_notes,
                topic_tags,
                status,
                confidence,
                draft_explanation,
                glossary_hits_json,
                example_hits_json,
                transcript_words_json,
                warnings_json,
                created_at,
                updated_at,
                approved_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["id"],
                payload["original_filename"],
                payload["audio_path"],
                payload.get("mime_type", ""),
                payload.get("raw_transcript", ""),
                payload.get("corrected_transcript", ""),
                payload.get("draft_translation", ""),
                payload.get("final_translation", ""),
                payload.get("translation_notes", ""),
                payload.get("topic_tags", ""),
                payload.get("status", "needs_review"),
                payload.get("confidence", "low"),
                payload.get("draft_explanation", ""),
                json.dumps(payload.get("glossary_hits", [])),
                json.dumps(payload.get("example_hits", [])),
                json.dumps(payload.get("transcript_words", [])),
                json.dumps(payload.get("warnings", [])),
                timestamp,
                timestamp,
                payload.get("approved_at"),
            ),
        )
        row = connection.execute("SELECT * FROM recordings WHERE id = ?", (payload["id"],)).fetchone()
    return _row_to_dict(row)


def update_recording(recording_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
    if not updates:
        return get_recording(recording_id)

    normalized = dict(updates)
    for field in JSON_FIELDS:
        if field in normalized:
            normalized[f"{field}_json"] = json.dumps(normalized.pop(field))

    normalized["updated_at"] = utc_now()
    set_clause = ", ".join(f"{column} = ?" for column in normalized)
    values = list(normalized.values()) + [recording_id]

    with get_connection() as connection:
        connection.execute(f"UPDATE recordings SET {set_clause} WHERE id = ?", values)
        row = connection.execute("SELECT * FROM recordings WHERE id = ?", (recording_id,)).fetchone()
    return _row_to_dict(row)


def get_recording(recording_id: str) -> dict[str, Any] | None:
    with get_connection() as connection:
        row = connection.execute("SELECT * FROM recordings WHERE id = ?", (recording_id,)).fetchone()
    return _row_to_dict(row)


def list_recent_recordings(limit: int = 12) -> list[dict[str, Any]]:
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM recordings
            ORDER BY datetime(created_at) DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def list_approved_memories(limit: int = 20) -> list[dict[str, Any]]:
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM recordings
            WHERE status = 'approved'
            ORDER BY datetime(approved_at) DESC, datetime(updated_at) DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [_row_to_dict(row) for row in rows]
