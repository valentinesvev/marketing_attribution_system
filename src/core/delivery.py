"""Durable Telegram inbox. No Telegram/network dependencies in storage code."""
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone

CURRENT_EVENT = ContextVar("current_event", default=None)
MSK = timezone(timedelta(hours=3))


def stamp(value=None):
    return (value or datetime.now(MSK)).astimezone(MSK).strftime("%Y-%m-%d %H:%M:%S")


def enqueue(conn, bot_id, updates):
    """Commit the whole batch before allowing the next getUpdates offset."""
    with conn:
        for update in updates:
            received = stamp()
            message = update.message
            event_at = stamp(message.date) if message else received
            origin = "telegram_message" if message else "received_estimate"
            conn.execute(
                "INSERT OR IGNORE INTO telegram_inbox "
                "(bot_id, update_id, payload, event_at, received_at, time_source) VALUES (?,?,?,?,?,?)",
                (bot_id, update.update_id, update.model_dump_json(exclude_none=True),
                 event_at, received, origin),
            )
        if updates:
            offset = max(u.update_id for u in updates) + 1
            conn.execute("INSERT INTO telegram_polling(bot_id, next_offset) VALUES (?,?) "
                         "ON CONFLICT(bot_id) DO UPDATE SET next_offset=excluded.next_offset",
                         (bot_id, offset))


def next_pending(conn, bot_id):
    return conn.execute("SELECT * FROM telegram_inbox WHERE bot_id=? AND status!='done' "
                        "ORDER BY queue_id LIMIT 1", (bot_id,)).fetchone()


def event_time():
    event = CURRENT_EVENT.get()
    return event["event_at"] if event else stamp()


def audit(conn, table, pk, value):
    event = CURRENT_EVENT.get()
    if event:
        conn.execute(f'UPDATE "{table}" SET source_bot_id=?, source_update_id=?, processed_at=?, '
                     f'time_source=? WHERE "{pk}"=?',
                     (event["bot_id"], event["update_id"], stamp(), event["time_source"], value))
