from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import aiosqlite


DEFAULT_ITEMS = [
    ("Хлеб", 25),
    ("Вода", 15),
    ("Яблоко", 20),
    ("Тушенка", 40),
    ("Чай", 10),
    ("Суп", 30),
]


def _normalize(text: str) -> str:
    return " ".join(text.casefold().strip().split())


def _loads_inventory(raw: str | None) -> list[str]:
    try:
        data = json.loads(raw or "[]")
        return [str(item) for item in data if str(item).strip()]
    except Exception:
        return []


def _dumps_inventory(items: list[str]) -> str:
    return json.dumps(items, ensure_ascii=False)


class GameDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL;")
        await self.conn.execute("PRAGMA foreign_keys=ON;")

    async def close(self) -> None:
        if self.conn is not None:
            await self.conn.close()
            self.conn = None

    async def init(self) -> None:
        if self.conn is None:
            await self.connect()

        assert self.conn is not None
        await self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                state TEXT DEFAULT 'new',
                player_name TEXT DEFAULT '',
                companion_name TEXT DEFAULT '',
                setting TEXT DEFAULT '',
                companion_personality TEXT DEFAULT '',
                hunger INTEGER DEFAULT 100,
                inventory TEXT DEFAULT '[]',
                current_location TEXT DEFAULT '',
                system_prompt TEXT DEFAULT '',
                last_scene_description TEXT DEFAULT '',
                bot_turns INTEGER DEFAULT 0,
                game_over INTEGER DEFAULT 0
            )
            """
        )
        await self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                hunger_restore INTEGER
            )
            """
        )
        await self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await self.conn.commit()
        await self.seed_items()

    async def seed_items(self) -> None:
        assert self.conn is not None
        async with self.conn.execute("SELECT COUNT(*) AS count FROM items") as cursor:
            row = await cursor.fetchone()
        if row and row["count"]:
            return
        await self.conn.executemany(
            "INSERT INTO items (name, hunger_restore) VALUES (?, ?)",
            DEFAULT_ITEMS,
        )
        await self.conn.commit()

    async def ensure_user(self, user_id: int) -> dict[str, Any]:
        assert self.conn is not None
        await self.conn.execute(
            """
            INSERT OR IGNORE INTO users (
                user_id, state, player_name, companion_name, setting,
                companion_personality, hunger, inventory, current_location,
                system_prompt, last_scene_description, bot_turns, game_over
            )
            VALUES (?, 'new', '', '', '', '', 100, '[]', '', '', '', 0, 0)
            """,
            (user_id,),
        )
        await self.conn.commit()
        return await self.get_user(user_id)

    async def get_user(self, user_id: int) -> dict[str, Any]:
        assert self.conn is not None
        async with self.conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row else {}

    async def update_user(self, user_id: int, **fields: Any) -> None:
        assert self.conn is not None
        if not fields:
            return
        allowed = {
            "state",
            "player_name",
            "companion_name",
            "setting",
            "companion_personality",
            "hunger",
            "inventory",
            "current_location",
            "system_prompt",
            "last_scene_description",
            "bot_turns",
            "game_over",
        }
        invalid = set(fields) - allowed
        if invalid:
            raise ValueError(f"Unsupported fields: {sorted(invalid)}")

        columns = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values()) + [user_id]
        await self.conn.execute(f"UPDATE users SET {columns} WHERE user_id = ?", values)
        await self.conn.commit()

    async def reset_user(self, user_id: int) -> None:
        await self.update_user(
            user_id,
            state="new",
            player_name="",
            companion_name="",
            setting="",
            companion_personality="",
            hunger=100,
            inventory="[]",
            current_location="",
            system_prompt="",
            last_scene_description="",
            bot_turns=0,
            game_over=0,
        )

    async def add_message(self, user_id: int, role: str, content: str) -> None:
        assert self.conn is not None
        await self.conn.execute(
            "INSERT INTO messages (user_id, role, content) VALUES (?, ?, ?)",
            (user_id, role, content),
        )
        await self.conn.commit()

    async def get_recent_messages(self, user_id: int, limit: int) -> list[dict[str, str]]:
        assert self.conn is not None
        async with self.conn.execute(
            """
            SELECT role, content
            FROM messages
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (user_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        rows = list(reversed(rows))
        return [{"role": row["role"], "content": row["content"]} for row in rows]

    async def get_items(self) -> list[dict[str, Any]]:
        assert self.conn is not None
        async with self.conn.execute(
            "SELECT id, name, hunger_restore FROM items ORDER BY id ASC"
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def find_item(self, query: str) -> dict[str, Any] | None:
        query_norm = _normalize(query)
        items = await self.get_items()
        for item in items:
            if _normalize(item["name"]) == query_norm:
                return item
        for item in items:
            if query_norm and query_norm in _normalize(item["name"]):
                return item
        return None

    async def get_inventory(self, user_id: int) -> list[str]:
        user = await self.get_user(user_id)
        return _loads_inventory(user.get("inventory"))

    async def set_inventory(self, user_id: int, inventory: list[str]) -> None:
        await self.update_user(user_id, inventory=_dumps_inventory(inventory))

    async def consume_item(self, user_id: int, item_name: str) -> tuple[bool, str, int]:
        assert self.conn is not None
        async with self._lock:
            user = await self.get_user(user_id)
            if not user:
                return False, "Пользователь не найден.", 0
            inventory = _loads_inventory(user.get("inventory"))
            target_index = next(
                (
                    index
                    for index, value in enumerate(inventory)
                    if _normalize(value) == _normalize(item_name)
                ),
                None,
            )
            if target_index is None:
                return False, "Такого предмета нет в инвентаре.", 0
            item = await self.find_item(item_name)
            if item is None:
                return False, "Этот предмет нельзя съесть.", 0
            inventory.pop(target_index)
            hunger = min(100, int(user["hunger"]) + int(item["hunger_restore"]))
            await self.conn.execute(
                "UPDATE users SET hunger = ?, inventory = ? WHERE user_id = ?",
                (hunger, _dumps_inventory(inventory), user_id),
            )
            await self.conn.commit()
            return True, item["name"], int(item["hunger_restore"])

    async def advance_story_turn(self, user_id: int) -> dict[str, int]:
        assert self.conn is not None
        async with self._lock:
            user = await self.get_user(user_id)
            if not user:
                return {"bot_turns": 0, "hunger": 0, "game_over": 1}

            bot_turns = int(user["bot_turns"]) + 1
            hunger = int(user["hunger"])
            game_over = int(user["game_over"])

            if bot_turns % 5 == 0 and hunger > 0:
                hunger = max(0, hunger - 5)
                if hunger == 0:
                    game_over = 1

            await self.conn.execute(
                "UPDATE users SET bot_turns = ?, hunger = ?, game_over = ? WHERE user_id = ?",
                (bot_turns, hunger, game_over, user_id),
            )
            await self.conn.commit()
            return {"bot_turns": bot_turns, "hunger": hunger, "game_over": game_over}
