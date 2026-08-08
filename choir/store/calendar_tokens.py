import time

from choir.schemas import CalendarTokens
from choir.store.profiles import _connect


def save_calendar_tokens(telegram_user_id: int, access_token: str, refresh_token: str, token_expiry: int):
    conn = _connect()
    conn.execute(
        """
        INSERT INTO calendar_tokens (telegram_user_id, access_token, refresh_token, token_expiry, connected_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(telegram_user_id) DO UPDATE SET
            access_token = excluded.access_token,
            refresh_token = excluded.refresh_token,
            token_expiry = excluded.token_expiry
        """,
        (telegram_user_id, access_token, refresh_token, token_expiry, int(time.time())),
    )
    conn.commit()
    conn.close()


def get_calendar_tokens(telegram_user_id: int) -> CalendarTokens | None:
    conn = _connect()
    row = conn.execute(
        "SELECT telegram_user_id, access_token, refresh_token, token_expiry "
        "FROM calendar_tokens WHERE telegram_user_id = ?",
        (telegram_user_id,),
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return CalendarTokens(telegram_user_id=row[0], access_token=row[1], refresh_token=row[2], token_expiry=row[3])


def is_calendar_connected(telegram_user_id: int) -> bool:
    return get_calendar_tokens(telegram_user_id) is not None
