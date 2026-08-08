import sqlite3
import json
from choir.schemas import UserProfile

DB_PATH = "choir.db"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS profiles (
            telegram_user_id INTEGER PRIMARY KEY,
            budget_min INTEGER,
            budget_max INTEGER,
            preferences TEXT,        -- stored as JSON list
            area TEXT,
            dietary_notes TEXT,
            temporary_context TEXT
        )
    """)
    # profiles may already exist from before the onboarding free-text step was
    # added — ALTER rather than rely on CREATE TABLE IF NOT EXISTS, which
    # won't add columns to an existing table.
    try:
        conn.execute("ALTER TABLE profiles ADD COLUMN notes TEXT")
    except sqlite3.OperationalError:
        pass
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_members (
            chat_id INTEGER,
            user_id INTEGER,
            PRIMARY KEY (chat_id, user_id)
        )
    """)
    # seen_members may already exist from before first_name was added — ALTER
    # rather than rely on CREATE TABLE IF NOT EXISTS, which won't add columns
    # to an existing table.
    try:
        conn.execute("ALTER TABLE seen_members ADD COLUMN first_name TEXT")
    except sqlite3.OperationalError:
        pass
    conn.execute("""
        CREATE TABLE IF NOT EXISTS calendar_tokens (
            telegram_user_id INTEGER PRIMARY KEY,
            access_token TEXT,
            refresh_token TEXT,
            token_expiry INTEGER,
            connected_at INTEGER
        )
    """)
    conn.commit()
    conn.close()


def save_profile(profile: UserProfile):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT OR REPLACE INTO profiles
            (telegram_user_id, budget_min, budget_max, preferences, area, dietary_notes, temporary_context, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        profile.telegram_user_id, profile.budget_min, profile.budget_max,
        json.dumps(profile.preferences), profile.area,
        profile.dietary_notes, profile.temporary_context, profile.notes,
    ))
    conn.commit()
    conn.close()


def get_profile(telegram_user_id: int) -> UserProfile | None:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT telegram_user_id, budget_min, budget_max, preferences, area, dietary_notes, temporary_context, "
        "notes FROM profiles WHERE telegram_user_id = ?",
        (telegram_user_id,),
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return UserProfile(
        telegram_user_id=row[0], budget_min=row[1], budget_max=row[2],
        preferences=json.loads(row[3]), area=row[4],
        dietary_notes=row[5], temporary_context=row[6], notes=row[7],
    )


def clear_temporary_context(telegram_user_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE profiles SET temporary_context = NULL WHERE telegram_user_id = ?", (telegram_user_id,))
    conn.commit()
    conn.close()


def record_seen_member(chat_id: int, user_id: int, first_name: str | None = None):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR IGNORE INTO seen_members (chat_id, user_id, first_name) VALUES (?, ?, ?)",
        (chat_id, user_id, first_name),
    )
    if first_name is not None:
        # first_name is a property of the person, not the chat — backfill it
        # across every chat this user_id has a row in, not just this one, so
        # a name learned in one group (or during onboarding) fixes stale NULL
        # rows left over from before first_name existed.
        conn.execute(
            "UPDATE seen_members SET first_name = ? WHERE user_id = ?",
            (first_name, user_id),
        )
    conn.commit()
    conn.close()


def get_seen_members(chat_id: int) -> list[int]:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT user_id FROM seen_members WHERE chat_id = ?", (chat_id,)).fetchall()
    conn.close()
    return [row[0] for row in rows]


def get_member_name(chat_id: int, user_id: int) -> str:
    # Raw Telegram IDs should never be shown to the group — "Member" is the
    # fallback whenever a name isn't on file for any reason.
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT first_name FROM seen_members WHERE chat_id = ? AND user_id = ?",
        (chat_id, user_id),
    ).fetchone()
    conn.close()
    return row[0] if row and row[0] else "Member"
