from __future__ import annotations

import hashlib
import re
import sqlite3
import json
from collections import Counter
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
            CREATE TABLE IF NOT EXISTS control (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
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

    def read_control(self) -> dict | None:
        row = self.db.execute("SELECT payload FROM control WHERE id=1").fetchone()
        return json.loads(row[0]) if row else None

    def write_control(self, value: dict) -> None:
        with self.db:
            self.db.execute("INSERT INTO control(id,payload) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload", (json.dumps(value, allow_nan=False),))

    def page(self, query: str = "", offset: int = 0, limit: int = 20) -> dict:
        query = query[:150]
        offset, limit = max(0, min(offset, 100_000)), max(1, min(limit, 50))
        pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where = "WHERE text LIKE ? ESCAPE '\\'" if query else ""
        parameters = (pattern,) if query else ()
        total = self.db.execute(f"SELECT COUNT(*) FROM examples {where}", parameters).fetchone()[0]
        rows = self.db.execute(f"SELECT id,text FROM examples {where} ORDER BY id DESC LIMIT ? OFFSET ?", (*parameters, limit, offset)).fetchall()
        return {"total": total, "offset": offset, "limit": limit, "items": [{"id": row[0], "text": row[1], "characters": len(row[1])} for row in rows]}

    def library_summary(self) -> dict:
        return self.summarize_texts(self.library_texts())

    def library_texts(self) -> list[str]:
        return [row[0] for row in self.db.execute("SELECT text FROM examples ORDER BY id DESC LIMIT 100000")]

    @staticmethod
    def summarize_texts(texts: list[str]) -> dict:
        stop = set("a an and are as at be been being but by can could did do does doing for from had has have how i if in into is it its just like me my no not of on or our out really so some than that the their them then there these they this those to too up us very was we were what when where which who why will with would you your yeah yes okay ok link email mention number secret everyone here person im ive dont thats youre lol don didn isn doesn wasn couldn shouldn wouldn haven hasn".split())
        counts: Counter[str] = Counter()
        messages, characters = 0, 0
        for text in texts:
            messages += 1
            characters += len(text)
            counts.update(word for word in re.findall(r"[^\W\d_]{3,24}", text.casefold()) if word not in stop)
        return {"messages": messages, "characters": characters, "unique_words": len(counts), "words": [{"word": word, "count": count} for word, count in counts.most_common(60)], "scan_limit": 100000}

