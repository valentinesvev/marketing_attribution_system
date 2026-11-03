"""
Атрибуция и ROMI.

Три правила движка:
1. Окно атрибуции (по умолчанию 30 дней, ATTRIBUTION_WINDOW_DAYS): касание старше окна «протухло»
   и выручку не получает.
2. Повторные покупки: для каждого заказа берутся только касания ПОСЛЕ предыдущей покупки этого клиента.
   Заслуги рекламы, которая уже привела к прошлой покупке, обнуляются.
3. Органика: если рекламных касаний в окне нет, выручка не пропадает — она уходит в Organic/Direct.

Касание = заход в бота по рекламной метке (fact_touch с placement_id). Заходы без метки не рекламные.

Модели:  last — 100% последнему касанию (по умолчанию) · first — 100% первому ·
         linear — поровну · position — 40% первому, 40% последнему, 20% поровну средним
         (при двух касаниях 50/50, при одном 100%).
Результат по всем моделям пишется в fact_attribution.
"""
import numpy as np
import pandas as pd

from core.db import now_msk

MODELS = ("last", "first", "linear", "position")
ORGANIC_BOT = "organic_bot"
HISTORY = "history_base_xlsx"


def load(conn):
    q = lambda sql: pd.read_sql(sql, conn)
    pl = q("SELECT * FROM dim_placement")
    touches = q("SELECT * FROM fact_touch")
    events = q("SELECT * FROM fact_bot_event")
    leads = q("SELECT * FROM fact_lead")
    orders = q("SELECT * FROM fact_order")
    for df, col in [(touches, "timestamp"), (orders, "timestamp")]:
        # Backups may contain both ISO dates and ISO dates with time.
        df[col] = pd.to_datetime(df[col], format="ISO8601")
    return pl, touches, events, leads, orders


def compute(touches, orders, model="last", window_days=30) -> pd.DataFrame:
    """Строки атрибуции: order_id, touch_id, placement_id, weight, attributed_revenue, attributed_margin."""
    o = orders[["order_id", "user_id_hash", "amount", "variable_costs", "timestamp"]].sort_values(["user_id_hash", "timestamp"])
    o["prev_order_ts"] = o.groupby("user_id_hash").timestamp.shift()          # правило 2: прошлая покупка клиента
    paid = (touches[touches.placement_id.notna()][["touch_id", "user_id_hash", "placement_id", "timestamp"]]
            .rename(columns={"timestamp": "touch_ts"}))
    m = o.merge(paid, on="user_id_hash")
    m = m[(m.touch_ts <= m.timestamp)
          & (m.touch_ts >= m.timestamp - pd.Timedelta(days=window_days))       # правило 1: окно
          & (m.prev_order_ts.isna() | (m.touch_ts > m.prev_order_ts))]       # правило 2: только после прошлой покупки
    m = m.sort_values(["order_id", "touch_ts", "touch_id"])
    n = m.groupby("order_id").order_id.transform("size")
    pos = m.groupby("order_id").cumcount()

    if model == "last":
        w = (pos == n - 1).astype(float)
    elif model == "first":
        w = (pos == 0).astype(float)
    elif model == "linear":
        w = 1 / n
    elif model == "position":
        edge = (pos == 0) | (pos == n - 1)
        w = np.select([n == 1, n == 2, edge], [1.0, 0.5, 0.4], default=0.2 / (n - 2).clip(lower=1))
    else:
        raise ValueError(f"model должна быть одной из {MODELS}")
    m = m.assign(weight=w)
    m = m[m.weight > 0]

    org = o[~o.order_id.isin(m.order_id)].assign(touch_id=None, placement_id=None, weight=1.0)   # правило 3
    out = pd.concat([m, org], ignore_index=True)
    out["attributed_revenue"] = out.amount * out.weight
    out["attributed_margin"] = (out.amount - out.variable_costs) * out.weight
    return out[["order_id", "touch_id", "placement_id", "weight", "attributed_revenue", "attributed_margin"]]


def rebuild(conn, window_days=30) -> None:
    """Пересчитывает fact_attribution по всем моделям."""
    _, touches, _, _, orders = load(conn)
    conn.execute("DELETE FROM fact_attribution")
    ts = now_msk()
    for model in MODELS:
        a = compute(touches, orders, model, window_days)
        conn.executemany(
            "INSERT INTO fact_attribution(order_id, touch_id, placement_id, attribution_model, weight, "
            "attributed_revenue, attributed_margin, window_days, calculated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            [(r.order_id, None if pd.isna(r.touch_id) else int(r.touch_id),
              None if pd.isna(r.placement_id) else r.placement_id, model, float(r.weight),
              float(r.attributed_revenue), float(r.attributed_margin), window_days, ts)
             for r in a.itertuples()])
    conn.commit()


def report(conn, model="last", window_days=30, persist=True) -> pd.DataFrame:
    """
    Строка на каждое размещение + две строки без рекламы:
      organic_bot       — пришли в бота без метки / купили вне окна атрибуции
      history_base_xlsx — старые продажи из base.xlsx, источник которых неизвестен
    ЗНАЕМ (факт):      starts → viewed → leads → paid / paid_revenue (оплаты по заявкам с этой ссылки), cost
    ОЦЕНИВАЕМ (модель): orders / revenue / margin (атрибуция с окном), CPA, ROMI
    ROMI = (выручка − стоимость) / стоимость;  ROMI_margin — то же по марже (выручка − эквайринг).
    """
    if persist:
        rebuild(conn, window_days)      # основной расчёт сохраняем в fact_attribution
    pl, touches, events, leads, orders = load(conn)
    touches["key"] = touches.placement_id.fillna(ORGANIC_BOT)
    events = events[events.event_type.isin(["view_direction", "view_course", "view_bundle"])]
    ev = events.merge(touches[["touch_id", "key"]], on="touch_id")
    ld = leads.merge(touches[["touch_id", "key"]], on="touch_id", how="left").fillna({"key": ORGANIC_BOT})
    # ФАКТ: оплаты, которые менеджер отметил по заявке, пришедшей по этой ссылке (без модели и окна)
    direct = orders[orders.lead_id.notna()].merge(ld[["lead_id", "key"]], on="lead_id")
    funnel = pd.DataFrame({
        "starts": touches.groupby("key").user_id_hash.nunique(),
        "viewed": ev.groupby("key").user_id_hash.nunique(),
        "leads": ld.groupby("key").lead_id.nunique(),
        "paid": direct.groupby("key").order_id.nunique(),
        "paid_revenue": direct.groupby("key").amount.sum(),
    })

    att = compute(touches, orders, model, window_days).merge(orders[["order_id", "source_system"]], on="order_id")
    att["key"] = att.placement_id
    att.loc[att.key.isna(), "key"] = att.source_system.map({"bot": ORGANIC_BOT, "base_xlsx": HISTORY})
    att["commission_known"] = att.order_id.map(orders.set_index("order_id").variable_costs_source).isin(["actual", "rate_estimate"])
    money = att.groupby("key").agg(orders=("weight", "sum"), revenue=("attributed_revenue", "sum"),
                                   margin=("attributed_margin", "sum"))

    complete_commission = att.groupby("key").commission_known.all()
    money.loc[~complete_commission, "margin"] = np.nan
    base = pl.set_index("placement_id")[["channel", "campaign_name", "post_category", "placement_type",
                                         "publication_time", "cost", "cost_known"]]
    extra = pd.DataFrame({"channel": ["Бот без рекламной метки", "История base.xlsx (источник неизвестен)"],
                          "placement_type": ["-", "-"], "cost": [0.0, 0.0]}, index=[ORGANIC_BOT, HISTORY])
    rep = pd.concat([base, extra]).join(funnel).join(money)
    cols = ["starts", "viewed", "leads", "paid", "paid_revenue", "orders", "revenue"]
    rep[cols] = rep[cols].fillna(0).astype(float)
    rep.loc[~rep.index.isin(money.index), "margin"] = 0.0
    rep.loc[rep.cost_known.ne(1), "cost"] = np.nan
    rep = rep[(rep.index.isin(pl.placement_id)) | (rep[["starts", "orders"]].sum(axis=1) > 0)]
    rep["orders"] = rep.orders.round(6)

    paid = rep.cost > 0
    rep["cost_per_attributed_order"] = rep.cost / rep.orders.where(rep.orders > 0)
    rep["cpa"] = rep.cost_per_attributed_order  # compatibility alias
    rep["romi"] = (rep.revenue - rep.cost) / rep.cost.where(paid)
    rep["romi_margin"] = (rep.margin - rep.cost) / rep.cost.where(paid)
    rep["attributed_after_acquiring"] = rep.margin
    rep["romi_after_acquiring_pct"] = 100 * rep.romi_margin
    rep["attributed_romi_pct"] = 100 * rep.romi
    rep.index.name = "placement_id"
    rep = rep.sort_values(["cost", "revenue"], ascending=False)
    return pd.concat([rep.drop(index=HISTORY, errors="ignore"), rep.loc[rep.index == HISTORY]])  # история — в конец


def compare_models(conn, window_days=30) -> pd.DataFrame:
    """ROMI размещений во всех моделях: если вывод о канале меняется от модели — решение по нему ненадёжно."""
    return pd.DataFrame({m: report(conn, m, window_days, persist=False)["romi"] for m in MODELS}).dropna(how="all")


def compare_windows(conn, model="last", windows=(7, 30)) -> pd.DataFrame:
    """ROMI при разных окнах атрибуции: длинное окно отдаёт рекламе часть органики."""
    return pd.DataFrame({f"{w} дн.": report(conn, model, w, persist=False)["romi"] for w in windows}).dropna(how="all")


def summary(conn, model="last", window_days=30) -> dict:
    """Свод для дашборда: период, выручка, затраты, приписанная выручка, ROMI, топ размещений."""
    rep = report(conn, model, window_days)
    ads = rep[~rep.index.isin([ORGANIC_BOT, HISTORY])]
    paid = ads[ads.cost > 0]
    cost_complete = ads.cost.notna().all()
    cost = float(ads.cost.sum()) if cost_complete else None
    attributed = float(ads.revenue.sum())
    after_acquiring = float(ads.margin.sum()) if ads.margin.notna().all() else None
    period = conn.execute("SELECT MIN(timestamp), MAX(timestamp) FROM fact_order").fetchone()
    return {
        "model": model, "window": window_days,
        "period": (period[0], period[1]),
        "revenue_total": float(rep.revenue.sum()),
        "revenue_bot": float(rep.drop(index=HISTORY, errors="ignore").revenue.sum()),
        "cost": cost,
        "attributed": attributed,
        "margin": after_acquiring,
        "cost_complete": bool(cost_complete),
        "romi": (attributed - cost) / cost if cost else None,
        "romi_margin": (after_acquiring - cost) / cost if cost and after_acquiring is not None else None,
        "placements": paid.sort_values("romi", ascending=False),
        "n_placements": len(ads),
    }


def stable_winners(conn, window_days=30, min_orders=1) -> tuple[list, list]:
    """Размещения, которые окупаются (или нет) во ВСЕХ моделях — по ним вывод устойчив."""
    cmp_ = compare_models(conn, window_days)
    if cmp_.empty:
        return [], []
    # Apply the documented minimum to every model's fractional order count.
    sufficient = pd.concat([report(conn, m, window_days, persist=False).orders.ge(min_orders)
                            for m in MODELS], axis=1).all(axis=1)
    cmp_ = cmp_.loc[cmp_.index.intersection(sufficient[sufficient].index)]
    good = cmp_[(cmp_ > 0).all(axis=1)].index.tolist()
    bad = cmp_[(cmp_ < 0).all(axis=1)].index.tolist()
    return good, bad
