import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .config import settings


class Store:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path or settings.db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS human_followups (
                    id TEXT PRIMARY KEY, record TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, patient_id TEXT NOT NULL, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS appointments (
                    id TEXT PRIMARY KEY, patient_id TEXT NOT NULL, doctor_id TEXT NOT NULL,
                    start TEXT NOT NULL, end TEXT NOT NULL, reason TEXT NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS occupied_slot
                    ON appointments(doctor_id, start) WHERE status = 'confirmed';
                CREATE TABLE IF NOT EXISTS holds (
                    id TEXT PRIMARY KEY, patient_id TEXT NOT NULL, session_id TEXT NOT NULL,
                    doctor_id TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
                    reason TEXT NOT NULL, expires REAL NOT NULL, appointment_id TEXT);
                CREATE UNIQUE INDEX IF NOT EXISTS held_slot ON holds(doctor_id, start);
                CREATE TABLE IF NOT EXISTS operations (
                    patient_id TEXT NOT NULL, key TEXT NOT NULL, response TEXT NOT NULL,
                    PRIMARY KEY(patient_id, key));
            ''')

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def create_session(self, session_id, uid):
        with self.connection(write=True) as db:
            db.execute('INSERT INTO sessions VALUES (?, ?, ?)', (session_id, uid, '{}'))

    def save_followup(self, record):
        with self.connection(write=True) as db:
            db.execute('INSERT OR REPLACE INTO human_followups VALUES (?, ?)',
                       (record['id'], json.dumps(record)))

    def get_session(self, session_id, uid):
        with self.connection() as db:
            row = db.execute('SELECT state FROM sessions WHERE id=? AND patient_id=?', (session_id, uid)).fetchone()
        return json.loads(row['state']) if row else None

    def save_session(self, session_id, uid, state):
        with self.connection(write=True) as db:
            db.execute('UPDATE sessions SET state=? WHERE id=? AND patient_id=?', (json.dumps(state), session_id, uid))
