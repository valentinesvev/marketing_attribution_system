"""Stage 1: consistent simulation, revenue conservation, equal-age comparisons.

The historical demand curve is a scenario input, NOT an estimate of natural demand.
Generator labels describe the purchase mechanism, NOT a counterfactual experiment.
"""
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import tempfile

import numpy as np
import pandas as pd

from core import attribution, db
from core.importer import read_sales


@dataclass(frozen=True)
class Config:
    seed: int = 42
    n_users: int = 1000
    n_runs: int = 30
    attribution_days: int = 30
    horizons: tuple = (7, 14, 30)
    base_daily_purchase: float = 0.018
    organic_visit_probability: float = 0.28
    view_probability: float = 0.72
    propensity_sigma: float = 0.45
    propensity_min: float = 0.45
    propensity_max: float = 2.4
    decay_tau_days: float = 7.0  # e-folding time, not half-life
    min_repeat_days: float = 3.0
    max_orders: int = 2
    acquiring_rate: float = 0.03
    # Outside observed history use an explicit flat mean (normalized index=1).
    demand_extension: float = 1.0

    def __post_init__(self):
        if self.n_users < 1 or self.n_runs < 1 or self.attribution_days <= 0:
            raise ValueError("Population, runs and attribution window must be positive")
        if not self.horizons or any(h <= 0 for h in self.horizons):
            raise ValueError("Evaluation horizons must be positive")
        if self.decay_tau_days <= 0 or self.min_repeat_days < 0 or self.max_orders < 1:
            raise ValueError("Invalid time/order parameters")
        if self.propensity_sigma < 0 or not 0 < self.propensity_min <= self.propensity_max:
            raise ValueError("Invalid propensity parameters")
        if self.demand_extension < 0:
            raise ValueError("Demand must be nonnegative")
        for p in (self.base_daily_purchase, self.organic_visit_probability,
                  self.view_probability, self.acquiring_rate):
            if not 0 <= p <= 1:
                raise ValueError("Probability/rate must be in [0,1]")


def load_history(path):
    """Fail visibly on missing/malformed history; never substitute mock inputs."""
    raw = read_sales(Path(path))
    if raw.empty or (raw.amount <= 0).any():
        raise ValueError("History must contain positive sales")
    orders = raw.groupby(["student_id", "ts"], as_index=False).agg(
        amount=("amount", "sum"), n_items=("course", "size"))
    daily = orders.groupby(orders.ts.dt.normalize()).size().asfreq("D", fill_value=0)
    smooth = daily.rolling(5, center=True, min_periods=1).mean()
    demand = smooth / smooth.mean()
    return {"orders": orders, "demand": demand,
            "repeat_rate": float(orders.groupby("student_id").size().gt(1).mean()),
            "source": str(Path(path).resolve())}


def build_placements(start):
    """Original four placements; category-specific mechanics deferred to stage 2."""
    rows = [
        ("a", "tg_ml_jobs", 3, 70000, .24, .0060, "sale"),
        ("b", "tg_data_jobs", 13, 55000, .22, .0048, "native"),
        ("c", "tg_students", 23, 40000, .18, .0035, "discount"),
        ("d", "tg_career", 30, 40000, .34, .0015, "content"),
    ]
    return pd.DataFrame([dict(placement_id=pid, channel=channel, campaign_name="stage1",
                             placement_type="external", creative=f"creative_{pid}", post_category=cat,
                             discount_value=0, cost=float(cost),
                             publication_time=pd.Timestamp(start) + pd.Timedelta(days=day),
                             reach_probability=reach, daily_lift=lift)
                         for pid, channel, day, cost, reach, lift, cat in rows])


def demand_at(history, day, config):
    return float(history["demand"].get(pd.Timestamp(day).normalize(), config.demand_extension))


def eligible_touches(touches, purchase_at, previous_at, window_days):
    return [t for t in touches if t["placement_id"] is not None
            and purchase_at - pd.Timedelta(days=window_days) <= t["timestamp"] <= purchase_at
            and (previous_at is None or t["timestamp"] > previous_at)]


def _empty_frames():
    # Fixed columns make zero-sale and zero-touch scenarios valid too.
    schemas = {
        "dim_user": "user_id_hash username_hash first_seen first_placement_id source_system",
        "fact_touch": "touch_id user_id_hash placement_id touch_type timestamp is_synthetic",
        "fact_bot_event": "event_id user_id_hash touch_id event_type item timestamp is_synthetic",
        "fact_lead": "lead_id user_id_hash touch_id course_interest self_reported_source conversation_started_at manager_id status is_synthetic",
        "fact_order": "order_id user_id_hash lead_id student_id amount variable_costs variable_costs_source timestamp source_system is_synthetic",
        "fact_order_item": "item_id order_id course amount",
        "order_truth": "order_id mechanism driver_placement_id driver_touch_id timestamp amount",
    }
    return schemas, {name: [] for name in schemas}


def simulate(history, placements, config):
    rng = np.random.default_rng(config.seed)
    start = history["demand"].index.min()
    # Half-open observation interval [start, end); all placements mature to max H.
    end = max(history["demand"].index.max() + pd.Timedelta(days=1),
              placements.publication_time.max() + pd.Timedelta(days=max(config.horizons)))
    days = pd.date_range(start, end, freq="D", inclusive="left")
    amounts = history["orders"].amount.to_numpy(float)
    meta = placements.set_index("placement_id").to_dict("index")
    if placements.placement_id.duplicated().any() or (placements.cost < 0).any():
        raise ValueError("Placement IDs must be unique; costs must be nonnegative")
    if not placements.reach_probability.between(0, 1).all() or (placements.daily_lift < 0).any():
        raise ValueError("Invalid reach/lift")
    schemas, rows = _empty_frames()
    for i in range(config.n_users):
        uid = f"u{i:05d}"
        propensity = float(np.clip(rng.lognormal(-.08, config.propensity_sigma),
                                   config.propensity_min, config.propensity_max))
        touches = []

        def add_touch(at, pid):
            if not start <= at < end:
                return
            t = dict(touch_id=len(rows["fact_touch"]) + 1, user_id_hash=uid,
                     placement_id=pid, touch_type="bot_start", timestamp=at, is_synthetic=1)
            touches.append(t)
            rows["fact_touch"].append(t)

        for p in placements.itertuples():
            probability = min(.90, p.reach_probability * (.8 + .2 * propensity))
            if rng.random() < probability:
                add_touch(p.publication_time + pd.Timedelta(hours=int(rng.integers(0, 60))), p.placement_id)
        if rng.random() < config.organic_visit_probability:
            add_touch(start + pd.Timedelta(minutes=int(rng.integers(0, (end-start).total_seconds()/60))), None)
        previous = None
        user_orders = []
        for day in days:
            if len(user_orders) >= config.max_orders:
                break
            # Decide chronological eligibility at the ACTUAL candidate timestamp.
            at = day + pd.Timedelta(hours=int(rng.integers(10, 22)), minutes=int(rng.integers(0, 60)))
            if at >= end or (previous is not None and at <= previous + pd.Timedelta(days=config.min_repeat_days)):
                continue
            repeat_multiplier = 1.0 if previous is None else min(.55, max(.20, history["repeat_rate"] * 1.8))
            organic_probability = min(.20, config.base_daily_purchase * demand_at(history, day, config)
                                       * propensity * repeat_multiplier)
            mechanism, driver = None, None
            if rng.random() < organic_probability:
                mechanism = "organic"
            else:
                eligible = eligible_touches(touches, at, previous, config.attribution_days)
                effects = np.array([meta[t["placement_id"]]["daily_lift"] * np.exp(
                    -(at-t["timestamp"]).total_seconds()/86400/config.decay_tau_days) for t in eligible])
                total = effects.sum()
                if total > 0 and rng.random() < min(.12, total):
                    mechanism = "ad_tagged"
                    driver = eligible[int(rng.choice(len(eligible), p=effects/total))]
            if mechanism is None:
                continue
            amount = round(float(rng.choice(amounts)), 2)
            before = [t for t in touches if t["timestamp"] <= at]
            latest = max(before, key=lambda t: (t["timestamp"], t["touch_id"])) if before else None
            lead_at = max(at-pd.Timedelta(hours=2), latest["timestamp"]) if latest else at-pd.Timedelta(hours=2)
            lid = len(rows["fact_lead"]) + 1001
            oid = f"s{config.seed}_{uid}_{len(user_orders)+1}"
            rows["fact_lead"].append(dict(lead_id=lid, user_id_hash=uid,
                touch_id=latest["touch_id"] if latest else None, course_interest="synthetic_course",
                self_reported_source=None, conversation_started_at=lead_at,
                manager_id="synthetic_manager", status="paid", is_synthetic=1))
            rows["fact_order"].append(dict(order_id=oid, user_id_hash=uid, lead_id=lid, student_id=None,
                amount=amount, variable_costs=round(amount*config.acquiring_rate, 2),
                variable_costs_source="rate_estimate", timestamp=at, source_system="bot", is_synthetic=1))
            rows["fact_order_item"].append(dict(item_id=len(rows["fact_order_item"])+1,
                                                order_id=oid, course="synthetic_course", amount=amount))
            rows["order_truth"].append(dict(order_id=oid, mechanism=mechanism,
                driver_placement_id=driver["placement_id"] if driver else None,
                driver_touch_id=driver["touch_id"] if driver else None, timestamp=at, amount=amount))
            user_orders.append(at)
            previous = at
            if len(user_orders) == 1 and rng.random() < history["repeat_rate"]:
                future = placements[(placements.publication_time > at) & (placements.reach_probability > 0)]
                if not future.empty:
                    p = future.iloc[int(rng.integers(len(future)))]
                    add_touch(max(at+pd.Timedelta(days=4), p.publication_time), p.placement_id)
        for t in touches:
            viewed_at = t["timestamp"] + pd.Timedelta(minutes=5)
            if viewed_at < end and rng.random() < config.view_probability:
                rows["fact_bot_event"].append(dict(event_id=len(rows["fact_bot_event"])+1,
                    user_id_hash=uid, touch_id=t["touch_id"], event_type="view_course",
                    item="synthetic_course", timestamp=viewed_at, is_synthetic=1))
        known = [t["timestamp"] for t in touches] + [t-pd.Timedelta(hours=2) for t in user_orders]
        first = min(touches, key=lambda t: t["timestamp"]) if touches else None
        rows["dim_user"].append(dict(user_id_hash=uid, username_hash=None,
            first_seen=min(known) if known else start,
            first_placement_id=first["placement_id"] if first else None, source_system="bot"))
    frames = {name: pd.DataFrame(rows[name], columns=columns.split()) for name, columns in schemas.items()}
    for name, column in (("fact_touch", "timestamp"), ("fact_bot_event", "timestamp"),
                         ("fact_order", "timestamp"), ("order_truth", "timestamp"),
                         ("fact_lead", "conversation_started_at"), ("dim_user", "first_seen")):
        frames[name][column] = pd.to_datetime(frames[name][column])
    frames["dim_placement"] = placements.drop(columns=["reach_probability", "daily_lift"]).assign(
        created_at=start, is_synthetic=1, cost_known=1)
    return {"frames": frames, "placements": placements.copy(), "config": config,
            "start": start, "end": end}


def allocate(simulation, model):
    """Use exactly the bot's engine; retain organic rows for balance checks."""
    frames = simulation["frames"]
    return attribution.compute(frames["fact_touch"], frames["fact_order"],
                               model, simulation["config"].attribution_days)


def validate_simulation(sim):
    f, config = sim["frames"], sim["config"]
    def require(condition, message):
        if not condition:
            raise ValueError(message)
    for table, pk in (("dim_user", "user_id_hash"), ("dim_placement", "placement_id"),
                      ("fact_touch", "touch_id"), ("fact_lead", "lead_id"),
                      ("fact_order", "order_id"), ("fact_order_item", "item_id"),
                      ("fact_bot_event", "event_id"), ("order_truth", "order_id")):
        require(not f[table][pk].duplicated().any(), f"Duplicate key: {table}")
    for table in ("fact_touch", "fact_bot_event", "fact_lead", "fact_order"):
        require(f[table].user_id_hash.isin(f["dim_user"].user_id_hash).all(), f"Missing user: {table}")
    touches = f["fact_touch"].set_index("touch_id")
    leads = f["fact_lead"].set_index("lead_id")
    orders = f["fact_order"].sort_values(["user_id_hash", "timestamp"]).copy()
    require(f["fact_touch"].placement_id.dropna().isin(f["dim_placement"].placement_id).all(), "Missing placement")
    for table, time in (("fact_touch", "timestamp"), ("fact_bot_event", "timestamp"),
                        ("fact_lead", "conversation_started_at"), ("fact_order", "timestamp")):
        require(((f[table][time] >= sim["start"]) & (f[table][time] < sim["end"])).all(), "Event outside observation")
    for table, time in (("fact_bot_event", "timestamp"), ("fact_lead", "conversation_started_at")):
        for r in f[table].itertuples():
            if pd.notna(r.touch_id):
                require(r.touch_id in touches.index, "Missing touch")
                t = touches.loc[r.touch_id]
                require(t.user_id_hash == r.user_id_hash and t.timestamp <= getattr(r, time), "Invalid touch chronology/user")
    for r in orders.itertuples():
        require(r.lead_id in leads.index, "Missing lead")
        lead = leads.loc[r.lead_id]
        require(lead.user_id_hash == r.user_id_hash and lead.conversation_started_at <= r.timestamp, "Invalid lead chronology/user")
    require(f["fact_order_item"].order_id.isin(orders.order_id).all(), "Orphan order item")
    require(set(f["order_truth"].order_id) == set(orders.order_id), "Missing mechanism labels")
    item_sums = f["fact_order_item"].groupby("order_id").amount.sum().reindex(orders.order_id).to_numpy(float)
    require(np.allclose(item_sums, orders.amount.to_numpy(float)), "Order item sum mismatch")
    previous = orders.groupby("user_id_hash").timestamp.shift()
    require(((orders.timestamp-previous).dt.total_seconds().dropna()/86400 > config.min_repeat_days).all(), "Repeat too early")
    require(orders.groupby("user_id_hash").size().le(config.max_orders).all(), "Too many purchases")
    previous_by_order = dict(zip(orders.order_id, previous))
    order_index = orders.set_index("order_id")
    for r in f["order_truth"].itertuples():
        if r.mechanism == "organic":
            require(pd.isna(r.driver_placement_id) and pd.isna(r.driver_touch_id), "Organic has driver")
        else:
            require(r.mechanism == "ad_tagged" and r.driver_touch_id in touches.index, "Missing advertising driver")
            touch = touches.loc[r.driver_touch_id]
            order = order_index.loc[r.order_id]
            require(touch.user_id_hash == order.user_id_hash and touch.placement_id == r.driver_placement_id, "Wrong driver")
            require(r.timestamp-pd.Timedelta(days=config.attribution_days) <= touch.timestamp <= r.timestamp, "Driver outside window")
            prev = previous_by_order[r.order_id]
            require(pd.isna(prev) or touch.timestamp > prev, "Driver reused after previous purchase")
    for model in attribution.MODELS:
        att = allocate(sim, model)
        grouped = att.groupby("order_id")
        weights = grouped.weight.sum().reindex(orders.order_id).to_numpy(float)
        money = grouped.attributed_revenue.sum().reindex(orders.order_id).to_numpy(float)
        require(np.allclose(weights, 1), f"Weights do not sum to one: {model}")
        require(np.allclose(money, orders.amount.to_numpy(float)), f"Revenue lost: {model}")
    return {"users": len(f["dim_user"]), "touches": len(touches), "orders": len(orders),
            "revenue": float(orders.amount.sum()), "checks": "passed"}


def evaluate_horizons(sim, as_of=None):
    """D7/D14/D30 from publication; keep pre-cutoff orders for repeat reset.

    Filter allocation results AFTER computing with full prior history. Do not
    renormalize weights when cutting off a placement's maturity horizon.
    """
    cutoff = min(pd.Timestamp(as_of), sim["end"]) if as_of is not None else sim["end"]
    frames = sim["frames"]
    visible = dict(sim, frames=dict(frames, fact_order=frames["fact_order"][frames["fact_order"].timestamp < cutoff]))
    orders = frames["fact_order"][["order_id", "timestamp"]]
    allocated = {m: allocate(visible, m).merge(orders, on="order_id") for m in attribution.MODELS}
    labels = frames["order_truth"]
    results = []
    for p in sim["placements"].itertuples():
        age = max(0., (cutoff-p.publication_time).total_seconds()/86400)
        for horizon in sim["config"].horizons:
            end = p.publication_time + pd.Timedelta(days=horizon)
            mature = cutoff >= end
            tagged = labels[(labels.driver_placement_id == p.placement_id)
                            & (labels.timestamp >= p.publication_time) & (labels.timestamp < min(end, cutoff))]
            tagged_revenue = float(tagged.amount.sum()) if mature else np.nan
            for model, att in allocated.items():
                rows = att[(att.placement_id == p.placement_id) & (att.timestamp >= p.publication_time)
                           & (att.timestamp < min(end, cutoff))]
                revenue = float(rows.attributed_revenue.sum()) if mature else np.nan
                results.append(dict(placement_id=p.placement_id, model=model, horizon_days=horizon,
                    observed_days=min(age, horizon), mature=mature, cost=p.cost,
                    attributed_revenue=revenue, tagged_revenue=tagged_revenue,
                    attributed_net_revenue=revenue-p.cost,
                    tagged_net_revenue=tagged_revenue-p.cost,
                    attributed_romi_pct=100*(revenue/p.cost-1) if p.cost > 0 else np.nan,
                    tagged_romi_pct=100*(tagged_revenue/p.cost-1) if p.cost > 0 else np.nan))
    return pd.DataFrame(results)


def score_horizons(result):
    """Diagnostic discrepancy against generator tags, not causal accuracy."""
    rows = []
    for (horizon, model), group in result.groupby(["horizon_days", "model"]):
        g = group[group.mature].dropna(subset=["attributed_romi_pct", "tagged_romi_pct"])
        if g.empty:
            continue
        predicted, tagged = g.attributed_romi_pct, g.tagged_romi_pct
        # Report ties rather than choosing the first DataFrame row arbitrarily.
        pred_best = set(g.loc[predicted == predicted.max(), "placement_id"])
        tag_best = set(g.loc[tagged == tagged.max(), "placement_id"])
        rows.append(dict(horizon_days=horizon, model=model, mature_placements=len(g),
            mae_pp=float((predicted-tagged).abs().mean()), bias_pp=float((predicted-tagged).mean()),
            rank_correlation=predicted.rank().corr(tagged.rank()) if predicted.nunique()>1 and tagged.nunique()>1 else np.nan,
            best_match_fraction=len(pred_best & tag_best)/len(pred_best),
            sign_accuracy=float(((predicted>0)==(tagged>0)).mean())))
    return pd.DataFrame(rows)


def run_many(history, placements, config):
    from dataclasses import replace
    rows = []
    for i in range(config.n_runs):
        cfg = replace(config, seed=config.seed+i)
        sim = simulate(history, placements, cfg)
        validate_simulation(sim)
        rows.append(score_horizons(evaluate_horizons(sim)).assign(seed=cfg.seed))
    return pd.concat(rows, ignore_index=True)


def export_database(sim, output_root):
    """New isolated directory each run; never overwrite a working/demo database."""
    validate_simulation(sim)
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix=f"seed{sim['config'].seed}_", dir=root))
    path = folder / "synthetic.db"
    conn = db.connect(str(path))
    try:
        for name in ("dim_placement", "dim_user", "fact_touch", "fact_bot_event", "fact_lead", "fact_order", "fact_order_item"):
            frame = sim["frames"][name].copy()
            for column in frame:
                if pd.api.types.is_datetime64_any_dtype(frame[column]):
                    frame[column] = frame[column].dt.strftime("%Y-%m-%d %H:%M:%S")
            frame.to_sql(name, conn, index=False, if_exists="append")
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("Export broke foreign keys")
        attribution.rebuild(conn, sim["config"].attribution_days)
        for model in attribution.MODELS:
            attribution.report(conn, model, sim["config"].attribution_days, persist=False)
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Invalid export")
    finally:
        conn.close()
    manifest = dict(config=asdict(sim["config"]), observation_start=str(sim["start"]),
                    observation_end_exclusive=str(sim["end"]),
                    demand_note="Historical sales shape, flat mean beyond history; not a natural-demand estimate",
                    truth_note="Mechanism labels; not counterfactual incremental revenue",
                    placements=sim["placements"].to_dict("records"))
    (folder / "manifest.json").write_text(json.dumps(manifest, default=str, ensure_ascii=False, indent=2))
    evaluate_horizons(sim).to_csv(folder / "placement_horizons.csv", index=False)
    return folder
