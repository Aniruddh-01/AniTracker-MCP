# mcp_neon.py
from dotenv import load_dotenv
load_dotenv()

from fastmcp import FastMCP
import os
import json
import asyncpg

from datetime import date, datetime, timedelta
from typing import Optional, List, Dict

# ==================================================
# CONFIG
# ==================================================
DB_DSN = os.environ.get("DATABASE_URL")
if not DB_DSN:
    raise RuntimeError("Please set DATABASE_URL")

BASE_DIR = os.path.dirname(__file__)
CATEGORIES_PATH = os.path.join(BASE_DIR, "categories.json")

mcp = FastMCP("AniTracker")

POOL: asyncpg.Pool | None = None
DB_READY = False


# ==================================================
# DATABASE SCHEMA
# ==================================================
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS categories (
    id SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS expenses (
    id SERIAL PRIMARY KEY,
    amount NUMERIC NOT NULL CHECK (amount > 0),
    category_id INTEGER NOT NULL REFERENCES categories(id),
    description TEXT,
    expense_date DATE NOT NULL,
    paid_by INTEGER NOT NULL REFERENCES users(id),
    split_type TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS expense_splits (
    id SERIAL PRIMARY KEY,
    expense_id INTEGER NOT NULL REFERENCES expenses(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    amount NUMERIC NOT NULL,
    UNIQUE(expense_id, user_id)
);

CREATE TABLE IF NOT EXISTS balances (
    from_user INTEGER NOT NULL,
    to_user INTEGER NOT NULL,
    amount NUMERIC NOT NULL,
    PRIMARY KEY (from_user, to_user)
);

CREATE TABLE IF NOT EXISTS settlements (
    id SERIAL PRIMARY KEY,
    from_user INTEGER NOT NULL,
    to_user INTEGER NOT NULL,
    amount NUMERIC NOT NULL,
    settlement_date DATE NOT NULL,
    note TEXT,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);
"""


# ==================================================
# LAZY POOL INITIALIZATION
# ==================================================
async def get_pool() -> asyncpg.Pool:
    global POOL, DB_READY

    if POOL is None:
        POOL = await asyncpg.create_pool(
            dsn=DB_DSN,
            min_size=1,
            max_size=5,
        )
        print("✅ Connected to Neon Postgres")

    if not DB_READY:
        async with POOL.acquire() as conn:
            await conn.execute(CREATE_SQL)

            if os.path.exists(CATEGORIES_PATH):
                with open(CATEGORIES_PATH, "r", encoding="utf-8") as f:
                    categories = json.load(f)
                    for cat in categories.keys():
                        await conn.execute(
                            """
                            INSERT INTO categories(name)
                            VALUES ($1)
                            ON CONFLICT (name) DO NOTHING
                            """,
                            cat,
                        )

        DB_READY = True
        print("✅ Database initialized")

    return POOL


# ==================================================
# DATE NORMALIZATION
# ==================================================
def normalize_date(d: Optional[str]) -> date:
    """
    Accepts:
      - None
      - '2026-01-29'
      - 'today'
      - 'yesterday'
      - 'tomorrow'
    Returns:
      datetime.date
    """
    if not d:
        return date.today()

    d = d.strip().lower()

    if d in ("today", "now"):
        return date.today()

    if d == "yesterday":
        return date.today() - timedelta(days=1)

    if d == "tomorrow":
        return date.today() + timedelta(days=1)

    return datetime.strptime(d, "%Y-%m-%d").date()


# ==================================================
# HELPERS
# ==================================================
async def get_user_id(conn, name: str) -> int:
    name = name.strip()

    row = await conn.fetchrow(
        "SELECT id FROM users WHERE name=$1",
        name,
    )
    if row:
        return row["id"]

    row = await conn.fetchrow(
        "INSERT INTO users(name) VALUES ($1) RETURNING id",
        name,
    )
    return row["id"]


async def get_category_id(conn, name: str) -> int:
    name = name.strip()

    # exact match
    row = await conn.fetchrow(
        """
        SELECT id FROM categories
        WHERE lower(name) = lower($1)
        """,
        name,
    )
    if row:
        return row["id"]

    # fuzzy match
    row = await conn.fetchrow(
        """
        SELECT id FROM categories
        WHERE lower(name) LIKE lower($1)
        LIMIT 1
        """,
        f"%{name}%",
    )
    if row:
        return row["id"]

    raise ValueError(
        f"Invalid category '{name}'. "
        "Use list_categories to see valid options."
    )


# ==================================================
# MCP TOOLS
# ==================================================
@mcp.tool()
async def list_users():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, name FROM users ORDER BY name"
        )
    return [{"id": r["id"], "name": r["name"]} for r in rows]


@mcp.tool()
async def list_categories():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT name FROM categories ORDER BY name"
        )
    return [r["name"] for r in rows]


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
    """
    expense_date examples:
      - "2026-01-29"
      - "today"
      - "yesterday"
      - "tomorrow"
    """

    pool = await get_pool()
    expense_date = normalize_date(expense_date)

    async with pool.acquire() as conn:
        async with conn.transaction():

            payer_id = await get_user_id(conn, paid_by)

            participant_ids = {}
            for p in participants:
                participant_ids[p] = await get_user_id(conn, p)

            cat_id = await get_category_id(conn, category)

            # -------------------------------
            # SPLIT LOGIC
            # -------------------------------
            if split_type == "equal":
                share = round(float(amount) / len(participants), 2)
                split_map = {p: share for p in participants}

            elif split_type == "unequal":
                if not splits:
                    raise ValueError("splits required for unequal split")
                split_map = splits

            elif split_type == "percent":
                if not splits:
                    raise ValueError("splits required for percent split")
                split_map = {
                    u: round(float(amount) * p / 100, 2)
                    for u, p in splits.items()
                }

            else:
                raise ValueError(
                    "split_type must be: equal | unequal | percent"
                )

            if isinstance(expense_date, str):
                expense_date = datetime.strptime(
                    expense_date, "%Y-%m-%d"
                ).date()


            row = await conn.fetchrow(
                """
                INSERT INTO expenses
                (amount, category_id, description,
                expense_date, paid_by, split_type)
                VALUES ($1,$2,$3,$4,$5,$6)
                RETURNING id
                """,
                amount,
                cat_id,
                description,
                expense_date,   # now guaranteed date object
                payer_id,
                split_type,
            )

            expense_id = row["id"]

            for user, amt in split_map.items():
                uid = participant_ids[user]

                await conn.execute(
                    """
                    INSERT INTO expense_splits
                    (expense_id,user_id,amount)
                    VALUES ($1,$2,$3)
                    """,
                    expense_id,
                    uid,
                    amt,
                )

                if uid != payer_id:
                    await conn.execute(
                        """
                        INSERT INTO balances
                        (from_user,to_user,amount)
                        VALUES ($1,$2,$3)
                        ON CONFLICT (from_user,to_user)
                        DO UPDATE
                        SET amount =
                          balances.amount + EXCLUDED.amount
                        """,
                        uid,
                        payer_id,
                        amt,
                    )

    return {
        "status": "ok",
        "expense_id": expense_id,
        "date": expense_date.isoformat(),
    }


@mcp.tool()
async def get_balances():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT u1.name AS from_user,
                   u2.name AS to_user,
                   b.amount
            FROM balances b
            JOIN users u1 ON u1.id = b.from_user
            JOIN users u2 ON u2.id = b.to_user
            ORDER BY b.amount DESC
            """
        )

    return [
        {
            "from": r["from_user"],
            "to": r["to_user"],
            "amount": float(r["amount"]),
        }
        for r in rows
    ]


# ==================================================
# MCP RESOURCE
# ==================================================
@mcp.resource("expense:///categories", mime_type="application/json")
def categories():
    with open(CATEGORIES_PATH, "r", encoding="utf-8") as f:
        return f.read()


# ==================================================
# RUN SERVER
# ==================================================
if __name__ == "__main__":
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=8000,
    )
