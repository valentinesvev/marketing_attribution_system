"""
Хранилище measurement system (SQLite). Таблицы повторяют ER-схему команды:

dim_placement    - размещение рекламы (одна метка = один пост в одном канале), входит в кампанию
dim_user         - пользователь (только хеши)
fact_touch       - касание: заход в бота (по метке или без неё)
fact_bot_event   - действия в боте после касания (смотрел направление / курс / пакет) - для воронки
fact_lead        - лид: нажал «Хочу курс», начало диалога с менеджером
fact_order       - заказ (оплата); fact_order_item — курсы внутри заказа (пакеты)
fact_attribution - какая доля выручки заказа приписана какому касанию, по каждой модели

Связи: touch.placement_id → placement; lead.touch_id → touch; order.lead_id → lead;
       attribution.order_id → order, attribution.touch_id → touch; *.user_id_hash → dim_user
"""
import hashlib
import math
import os
import sqlite3
import uuid
from core.delivery import event_time, audit
from datetime import datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3))

SCHEMA = """
CREATE TABLE IF NOT EXISTS dim_placement (
    placement_id     TEXT PRIMARY KEY,             -- метка из ссылки t.me/<bot>?start=<placement_id>
    campaign_name    TEXT,                         -- кампания: «Запуск AI агентов», «Распродажа август»
    channel          TEXT NOT NULL,                -- Telegram-канал
    placement_type   TEXT NOT NULL DEFAULT 'external',  -- external (чужой канал) | own (свой канал)
    creative         TEXT,                         -- вариант поста / креатива
    post_category    TEXT,                         -- sale | discount | launch | native | content
    discount_value   REAL,                         -- размер скидки, %
    cost             REAL NOT NULL DEFAULT 0,      -- стоимость; проверять cost_known
    cost_known       INTEGER NOT NULL DEFAULT 0,  -- 1: явно известна, включая бесплатную
    publication_time TEXT,                         -- время публикации (МСК); NULL = не указано
    created_at       TEXT,
    is_synthetic     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS dim_user (
    user_id_hash       TEXT PRIMARY KEY,           -- хеш Telegram ID (или обезличенного student_id для истории)
    username_hash      TEXT,                       -- хеш username: для склейки с таблицей оплат бизнеса
    first_seen         TEXT,
    first_placement_id TEXT,
    source_system      TEXT NOT NULL DEFAULT 'bot' -- bot | base_xlsx
);
CREATE TABLE IF NOT EXISTS fact_touch (
    touch_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id_hash  TEXT NOT NULL REFERENCES dim_user(user_id_hash),
    placement_id  TEXT REFERENCES dim_placement(placement_id),  -- NULL = пришёл без метки
    touch_type    TEXT NOT NULL DEFAULT 'bot_start',
    timestamp     TEXT NOT NULL,
    is_synthetic  INTEGER NOT NULL DEFAULT 0,
    source_bot_id INTEGER,
    source_update_id INTEGER,
    processed_at TEXT,
    time_source TEXT NOT NULL DEFAULT 'legacy'
);
CREATE TABLE IF NOT EXISTS fact_bot_event (
    event_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id_hash  TEXT NOT NULL REFERENCES dim_user(user_id_hash),
    touch_id      INTEGER REFERENCES fact_touch(touch_id),     -- в рамках какого захода
    event_type    TEXT NOT NULL,                               -- view_direction | view_course | view_bundle
    item          TEXT,
    timestamp     TEXT NOT NULL,
    is_synthetic  INTEGER NOT NULL DEFAULT 0,
    source_bot_id INTEGER,
    source_update_id INTEGER,
    processed_at TEXT,
    time_source TEXT NOT NULL DEFAULT 'legacy'
);
CREATE TABLE IF NOT EXISTS fact_lead (
    lead_id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id_hash            TEXT NOT NULL REFERENCES dim_user(user_id_hash),
    touch_id                INTEGER REFERENCES fact_touch(touch_id),
    course_interest         TEXT,
    self_reported_source    TEXT,                  -- ответ «откуда узнал», если пришёл без метки
    conversation_started_at TEXT NOT NULL,
    manager_id              TEXT,                  -- кто из менеджеров отметил оплату
    status                  TEXT NOT NULL DEFAULT 'open',  -- open | paid
    is_synthetic            INTEGER NOT NULL DEFAULT 0,
    source_bot_id INTEGER,
    source_update_id INTEGER,
    processed_at TEXT,
    time_source TEXT NOT NULL DEFAULT 'legacy'
);
CREATE TABLE IF NOT EXISTS fact_order (
    order_id       TEXT PRIMARY KEY,
    user_id_hash   TEXT NOT NULL REFERENCES dim_user(user_id_hash),
    lead_id        INTEGER REFERENCES fact_lead(lead_id),      -- NULL для истории из base.xlsx
    student_id     TEXT,                                       -- обезличенный ID из base.xlsx
    amount         REAL NOT NULL,                              -- валовая выручка заказа
    variable_costs REAL NOT NULL DEFAULT 0,                    -- эквайринг
    variable_costs_source TEXT NOT NULL DEFAULT 'none',        -- none | rate_estimate (оценка по ставке) | actual
    timestamp      TEXT NOT NULL,
    source_system  TEXT NOT NULL DEFAULT 'bot',                -- bot | base_xlsx
    is_synthetic   INTEGER NOT NULL DEFAULT 0,
    source_bot_id INTEGER,
    source_update_id INTEGER,
    processed_at TEXT,
    time_source TEXT NOT NULL DEFAULT 'legacy'
);
CREATE TABLE IF NOT EXISTS fact_order_item (
    item_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id  TEXT NOT NULL REFERENCES fact_order(order_id),
    course    TEXT NOT NULL,
    amount    REAL NOT NULL                                    -- доля цены заказа, как в base.xlsx
);
CREATE TABLE IF NOT EXISTS fact_attribution (
    attribution_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id           TEXT NOT NULL REFERENCES fact_order(order_id),
    touch_id           INTEGER REFERENCES fact_touch(touch_id),  -- NULL = органика (нет рекламного касания)
    placement_id       TEXT REFERENCES dim_placement(placement_id),
    attribution_model  TEXT NOT NULL,                            -- last | first | linear
    weight             REAL NOT NULL,                            -- доля заказа (в linear может быть 0.5)
    attributed_revenue REAL NOT NULL,
    attributed_margin  REAL NOT NULL,                            -- выручка минус эквайринг
    window_days        INTEGER NOT NULL,
    calculated_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS telegram_inbox (
    queue_id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL,
    update_id INTEGER NOT NULL,
    payload TEXT,
    event_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    time_source TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    processed_at TEXT,
    UNIQUE(bot_id, update_id)
);
CREATE INDEX IF NOT EXISTS ix_inbox_pending ON telegram_inbox(bot_id, status, queue_id);
CREATE TABLE IF NOT EXISTS telegram_polling (
    bot_id INTEGER PRIMARY KEY,
    next_offset INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_touch_user  ON fact_touch(user_id_hash, timestamp);
CREATE INDEX IF NOT EXISTS ix_touch_pl    ON fact_touch(placement_id);
CREATE INDEX IF NOT EXISTS ix_event_touch ON fact_bot_event(touch_id);
CREATE INDEX IF NOT EXISTS ix_lead_user   ON fact_lead(user_id_hash);
CREATE INDEX IF NOT EXISTS ix_order_user  ON fact_order(user_id_hash, timestamp);
CREATE INDEX IF NOT EXISTS ix_item_order  ON fact_order_item(order_id);
CREATE INDEX IF NOT EXISTS ix_attr_model  ON fact_attribution(attribution_model);
"""


def now_msk() -> str:
    return datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S")


class EventConnection(sqlite3.Connection):
    # Existing domain functions commit independently outside update processing.
    # Within one update the worker owns the only commit/rollback boundary.
    processing_event = False

    def commit(self):
        if not self.processing_event:
            super().commit()


def connect(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)   # папка может отсутствовать после клонирования
    conn = sqlite3.connect(path, check_same_thread=False, factory=EventConnection)
    conn.row_factory = sqlite3.Row
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='events'").fetchone():
        raise SystemExit(f"{path} создан старой версией бота. Удалите этот файл, бот создаст новую базу.")
    conn.executescript(SCHEMA)
    migrate(conn)
    return conn


def migrate(conn):
    """Добавляет колонки, появившиеся в новых версиях, не трогая данные."""
    for table in ("fact_touch", "fact_bot_event", "fact_lead", "fact_order"):
        existing = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
        for name, kind in (("source_bot_id", "INTEGER"), ("source_update_id", "INTEGER"),
                           ("processed_at", "TEXT"), ("time_source", "TEXT NOT NULL DEFAULT 'legacy'")):
            if name not in existing:
                conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {kind}')
    conn.commit()
    placement_cols = {r[1] for r in conn.execute("PRAGMA table_info(dim_placement)")}
    if "cost_known" not in placement_cols:
        conn.execute("ALTER TABLE dim_placement ADD COLUMN cost_known INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE dim_placement SET cost_known=1 WHERE placement_type!='unknown' AND (cost>0 OR placement_type='own')")
        conn.commit()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(fact_order)")}
    if "variable_costs_source" not in cols:
        conn.execute("ALTER TABLE fact_order ADD COLUMN variable_costs_source TEXT NOT NULL DEFAULT 'none'")
        conn.execute("UPDATE fact_order SET variable_costs_source='rate_estimate' WHERE variable_costs > 0")
        conn.commit()
    if not conn.execute("SELECT 1 FROM sqlite_sequence WHERE name='fact_lead'").fetchone():
        conn.execute("INSERT INTO sqlite_sequence(name, seq) VALUES ('fact_lead', 1000)")
        conn.commit()


def hash_id(value, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{value}".encode()).hexdigest()[:16]


def legacy_user_hash(student_id, salt: str) -> str:
    """Хеш для покупателей из base.xlsx (там только обезличенный student_id)."""
    return hash_id(f"student:{student_id}", salt)


# ---------- Пользователи и касания ----------
def upsert_user(conn, uid, username_hash=None, placement_id=None, source="bot", ts=None):
    conn.execute("INSERT OR IGNORE INTO dim_user VALUES (?,?,?,?,?)",
                 (uid, username_hash, ts or event_time(), placement_id, source))


def add_touch(conn, uid, placement_id, ts=None) -> int:
    cur = conn.execute("INSERT INTO fact_touch(user_id_hash, placement_id, timestamp) VALUES (?,?,?)",
                       (uid, placement_id, ts or event_time()))
    audit(conn, "fact_touch", "touch_id", cur.lastrowid)
    conn.commit()
    return cur.lastrowid


def current_touch(conn, uid):
    """Последний заход пользователя в бота: к нему привязываются просмотры и лид."""
    return conn.execute("SELECT * FROM fact_touch WHERE user_id_hash=? ORDER BY timestamp DESC, touch_id DESC LIMIT 1",
                        (uid,)).fetchone()


def log_event(conn, uid, touch_id, event_type, item=None):
    cur = conn.execute("INSERT INTO fact_bot_event(user_id_hash, touch_id, event_type, item, timestamp) VALUES (?,?,?,?,?)",
                 (uid, touch_id, event_type, item, event_time()))
    audit(conn, "fact_bot_event", "event_id", cur.lastrowid)
    conn.commit()


# ---------- Лиды ----------
def last_self_source(conn, uid):
    row = conn.execute("SELECT self_reported_source FROM fact_lead WHERE user_id_hash=? AND self_reported_source IS NOT NULL "
                       "ORDER BY lead_id DESC LIMIT 1", (uid,)).fetchone()
    return row[0] if row else None


def create_lead(conn, uid, touch_id, course_interest, self_source=None) -> int:
    cur = conn.execute("INSERT INTO fact_lead(user_id_hash, touch_id, course_interest, self_reported_source, "
                       "conversation_started_at) VALUES (?,?,?,?,?)",
                       (uid, touch_id, course_interest, self_source, event_time()))
    audit(conn, "fact_lead", "lead_id", cur.lastrowid)
    conn.commit()
    return cur.lastrowid


def get_lead(conn, lead_id):
    return conn.execute("SELECT l.*, t.placement_id FROM fact_lead l LEFT JOIN fact_touch t USING(touch_id) "
                        "WHERE l.lead_id=?", (lead_id,)).fetchone()


# ---------- Размещения ----------
def add_placement(conn, placement_id, channel, cost, publication_time=None, campaign_name=None,
                  post_category=None, discount_value=None, creative=None, placement_type=None):
    ptype = placement_type or ("own" if cost == 0 else "external")
    known = cost is not None and ptype != "unknown"
    if known and (not math.isfinite(cost) or cost < 0):
        raise ValueError("Стоимость должна быть конечным неотрицательным числом")
    conn.execute("INSERT OR REPLACE INTO dim_placement(placement_id, campaign_name, channel, placement_type, creative, "
                 "post_category, discount_value, cost, cost_known, publication_time, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (placement_id, campaign_name, channel, ptype, creative, post_category, discount_value, cost if known else 0, int(known),
                  publication_time, now_msk()))
    conn.commit()


def get_placement(conn, placement_id):
    return conn.execute("SELECT * FROM dim_placement WHERE placement_id=?", (placement_id,)).fetchone() if placement_id else None


# ---------- Заказы ----------
def add_order(conn, uid, amount, courses, acquiring_rate=0.0, ts=None, lead_id=None, source="bot",
              student_id=None, order_id=None, commit=True):
    """Заказ + курсы в нём. Сумма делится поровну между курсами, как в base.xlsx.
    Возвращает order_id или None, если такой заказ уже есть (повторный импорт)."""
    order_id = order_id or f"b{uuid.uuid4().hex[:12]}"
    cur = conn.execute(
        "INSERT OR IGNORE INTO fact_order(order_id, user_id_hash, lead_id, student_id, amount, variable_costs, "
        "variable_costs_source, timestamp, source_system) VALUES (?,?,?,?,?,?,?,?,?)",
        (order_id, uid, lead_id, student_id, amount, round(amount * acquiring_rate, 2),
         "rate_estimate" if acquiring_rate else "none", ts or event_time(), source))
    if cur.rowcount == 0:
        return None
    audit(conn, "fact_order", "order_id", order_id)
    courses = courses or ["не указан"]
    total_cents = round(amount * 100)
    share_cents, remainder = divmod(total_cents, len(courses))
    conn.executemany("INSERT INTO fact_order_item(order_id, course, amount) VALUES (?,?,?)",
                     [(order_id, c, (share_cents + (i < remainder))/100) for i, c in enumerate(courses)])
    if lead_id:
        conn.execute("UPDATE fact_lead SET status='paid' WHERE lead_id=?", (lead_id,))
    if commit:
        conn.commit()
    return order_id


def set_lead_manager(conn, lead_id, manager_id):
    conn.execute("UPDATE fact_lead SET manager_id=? WHERE lead_id=?", (str(manager_id), lead_id))
    conn.commit()
