-- Schema reference; runtime migrations live in src/core/db.py.
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
