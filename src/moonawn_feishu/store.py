"""Single-owner local delivery journal; never opens Hermes runtime databases."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import sqlite3
import json
import time
import threading

from .model import Phase, digest
from dataclasses import asdict

APPLICATION_ID = 0x4D4F4F4E


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        if self.path.is_symlink():
            raise ValueError("journal must not be a symlink")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._owner = open(str(path) + ".owner", "a")
        os.chmod(str(path) + ".owner", 0o600)
        try:
            fcntl.flock(self._owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._owner.close()
            raise RuntimeError("another dispatcher already owns this journal") from None
        self._mutex = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        application_id = self.db.execute("PRAGMA application_id").fetchone()[0]
        if tables - {"phases", "operations"} or (tables and application_id != APPLICATION_ID):
            self.db.close()
            self._owner.close()
            raise ValueError("refusing to use an unrelated database")
        os.chmod(path, 0o600)
        self.db.execute(f"PRAGMA application_id={APPLICATION_ID}")
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS phases (key TEXT PRIMARY KEY, state TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS operations (key TEXT PRIMARY KEY, state TEXT NOT NULL)")
        self.db.commit()

    def save(self, phase: Phase):
        with self._mutex, self.db:
            self.db.execute("INSERT INTO phases VALUES (?,?) ON CONFLICT(key) DO UPDATE SET state=excluded.state",
                            (phase.identity.key, phase.encode()))

    def all(self) -> list[Phase]:
        with self._mutex:
            return [Phase.decode(row[0]) for row in self.db.execute("SELECT state FROM phases")]

    def operation(self, key: str) -> dict | None:
        with self._mutex:
            row = self.db.execute("SELECT state FROM operations WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_operation(self, key: str, state: dict):
        with self._mutex, self.db:
            self.db.execute("INSERT INTO operations VALUES (?,?) ON CONFLICT(key) DO UPDATE SET state=excluded.state",
                            (key, json.dumps(state, ensure_ascii=False, sort_keys=True)))

    def save_operations(self, records: dict):
        """Publish a panel and all its capabilities in one durable transaction."""
        rows = [(key, json.dumps(value, ensure_ascii=False, sort_keys=True)) for key, value in records.items()]
        with self._mutex, self.db:
            self.db.executemany("INSERT INTO operations VALUES (?,?) ON CONFLICT(key) DO UPDATE SET state=excluded.state", rows)

    def compact(self, *, before: float) -> int:
        """Drop old confirmed content, retaining identity/hash receipts for deduplication.

        Never erase unresolved deliveries. This is deliberately not a DB-file copy.
        """
        count = 0
        phase_keys, card_prefixes = set(), set()
        for phase in self.all():
            if (phase.terminal and phase.delivery == "confirmed" and phase.seal == "closed"
                    and phase.updated_at < before and not phase.archived):
                phase.body, phase.tools, phase.tool_seen, phase.parts = "", {}, {}, []
                phase.archived = True
                self.save(phase)
                phase_keys.add(phase.identity.key)
                card_prefixes.add(digest(json.dumps(asdict(phase.identity.route), sort_keys=True)) + ":" + phase.card_id)
                count += 1
        with self._mutex:
            records = self.db.execute("SELECT key,state FROM operations").fetchall()
        for key, encoded in records:
            op = json.loads(encoded)
            if key.startswith(("control:", "panel:")) and op.get("expires_at", float("inf")) < before:
                with self._mutex, self.db:
                    self.db.execute("DELETE FROM operations WHERE key=?", (key,))
                continue
            if (op.get("state") == "done" and (key.split(":", 1)[0] in phase_keys
                    or ":".join(key.split(":")[:2]) in card_prefixes
                    or key.startswith(("panel-send:", "topic:")) and op.get("created_at", float("inf")) < before)):
                self.save_operation(key, {"state": "archived", "request_hash": op["request_hash"]})
        return count

    def backup(self, destination: Path):
        """SQLite online backup API, never shutil/cp of an active database."""
        if Path(destination).exists():
            raise FileExistsError(destination)
        target = sqlite3.connect(destination)
        try:
            with self._mutex:
                self.db.backup(target)
        finally:
            target.close()
        os.chmod(destination, 0o600)

    def close(self):
        self.db.close()
        self._owner.close()
