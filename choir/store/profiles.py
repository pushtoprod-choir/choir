import sqlite3
import json
from choir.schemas import UserProfile

DB_PATH = "choir.db"

# Starting confidence for a preference tag that's never been through a real
# negotiation outcome yet (e.g. freshly stated at onboarding) — see
# plan.md's Phase 8.1 sketch, which picked the same number: high enough that
# a stated preference is taken seriously immediately, low enough that it can
# still visibly move in either direction after just one or two negotiations.
DEFAULT_CONFIDENCE = 0.6
# Deliberately small and boring per plan.md's own reasoning: this doesn't
# need to be sophisticated to feel smart in a demo, it needs to visibly
# shift over a few real negotiations, not swing wildly on one data point.
CONFIDENCE_STEP = 0.05


def _connect() -> sqlite3.Connection:
    # timeout=10: wait up to 10s for a lock instead of failing immediately
    # with "database is locked" — cheap insurance now that negotiation
    # logging writes happen mid-negotiation (once per round), not just at
    # onboarding time, so concurrent access is more likely than it used to be.
    return sqlite3.connect(DB_PATH, timeout=10)


def init_db():
    conn = _connect()
    # WAL allows concurrent readers while a write transaction is open,
    # instead of the default rollback-journal mode locking the whole file for
    # the duration of a write — meaningfully reduces contention under the
    # same "more concurrent writes than before" pressure noted above.
    conn.execute("PRAGMA journal_mode=WAL")
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
    # preference_confidence may be absent on a profile row created before
    # this feature existed — ALTER guard, same pattern as `notes` above.
    # NULL/missing means "no tag has ever gone through get_profile's
    # default-fill yet", not "confidence zero" (see get_profile below).
    try:
        conn.execute("ALTER TABLE profiles ADD COLUMN preference_confidence TEXT")
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
    # Append-only audit log: every negotiation and every round it ran,
    # written incrementally as they happen (not just at the end), so a crash
    # or restart mid-negotiation still leaves a real record instead of total
    # silent loss. This is an audit trail, not a resume mechanism — recovering
    # an in-flight negotiation to continue exactly where it left off is a
    # bigger feature this doesn't attempt.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS negotiations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            goal_text TEXT,
            started_at TEXT,
            finished_at TEXT,
            converged INTEGER,
            decision TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS negotiation_rounds (
            negotiation_id INTEGER,
            round_num INTEGER,
            signals_json TEXT,
            recorded_at TEXT,
            PRIMARY KEY (negotiation_id, round_num)
        )
    """)
    # A trip is a container spanning possibly-multiple negotiations (e.g.
    # destination, then dates, then venue) between /choir start trip and
    # /choir end trip. One row per trip; negotiations link back via the
    # trip_id column added below.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trips (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            started_by INTEGER,
            title TEXT,
            started_at TEXT,
            ended_at TEXT,
            status TEXT,
            summary TEXT
        )
    """)
    # negotiations may already exist from before trips were added — ALTER
    # rather than rely on CREATE TABLE IF NOT EXISTS, which won't add columns
    # to an existing table. NULL trip_id means "not part of any trip" (the
    # case for every pre-existing row, and for negotiations run outside an
    # active trip).
    try:
        conn.execute("ALTER TABLE negotiations ADD COLUMN trip_id INTEGER")
    except sqlite3.OperationalError:
        pass
    # plan_date is known before the negotiation starts (extracted from the
    # /choir plan text); decided_time is only known if it converges — same
    # ALTER pattern as trip_id, added after negotiations already existed.
    try:
        conn.execute("ALTER TABLE negotiations ADD COLUMN plan_date TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE negotiations ADD COLUMN decided_time TEXT")
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
    # One row per (negotiation, person) whose calendar got a real event
    # created for that negotiation's decision — lets a later /choir update
    # find and delete exactly these events before creating new ones, instead
    # of stacking a duplicate event on top of a plan that's since changed.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS calendar_events (
            negotiation_id INTEGER,
            telegram_user_id INTEGER,
            event_id TEXT,
            PRIMARY KEY (negotiation_id, telegram_user_id)
        )
    """)
    conn.commit()
    conn.close()


def save_profile(profile: UserProfile):
    conn = _connect()
    conn.execute("""
        INSERT OR REPLACE INTO profiles
            (telegram_user_id, budget_min, budget_max, preferences, area, dietary_notes, temporary_context, notes,
             preference_confidence)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        profile.telegram_user_id, profile.budget_min, profile.budget_max,
        json.dumps(profile.preferences), profile.area,
        profile.dietary_notes, profile.temporary_context, profile.notes,
        json.dumps(profile.preference_confidence or {}),
    ))
    conn.commit()
    conn.close()


def get_profile(telegram_user_id: int) -> UserProfile | None:
    conn = _connect()
    row = conn.execute(
        "SELECT telegram_user_id, budget_min, budget_max, preferences, area, dietary_notes, temporary_context, "
        "notes, preference_confidence FROM profiles WHERE telegram_user_id = ?",
        (telegram_user_id,),
    ).fetchone()
    conn.close()
    if row is None:
        return None
    # NULL for anyone onboarded before this column existed, or before any
    # negotiation has run for them yet — an empty dict, not a crash.
    confidence = json.loads(row[8]) if row[8] else {}
    return UserProfile(
        telegram_user_id=row[0], budget_min=row[1], budget_max=row[2],
        preferences=json.loads(row[3]), area=row[4],
        dietary_notes=row[5], temporary_context=row[6], notes=row[7],
        preference_confidence=confidence,
    )


def update_preference_confidence(telegram_user_id: int, accepted: bool):
    """Called once per person after every negotiation resolves (converged or
    not), based on THEIR final-round stance — not the group's overall
    outcome, since one person accepting a compromise they don't love is a
    different signal than one person who never budged. Nudges every tag this
    person stated at onboarding by +/- CONFIDENCE_STEP, clamped to [0, 1].
    Deliberately doesn't try to attribute the outcome to any ONE specific
    tag (e.g. "budget mattered here, quiet_places didn't") — that needs a
    real classification step this hackathon-scale version skips in favor of
    a signal that's simple enough to trust and cheap enough to run every
    time."""
    profile = get_profile(telegram_user_id)
    if profile is None or not profile.preferences:
        return
    delta = CONFIDENCE_STEP if accepted else -CONFIDENCE_STEP
    updated = dict(profile.preference_confidence)
    for tag in profile.preferences:
        current = updated.get(tag, DEFAULT_CONFIDENCE)
        updated[tag] = round(max(0.0, min(1.0, current + delta)), 3)
    conn = _connect()
    conn.execute(
        "UPDATE profiles SET preference_confidence = ? WHERE telegram_user_id = ?",
        (json.dumps(updated), telegram_user_id),
    )
    conn.commit()
    conn.close()


def clear_temporary_context(telegram_user_id: int):
    conn = _connect()
    conn.execute("UPDATE profiles SET temporary_context = NULL WHERE telegram_user_id = ?", (telegram_user_id,))
    conn.commit()
    conn.close()


def record_seen_member(chat_id: int, user_id: int, first_name: str | None = None):
    conn = _connect()
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
    conn = _connect()
    rows = conn.execute("SELECT user_id FROM seen_members WHERE chat_id = ?", (chat_id,)).fetchall()
    conn.close()
    return [row[0] for row in rows]


def get_member_name(chat_id: int, user_id: int) -> str:
    # Raw Telegram IDs should never be shown to the group — "Member" is the
    # fallback whenever a name isn't on file for any reason.
    conn = _connect()
    row = conn.execute(
        "SELECT first_name FROM seen_members WHERE chat_id = ? AND user_id = ?",
        (chat_id, user_id),
    ).fetchone()
    conn.close()
    return row[0] if row and row[0] else "Member"


def start_negotiation_log(
    chat_id: int, goal_text: str, trip_id: int | None = None, plan_date: str | None = None
) -> int:
    """Call before run_negotiation(). Returns a negotiation_id to pass to
    record_negotiation_round() and finish_negotiation_log(). trip_id links
    this negotiation to the chat's active trip, if any — None (the default)
    means it isn't part of a trip. plan_date is known up front (extracted
    from the /choir plan text before negotiation starts), unlike
    decided_time which is only known if it converges."""
    conn = _connect()
    cursor = conn.execute(
        "INSERT INTO negotiations (chat_id, goal_text, started_at, converged, decision, trip_id, plan_date) "
        "VALUES (?, ?, datetime('now'), NULL, NULL, ?, ?)",
        (chat_id, goal_text, trip_id, plan_date),
    )
    conn.commit()
    negotiation_id = cursor.lastrowid
    conn.close()
    return negotiation_id


def record_negotiation_round(negotiation_id: int, round_num: int, signals_json: str):
    """Written as each round completes, not batched at the end — a crash
    mid-negotiation still leaves every round up to that point on disk."""
    conn = _connect()
    conn.execute(
        "INSERT OR REPLACE INTO negotiation_rounds (negotiation_id, round_num, signals_json, recorded_at) "
        "VALUES (?, ?, ?, datetime('now'))",
        (negotiation_id, round_num, signals_json),
    )
    conn.commit()
    conn.close()


def finish_negotiation_log(
    negotiation_id: int, converged: bool, decision: str | None, decided_time: str | None = None
):
    conn = _connect()
    conn.execute(
        "UPDATE negotiations SET finished_at = datetime('now'), converged = ?, decision = ?, decided_time = ? "
        "WHERE id = ?",
        (int(converged), decision, decided_time, negotiation_id),
    )
    conn.commit()
    conn.close()


def get_negotiation_history(chat_id: int, limit: int = 10) -> list[dict]:
    """Most recent negotiations for a chat, newest first — the actual
    auditability payoff: answering "what did we decide last time and why"
    without relying on Telegram's own message history."""
    conn = _connect()
    rows = conn.execute(
        "SELECT id, goal_text, started_at, finished_at, converged, decision, plan_date, decided_time "
        "FROM negotiations "
        # Ordered by id, not started_at: started_at has only second-level
        # granularity (SQLite's datetime('now')), so two negotiations
        # starting in the same second would tie under a timestamp sort. The
        # autoincrement id is always strictly increasing regardless.
        "WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
        (chat_id, limit),
    ).fetchall()
    conn.close()
    return [
        {
            "id": row[0],
            "goal_text": row[1],
            "started_at": row[2],
            "finished_at": row[3],
            "converged": bool(row[4]) if row[4] is not None else None,
            "decision": row[5],
            "plan_date": row[6],
            "decided_time": row[7],
        }
        for row in rows
    ]


def start_trip(chat_id: int, started_by: int, title: str | None) -> int:
    """Call when /choir start trip is issued. Returns a trip_id to pass to
    start_negotiation_log() for every negotiation run while this trip is
    active, and later to end_trip(). Caller is responsible for checking
    get_active_trip() first — this doesn't itself guard against a second
    trip starting in the same chat."""
    conn = _connect()
    cursor = conn.execute(
        "INSERT INTO trips (chat_id, started_by, title, started_at, status) "
        "VALUES (?, ?, ?, datetime('now'), 'active')",
        (chat_id, started_by, title),
    )
    conn.commit()
    trip_id = cursor.lastrowid
    conn.close()
    return trip_id


def get_active_trip(chat_id: int) -> dict | None:
    """The chat's currently in-progress trip, if any. Queried fresh from the
    DB (not cached in memory) so it survives a process restart the same way
    get_negotiation_history's fallback does."""
    conn = _connect()
    row = conn.execute(
        "SELECT id, chat_id, started_by, title, started_at FROM trips "
        "WHERE chat_id = ? AND status = 'active' ORDER BY id DESC LIMIT 1",
        (chat_id,),
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return {
        "id": row[0],
        "chat_id": row[1],
        "started_by": row[2],
        "title": row[3],
        "started_at": row[4],
    }


def get_trip_negotiations(trip_id: int) -> list[dict]:
    """Every negotiation run under a given trip, oldest first — the raw
    material end_trip() rolls up into a summary."""
    conn = _connect()
    rows = conn.execute(
        "SELECT goal_text, converged, decision, plan_date, decided_time FROM negotiations "
        "WHERE trip_id = ? ORDER BY id ASC",
        (trip_id,),
    ).fetchall()
    conn.close()
    return [
        {
            "goal_text": row[0],
            "converged": bool(row[1]) if row[1] is not None else None,
            "decision": row[2],
            "plan_date": row[3],
            "decided_time": row[4],
        }
        for row in rows
    ]


def end_trip(trip_id: int, summary: str | None):
    conn = _connect()
    conn.execute(
        "UPDATE trips SET ended_at = datetime('now'), status = 'ended', summary = ? WHERE id = ?",
        (summary, trip_id),
    )
    conn.commit()
    conn.close()


def record_calendar_events(negotiation_id: int, event_ids: dict[int, str]):
    """One row per person whose calendar actually got an event for this
    negotiation's decision — see get_calendar_events/delete_calendar_event_records,
    which a later /choir update uses to clean these up before creating new ones."""
    if not event_ids:
        return
    conn = _connect()
    conn.executemany(
        "INSERT OR REPLACE INTO calendar_events (negotiation_id, telegram_user_id, event_id) VALUES (?, ?, ?)",
        [(negotiation_id, user_id, event_id) for user_id, event_id in event_ids.items()],
    )
    conn.commit()
    conn.close()


def get_calendar_events(negotiation_id: int) -> dict[int, str]:
    conn = _connect()
    rows = conn.execute(
        "SELECT telegram_user_id, event_id FROM calendar_events WHERE negotiation_id = ?",
        (negotiation_id,),
    ).fetchall()
    conn.close()
    return {row[0]: row[1] for row in rows}


def delete_calendar_event_records(negotiation_id: int):
    """Removes the bookkeeping rows only — actually deleting the events on
    Google's side is choir.calendar.scheduling.delete_events_for_negotiation's
    job, since that needs a live access token per person, which this
    DB-only module has no business knowing about."""
    conn = _connect()
    conn.execute("DELETE FROM calendar_events WHERE negotiation_id = ?", (negotiation_id,))
    conn.commit()
    conn.close()


def get_trip_history(chat_id: int, limit: int = 10) -> list[dict]:
    """Most recent trips for a chat, newest first."""
    conn = _connect()
    rows = conn.execute(
        "SELECT id, title, started_at, ended_at, status, summary FROM trips "
        "WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
        (chat_id, limit),
    ).fetchall()
    conn.close()
    return [
        {
            "id": row[0],
            "title": row[1],
            "started_at": row[2],
            "ended_at": row[3],
            "status": row[4],
            "summary": row[5],
        }
        for row in rows
    ]
