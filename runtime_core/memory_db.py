import sqlite3
import json
import time
from pathlib import Path
from typing import Any, Optional

class MemoryDB:
    """
    Transactional SQLite spine for Runtime v0.5.
    Ensures 'Atomic Commits' of identity and episodic state.
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            # 1. Immutable Event Log
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT,
                    cycle INTEGER,
                    seq INTEGER,
                    kind TEXT,
                    module TEXT,
                    msg TEXT,
                    details TEXT,
                    timestamp REAL,
                    UNIQUE(run_id, seq)
                )
            """)

            # 2. Episodic Memory (Frames)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS memory_frames (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT,
                    cycle INTEGER,
                    role TEXT,
                    content TEXT,
                    embedding BLOB,
                    timestamp REAL
                )
            """)

            # 3. Identity Anchors (Ground Truth)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS identity_anchors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trait TEXT,
                    content TEXT,
                    embedding BLOB,
                    timestamp REAL,
                    UNIQUE(trait)
                )
            """)

            # 4. Signal Logs (Emotional Trace)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS signal_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT,
                    cycle INTEGER,
                    emotion TEXT,
                    value REAL,
                    timestamp REAL
                )
            """)
            
            conn.commit()

    def log_event(self, event: dict[str, Any]):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO events (run_id, cycle, seq, kind, module, msg, details, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                event["run_id"],
                event["cycle"],
                event["seq"],
                event["kind"],
                event["module"],
                event.get("msg", ""),
                json.dumps(event.get("details", {})),
                event.get("timestamp", time.time())
            ))
            conn.commit()

    def commit_frame(self, run_id: str, cycle: int, role: str, content: str, embedding: Optional[bytes] = None):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO memory_frames (run_id, cycle, role, content, embedding, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (run_id, cycle, role, content, embedding, time.time()))
            conn.commit()

    def set_anchor(self, trait: str, content: str, embedding: Optional[bytes] = None):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO identity_anchors (trait, content, embedding, timestamp)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(trait) DO UPDATE SET
                    content=excluded.content,
                    embedding=excluded.embedding,
                    timestamp=excluded.timestamp
            """, (trait, content, embedding, time.time()))
            conn.commit()

    def get_anchor(self, trait: str) -> Optional[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM identity_anchors WHERE trait = ?", (trait,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def log_signal(self, run_id: str, cycle: int, emotion: str, value: float):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO signal_logs (run_id, cycle, emotion, value, timestamp)
                VALUES (?, ?, ?, ?, ?)
            """, (run_id, cycle, emotion, value, time.time()))
            conn.commit()

    def get_latest_frames(self, limit: int = 10) -> list[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM memory_frames ORDER BY id DESC LIMIT ?", (limit,))
            return [dict(row) for row in cursor.fetchall()]

    def get_committed_proposals(self, run_id: str) -> list[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM events 
                WHERE run_id = ? AND kind = 'identity.pulse'
                ORDER BY cycle ASC
            """, (run_id,))
            return [dict(row) for row in cursor.fetchall()]

    def get_session_frames(self, run_id: str) -> list[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM memory_frames WHERE run_id = ? ORDER BY cycle ASC", (run_id,))
            return [dict(row) for row in cursor.fetchall()]
