"""Four reproducible pilot histories for checking the synthetic sample design."""

import numpy as np
import pandas as pd

from core.importer import read_sales

COURSES = ("Аналитика старт", "Аналитика про")
SCENARIOS = ("flat", "external_event", "course_launch", "zero_ad_effect")


def generate_pilots(history_path, seed=42, n_users=1000):
    """Yield a calendar and linked synthetic event tables for each scenario."""
    sales = read_sales(history_path)
    prices = [
        sales.loc[sales.course.eq(course), "amount"].to_numpy()
        for course in COURSES
    ]
    if any(not len(amounts) for amounts in prices):
        raise ValueError("Missing historical prices for a selected course")

    rng = np.random.default_rng(seed)
    days = 90
    users = np.arange(1, n_users + 1)
    post_days = np.array([5, 12, 19, 30, 38, 45, 52, 59])
    targets = np.array([0, 1, 0, 1, 0, 1, 0, 1])
    categories = ["regular", "regular", "discount", "regular",
                  "event_announcement", "regular", "regular", "discount"]
    reach = rng.random((n_users, len(post_days)))
    view = rng.random(reach.shape)
    lead = rng.random(reach.shape)
    direct_day = rng.integers(0, days, n_users)
    direct_visit = rng.random(n_users) < .30
    purchase = rng.random((days, n_users, 2))
    price_index = [
        rng.integers(len(amounts), size=(days, n_users)) for amounts in prices
    ]
    bundle = rng.random((days, n_users))
    for scenario in SCENARIOS:
        placements = pd.DataFrame({
            "placement_id": [f"p{i+1}" for i in range(len(post_days))],
            "day": post_days + 1, "course": [COURSES[i] for i in targets],
            "category": categories, "cost": 6000, "placement_type": "external",
            "discount_fraction": [.15 if c == "discount" else 0 for c in categories],
        })
        if scenario == "course_launch":
            placements.loc[3, "category"] = "course_launch"
        available = np.ones((days, 2), dtype=bool)
        background = np.full((days, 2), .001)
        if scenario == "external_event":
            background[30:37, 1] *= 3
        elif scenario == "course_launch":
            available[:30, 1] = False
            background[30:37, 1] *= 3

        eligible = reach < .16
        eligible = eligible & available[post_days, targets]
        touch_rows, view_rows, lead_rows = [], [], []
        for u, p in zip(*np.where(eligible)):
            tid = len(touch_rows) + 1
            pid, course, day = placements.loc[p, ["placement_id", "course", "day"]]
            touch_rows.append((tid, users[u], pid, day))
            if view[u, p] < .65:
                view_rows.append((tid, users[u], course, day))
                if lead[u, p] < .25:
                    lead_rows.append((len(lead_rows)+1, tid, users[u], course, day))
        for u in np.flatnonzero(direct_visit):
            touch_rows.append((len(touch_rows)+1, users[u], None, direct_day[u]+1))

        order_rows, item_rows, truth_rows = [], [], []
        last = np.full(n_users, -100)
        order_count = np.zeros(n_users, dtype=int)
        for day in range(days):
            active = (post_days <= day) & (day - post_days < 7)
            recent = eligible & active & (post_days > last[:, None])
            allowed = (day-last > 3) & (order_count < 2)
            for u in np.flatnonzero(allowed):
                for c in np.flatnonzero(available[day]):
                    candidates = np.flatnonzero(recent[u] & (targets == c))
                    lift = .003 if len(candidates) and scenario != "zero_ad_effect" else 0
                    base = background[day, c] * (.25 if order_count[u] else 1)
                    if purchase[day, u, c] >= base + lift:
                        continue
                    driver = int(candidates[-1]) if purchase[day, u, c] >= base else None
                    order_id = len(order_rows) + 1
                    items = [c]
                    if bundle[day, u] < .10 and available[day, 1-c]:
                        items.append(1-c)
                    amounts = []
                    for item in items:
                        promo = recent[u] & (targets == item) & placements.discount_fraction.gt(0).to_numpy()
                        # The null scenario preserves contacts while disabling price and purchase effects.
                        discount = .15 if promo.any() and scenario != "zero_ad_effect" else 0
                        amount = round(float(prices[item][price_index[item][day, u]]) * (1-discount), 2)
                        amounts.append(amount)
                        item_rows.append((order_id, COURSES[item], amount, discount))
                    order_rows.append((order_id, users[u], day+1, round(sum(amounts), 2)))
                    truth_rows.append((order_id, "organic" if driver is None else "ad_tagged",
                                       None if driver is None else placements.placement_id[driver]))
                    last[u] = day
                    order_count[u] += 1
                    break
        yield scenario, {
            "users": pd.DataFrame({"user_id": users}),
            "placements": placements,
            "touches": pd.DataFrame(touch_rows, columns=["touch_id", "user_id", "placement_id", "day"]),
            "views": pd.DataFrame(view_rows, columns=["touch_id", "user_id", "course", "day"]),
            "leads": pd.DataFrame(lead_rows, columns=["lead_id", "touch_id", "user_id", "course", "day"]),
            "orders": pd.DataFrame(order_rows, columns=["order_id", "user_id", "day", "amount"]),
            "items": pd.DataFrame(item_rows, columns=["order_id", "course", "amount", "discount_fraction"]),
            "mechanism_labels": pd.DataFrame(truth_rows, columns=["order_id", "mechanism", "driver_placement_id"]),
        }


def summarize(pilots):
    rows = []
    for scenario, tables in pilots:
        orders, items, leads = (tables[name] for name in ("orders", "items", "leads"))
        buyers = set(orders.user_id)
        rows.append({
            "scenario": scenario, "users": len(tables["users"]),
            "visitors": tables["touches"].user_id.nunique(),
            "touches": len(tables["touches"]), "views": len(tables["views"]),
            "leads": len(leads), "leads_from_nonbuyers": (~leads.user_id.isin(buyers)).sum(),
            "buyers": len(buyers), "orders": len(orders), "items": len(items),
            "revenue_rub": round(orders.amount.sum(), 2),
            "ad_tagged_orders": tables["mechanism_labels"].mechanism.eq("ad_tagged").sum(),
        })
    return pd.DataFrame(rows)
