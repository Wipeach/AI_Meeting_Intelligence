"""Meeting storage backed by SQLite.

The public ``MeetingStore`` interface intentionally matches the previous JSON
store so the collectors, analyzer, scheduler, and web layer can keep using the
same calls. Existing ``data/meetings.json`` data is migrated automatically when
the SQLite database is first opened and empty.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from meeting_platform.core.models import Meeting, MeetingSource, MeetingStatus, utc_now_iso


class MeetingStore:
    """SQLite meeting store indexed by ``meeting_id`` and ``(source, source_ref)``."""

    _write_lock = threading.Lock()

    def __init__(self, file_path: str = "data/meetings.db"):
        db_path = Path(file_path)
        if db_path.suffix.lower() == ".json":
            db_path = db_path.with_suffix(".db")
        self.file_path = db_path
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._migrate_json_if_empty(Path(file_path))

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.file_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS meetings (
                    meeting_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    meeting_code TEXT DEFAULT '',
                    department TEXT DEFAULT '',
                    title TEXT DEFAULT '',
                    subject TEXT DEFAULT '',
                    start_time TEXT DEFAULT '',
                    end_time TEXT DEFAULT '',
                    owner TEXT DEFAULT '',
                    participants TEXT DEFAULT '[]',
                    content_url TEXT DEFAULT '',
                    media_url TEXT DEFAULT '',
                    raw_content TEXT DEFAULT '',
                    status TEXT NOT NULL,
                    supervised INTEGER NOT NULL DEFAULT 0,
                    analysis TEXT,
                    captured_at TEXT DEFAULT '',
                    updated_at TEXT DEFAULT '',
                    payload TEXT NOT NULL,
                    UNIQUE(source, source_ref)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_meetings_start_time ON meetings(start_time)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_meetings_owner ON meetings(owner)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_meetings_status ON meetings(status)")

    def _migrate_json_if_empty(self, requested_path: Path) -> None:
        json_path = requested_path if requested_path.suffix.lower() == ".json" else requested_path.with_suffix(".json")
        if not json_path.exists():
            return
        with self._connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM meetings").fetchone()[0]
        if count:
            return
        try:
            with json_path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError, ValueError):
            return
        for raw in (data.get("meetings") or {}).values():
            try:
                self.upsert(Meeting.from_dict(raw))
            except Exception:
                continue

    @staticmethod
    def _row(meeting: Meeting) -> Dict[str, Any]:
        payload = meeting.to_dict()
        analysis = payload.get("analysis")
        return {
            "meeting_id": meeting.meeting_id,
            "source": meeting.source.value,
            "source_ref": meeting.source_ref,
            "meeting_code": meeting.meeting_code,
            "department": meeting.department,
            "title": meeting.title,
            "subject": meeting.subject,
            "start_time": meeting.start_time,
            "end_time": meeting.end_time,
            "owner": meeting.owner,
            "participants": json.dumps(payload.get("participants") or [], ensure_ascii=False),
            "content_url": meeting.content_url,
            "media_url": meeting.media_url,
            "raw_content": meeting.raw_content,
            "status": meeting.status.value,
            "supervised": 1 if meeting.supervised else 0,
            "analysis": json.dumps(analysis, ensure_ascii=False) if analysis is not None else None,
            "captured_at": meeting.captured_at,
            "updated_at": meeting.updated_at,
            "payload": json.dumps(payload, ensure_ascii=False),
        }

    @staticmethod
    def _meeting_from_row(row: sqlite3.Row) -> Meeting:
        return Meeting.from_dict(json.loads(row["payload"]))

    def upsert(self, meeting: Meeting) -> Meeting:
        meeting.updated_at = utc_now_iso()
        if not meeting.captured_at:
            meeting.captured_at = meeting.updated_at
        with self._write_lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT meeting_id FROM meetings WHERE source = ? AND source_ref = ?",
                (meeting.source.value, meeting.source_ref),
            ).fetchone()
            if existing:
                meeting.meeting_id = existing["meeting_id"]
            row = self._row(meeting)
            conn.execute(
                """
                INSERT INTO meetings (
                    meeting_id, source, source_ref, meeting_code, department, title, subject,
                    start_time, end_time, owner, participants, content_url, media_url, raw_content,
                    status, supervised, analysis, captured_at, updated_at, payload
                ) VALUES (
                    :meeting_id, :source, :source_ref, :meeting_code, :department, :title, :subject,
                    :start_time, :end_time, :owner, :participants, :content_url, :media_url, :raw_content,
                    :status, :supervised, :analysis, :captured_at, :updated_at, :payload
                )
                ON CONFLICT(meeting_id) DO UPDATE SET
                    source = excluded.source,
                    source_ref = excluded.source_ref,
                    meeting_code = excluded.meeting_code,
                    department = excluded.department,
                    title = excluded.title,
                    subject = excluded.subject,
                    start_time = excluded.start_time,
                    end_time = excluded.end_time,
                    owner = excluded.owner,
                    participants = excluded.participants,
                    content_url = excluded.content_url,
                    media_url = excluded.media_url,
                    raw_content = excluded.raw_content,
                    status = excluded.status,
                    supervised = excluded.supervised,
                    analysis = excluded.analysis,
                    captured_at = excluded.captured_at,
                    updated_at = excluded.updated_at,
                    payload = excluded.payload
                """,
                row,
            )
        return meeting

    def get(self, meeting_id: str) -> Optional[Meeting]:
        with self._connect() as conn:
            row = conn.execute("SELECT payload FROM meetings WHERE meeting_id = ?", (meeting_id,)).fetchone()
        return self._meeting_from_row(row) if row else None

    def find_by_source_ref(self, source: MeetingSource, source_ref: str) -> Optional[Meeting]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM meetings WHERE source = ? AND source_ref = ?",
                (source.value, source_ref),
            ).fetchone()
        return self._meeting_from_row(row) if row else None

    def list(
        self,
        *,
        source: Optional[MeetingSource] = None,
        status: Optional[MeetingStatus] = None,
        meeting_code: Optional[str] = None,
        department: Optional[str] = None,
        owner: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> List[Meeting]:
        clauses = []
        params: List[Any] = []
        if source is not None:
            clauses.append("source = ?")
            params.append(source.value)
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if meeting_code:
            clauses.append("REPLACE(meeting_code, ' ', '') = ?")
            params.append(meeting_code.replace(" ", ""))
        if department:
            clauses.append("department LIKE ?")
            params.append(f"%{department}%")
        if owner:
            clauses.append("owner = ?")
            params.append(owner)
        if date_from:
            clauses.append("start_time >= ?")
            params.append(date_from)
        if date_to:
            if len(date_to) == 10:
                end_exclusive = (datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
                clauses.append("start_time < ?")
                params.append(end_exclusive)
            else:
                clauses.append("start_time <= ?")
                params.append(date_to)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(f"SELECT payload FROM meetings{where} ORDER BY start_time DESC", params).fetchall()
        return [self._meeting_from_row(row) for row in rows]

    def owners(self) -> List[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT owner, COUNT(*) AS count FROM meetings WHERE owner <> '' GROUP BY owner ORDER BY owner"
            ).fetchall()
        return [{"owner": row["owner"], "count": row["count"]} for row in rows]

    def delete(self, meeting_id: str) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM meetings WHERE meeting_id = ?", (meeting_id,))
        return cur.rowcount > 0
