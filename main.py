from fastmcp import FastMCP
import os
import json
import sqlite3
import aiosqlite
import tempfile
from datetime import datetime, date
from typing import Optional, List, Dict, Any

# ==================================================
# CONFIG
# ==================================================

TEMP_DIR = tempfile.gettempdir()
DB_PATH = os.path.join(TEMP_DIR, "expenses.db")

BASE_DIR = os.path.dirname(__file__)
CATEGORIES_PATH = os.path.join(BASE_DIR, "categories.json")

mcp = FastMCP("AniTracker")

# ==================================================
# SYNC DATABASE INIT (SAFE)
# ==================================================

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON;")

        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        );

        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            amount REAL NOT NULL CHECK(amount > 0),
            category_id INTEGER NOT NULL,
            description TEXT,
            expense_date TEXT NOT NULL,
            paid_by INTEGER NOT NULL,
            split_type TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (category_id) REFERENCES categories(id),
            FOREIGN KEY (paid_by) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS expense_splits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            expense_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            FOREIGN KEY (expense_id) REFERENCES expenses(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id),
            UNIQUE(expense_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS balances (
            from_user INTEGER NOT NULL,
            to_user INTEGER NOT NULL,
            amount REAL NOT NULL,
            PRIMARY KEY (from_user, to_user)
        );

        CREATE TABLE IF NOT EXISTS settlements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_user INTEGER NOT NULL,
            to_user INTEGER NOT NULL,
            amount REAL NOT NULL,
            settlement_date TEXT NOT NULL,
            note TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)

        if os.path.exists(CATEGORIES_PATH):
            with open(CATEGORIES_PATH, "r", encoding="utf-8") as f:
                cats = json.load(f)
                for cat in cats.keys():
                    conn.execute(
                        "INSERT OR IGNORE INTO categories(name) VALUES (?)",
                        (cat,)
                    )

        conn.commit()

# Initialize immediately (safe)
init_db()

# ==================================================
# HELPERS
# ==================================================

def normalize_date(d: Optional[str]) -> str:
    if not d:
        return date.today().isoformat()
    datetime.strptime(d, "%Y-%m-%d")
    return d

async def get_user_id(conn, name: str) -> int:
    async with conn.execute(
        "SELECT id FROM users WHERE name=?", (name.strip(),)
    ) as cur:
        row = await cur.fetchone()

    if row:
        return row[0]

    cur = await conn.execute(
        "INSERT INTO users(name) VALUES (?)", (name.strip(),)
    )
    return cur.lastrowid

async def get_category_id(conn, name: str) -> int:
    async with conn.execute(
        "SELECT id FROM categories WHERE name=?", (name.strip(),)
    ) as cur:
        row = await cur.fetchone()

    if not row:
        raise ValueError(f"Invalid category: {name}")

    return row[0]

# ==================================================
# MCP TOOLS
# ==================================================

@mcp.tool()
async def list_users():
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("SELECT id, name FROM users ORDER BY name") as cur:
            return [{"id": r[0], "name": r[1]} for r in await cur.fetchall()]

@mcp.tool()
async def add_split_expense(
    amount: float,
    category: str,
    paid_by: str,
    participants: List[str],
    split_type: str,
    splits: Optional[Dict[str, float]] = None,
    description: Optional[str] = None,
    expense_date: Optional[str] = None,
):
    expense_date = normalize_date(expense_date)

    async with aiosqlite.connect(DB_PATH) as conn:
        payer_id = await get_user_id(conn, paid_by)
        participant_ids = {p: await get_user_id(conn, p) for p in participants}
        cat_id = await get_category_id(conn, category)

        if split_type == "equal":
            share = round(amount / len(participants), 2)
            split_map = {p: share for p in participants}
        elif split_type == "unequal":
            split_map = splits
        else:
            split_map = {u: round(amount * p / 100, 2) for u, p in splits.items()}

        cur = await conn.execute(
            """
            INSERT INTO expenses
            (amount, category_id, description, expense_date, paid_by, split_type)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (amount, cat_id, description, expense_date, payer_id, split_type),
        )

        expense_id = cur.lastrowid

        for user, amt in split_map.items():
            uid = participant_ids[user]
            await conn.execute(
                "INSERT INTO expense_splits(expense_id, user_id, amount) VALUES (?, ?, ?)",
                (expense_id, uid, amt),
            )

            if uid != payer_id:
                await conn.execute(
                    """
                    INSERT INTO balances(from_user, to_user, amount)
                    VALUES (?, ?, ?)
                    ON CONFLICT(from_user, to_user)
                    DO UPDATE SET amount = amount + excluded.amount
                    """,
                    (uid, payer_id, amt),
                )

        await conn.commit()

    return {"status": "ok", "expense_id": expense_id}

@mcp.tool()
async def get_balances():
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("""
            SELECT u1.name, u2.name, b.amount
            FROM balances b
            JOIN users u1 ON u1.id = b.from_user
            JOIN users u2 ON u2.id = b.to_user
            ORDER BY b.amount DESC
        """) as cur:
            return [
                {"from": r[0], "to": r[1], "amount": r[2]}
                for r in await cur.fetchall()
            ]

# ==================================================
# MCP RESOURCE
# ==================================================

@mcp.resource("expense:///categories", mime_type="application/json")
def categories():
    with open(CATEGORIES_PATH, "r", encoding="utf-8") as f:
        return f.read()

# ==================================================
# START SERVER
# ==================================================

if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8000)
