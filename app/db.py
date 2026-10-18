import time
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS members (
    tg_id    INTEGER PRIMARY KEY,
    role     TEXT NOT NULL,              -- owner | manager
    name     TEXT,
    added_at INTEGER
);
CREATE TABLE IF NOT EXISTS accounts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    olx_user_id   TEXT UNIQUE NOT NULL,
    label         TEXT NOT NULL,
    email         TEXT,
    emoji         TEXT NOT NULL,
    access_token  TEXT,
    refresh_token TEXT,
    expires_at    INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'active',  -- active | paused | auth_error
    baseline_done INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER
);
CREATE TABLE IF NOT EXISTS threads (
    account_id      INTEGER NOT NULL,
    thread_id       TEXT NOT NULL,
    advert_id       TEXT,
    interlocutor_id TEXT,
    total_count     INTEGER NOT NULL DEFAULT 0,
    unread_count    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, thread_id)
);
CREATE TABLE IF NOT EXISTS seen (
    account_id INTEGER NOT NULL,
    msg_id     TEXT NOT NULL,
    created_at INTEGER,
    PRIMARY KEY (account_id, msg_id)
);
CREATE TABLE IF NOT EXISTS links (
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    thread_id  TEXT NOT NULL,
    created_at INTEGER,
    PRIMARY KEY (chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS cache (
    account_id INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT,
    PRIMARY KEY (account_id, kind, key)
);
"""

EMOJIS = ["🟦", "🟩", "🟧", "🟪", "🟥", "🟨", "🟫", "🔵", "🟢", "🟠", "🟣", "🔴", "🟡", "🟤", "⚫", "⚪"]


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    async def _one(self, sql: str, args=()) -> dict | None:
        async with self.conn.execute(sql, args) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def _all(self, sql: str, args=()) -> list[dict]:
        async with self.conn.execute(sql, args) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def _exec(self, sql: str, args=()) -> None:
        await self.conn.execute(sql, args)
        await self.conn.commit()

    # ---------- settings ----------
    async def get_setting(self, key: str) -> str | None:
        row = await self._one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        await self._exec(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # ---------- members ----------
    async def get_member(self, tg_id: int) -> dict | None:
        return await self._one("SELECT * FROM members WHERE tg_id = ?", (tg_id,))

    async def list_members(self) -> list[dict]:
        return await self._all("SELECT * FROM members ORDER BY role DESC, added_at")

    async def add_member(self, tg_id: int, role: str, name: str) -> None:
        await self._exec(
            "INSERT INTO members(tg_id, role, name, added_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(tg_id) DO UPDATE SET name = excluded.name",
            (tg_id, role, name, int(time.time())),
        )

    async def remove_member(self, tg_id: int) -> None:
        await self._exec("DELETE FROM members WHERE tg_id = ? AND role != 'owner'", (tg_id,))

    # ---------- accounts ----------
    async def get_account(self, account_id: int) -> dict | None:
        return await self._one("SELECT * FROM accounts WHERE id = ?", (account_id,))

    async def list_accounts(self) -> list[dict]:
        return await self._all("SELECT * FROM accounts ORDER BY id")

    async def upsert_account(self, olx_user_id: str, label: str, email: str | None,
                             token: dict) -> tuple[int, bool]:
        """Создаёт аккаунт или обновляет токены существующего. Возвращает (id, создан_ли)."""
        expires_at = int(time.time()) + int(token.get("expires_in", 86400))
        existing = await self._one("SELECT id FROM accounts WHERE olx_user_id = ?", (olx_user_id,))
        if existing:
            await self._exec(
                "UPDATE accounts SET access_token = ?, refresh_token = ?, expires_at = ?, "
                "email = ?, status = 'active' WHERE id = ?",
                (token["access_token"], token.get("refresh_token"), expires_at, email, existing["id"]),
            )
            return existing["id"], False
        used = {a["emoji"] for a in await self.list_accounts()}
        emoji = next((e for e in EMOJIS if e not in used), EMOJIS[len(used) % len(EMOJIS)])
        cur = await self.conn.execute(
            "INSERT INTO accounts(olx_user_id, label, email, emoji, access_token, refresh_token, "
            "expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (olx_user_id, label, email, emoji, token["access_token"], token.get("refresh_token"),
             expires_at, int(time.time())),
        )
        await self.conn.commit()
        return cur.lastrowid, True

    async def save_tokens(self, account_id: int, token: dict) -> None:
        await self._exec(
            "UPDATE accounts SET access_token = ?, refresh_token = COALESCE(?, refresh_token), "
            "expires_at = ? WHERE id = ?",
            (token["access_token"], token.get("refresh_token"),
             int(time.time()) + int(token.get("expires_in", 86400)), account_id),
        )

    async def set_account_field(self, account_id: int, field: str, value) -> None:
        assert field in {"label", "status", "baseline_done"}
        await self._exec(f"UPDATE accounts SET {field} = ? WHERE id = ?", (value, account_id))

    async def delete_account(self, account_id: int) -> None:
        for table, col in (("threads", "account_id"), ("seen", "account_id"), ("links", "account_id"),
                           ("cache", "account_id"), ("accounts", "id")):
            await self.conn.execute(f"DELETE FROM {table} WHERE {col} = ?", (account_id,))
        await self.conn.commit()

    # ---------- threads ----------
    async def get_threads(self, account_id: int) -> dict[str, dict]:
        rows = await self._all("SELECT * FROM threads WHERE account_id = ?", (account_id,))
        return {r["thread_id"]: r for r in rows}

    async def get_thread(self, account_id: int, thread_id: str) -> dict | None:
        return await self._one(
            "SELECT * FROM threads WHERE account_id = ? AND thread_id = ?", (account_id, thread_id))

    async def save_thread(self, account_id: int, thread_id: str, advert_id, interlocutor_id,
                          total_count: int, unread_count: int) -> None:
        await self._exec(
            "INSERT INTO threads(account_id, thread_id, advert_id, interlocutor_id, total_count, unread_count) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(account_id, thread_id) DO UPDATE SET "
            "total_count = excluded.total_count, unread_count = excluded.unread_count",
            (account_id, thread_id, str(advert_id or ""), str(interlocutor_id or ""),
             total_count, unread_count),
        )

    async def set_unread(self, account_id: int, thread_id: str, unread: int) -> None:
        await self._exec("UPDATE threads SET unread_count = ? WHERE account_id = ? AND thread_id = ?",
                         (unread, account_id, thread_id))

    async def unread_threads(self, limit: int = 30) -> list[dict]:
        return await self._all(
            "SELECT t.* FROM threads t JOIN accounts a ON a.id = t.account_id "
            "WHERE t.unread_count > 0 ORDER BY a.id LIMIT ?", (limit,))

    async def count_threads(self, account_id: int) -> tuple[int, int]:
        row = await self._one(
            "SELECT COUNT(*) AS n, COALESCE(SUM(unread_count > 0), 0) AS u "
            "FROM threads WHERE account_id = ?", (account_id,))
        return row["n"], row["u"]

    # ---------- seen messages ----------
    async def is_seen(self, account_id: int, msg_id: str) -> bool:
        return await self._one("SELECT 1 AS x FROM seen WHERE account_id = ? AND msg_id = ?",
                               (account_id, msg_id)) is not None

    async def mark_seen(self, account_id: int, msg_id: str) -> None:
        await self._exec("INSERT OR IGNORE INTO seen(account_id, msg_id, created_at) VALUES (?, ?, ?)",
                         (account_id, msg_id, int(time.time())))

    # ---------- telegram message -> olx thread ----------
    async def add_link(self, chat_id: int, message_id: int, account_id: int, thread_id: str) -> None:
        await self._exec(
            "INSERT OR REPLACE INTO links(chat_id, message_id, account_id, thread_id, created_at) "
            "VALUES (?, ?, ?, ?, ?)", (chat_id, message_id, account_id, thread_id, int(time.time())))

    async def get_link(self, chat_id: int, message_id: int) -> dict | None:
        return await self._one("SELECT * FROM links WHERE chat_id = ? AND message_id = ?",
                               (chat_id, message_id))

    # ---------- cache (названия объявлений, имена собеседников) ----------
    async def cache_get(self, account_id: int, kind: str, key: str) -> str | None:
        row = await self._one("SELECT value FROM cache WHERE account_id = ? AND kind = ? AND key = ?",
                              (account_id, kind, key))
        return row["value"] if row else None

    async def cache_set(self, account_id: int, kind: str, key: str, value: str) -> None:
        await self._exec("INSERT OR REPLACE INTO cache(account_id, kind, key, value) VALUES (?, ?, ?, ?)",
                         (account_id, kind, key, value))

    async def cleanup(self, days: int = 180) -> None:
        border = int(time.time()) - days * 86400
        await self.conn.execute("DELETE FROM links WHERE created_at < ?", (border,))
        await self.conn.execute("DELETE FROM seen WHERE created_at < ?", (border,))
        await self.conn.commit()
