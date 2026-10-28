from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=5)
    path.chmod(0o600)
    db.execute("PRAGMA secure_delete=ON")
    db.execute("PRAGMA journal_mode=DELETE")
    return db


class Store:
    """Only reviewed examples and paused flags are persistent. No live chats."""

    def __init__(self, path: Path):
        self.db = connect(path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS examples (
                id INTEGER PRIMARY KEY, digest TEXT UNIQUE NOT NULL, text TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(text);
            CREATE TABLE IF NOT EXISTS paused (guild_id INTEGER PRIMARY KEY);
        """)

    def add_examples(self, texts: list[str]) -> int:
        inserted = 0
        with self.db:
            for text in texts:
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                cursor = self.db.execute("INSERT OR IGNORE INTO examples(digest,text) VALUES (?,?)", (digest, text))
                if cursor.rowcount:
                    self.db.execute("INSERT INTO search(rowid,text) VALUES (?,?)", (cursor.lastrowid, text))
                    inserted += 1
        return inserted

    def examples(self, prompt: str, limit: int = 4) -> list[str]:
        words = list(dict.fromkeys(word.lower() for word in re.findall(r"[^\W_]{2,24}", prompt, re.UNICODE) if not word.isnumeric()))[:12]
        rows = []
        if words:
            query = " OR ".join(f'"{word}"' for word in words)
            rows = self.db.execute("SELECT text FROM search WHERE search MATCH ? ORDER BY rank LIMIT ?", (query, limit)).fetchall()
        if not rows:
            rows = self.db.execute("SELECT text FROM examples ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [row[0] for row in rows]

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM examples").fetchone()[0]

    def set_paused(self, guild_id: int, paused: bool) -> None:
        with self.db:
            if paused:
                self.db.execute("INSERT OR IGNORE INTO paused VALUES (?)", (guild_id,))
            else:
                self.db.execute("DELETE FROM paused WHERE guild_id=?", (guild_id,))

    def is_paused(self, guild_id: int) -> bool:
        return self.db.execute("SELECT 1 FROM paused WHERE guild_id=?", (guild_id,)).fetchone() is not None

    def clear_examples(self) -> None:
        with self.db:
            self.db.execute("DELETE FROM examples")
            self.db.execute("DELETE FROM search")
            self.db.execute("INSERT INTO search(search) VALUES ('rebuild')")
        self.db.execute("VACUUM")

    def close(self) -> None:
        self.db.close()

