"""Sequential processing with durable acknowledgement and atomic DB effects."""
import asyncio
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import logging
from pathlib import Path
import signal
import sqlite3

from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from aiogram.methods import AnswerCallbackQuery, EditMessageText, GetUpdates
from aiogram.types import Update

from core import db
from core.delivery import CURRENT_EVENT, enqueue, next_pending, stamp


@contextmanager
def instance_lock(path):
    """flock is supported on both Linux and macOS; released even after a crash."""
    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Эта база уже используется другим экземпляром бота.")
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


async def tolerate_stale_ui(make_request, bot, method):
    """Expired callback spinners and already-rendered cards must not undo events."""
    try:
        return await make_request(bot, method)
    except TelegramBadRequest as exc:
        text = exc.message.lower()
        if isinstance(method, AnswerCallbackQuery) and (
            "query is too old" in text or "query id is invalid" in text
        ):
            return True
        if isinstance(method, EditMessageText) and "message is not modified" in text:
            return True
        raise


async def process_one(conn, dispatcher, bot):
    row = next_pending(conn, bot.id)
    if row is None:
        return False
    # FSM persistence is intentionally out of scope. Roll back in-memory state
    # on a failed attempt so a retry in this process can use the same input.
    memory = deepcopy(dispatcher.storage.storage)
    token = CURRENT_EVENT.set(dict(row))
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.processing_event = True
        update = Update.model_validate_json(row["payload"], context={"bot": bot})
        await dispatcher.feed_update(bot, update)
        conn.execute("UPDATE telegram_inbox SET status='done', payload=NULL, attempts=attempts+1, "
                     "last_error=NULL, processed_at=? WHERE queue_id=?", (stamp(), row["queue_id"]))
        sqlite3.Connection.commit(conn)
        return True
    except BaseException as exc:
        sqlite3.Connection.rollback(conn)
        dispatcher.storage.storage = memory
        if isinstance(exc, Exception):
            # Store only the class: Telegram error text can contain user content.
            conn.execute("UPDATE telegram_inbox SET status='failed', attempts=attempts+1, last_error=? "
                         "WHERE queue_id=?", (type(exc).__name__, row["queue_id"]))
            sqlite3.Connection.commit(conn)
            logging.error("Update %s failed (%s); will retry in order", row["update_id"], type(exc).__name__)
        raise
    finally:
        conn.processing_event = False
        CURRENT_EVENT.reset(token)


async def collect(bot, conn, allowed_updates):
    # Start with unconfirmed Telegram updates rather than acknowledging a cursor
    # from a restored/old file. Duplicates are harmless because inbox IDs persist.
    offset = None
    while True:
        try:
            updates = await bot(GetUpdates(offset=offset, timeout=20, allowed_updates=allowed_updates),
                                request_timeout=30)
        except (TelegramNetworkError, TelegramRetryAfter, TelegramServerError) as exc:
            offset = None
            await asyncio.sleep(getattr(exc, "retry_after", 3))
            continue
        if updates:
            # The collector has its own connection. A busy processing transaction
            # must not block the event loop while receiving a batch.
            while True:
                try:
                    task = asyncio.create_task(asyncio.to_thread(enqueue, conn, bot.id, updates))
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        # Keep cancellation even if the final disk attempt fails.
                        await asyncio.gather(task, return_exceptions=True)
                        raise
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc):
                        raise
                    await asyncio.sleep(0.2)
            # Sending this offset acknowledges only a committed local batch.
            offset = max(u.update_id for u in updates) + 1
        else:
            # A previous offset has now been acknowledged. Avoid keeping it
            # indefinitely: Telegram can randomize IDs after a week of silence.
            offset = None


async def work(conn, dispatcher, bot):
    while True:
        try:
            processed = await process_one(conn, dispatcher, bot)
        except Exception as exc:
            # Preserve order: never silently skip a failed update.
            await asyncio.sleep(max(5, getattr(exc, "retry_after", 0)))
        else:
            if not processed:
                await asyncio.sleep(0.1)


async def run(conn, dispatcher, bot, path):
    receiver = db.connect(path)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    tasks = [asyncio.create_task(collect(bot, receiver, ["message", "callback_query"])),
             asyncio.create_task(work(conn, dispatcher, bot)), asyncio.create_task(stop.wait())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
        receiver.close()
        await dispatcher.storage.close()
