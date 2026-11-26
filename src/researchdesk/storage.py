"""Application persistence and replay receipts, separate from LangGraph checkpoints."""
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from uuid import uuid4


class RecoveryError(ValueError):
    pass


def atomic_text(path, text):
    temp = path.with_name("." + path.name + "." + uuid4().hex + ".tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    # Windows readers can briefly prevent replacing an open destination.
    for delay in (0.01, 0.02, 0.04, 0.08, 0):
        try:
            os.replace(temp, path)
            break
        except PermissionError as error:
            if not delay or getattr(error, "winerror", None) not in {5, 32, 33}:
                raise
            time.sleep(delay)


class RecoveryStore:
    def __init__(self, directory, *, resume=False):
        self.directory = directory
        path = directory / "application.sqlite3"
        if resume and not path.is_file():
            raise RecoveryError("This run has no recovery database; legacy runs cannot be resumed.")
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, timeout=5, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        if resume and self.conn.execute("PRAGMA user_version").fetchone()[0] != 1:
            self.conn.close()
            raise RecoveryError("Unsupported application recovery database version.")
        self.conn.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS records (
                kind TEXT, id TEXT, payload TEXT NOT NULL, PRIMARY KEY(kind,id));
            CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
            INSERT OR IGNORE INTO counters VALUES ('calls',0),('requests',0);
            CREATE TABLE IF NOT EXISTS dispatches (
                id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, tool TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, result TEXT);
            CREATE TABLE IF NOT EXISTS receipts (fingerprint TEXT PRIMARY KEY, result TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
            PRAGMA user_version=1;
        """)

    @contextmanager
    def transaction(self):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise

    def records(self, kind):
        with self.lock:
            return [json.loads(row[0]) for row in self.conn.execute(
                "SELECT payload FROM records WHERE kind=? ORDER BY rowid", (kind,))]

    def save_record(self, kind, record):
        with self.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO records VALUES (?,?,?)",
                         (kind, record["id"], json.dumps(record, ensure_ascii=False)))

    def counters(self):
        with self.lock:
            return dict(self.conn.execute("SELECT name,value FROM counters"))

    def begin_call(self, call_id, tool, fingerprint, maximum):
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM dispatches WHERE id=?", (call_id,)).fetchone()
            if row is not None:
                if row["fingerprint"] != fingerprint or row["tool"] != tool:
                    raise RecoveryError("Operation ID was reused with different arguments.")
                return dict(row), True
            count = conn.execute("SELECT value FROM counters WHERE name='calls'").fetchone()[0]
            if count >= maximum:
                return None, False
            conn.execute("UPDATE counters SET value=value+1 WHERE name='calls'")
            conn.execute("INSERT INTO dispatches(id,fingerprint,tool) VALUES (?,?,?)",
                         (call_id, fingerprint, tool))
            return dict(id=call_id, fingerprint=fingerprint, tool=tool, attempts=0, result=None), False

    def reserve_request(self, maximum):
        with self.transaction() as conn:
            used = conn.execute("SELECT value FROM counters WHERE name='requests'").fetchone()[0]
            if used >= maximum:
                return False
            conn.execute("UPDATE counters SET value=value+1 WHERE name='requests'")
            return True

    def attempt(self, call_id):
        with self.transaction() as conn:
            conn.execute("UPDATE dispatches SET attempts=attempts+1 WHERE id=?", (call_id,))

    def cached(self, fingerprint):
        with self.lock:
            row = self.conn.execute("SELECT result FROM receipts WHERE fingerprint=?", (fingerprint,)).fetchone()
            return json.loads(row[0]) if row else None

    def finish(self, call_id, fingerprint, result, success):
        payload = json.dumps(result, ensure_ascii=False)
        with self.transaction() as conn:
            conn.execute("UPDATE dispatches SET result=? WHERE id=?", (payload, call_id))
            if success:
                conn.execute("INSERT OR IGNORE INTO receipts VALUES (?,?)", (fingerprint, payload))

    def add_event(self, row):
        payload = json.dumps(row, ensure_ascii=False)
        with self.transaction() as conn:
            conn.execute("INSERT INTO events(payload) VALUES (?)", (payload,))
        with (self.directory / "tool_events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(payload + "\n")
            handle.flush()

    def export(self):
        for kind in ("sources", "evidence"):
            atomic_text(self.directory / (kind + ".jsonl"),
                        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in self.records(kind)))
        with self.lock:
            events = [row[0] for row in self.conn.execute("SELECT payload FROM events ORDER BY id")]
        atomic_text(self.directory / "tool_events.jsonl", "".join(row+"\n" for row in events))

    def close(self):
        self.conn.close()


class RunLock:
    """OS-owned lock: released automatically on process death."""
    def __init__(self, directory):
        self.path = directory / "run.lock"
        self.handle = None

    def __enter__(self):
        self.handle = self.path.open("a+b")
        self.handle.seek(0, os.SEEK_END)
        if self.handle.tell() == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise RecoveryError("Another process owns this run.") from None
        return self

    def __exit__(self, *exc):
        try:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()

