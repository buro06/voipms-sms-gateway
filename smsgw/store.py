"""SQLite persistence: number <-> topic mapping, seen message ids, daily send quota."""
from dataclasses import dataclass
from datetime import date

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    number     TEXT PRIMARY KEY,
    name       TEXT,
    thread_id  INTEGER UNIQUE,
    closed     INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    voipms_id  TEXT,
    direction  TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    number     TEXT NOT NULL,
    body       TEXT,
    media      INTEGER NOT NULL DEFAULT 0,
    tg_user_id INTEGER,
    ts         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS messages_in_id ON messages (voipms_id) WHERE direction = 'in';
CREATE TABLE IF NOT EXISTS sends (
    day       TEXT PRIMARY KEY,
    count     INTEGER NOT NULL DEFAULT 0,
    exhausted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""


@dataclass
class Contact:
    number: str
    name: str | None
    thread_id: int | None
    closed: bool


class Store:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    @classmethod
    async def open(cls, path: str) -> "Store":
        db = await aiosqlite.connect(path)
        await db.executescript(SCHEMA)
        await db.commit()
        return cls(db)

    async def close(self) -> None:
        await self.db.close()

    # --- contacts -----------------------------------------------------------------

    @staticmethod
    def _contact(row) -> Contact | None:
        return Contact(row[0], row[1], row[2], bool(row[3])) if row else None

    async def contact(self, number: str) -> Contact | None:
        async with self.db.execute(
            "SELECT number, name, thread_id, closed FROM contacts WHERE number = ?", (number,)
        ) as cur:
            return self._contact(await cur.fetchone())

    async def contact_by_thread(self, thread_id: int) -> Contact | None:
        async with self.db.execute(
            "SELECT number, name, thread_id, closed FROM contacts WHERE thread_id = ?", (thread_id,)
        ) as cur:
            return self._contact(await cur.fetchone())

    async def contacts(self) -> list[Contact]:
        async with self.db.execute(
            "SELECT number, name, thread_id, closed FROM contacts ORDER BY COALESCE(name, number)"
        ) as cur:
            return [self._contact(r) for r in await cur.fetchall()]

    async def set_thread(self, number: str, thread_id: int | None) -> None:
        await self.db.execute(
            "INSERT INTO contacts (number, thread_id) VALUES (?, ?) "
            "ON CONFLICT(number) DO UPDATE SET thread_id = excluded.thread_id, closed = 0",
            (number, thread_id),
        )
        await self.db.commit()

    async def set_name(self, number: str, name: str | None) -> None:
        await self.db.execute(
            "INSERT INTO contacts (number, name) VALUES (?, ?) "
            "ON CONFLICT(number) DO UPDATE SET name = excluded.name",
            (number, name),
        )
        await self.db.commit()

    async def set_closed(self, number: str, closed: bool) -> None:
        await self.db.execute("UPDATE contacts SET closed = ? WHERE number = ?", (int(closed), number))
        await self.db.commit()

    # --- message log --------------------------------------------------------------

    async def seen(self, voipms_id: str) -> bool:
        async with self.db.execute(
            "SELECT 1 FROM messages WHERE direction = 'in' AND voipms_id = ?", (voipms_id,)
        ) as cur:
            return await cur.fetchone() is not None

    async def log_in(self, voipms_id: str, number: str, body: str | None, media: int = 0) -> None:
        await self.db.execute(
            "INSERT OR IGNORE INTO messages (voipms_id, direction, number, body, media) VALUES (?, 'in', ?, ?, ?)",
            (voipms_id, number, body, media),
        )
        await self.db.commit()

    async def log_out(self, voipms_id: str, number: str, body: str | None, media: int, tg_user_id: int | None) -> None:
        await self.db.execute(
            "INSERT INTO messages (voipms_id, direction, number, body, media, tg_user_id) VALUES (?, 'out', ?, ?, ?, ?)",
            (voipms_id, number, body, media, tg_user_id),
        )
        await self.db.commit()

    # --- quota --------------------------------------------------------------------

    async def sends_today(self, today: date | None = None) -> tuple[int, bool]:
        day = (today or date.today()).isoformat()
        async with self.db.execute("SELECT count, exhausted FROM sends WHERE day = ?", (day,)) as cur:
            row = await cur.fetchone()
        return (row[0], bool(row[1])) if row else (0, False)

    async def count_send(self, n: int = 1, today: date | None = None) -> int:
        day = (today or date.today()).isoformat()
        await self.db.execute(
            "INSERT INTO sends (day, count) VALUES (?, ?) ON CONFLICT(day) DO UPDATE SET count = count + excluded.count",
            (day, n),
        )
        await self.db.commit()
        return (await self.sends_today(today))[0]

    async def mark_exhausted(self, today: date | None = None) -> None:
        day = (today or date.today()).isoformat()
        await self.db.execute(
            "INSERT INTO sends (day, exhausted) VALUES (?, 1) ON CONFLICT(day) DO UPDATE SET exhausted = 1", (day,)
        )
        await self.db.commit()

    # --- key/value ----------------------------------------------------------------

    async def get(self, key: str) -> str | None:
        async with self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def put(self, key: str, value: str) -> None:
        await self.db.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self.db.commit()
