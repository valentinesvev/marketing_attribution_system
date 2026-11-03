"""Descriptive post categories; automatic candidates plus reviewed overrides."""
import re
import pandas as pd

LABELS = {"discount": "Скидка", "course_launch": "Старт / запуск курса",
          "event_announcement": "Анонс события", "regular": "Регулярный пост"}

# Reviewed in the local Telegram export. Stable IDs, reasons retained for audit.
REVIEWED = {
    1834: ("discount", "external", "Скидка на магистратуру РУДН/Wildberries, не на собственные курсы"),
    1836: ("discount", "own", "Финальная скидка 55% на СТАРТы"),
    1837: ("discount", "own", "Последние 6 часов распродажи СТАРТ"),
    1838: ("discount", "own", "Продление распродажи до конца дня"),
    1840: ("discount", "own", "Финальные 4 часа скидки на курсы СТАРТ"),
    1859: ("course_launch", "own", "Мы запускаем ПРО; открываем пять направлений; дата старта занятий не указана"),
    1874: ("discount", "own", "Запись вебинара с промокодом на скидку"),
    1887: ("event_announcement", "own", "Бесплатная неделя Аналитика вне SQL 7–13 сентября"),
    1892: ("event_announcement", "own", "Продвижение открытой недели аналитики, стартующей сегодня"),
    1893: ("event_announcement", "own", "Приглашение на первое занятие сегодня в 19:00"),
}


def clean_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(x if isinstance(x, str) else str(x.get("text", "")) for x in value)
    return ""


def classify_post(text, post_id=None, products=()):
    t = text.lower()
    # A percentage may describe hiring statistics; it is not itself an offer.
    discount = bool(re.search(r"скидк\w*|скидоч\w*|промокод\w*|распродаж\w*|спецпредложени\w*", t))
    launch = bool(re.search(r"(?:запускаем|открываем|стартует|стартуем|старт продаж|открыли продажи|набор на курс).{0,100}(?:курс|про|поток|направлен)|курс.{0,40}(?:стартует|запускается)", t, re.S))
    event = bool(re.search(r"(?:проводим|приглашаем|присоедин|ждем вас|ждём вас|завтра|сегодня).{0,100}(?:вебинар|недел|интенсив|заняти|встреч)|(?:вебинар|интенсив|встреч).{0,70}(?:завтра|сегодня|состоится)", t, re.S))
    category = "discount" if discount else "course_launch" if launch else "event_announcement" if event else "regular"
    scope, method, reason = "unclear", "rule_candidate", "Автоматическая разметка; требует просмотра текста"
    if post_id in REVIEWED:
        category, scope, reason = REVIEWED[post_id]
        method = "reviewed"
        discount |= category == "discount"
        launch |= category == "course_launch"
        event |= category == "event_announcement"
    return dict(post_type=category, product=next((p for p in sorted(products, key=len, reverse=True) if p.lower() in t), None),
                is_discount=discount, is_course_launch=launch, is_event_announcement=event,
                offer_scope=scope, classification_method=method, classification_reason=reason)


def classify_posts(posts, products=()):
    rows = [dict(r, **classify_post(r["post_text"], r["post_id"], products)) for r in posts.to_dict("records")]
    return pd.DataFrame(rows)
