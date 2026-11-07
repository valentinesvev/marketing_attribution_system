import asyncio
import importlib.util
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.types import Message, Update, User

from bot.reliable import collect, instance_lock, process_one, tolerate_stale_ui
from core import config, db
from core.delivery import enqueue

ROOT = Path(__file__).resolve().parents[1]


class OfflineSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.fail = False
        self.expired = False
        self.exported = None
        self.sent = []

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        yield b""

    async def make_request(self, bot, method, timeout=None):
        name = type(method).__name__
        self.sent.append(name)
        if self.fail and name == "SendMessage":
            raise TelegramNetworkError(method=method, message="offline")
        if self.expired and name == "AnswerCallbackQuery":
            raise TelegramBadRequest(method=method, message="query is too old")
        if name == "GetMe":
            return User(id=123456, is_bot=True, first_name="Test", username="test_bot")
        if name == "SendDocument":
            self.exported = Path(method.document.path).read_bytes()
        if name in ("SendMessage", "EditMessageText", "SendDocument"):
            return Message(message_id=900, date=1800000000, chat={"id": 7, "type": "private"}, text="ok")
        return True


def message(i, text, user=7, date=1788253200):
    return Update.model_validate({"update_id": i, "message": {
        "message_id": i, "date": date, "chat": {"id": user, "type": "private"},
        "from": {"id": user, "is_bot": False, "first_name": "Tester"}, "text": text,
    }})


def callback(i, data, user=7):
    return Update.model_validate({"update_id": i, "callback_query": {
        "id": str(i), "chat_instance": "test", "data": data,
        "from": {"id": user, "is_bot": False, "first_name": "Tester"},
        "message": {"message_id": 900, "date": 1700000000,
                    "chat": {"id": user, "type": "private"}, "text": "old card"},
    }})


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "bot.db"))
    monkeypatch.setattr(config, "ADMIN_IDS", {7})
    monkeypatch.setattr(config, "MANAGER_IDS", {7})
    monkeypatch.setattr(config, "MANAGER_CHAT_ID", None)
    spec = importlib.util.spec_from_file_location("bot.test_instance", ROOT / "src/bot/main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    session = OfflineSession()
    session.middleware(tolerate_stale_ui)
    bot = Bot("123456:offline-test-token", session=session)
    dispatcher = Dispatcher()
    dispatcher.include_router(mod.router)
    yield mod, dispatcher, bot, session
    mod.conn.close()


def drain(app, updates):
    mod, dispatcher, bot, _ = app
    enqueue(mod.conn, bot.id, updates)
    async def run():
        while await asyncio.wait_for(process_one(mod.conn, dispatcher, bot), timeout=5):
            pass
    asyncio.run(run())


def test_downtime_dates_order_and_duplicate_updates(app):
    mod, _, bot, _ = app
    batch = [message(1, "/start ad"), callback(2, "lead:general"), message(3, "/paid 1001 500", date=1788339600)]
    drain(app, batch)
    drain(app, batch)
    assert mod.conn.execute("SELECT COUNT(*) FROM fact_touch").fetchone()[0] == 1
    assert mod.conn.execute("SELECT COUNT(*) FROM fact_lead").fetchone()[0] == 1
    assert mod.conn.execute("SELECT COUNT(*) FROM fact_order").fetchone()[0] == 1
    touch = mod.conn.execute("SELECT * FROM fact_touch").fetchone()
    assert touch["timestamp"] == "2026-09-01 12:00:00"
    assert touch["time_source"] == "telegram_message"
    assert touch["processed_at"] > touch["timestamp"]
    assert touch["source_bot_id"] == bot.id and touch["source_update_id"] == 1
    lead = mod.conn.execute("SELECT * FROM fact_lead").fetchone()
    queued = mod.conn.execute("SELECT * FROM telegram_inbox WHERE update_id=2").fetchone()
    assert lead["conversation_started_at"] == queued["received_at"]
    assert lead["time_source"] == "received_estimate"  # not the old card's date
    assert mod.conn.execute("SELECT COUNT(*) FROM telegram_inbox WHERE payload IS NOT NULL").fetchone()[0] == 0
    # Repeating payment as a NEW message must also not double-charge a lead.
    drain(app, [message(4, "/paid 1001 500")])
    assert mod.conn.execute("SELECT COUNT(*) FROM fact_order").fetchone()[0] == 1


def test_network_failure_rolls_back_then_retries_without_duplicates(app):
    mod, dispatcher, bot, session = app
    session.fail = True
    enqueue(mod.conn, bot.id, [message(1, "/start ad"), message(2, "/start next")])
    with pytest.raises(TelegramNetworkError):
        asyncio.run(process_one(mod.conn, dispatcher, bot))
    assert mod.conn.execute("SELECT COUNT(*) FROM dim_user").fetchone()[0] == 0
    assert mod.conn.execute("SELECT COUNT(*) FROM fact_touch").fetchone()[0] == 0
    assert mod.conn.execute("SELECT status FROM telegram_inbox WHERE update_id=2").fetchone()[0] == "pending"
    session.fail = False
    drain(app, [])
    assert [r[0] for r in mod.conn.execute("SELECT placement_id FROM fact_touch ORDER BY touch_id")] == ["ad", "next"]


def test_payment_retry_rolls_back_order_items_and_status(app):
    mod, dispatcher, bot, session = app
    drain(app, [message(1, "/start ad"), callback(2, "lead:general")])
    session.fail = True
    enqueue(mod.conn, bot.id, [message(3, "/paid 1001 500")])
    with pytest.raises(TelegramNetworkError):
        asyncio.run(process_one(mod.conn, dispatcher, bot))
    assert mod.conn.execute("SELECT COUNT(*) FROM fact_order_item").fetchone()[0] == 0
    assert mod.conn.execute("SELECT status FROM fact_lead").fetchone()[0] == "open"
    session.fail = False
    drain(app, [])
    assert mod.conn.execute("SELECT COUNT(*) FROM fact_order").fetchone()[0] == 1


def test_expired_button_does_not_lose_lead(app):
    mod, _, _, session = app
    session.expired = True
    drain(app, [message(1, "/start ad"), callback(2, "lead:general")])
    assert mod.conn.execute("SELECT COUNT(*) FROM fact_lead").fetchone()[0] == 1


def test_export_and_transfer_preserve_queue_and_dedup(app, tmp_path):
    mod, _, bot, session = app
    drain(app, [message(1, "/start ad")])
    # Export occurs with another event already queued; both must survive transfer.
    drain(app, [message(2, "/export"), message(3, "/start next")])
    restored = tmp_path / "restored.db"
    restored.write_bytes(session.exported)
    mod.conn.close()
    mod.conn = db.connect(str(restored))
    assert mod.conn.execute("SELECT status FROM telegram_inbox WHERE update_id=2").fetchone()[0] == "done"
    assert mod.conn.execute("SELECT status FROM telegram_inbox WHERE update_id=3").fetchone()[0] == "pending"
    drain(app, [message(1, "/start ad"), message(3, "/start next")])
    assert mod.conn.execute("SELECT COUNT(*) FROM fact_touch").fetchone()[0] == 2
    assert mod.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_collector_acknowledges_only_committed_batch(tmp_path):
    conn = db.connect(str(tmp_path / "queue.db"))
    class FakeBot:
        id = 123456
        count = 0
        async def __call__(self, method, **kwargs):
            self.count += 1
            if self.count == 1:
                assert method.offset is None
                return [message(10, "/start")]
            assert method.offset == 11
            with sqlite3.connect(tmp_path / "queue.db") as check:
                assert check.execute("SELECT update_id FROM telegram_inbox").fetchone()[0] == 10
                assert check.execute("SELECT next_offset FROM telegram_polling").fetchone()[0] == 11
            raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(collect(FakeBot(), conn, ["message"]))
    conn.close()


def test_crash_during_transaction_is_recoverable(app, tmp_path):
    mod, _, bot, _ = app
    enqueue(mod.conn, bot.id, [message(1, "/start")])
    code = '''
import os, sys
from core import db
c=db.connect(sys.argv[1])
c.execute('BEGIN IMMEDIATE')
c.processing_event=True
db.upsert_user(c, 'uncommitted')
db.add_touch(c, 'uncommitted', None)
os._exit(17)
'''
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    result = subprocess.run([sys.executable, "-c", code, config.DB_PATH], env=env, timeout=15)
    assert result.returncode == 17
    assert mod.conn.execute("SELECT COUNT(*) FROM dim_user").fetchone()[0] == 0
    drain(app, [])
    assert mod.conn.execute("SELECT COUNT(*) FROM fact_touch").fetchone()[0] == 1


def test_migrate_old_database_and_schema_reference(tmp_path):
    import re
    old = re.sub(r",\n    source_bot_id INTEGER,\n    source_update_id INTEGER,\n    processed_at TEXT,\n    time_source TEXT NOT NULL DEFAULT 'legacy'", "", db.SCHEMA)
    c = sqlite3.connect(tmp_path / "old.db")
    c.executescript(old)
    c.execute("INSERT INTO dim_user VALUES ('old', NULL, '2026-01-01', NULL, 'bot')")
    c.execute("INSERT INTO fact_touch(user_id_hash,timestamp) VALUES ('old','2026-01-01')")
    c.commit()
    c.close()
    migrated = db.connect(str(tmp_path / "old.db"))
    assert migrated.execute("SELECT time_source FROM fact_touch").fetchone()[0] == "legacy"
    reference = sqlite3.connect(":memory:")
    reference.executescript((ROOT / "data/schema.sql").read_text())
    for table in ("telegram_inbox", "telegram_polling", "fact_touch", "fact_order", "fact_lead", "fact_bot_event"):
        assert [tuple(r) for r in migrated.execute(f'PRAGMA table_info({table})')] == reference.execute(f'PRAGMA table_info({table})').fetchall()
    migrated.close()


def test_instance_lock_and_missing_token_startup(tmp_path):
    with instance_lock(tmp_path / "bot.db"):
        with pytest.raises(SystemExit, match="уже используется"):
            with instance_lock(tmp_path / "bot.db"):
                pass
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), DB_PATH=str(tmp_path / "fresh.db"),
               BOT_TOKEN="", ADMIN_IDS="", MANAGER_IDS="", MANAGER_CHAT_ID="", MANAGER_USERNAME="test")
    result = subprocess.run([sys.executable, "-m", "bot.main"], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode != 0
    assert "Нет BOT_TOKEN" in result.stderr
    assert (tmp_path / "fresh.db").exists()


def test_failed_disk_write_does_not_acknowledge(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "queue.db"))
    offsets = []
    class FakeBot:
        id = 123456
        async def __call__(self, method, **kwargs):
            offsets.append(method.offset)
            return [message(1, "/start")]
    def fail(*args):
        raise sqlite3.OperationalError("disk full")
    monkeypatch.setattr("bot.reliable.enqueue", fail)
    with pytest.raises(sqlite3.OperationalError, match="disk full"):
        asyncio.run(collect(FakeBot(), conn, ["message"]))
    assert offsets == [None]
    assert conn.execute("SELECT COUNT(*) FROM telegram_polling").fetchone()[0] == 0
    conn.close()


def test_real_entrypoint_with_mocked_telegram(app, monkeypatch, capsys):
    mod, dispatcher, bot, session = app
    # main constructs its own dispatcher, so detach the test fixture router.
    dispatcher.sub_routers.clear()
    mod.router._parent_router = None
    monkeypatch.setattr(config, "BOT_TOKEN", "123456:offline-test-token")
    monkeypatch.setattr(mod, "Bot", lambda *args, **kwargs: bot)
    async def short_run(conn, dp, actual_bot, path):
        enqueue(conn, actual_bot.id, [message(1, "/start ad"), callback(2, "lead:general")])
        while await process_one(conn, dp, actual_bot):
            pass
    monkeypatch.setattr(mod, "run", short_run)
    asyncio.run(mod.main())
    assert "Бот @test_bot запущен" in capsys.readouterr().out
    assert "SetMyCommands" in session.sent
    assert mod.conn.execute("SELECT COUNT(*) FROM fact_lead").fetchone()[0] == 1


def test_restart_without_fsm_does_not_silently_accept_amount(app):
    mod, _, _, session = app
    drain(app, [message(1, "500")])
    assert "SendMessage" in session.sent
    assert mod.conn.execute("SELECT COUNT(*) FROM fact_order").fetchone()[0] == 0


def test_report_after_recovery_and_export(app):
    mod, _, _, session = app
    drain(app, [message(1, "/start ad"), callback(2, "lead:general"), message(3, "/paid 1001 500")])
    for i, model in enumerate(("first", "last", "linear", "position"), 4):
        drain(app, [message(i, "/report " + model)])
    assert mod.conn.execute("SELECT COUNT(*) FROM fact_attribution").fetchone()[0] == 4
    assert mod.conn.execute("SELECT COUNT(*) FROM telegram_inbox WHERE status!='done'").fetchone()[0] == 0


def test_running_collector_worker_and_graceful_shutdown(app):
    import signal
    from bot.reliable import run
    mod, dispatcher, bot, _ = app
    class PollSession(OfflineSession):
        fetched = False
        async def make_request(self, bot, method, timeout=None):
            if type(method).__name__ == "GetUpdates":
                if not self.fetched:
                    self.fetched = True
                    return [message(1, "/start ad"), callback(2, "lead:general")]
                await asyncio.sleep(0.02)
                return []
            return await super().make_request(bot, method, timeout)
    bot.session = PollSession()
    async def scenario():
        async def stop_when_processed():
            while mod.conn.execute("SELECT COUNT(*) FROM telegram_inbox WHERE status='done'").fetchone()[0] < 2:
                await asyncio.sleep(0.02)
            os.kill(os.getpid(), signal.SIGTERM)
        watcher = asyncio.create_task(stop_when_processed())
        try:
            await asyncio.wait_for(run(mod.conn, dispatcher, bot, config.DB_PATH), 10)
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
    asyncio.run(scenario())
    assert mod.conn.execute("SELECT COUNT(*) FROM fact_lead").fetchone()[0] == 1
    assert not mod.conn.in_transaction


def test_launcher_in_path_with_spaces(tmp_path):
    import shutil
    project = tmp_path / "project with spaces"
    shutil.copytree(ROOT / "scripts", project / "scripts")
    shutil.copytree(ROOT / "src", project / "src", ignore=shutil.ignore_patterns(".env", "data", "__pycache__"))
    (project / "src/.env").write_text("BOT_TOKEN=\n")
    # Reuse this interpreter's environment; no download or real token.
    (project / ".venv312").symlink_to(Path(sys.prefix), target_is_directory=True)
    env = dict(os.environ, BOT_TOKEN="", DB_PATH=str(project / "fresh.db"),
               ADMIN_IDS="", MANAGER_IDS="", MANAGER_CHAT_ID="", MANAGER_USERNAME="test")
    result = subprocess.run(["bash", str(project / "scripts/start.sh")], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=15)
    assert "Нет BOT_TOKEN" in result.stderr
    assert (project / "fresh.db").exists()


def test_collector_cancellation_survives_disk_error(tmp_path, monkeypatch):
    import threading
    conn = db.connect(str(tmp_path / "queue.db"))
    started = threading.Event()
    release = threading.Event()
    def delayed_failure(*args):
        started.set()
        release.wait(5)
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr("bot.reliable.enqueue", delayed_failure)
    class FakeBot:
        id = 123456
        async def __call__(self, *args, **kwargs):
            return [message(1, "/start")]
    async def scenario():
        task = asyncio.create_task(collect(FakeBot(), conn, ["message"]))
        while not started.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
    asyncio.run(scenario())
    conn.close()


def test_report_displays_organic_orders_without_leads(app):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    mod, _, _, _ = app
    db.upsert_user(mod.conn, "organic-user")
    db.add_order(mod.conn, "organic-user", 100, ["course"])
    message = SimpleNamespace(answer=AsyncMock())
    asyncio.run(mod.send_report(message, "last"))
    text = " ".join(call.args[0] for call in message.answer.call_args_list)
    assert "приписанная выручка 100" in text


def test_unknown_cost_dashboard_does_not_claim_free_advertising(app):
    mod, _, _, _ = app
    db.add_placement(mod.conn, "unpriced", "Unknown cost", None)
    text = mod.romi_dashboard("last")
    assert "Общий атрибуционный ROMI не определён" in text
    assert "у всех стоимость 0" not in text
