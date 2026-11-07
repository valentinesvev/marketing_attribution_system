"""
Три уровня знания (как просят организаторы) + проверка полноты данных для /status.

ЗНАЕМ       — факты: оплаты (base.xlsx и кнопка «Оплатил»), заходы по меткам, лиды, стоимость рекламы.
ОЦЕНИВАЕМ   — результат модели: атрибуция, ROMI, эквайринг по ставке.
НЕ ВИДИМ    — то, что не видно даже с ботом (список BLIND_SPOTS).
"""

BLIND_SPOTS = [
    "клики по ссылке без нажатия «Старт» — Telegram их не показывает",
    "саму переписку с менеджером — бот видит только нажатие «Хочу курс»",
    "клиентов, написавших менеджеру напрямую, минуя бота",
    "купил бы клиент без рекламы — это покажет только эксперимент (holdout)",
    "откуда пришли покупатели из base.xlsx — меток тогда не было",
]


def status(conn, acquiring_rate: float) -> str:
    one = lambda sql, *a: conn.execute(sql, a).fetchone()[0] or 0
    ok, warn = "✅", "⚠️"
    out = ["<b>Полнота данных</b>"]

    # Размещения
    n = one("SELECT COUNT(*) FROM dim_placement WHERE placement_type != 'unknown'")
    out.append(f"\n<b>Реклама</b> (размещений: {n})")
    checks = [
        ("стоимость неизвестна", "cost_known=0"),
        ("нет времени публикации", "publication_time IS NULL"),
        ("нет типа поста", "post_category IS NULL"),
        ("нет кампании", "campaign_name IS NULL"),
        ("скидочный пост без размера скидки", "post_category = 'discount' AND discount_value IS NULL"),
    ]
    bad = False
    for label, cond in checks:
        k = one(f"SELECT COUNT(*) FROM dim_placement WHERE placement_type != 'unknown' AND {cond}")
        if k:
            bad = True
            out.append(f"{warn} {label}: {k}")
    unknown = one("SELECT COUNT(*) FROM dim_placement WHERE placement_type = 'unknown'")
    if unknown:
        bad = True
        out.append(f"{warn} заходы по меткам, которых нет в /newlink: {unknown} (стоимость неизвестна)")
    if not bad:
        out.append(f"{ok} все поля заполнены" if n else f"{warn} размещений нет — создайте через /newlink")

    # Заходы
    t = one("SELECT COUNT(*) FROM fact_touch")
    t_tag = one("SELECT COUNT(*) FROM fact_touch WHERE placement_id IS NOT NULL")
    out.append(f"\n<b>Заходы в бота</b> (всего: {t})")
    if t:
        out.append(f"{ok if t_tag else warn} по рекламной метке: {t_tag} ({t_tag / t:.0%}), без метки: {t - t_tag}")

    # Лиды
    leads = one("SELECT COUNT(*) FROM fact_lead")
    asked = one("SELECT COUNT(*) FROM fact_lead l LEFT JOIN fact_touch t USING(touch_id) WHERE t.placement_id IS NULL")
    answered = one("SELECT COUNT(*) FROM fact_lead l LEFT JOIN fact_touch t USING(touch_id) "
                   "WHERE t.placement_id IS NULL AND l.self_reported_source NOT IN ('skip') "
                   "AND l.self_reported_source IS NOT NULL")
    opened = one("SELECT COUNT(*) FROM fact_lead WHERE status = 'open'")
    out.append(f"\n<b>Лиды</b> (всего: {leads})")
    if leads:
        out.append(f"• ждут оплаты или отказались: {opened}")
    if asked:
        out.append(f"{ok if answered == asked else warn} пришли без метки: {asked}, "
                   f"ответили «откуда узнал»: {answered} ({answered / asked:.0%})")

    # Заказы
    bot_orders = one("SELECT COUNT(*) FROM fact_order WHERE source_system = 'bot'")
    hist = one("SELECT COUNT(*) FROM fact_order WHERE source_system = 'base_xlsx'")
    no_course = one("SELECT COUNT(DISTINCT order_id) FROM fact_order_item WHERE course = 'не указан'")
    out.append(f"\n<b>Оплаты</b> (через бота: {bot_orders}, история base.xlsx: {hist})")
    if hist:
        out.append(f"{warn} у {hist} заказов из base.xlsx источник неизвестен — это факт, не ошибка")
    if no_course:
        out.append(f"{warn} оплаты без указанного курса: {no_course}")
    if acquiring_rate:
        out.append(f"{warn} эквайринг — оценка по ставке {acquiring_rate:.1%}, а не фактическая комиссия")
    else:
        out.append(f"{warn} эквайринг не задан (ACQUIRING_RATE в .env) — ROMI с комиссией неизвестен для оплат без данных о комиссии")

    out.append("\n<b>Не видим даже с ботом</b>")
    out += [f"• {x}" for x in BLIND_SPOTS]
    return "\n".join(out)
