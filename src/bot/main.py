"""
Команды админа:
  /newlink                                                                      - создать размещение: бот спросит все поля по очереди
  /newlink метка | канал | стоимость | дата | кампания | тип поста | скидка %   - тоже самое что и ↑, но одной строкой
  /links                                                                        - все размещения и ссылки
  /report [last|first|linear|position]                                          - отчёт в три блока: знаем / оцениваем / не видим
  /status                                                                       - полнота данных: какие поля не заполнены, какие слепые зоны открыты
  /paid номер_лида сумма [курсы через запятую]                                  - внести оплату вручную
  /export                                                                       - скачать базу данных
  /cancel                                                                       - отменить ввод
"""
import asyncio
import logging
import math
import re
import tempfile
import sqlite3
from pathlib import Path
from urllib.parse import quote

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError, TelegramRetryAfter, TelegramServerError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BotCommand, BotCommandScopeChat, CallbackQuery, FSInputFile, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core import config, db
from core.delivery import CURRENT_EVENT, stamp
from bot.reliable import instance_lock, run, tolerate_stale_ui
from core.attribution import MODELS, ORGANIC_BOT, HISTORY, report, stable_winners, summary
from core.catalog import BUNDLES, COURSES, DIRECTIONS, bundle_title, rub
from core.quality import status
from datetime import datetime

router = Router()
conn = db.connect(config.DB_PATH)

TAG_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")                              # ограничения Telegram на параметр start в ссылке t.me/bot?start=...
AMOUNT_RE = re.compile(r"^\s*(\d[\d\s]*(?:[.,]\d+)?)\s*(.*)$")
CATEGORIES = ("sale", "discount", "launch", "native", "content")           # категории постов, которые мы различаем в размещениях
COURSE_NAMES = {c["name"].lower(): c["name"] for c in COURSES.values()}    # Словарь курсов

SOURCES = {                             # откуда узнали о нас (для self_reported_source в базе)
    "own": "Канал Поступашек",
    "ad": "Реклама в другом канале",
    "friend": "Посоветовал друг",
    "search": "Нашёл в поиске",
    "other": "Другое",
    "skip": "не ответил",
}


class PayForm(StatesGroup):               
    amount = State()


class NewLink(StatesGroup):
    waiting = State()

# для /newlink: 
# порядок полей, которые спрашиваем по очереди
NL_ORDER = ["placement_id", "channel", "cost", "publication_time", "campaign_name", "post_category", "discount_value"]
# словарь для отображения типа поста
CAT_LABELS = {"sale": "Продающий пост", "discount": "Скидка / акция", "launch": "Запуск курса",
              "native": "Нативная интеграция", "content": "Обычный контент"}
NONE = "-"   # явный ответ «нет», чтобы не спрашивать повторно


def uid_of(obj) -> tuple[str, str | None]:
    '''
    Возвращает (user_id_hash, username_hash) - хэши пользователя, чтобы не хранить реальные id и имена в базе.
    '''
    u = obj.from_user
    uname = db.hash_id(u.username.lower(), config.HASH_SALT) if u.username else None
    return db.hash_id(f"id:{u.id}", config.HASH_SALT), uname


def is_admin(obj) -> bool:
    '''
    Проверяет, является ли пользователь админом
    '''
    return obj.from_user.id in config.ADMIN_IDS


def is_staff(obj) -> bool:
    '''
    Проверяет, является ли пользователь сотрудником
    '''
    return is_admin(obj) or obj.from_user.id in config.MANAGER_IDS or obj.from_user.id == config.MANAGER_CHAT_ID


def touch_id_of(uid: str):
    '''
    находит номер последнего захода человека в бота
    '''
    t = db.current_touch(conn, uid)
    return t["touch_id"] if t else None


def interest_of(item: str) -> str:
    '''
    переводит код кнопки в название курса или пакета
    '''
    return COURSES[item]["name"] if item in COURSES else bundle_title(item) if item in BUNDLES else "общий вопрос"


def source_text(placement_id, self_source) -> str:
    '''
    готовит для менеджера строку "Источник:..."
    '''
    p = db.get_placement(conn, placement_id)
    if p:
        return f"{p['channel']} ({placement_id})"
    if self_source:
        return "не ответил на вопрос" if self_source == "skip" else f"сам ответил: {SOURCES.get(self_source, self_source)}"
    return "без метки"


# ---------- Клавиатуры ----------
def main_menu():
    '''
    собирает кнопки главного меню
    '''
    kb = InlineKeyboardBuilder()
    for did, (title, _) in DIRECTIONS.items():
        kb.button(text=title, callback_data=f"dir:{did}")
    kb.button(text="🎁 Популярные пакеты", callback_data="bundles")
    kb.button(text="💬 Задать вопрос менеджеру", callback_data="lead:general")
    kb.adjust(2, 2, 2, 1, 1)
    return kb.as_markup()


def back(to="menu"):
    '''
    собирает кнопку "← Назад"
    '''
    kb = InlineKeyboardBuilder()
    kb.button(text="← Назад", callback_data=to)
    return kb


# ---------- Клиент ----------
@router.message(CommandStart())
async def start(message: Message, command: CommandObject):
    '''
    Начало диалога с пользователем. Сохраняем метку размещения, если она есть.
    '''
    uid, uname = uid_of(message)
    raw = (command.args or "").strip()
    placement_id = raw if TAG_RE.match(raw) else None
    if placement_id and not db.get_placement(conn, placement_id):
        logging.warning("Неизвестная метка %s — ссылку не создали через /newlink", placement_id)
        db.add_placement(conn, placement_id, "неизвестно (нет в /newlink)", 0, placement_type="unknown")
    db.upsert_user(conn, uid, uname, placement_id)
    db.add_touch(conn, uid, placement_id)
    await message.answer(
        "Привет! Это Поступашки 👋\nПомогаем готовиться к поступлению, стажировкам и работе в BigTech.\n\n"
        "Выбери направление, чтобы посмотреть курсы и цены:", reply_markup=main_menu())
    if is_admin(message):
        await message.answer("Вы админ: /menu - панель управления рекламой и отчётами.")


@router.callback_query(F.data == "menu")
async def menu(cb: CallbackQuery):
    '''
    возвращает в главное меню
    '''
    await cb.message.edit_text("Выбери направление:", reply_markup=main_menu())
    await cb.answer()


@router.callback_query(F.data.startswith("dir:"))
async def direction(cb: CallbackQuery):
    '''
    показывает курсы направления, записывает просмотр
    '''
    uid, _ = uid_of(cb)
    did = cb.data.split(":", 1)[1]
    title, course_ids = DIRECTIONS[did]
    db.log_event(conn, uid, touch_id_of(uid), "view_direction", did)
    kb = InlineKeyboardBuilder()
    for cid in course_ids:
        kb.button(text=f"{COURSES[cid]['name']} · {rub(COURSES[cid]['price'])}", callback_data=f"course:{cid}")
    kb.attach(back())
    kb.adjust(1)
    await cb.message.edit_text(f"{title}\nВыбери курс:", reply_markup=kb.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("course:"))
async def course_card(cb: CallbackQuery):
    '''
    показывает карточку курса, записывает просмотр
    '''
    uid, _ = uid_of(cb)
    cid = cb.data.split(":", 1)[1]
    c = COURSES[cid]
    db.log_event(conn, uid, touch_id_of(uid), "view_course", c["name"])
    did = next(d for d, (_, ids) in DIRECTIONS.items() if cid in ids)
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Хочу этот курс", callback_data=f"lead:{cid}")
    kb.attach(back(f"dir:{did}"))
    kb.adjust(1)
    await cb.message.edit_text(f"<b>{c['name']}</b>\n\n{c['desc']}\n\nЦена: <b>{rub(c['price'])}</b>",
                               reply_markup=kb.as_markup())
    await cb.answer()


@router.callback_query(F.data == "bundles")
async def bundles(cb: CallbackQuery):
    '''
    список популярных пакетов
    '''
    kb = InlineKeyboardBuilder()
    for bid, b in BUNDLES.items():
        kb.button(text=f"{bundle_title(bid)} · {rub(b['price'])}", callback_data=f"bundle:{bid}")
    kb.attach(back())
    kb.adjust(1)
    await cb.message.edit_text("🎁 Пакеты из двух курсов, которые берут чаще всего:", reply_markup=kb.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("bundle:"))
async def bundle_card(cb: CallbackQuery):
    '''
    карточка пакета, записывает просмотр
    '''
    uid, _ = uid_of(cb)
    bid = cb.data.split(":", 1)[1]
    b = BUNDLES[bid]
    db.log_event(conn, uid, touch_id_of(uid), "view_bundle", bundle_title(bid))
    single = sum(COURSES[c]["price"] for c in b["courses"])
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Хочу этот пакет", callback_data=f"lead:{bid}")
    kb.attach(back("bundles"))
    kb.adjust(1)
    await cb.message.edit_text(f"<b>{bundle_title(bid)}</b>\n\nВместе: <b>{rub(b['price'])}</b> вместо {rub(single)}",
                               reply_markup=kb.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("lead:"))
async def lead(cb: CallbackQuery, bot: Bot):
    '''
    «Хочу курс»: если нет метки и ответа, спрашивает «откуда узнал»
    '''
    uid, _ = uid_of(cb)
    item = cb.data.split(":", 1)[1]
    touch = db.current_touch(conn, uid)
    if touch is None or touch["placement_id"] is None:          # пришёл без метки
        prev = db.last_self_source(conn, uid)
        if prev is None:                                        # ещё не отвечал — спрашиваем перед менеджером
            kb = InlineKeyboardBuilder()
            for sid, text in SOURCES.items():
                kb.button(text="Пропустить" if sid == "skip" else text, callback_data=f"src:{sid}:{item}")
            kb.adjust(1)
            await cb.message.edit_text("Последний шаг перед менеджером 🙂\nПодскажи, откуда ты о нас узнал?",
                                       reply_markup=kb.as_markup())
            await cb.answer()
            return
        await finish_lead(cb, bot, uid, item, prev)
    else:
        await finish_lead(cb, bot, uid, item, None)


@router.callback_query(F.data.startswith("src:"))
async def source_answer(cb: CallbackQuery, bot: Bot):
    '''
    принимает ответ «откуда узнал» и продолжает оформление
    '''
    uid, _ = uid_of(cb)
    _, sid, item = cb.data.split(":", 2)
    await finish_lead(cb, bot, uid, item, sid)


async def finish_lead(cb: CallbackQuery, bot: Bot, uid: str, item: str, self_source):
    '''
    создаёт лид, даёт кнопку к менеджеру, уведомляет менеджера
    '''
    touch = db.current_touch(conn, uid)
    interest = interest_of(item)
    lead_id = db.create_lead(conn, uid, touch["touch_id"] if touch else None, interest, self_source)

    hello = quote(f"Здравствуйте! Заявка №{lead_id}, интересует: {interest}")
    manager = config.MANAGER_USERNAMES[lead_id % len(config.MANAGER_USERNAMES)]   # заявки по очереди
    kb = InlineKeyboardBuilder()
    kb.button(text="💬 Написать менеджеру", url=f"https://t.me/{manager}?text={hello}")
    kb.attach(back())
    kb.adjust(1)
    await cb.message.edit_text(
        f"Твой номер заявки: <b>{lead_id}</b>\n\nИнтерес: <b>{interest}</b>\n\n"
        "Нажми кнопку — откроется чат с менеджером с готовым сообщением, останется отправить.",
        reply_markup=kb.as_markup())
    await cb.answer()

    if config.MANAGER_CHAT_ID:
        mk = InlineKeyboardBuilder()
        mk.button(text="✅ Оплатил", callback_data=f"pay:{lead_id}")
        await bot.send_message(
            config.MANAGER_CHAT_ID,
            f"🔔 Новый лид №{lead_id}\nИсточник: {source_text(touch['placement_id'] if touch else None, self_source)}\n"
            f"Интерес: {interest}\nВедёт: @{manager}\n\nКогда клиент оплатит — нажми «Оплатил».",
            reply_markup=mk.as_markup())


# ---------- Менеджер: оплата ----------
async def cleanup(bot: Bot, chat_id: int, message_ids: list[int]):
    """Удаляет служебные сообщения по лиду. Telegram позволяет удалять только за последние 48 часов"""
    for mid in message_ids:
        try:
            await bot.delete_message(chat_id, mid)
        except Exception:
            pass
@router.callback_query(F.data.startswith("pay:"))
async def pay_start(cb: CallbackQuery, state: FSMContext):
    '''
    кнопка «Оплатил»: проверяет права и включает режим «жду сумму»
    '''
    if not is_staff(cb):
        await cb.answer("Только для менеджера", show_alert=True)
        return
    lead_id = int(cb.data.split(":", 1)[1])
    ld = db.get_lead(conn, lead_id)
    if not ld:
        await cb.answer("Лид не найден", show_alert=True)
        return
    if ld["status"] == "paid":
        await cb.answer("Оплата по этому лиду уже записана", show_alert=True)
        return
    await state.set_state(PayForm.amount)
    await state.update_data(lead_id=lead_id, chat_id=cb.message.chat.id, message_id=cb.message.message_id,
                            to_delete=[cb.message.message_id])
    ask = await cb.message.answer(
        f"Лид №{lead_id} · интерес: {ld['course_interest']}\n\n"
        f"Введи сумму оплаты, например <code>14950</code>.\n"
        f"Если купил другие курсы — перечисли после суммы через запятую:\n"
        f"<code>14950 ML про, AI агенты</code>\n\n/cancel — отмена")
    await state.update_data(to_delete=[cb.message.message_id, ask.message_id])
    await cb.answer()


def parse_payment(text: str, default_interest: str):
    """'14 950 ML про, AI агенты' → (14950.0, ['ML про', 'AI агенты'])."""
    m = AMOUNT_RE.match(text or "")
    if not m:
        return None, None
    amount = float(re.sub(r"\s", "", m.group(1)).replace(",", "."))
    rest = m.group(2).strip()
    raw = [c.strip() for c in re.split(r"[,+]", rest) if c.strip()] if rest else \
          ([] if default_interest in (None, "общий вопрос") else default_interest.split(" + "))
    return amount, [COURSE_NAMES.get(c.lower(), c) for c in raw]


async def save_payment(message: Message, bot: Bot, lead_id: int, text: str, state_data=None) -> bool:
    '''
    записывает заказ, отмечает лид оплаченным, убирает кнопку
    '''
    ld = db.get_lead(conn, lead_id)
    if not ld:
        await message.answer("Лид не найден.")
        return False
    if ld["status"] == "paid":
        await message.answer("Оплата по этой заявке уже записана.")
        return False
    amount, courses = parse_payment(text, ld["course_interest"])
    if not amount or amount <= 0:
        await message.answer("Не понял сумму. Пример: <code>14950</code> или <code>14950 ML про, AI агенты</code>")
        return False
    unknown = [c for c in courses if c.lower() not in COURSE_NAMES]
    db.add_order(conn, ld["user_id_hash"], amount, courses, config.ACQUIRING_RATE, lead_id=lead_id)
    db.set_lead_manager(conn, lead_id, message.from_user.id)
    await message.answer(
        f"✅ Заявка №{lead_id} · записано: {rub(amount)} · {' + '.join(courses) or 'курс не указан'}\n"
        f"Источник: {source_text(ld['placement_id'], ld['self_reported_source'])}"
    )
    if state_data:
        try:
            await bot.edit_message_reply_markup(chat_id=state_data["chat_id"], message_id=state_data["message_id"],
                                                reply_markup=None)
        except Exception:
            pass
    return True


@router.message(PayForm.amount, F.text, ~F.text.startswith("/"))
async def pay_amount(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    if await save_payment(message, bot, data["lead_id"], message.text, data):
        await cleanup(bot, data["chat_id"], data.get("to_delete", []) + [message.message_id])
        await state.clear()


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext):
    '''
    /cancel: выключает любой режим ожидания
    '''
    await state.clear()
    await message.answer("Отменено.")


@router.message(Command("paid"))
async def paid(message: Message, command: CommandObject, bot: Bot):
    '''
    /paid: ручной ввод оплаты по номеру лида
    '''
    if not is_staff(message):
        return
    parts = (command.args or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[0].isdigit():
        await message.answer("Формат: <code>/paid номер_лида сумма [курсы через запятую]</code>\n"
                             "Например: /paid 12 14950 ML про, AI агенты")
        return
    ld = db.get_lead(conn, int(parts[0]))
    if ld and ld["status"] == "paid":
        await message.answer("Оплата по этому лиду уже записана.")
        return
    await save_payment(message, bot, int(parts[0]), parts[1])


# ---------- Админ ----------

# ---------- Панель админа ----------
MODEL_LABELS = {"last": "Last touch", "first": "First touch", "linear": "Linear", "position": "Position"}


def admin_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="📢 Создать размещение", callback_data="adm:newlink")
    kb.button(text="🔗 Мои размещения", callback_data="adm:links")
    kb.button(text="💰 ROMI", callback_data="romi:last")
    kb.button(text="📈 Отчёт", callback_data="adm:report:last")
    kb.button(text="🩺 Полнота данных", callback_data="adm:status")
    kb.adjust(1, 1, 2, 1)
    return kb.as_markup()


def romi_keyboard(model: str):
    """Кнопки переключения модели атрибуции + возврат в меню."""
    kb = InlineKeyboardBuilder()
    for m in MODELS:
        mark = "· " if m == model else ""
        kb.button(text=f"{mark}{MODEL_LABELS[m]}", callback_data=f"romi:{m}")
    kb.button(text="📈 Подробный отчёт", callback_data=f"adm:report:{model}")
    kb.button(text="← Меню", callback_data="adm:menu")
    kb.adjust(2, 2, 1, 1)
    return kb.as_markup()


def short_date(ts):
    return f"{ts[8:10]}.{ts[5:7]}" if ts else "—"


def romi_dashboard(model: str) -> str:
    """Сводка по рекламе: факты, оценки и осторожная рекомендация."""
    s = summary(conn, model, config.ATTRIBUTION_WINDOW_DAYS)
    if not s["n_placements"]:
        return "📊 <b>ROMI</b>\n\nРазмещений пока нет. Создайте первое через «Создать размещение»."

    head = [f"📊 <b>ROMI ПО РЕКЛАМЕ</b>",
            f"Период: {short_date(s['period'][0])} – {short_date(s['period'][1])} · "
            f"модель {MODEL_LABELS[model]}, окно {s['window']} дн.", ""]
    if not s["cost_complete"]:
        head.append("Не у всех размещений известны затраты. Общий атрибуционный ROMI не определён.")
        head.append("Показатели отдельных размещений: /report")
        return "\n".join(head)
    if not s["cost"]:
        head.append("Платных размещений нет: у всех стоимость 0, ROMI не считается.")
        head.append(f"Выручка через бота: <b>{rub(s['revenue_bot'])}</b>")
        return "\n".join(head)

    share = s["attributed"] / s["revenue_bot"] if s["revenue_bot"] else None
    share_text = f"{share:.0%}" if share is not None else "доля не определена"
    head += [
        f"💰 Выручка через бота: <b>{rub(s['revenue_bot'])}</b>",
        f"📊 Приписано рекламе: <b>{rub(s['attributed'])}</b> ({share_text})",
        f"💸 Затраты на рекламу: <b>{rub(s['cost'])}</b>",
        f"📈 ROMI: <b>{s['romi']:+.0%}</b>"
        + (f" · с учётом комиссии {s['romi_margin']:+.0%}" if s["romi_margin"] is not None else ""),
    ]

    pl = s["placements"]
    top = ["", "🏆 <b>ЛУЧШИЕ РАЗМЕЩЕНИЯ</b>"]
    winners = pl[pl.romi > 0]
    if len(winners):
        for i, (pid, r) in enumerate(winners.head(3).iterrows(), 1):
            top.append(f"{i}. <b>{r.channel}</b>\n    ROMI {r.romi:+.0%} · {rub(r.revenue)} · приписанных заказов {r.orders:.2f}")
    else:
        top.append("Пока ни одно размещение не окупилось.")
    loss = pl[pl.romi < 0].sort_values("romi")
    if len(loss):
        top += ["", "⚠️ <b>НЕ ОКУПИЛИСЬ</b>"]
        for pid, r in loss.head(3).iterrows():
            top.append(f"• <b>{r.channel}</b> · ROMI {r.romi:+.0%} · потрачено {rub(r.cost)} · приписанных заказов {r.orders:.2f}")

    good, bad = stable_winners(conn, config.ATTRIBUTION_WINDOW_DAYS)
    def names(ids, limit=3):
        got = [pl.loc[i, "channel"] for i in ids if i in pl.index]
        head = ", ".join(got[:limit])
        return head + (f" и ещё {len(got) - limit}" if len(got) > limit else "")
    tip = ["", "💡 <b>Рекомендация</b>"]
    if good:
        best = [i for i in pl.index if i in good]          # уже отсортированы по ROMI
        tip.append(f"Положительный атрибуционный ROMI во всех моделях: {names(best)}. Кандидаты для проверки контрольным экспериментом.")
    if bad:
        worst = [i for i in pl.sort_values("romi").index if i in bad]
        tip.append(f"В минусе во всех моделях: {names(worst)}. Кандидаты на сокращение.")
    if not good and not bad:
        tip.append("Выводы неустойчивы: результат меняется от модели атрибуции. "
                   "Нужно больше данных, прежде чем перекладывать бюджет.")
    tip.append("<i>Атрибуционный ROMI показывает, кому приписана покупка, а не то, случилась бы она без рекламы. "
               "Перед крупным перераспределением — holdout-эксперимент.</i>")
    return "\n".join(head + top + tip)


@router.message(Command("menu"))
async def admin_panel(message: Message):
    if not is_admin(message):
        return
    await message.answer("📊 <b>Marketing Analytics</b>\n\nЧто хотите сделать?", reply_markup=admin_menu())


@router.callback_query(F.data == "adm:menu")
async def adm_menu(cb: CallbackQuery):
    if not is_admin(cb):
        await cb.answer()
        return
    await cb.message.edit_text("📊 <b>Marketing Analytics</b>\n\nЧто хотите сделать?", reply_markup=admin_menu())
    await cb.answer()


@router.callback_query(F.data.startswith("romi:"))
async def adm_romi(cb: CallbackQuery):
    if not is_admin(cb):
        await cb.answer()
        return
    model = cb.data.split(":", 1)[1]
    await cb.answer("Считаю…")
    text = romi_dashboard(model)
    if cb.message.text != text:                      # Telegram отклоняет правку без изменений
        await cb.message.edit_text(text, reply_markup=romi_keyboard(model))


@router.callback_query(F.data.startswith("adm:"))
async def adm_actions(cb: CallbackQuery, state: FSMContext, bot: Bot):
    if not is_admin(cb):
        await cb.answer()
        return
    action = cb.data.split(":")[1]
    await cb.answer()
    if action == "newlink":
        await nl_ask(cb.message, state, {}, bot)
    elif action == "links":
        await send_links(cb.message, bot)
    elif action == "report":
        await send_report(cb.message, cb.data.split(":")[2])
    elif action == "status":
        await send_long(cb.message, status(conn, config.ACQUIRING_RATE), limit=3900)



# ================================================================================
# ВСТАВИТЬ В bot/main.py ПЕРЕД СТРОКОЙ:  async def main():

async def setup_commands(bot: Bot):
    """Кнопка «Меню» в Telegram: клиенту — только /start, админам — все команды."""
    await bot.set_my_commands([BotCommand(command="start", description="Курсы и цены")])
    admin_cmds = [
        BotCommand(command="menu", description="Панель: размещения, отчёты, ROMI"),
        BotCommand(command="newlink", description="Создать рекламное размещение"),
        BotCommand(command="links", description="Мои размещения и ссылки"),
        BotCommand(command="report", description="Отчёт: знаем / оцениваем / не видим"),
        BotCommand(command="status", description="Полнота данных"),
        BotCommand(command="export", description="Скачать базу"),
        BotCommand(command="cancel", description="Отменить ввод"),
    ]
    for chat_id in config.ADMIN_IDS | ({config.MANAGER_CHAT_ID} if config.MANAGER_CHAT_ID else set()):
        try:
            await bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(chat_id=chat_id))
        except Exception as e:                       # админ ещё не нажимал «Старт» — не критично
            logging.warning("Не удалось задать команды для %s: %s", chat_id, e)

def nl_parse(field: str, text: str):
    """Проверка одного поля размещения. Возвращает (значение, ошибка)"""
    t = (text or "").strip()
    if field == "placement_id":
        return (t, None) if TAG_RE.match(t) else (None, "Метка - только латиница, цифры, _ и -, без пробелов. Например fiztech_15sep")
    if field == "channel":
        return (t, None) if t else (None, "Напиши название канала")
    if field in ("cost", "discount_value"):
        v = t.replace(" ", "").replace(",", ".").rstrip("%₽р")
        try:
            num = float(v)
        except ValueError:
            return None, "Нужно число, например 10000" if field == "cost" else "Нужно число, например 20"
        if not math.isfinite(num) or num < 0 or (field == "discount_value" and num > 100):
            return None, "Число вне допустимого диапазона"
        return num, None
    if field == "publication_time":
        for fmt in ("%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M", "%d.%m %H:%M", "%Y-%m-%d", "%d.%m.%Y"):
            try:
                dt = datetime.strptime(t, fmt)
                if fmt.startswith("%d.%m %"):
                    dt = dt.replace(year=datetime.now().year)
                return dt.strftime("%Y-%m-%d %H:%M"), None
            except ValueError:
                pass
        return None, "Не понял дату. Пример: 15.09.2026 18:00"
    if field == "campaign_name":
        return (t, None) if t else (None, "Напиши название кампании")
    if field == "post_category":
        low = t.lower()
        for code, label in CAT_LABELS.items():
            if low in (code, label.lower()):
                return code, None
        return None, "Выбери тип поста кнопкой"
    return t, None


def nl_next(d: dict):
    for f in NL_ORDER:
        if f == "discount_value" and d.get("post_category") != "discount":
            continue          # размер скидки нужен только для скидочного поста
        if d.get(f) is None:
            return f
    return None


async def nl_ask(target: Message, state: FSMContext, d: dict, bot: Bot):
    """Спрашивает следующее незаполненное поле; когда всё заполнено - сохраняет размещение"""
    f = nl_next(d)
    if f is None:
        await nl_save(target, state, d, bot)
        return
    kb = InlineKeyboardBuilder()
    extra = {}
    if f == "placement_id":
        text = "Придумай <b>метку</b> для ссылки: латиница, цифры, _ и -.\nСхема: канал_дата, например <code>fiztech_15sep</code>"
    elif f == "channel":
        text = "<b>Где</b> размещение? Напиши название канала."
    elif f == "cost":
        text = "<b>Сколько стоит</b> размещение, ₽? Только число. 0 — если это свой канал."
    elif f == "publication_time":
        text = "<b>Когда выйдет пост?</b> Напиши дату и время, например <code>15.09.2026 18:00</code>"
        kb.button(text="Сейчас", callback_data="nl:pub:now")
    elif f == "campaign_name":
        camps = [r[0] for r in conn.execute("SELECT campaign_name FROM dim_placement WHERE campaign_name IS NOT NULL "
                                            "GROUP BY campaign_name ORDER BY MAX(created_at) DESC LIMIT 4")]
        extra["nl_camps"] = camps
        text = "К какой <b>кампании</b> относится? Выбери или напиши новое название (например «Запуск AI агентов»)."
        for i, c in enumerate(camps):
            kb.button(text=c, callback_data=f"nl:camp:{i}")
        kb.button(text="Без кампании", callback_data="nl:camp:none")
    elif f == "post_category":
        text = "Какой это <b>тип поста</b>?"
        for code, label in CAT_LABELS.items():
            kb.button(text=label, callback_data=f"nl:cat:{code}")
    else:
        text = "Какой <b>размер скидки</b>, %? Только число, например 20."
    kb.adjust(1)
    await state.set_state(NewLink.waiting)
    await state.update_data(nl=d, nl_await=f, **extra)
    await target.answer(text + "\n\n/cancel — отмена", reply_markup=kb.as_markup() if list(kb.buttons) else None)


async def nl_save(target: Message, state: FSMContext, d: dict, bot: Bot):
    existed = db.get_placement(conn, d["placement_id"]) is not None
    val = lambda k: None if d.get(k) == NONE else d.get(k)
    db.add_placement(conn, d["placement_id"], d["channel"], d["cost"], val("publication_time"), val("campaign_name"),
                     val("post_category"), val("discount_value"))
    await state.clear()
    me = await bot.get_me()
    cat = CAT_LABELS.get(d.get("post_category"), "—")
    disc = f", скидка {d['discount_value']:g}%" if isinstance(d.get("discount_value"), float) else ""
    await target.answer(
        ("⚠️ Метка уже была — данные размещения обновлены.\n\n" if existed else "") +
        f"Размещение сохранено ✅\n<b>{d['channel']}</b> · {rub(d['cost'])} · {d['publication_time']}\n"
        f"Кампания: {val('campaign_name') or '—'} · тип: {cat}{disc}\n\n"
        f"Ссылка для поста:\nhttps://t.me/{me.username}?start={d['placement_id']}",
        disable_web_page_preview=True)


@router.message(Command("newlink"))
async def newlink(message: Message, command: CommandObject, state: FSMContext, bot: Bot):
    if not is_admin(message):
        return
    parts = [x.strip() for x in (command.args or "").split("|")]
    d, errors = {}, []
    for field, raw in zip(NL_ORDER, parts):
        if not raw:
            continue
        v, err = nl_parse(field, raw)
        if err:
            errors.append(f"• {raw}: {err}")
        else:
            d[field] = v
    if errors:
        await message.answer("Эти значения не подошли, спрошу их заново:\n" + "\n".join(errors))
    await nl_ask(message, state, d, bot)


@router.message(NewLink.waiting, F.text, ~F.text.startswith("/"))
async def nl_text(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    d, f = data["nl"], data["nl_await"]
    v, err = nl_parse(f, message.text)
    if err:
        await message.answer(err)
        return
    d[f] = v
    await nl_ask(message, state, d, bot)


@router.callback_query(NewLink.waiting, F.data.startswith("nl:"))
async def nl_button(cb: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    d = data["nl"]
    _, kind, value = cb.data.split(":", 2)
    if kind == "pub":
        d["publication_time"] = datetime.now(db.MSK).strftime("%Y-%m-%d %H:%M")
    elif kind == "camp":
        d["campaign_name"] = NONE if value == "none" else data["nl_camps"][int(value)]
    elif kind == "cat":
        d["post_category"] = value
    await cb.answer()
    await nl_ask(cb.message, state, d, bot)


@router.message(Command("links"))
async def links(message: Message, bot: Bot):
    """/links: все размещения со ссылками"""
    if is_admin(message):
        await send_links(message, bot)


async def send_links(message: Message, bot: Bot):
    me = await bot.get_me()
    rows = conn.execute("SELECT * FROM dim_placement ORDER BY publication_time").fetchall()
    if not rows:
        await message.answer("Размещений пока нет. Создайте через /newlink")
        return
    text = "\n\n".join(f"<b>{r['channel']}</b> · {(rub(r['cost']) if r['cost_known'] else 'стоимость неизвестна')} · {r['publication_time'] or 'время не указано'}"
                       + (f" · {r['campaign_name']}" if r["campaign_name"] else "")
                       + f"\nhttps://t.me/{me.username}?start={r['placement_id']}" for r in rows)
    await send_long(message, text)


async def send_long(message: Message, text: str, limit=3800):
    chunk = ""
    for block in text.split("\n\n"):
        if len(chunk) + len(block) > limit:
            await message.answer(chunk, disable_web_page_preview=True)
            chunk = ""
        chunk += block + "\n\n"
    if chunk.strip():
        await message.answer(chunk, disable_web_page_preview=True)


@router.message(Command("report"))
async def report_cmd(message: Message, command: CommandObject):
    '''
    /report: отчёт в три блока, знаем / оцениваем / не видим'''
    if not is_admin(message):
        return
    model = (command.args or "last").strip().lower()
    if model not in MODELS:
        await message.answer(f"Модель: {', '.join(MODELS)}. Например: /report linear")
        return
    await send_report(message, model)


def pd_is_unknown_cost(cost):
    return cost is None or cost != cost


async def send_report(message: Message, model: str):
    """Компактный отчёт: по строке на размещение. Факты и оценки помечены в подписи."""
    rep = report(conn, model, config.ATTRIBUTION_WINDOW_DAYS)
    if rep.empty:
        await message.answer("Пока нет данных.")
        return

    rate = f" · эквайринг {config.ACQUIRING_RATE:.0%}" if config.ACQUIRING_RATE else ""
    blocks = [f"📈 <b>ОТЧЁТ ПО РЕКЛАМЕ</b>\nмодель {model} · окно {config.ATTRIBUTION_WINDOW_DAYS} дн.{rate}"]

    for pid, r in rep.iterrows():
        if pid == HISTORY:
            blocks.append(f"📦 <b>История base.xlsx</b>\n{r.orders:g} заказов · {rub(r.revenue)} · источник неизвестен")
            continue
        funnel = f"зашли {r.starts:g} → смотрели {r.viewed:g} → заявки {r.leads:g} → оплат {r.paid:g}"
        if pid == ORGANIC_BOT:
            blocks.append(f"🌱 <b>{r.channel}</b>\n{funnel}\nприписанная выручка {rub(r.revenue)}")
            continue
        line = [f"📢 <b>{r.channel}</b> ({pid})", funnel]
        if pd_is_unknown_cost(r.cost):
            line.append(f"⚠️ стоимость неизвестна · приписано {rub(r.revenue)} · ROMI не определён")
        elif r.cost > 0:
            money = f"затраты {rub(r.cost)} · приписано {rub(r.revenue)}"
            if r.cost_per_attributed_order == r.cost_per_attributed_order:
                money += f" · стоимость приписанной покупки {rub(r.cost_per_attributed_order)}"
            line.append(money)
            romi = f"<b>ROMI {r.romi:+.0%}</b>" if r.romi == r.romi else "ROMI —"
            if r.romi_margin == r.romi_margin:
                romi += f" · с учётом комиссии {r.romi_margin:+.0%}"
            line.append(romi)
        elif r.placement_type == "unknown":
            line.append("⚠️ метки нет в /newlink: канал и стоимость неизвестны")
        else:
            line.append(f"бесплатное размещение · приписано {rub(r.revenue)}")
        blocks.append("\n".join(line))

    blocks.append("<i>Воронка и оплаты - факты. ROMI - оценка модели атрибуции: "
                  "сравните /report linear и /report first.</i>\nЧто ещё не заполнено - /status")
    await send_long(message, "\n\n".join(blocks))


@router.message(Command("status"))
async def status_cmd(message: Message):
    if is_admin(message):
        await send_long(message, status(conn, config.ACQUIRING_RATE), limit=3900)


@router.message(Command("export"))
async def export(message: Message):
    if is_admin(message):
        with tempfile.TemporaryDirectory(prefix="attribution-export-") as folder:
            path = Path(folder) / "backup.db"
            # Read the committed database through a separate connection: backing
            # up the worker connection inside its event transaction would hang.
            with sqlite3.connect(config.DB_PATH) as reader, sqlite3.connect(path) as snapshot:
                reader.backup(snapshot)
                event = CURRENT_EVENT.get()
                if event:
                    snapshot.execute("UPDATE telegram_inbox SET status='done', payload=NULL, processed_at=? "
                                     "WHERE bot_id=? AND update_id=?",
                                     (stamp(), event["bot_id"], event["update_id"]))
            await message.answer_document(FSInputFile(path))


@router.callback_query(F.data.startswith("nl:"))
async def expired_link_dialog(cb: CallbackQuery):
    await cb.answer()
    if is_admin(cb):
        await cb.message.answer("Диалог создания размещения завершён или потерян после перезапуска. Начните заново: /newlink")


@router.message(F.text)
async def unhandled_text(message: Message):
    if is_staff(message):
        await message.answer("Нет активного диалога. После перезапуска незавершённый ввод нужно повторить. "
                             "Оплата: /paid номер_заявки сумма. Размещение: /newlink. Меню: /menu")
    else:
        await message.answer("Откройте каталог через /start.")


async def main():
    logging.basicConfig(level=logging.INFO)
    hint = f"Проверьте BOT_TOKEN в файле {config.ENV_PATH}"
    if not config.BOT_TOKEN:
        raise SystemExit(f"Нет BOT_TOKEN. Скопируйте .env.example в .env и вставьте токен от @BotFather.\n{hint}")
    if "your-token" in config.BOT_TOKEN:
        raise SystemExit(f"В .env остался токен-пример из .env.example. Вставьте свой токен от @BotFather.\n{hint}")
    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        while True:
            try:
                me = await bot.get_me()
                await setup_commands(bot)
                break
            except TelegramUnauthorizedError:
                raise SystemExit("Telegram не принял токен. Возьмите действующий токен в @BotFather.\n" + hint)
            except (TelegramNetworkError, TelegramRetryAfter, TelegramServerError) as exc:
                logging.warning("Telegram временно недоступен (%s), повтор подключения", type(exc).__name__)
                await asyncio.sleep(getattr(exc, "retry_after", 3))
        print(f"Бот @{me.username} запущен. База: {config.DB_PATH}. Остановить — Ctrl+C", flush=True)
        dp = Dispatcher()
        dp.include_router(router)
        bot.session.middleware(tolerate_stale_ui)
        await run(conn, dp, bot, config.DB_PATH)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    with instance_lock(config.DB_PATH):
        try:
            asyncio.run(main())
        finally:
            conn.close()
