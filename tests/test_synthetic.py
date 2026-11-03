from dataclasses import replace
import json
from pathlib import Path
import sqlite3

import numpy as np
import pandas as pd
import pytest

from core import attribution
from experiments.synthetic import (Config, allocate, build_placements, eligible_touches,
    evaluate_horizons, export_database, load_history, run_many, simulate, validate_simulation)


@pytest.fixture
def history():
    return {"orders": pd.DataFrame({"amount": [100., 200., 300.]}),
            "demand": pd.Series([1., 2., 1., .5], index=pd.date_range("2026-08-01", periods=4)),
            "repeat_rate": .1}


def test_reproducibility_chronology_and_revenue(history):
    config = Config(n_users=40, max_orders=4, base_daily_purchase=.1)
    placements = build_placements(history["demand"].index.min())
    first, second = [simulate(history, placements, config) for _ in range(2)]
    for name in first["frames"]:
        pd.testing.assert_frame_equal(first["frames"][name], second["frames"][name])
    assert validate_simulation(first)["checks"] == "passed"
    assert evaluate_horizons(first).mature.all()
    assert first["end"] >= placements.publication_time.max()+pd.Timedelta(days=30)


def test_purchases_without_touches_stay_organic(history):
    p = build_placements(history["demand"].index.min()).assign(reach_probability=0, daily_lift=0)
    sim = simulate(history, p, Config(n_users=15, organic_visit_probability=0, base_daily_purchase=.2))
    assert sim["frames"]["fact_touch"].empty
    assert not sim["frames"]["fact_order"].empty
    validate_simulation(sim)
    for model in attribution.MODELS:
        result = allocate(sim, model)
        assert result.placement_id.isna().all()
        assert result.attributed_revenue.sum() == pytest.approx(sim["frames"]["fact_order"].amount.sum())


def test_zero_sales_and_touches_supported(history):
    p = build_placements(history["demand"].index.min()).assign(reach_probability=0, daily_lift=0)
    sim = simulate(history, p, Config(n_users=5, organic_visit_probability=0, base_daily_purchase=0))
    validate_simulation(sim)
    result = evaluate_horizons(sim)
    assert (result.attributed_romi_pct == -100).all()


def test_no_future_touch_and_no_reuse_after_purchase():
    day = pd.Timestamp("2026-08-01")
    touches = [dict(touch_id=1, placement_id="a", timestamp=day+pd.Timedelta(hours=16))]
    assert eligible_touches(touches, day+pd.Timedelta(hours=10), None, 30) == []
    assert eligible_touches(touches, day+pd.Timedelta(hours=18), day+pd.Timedelta(hours=17), 30) == []
    assert eligible_touches(touches, day+pd.Timedelta(hours=18), None, 30) == touches


def horizon_example():
    start = pd.Timestamp("2026-08-01")
    placements = pd.DataFrame([dict(placement_id="a", cost=10., publication_time=start),
                               dict(placement_id="b", cost=20., publication_time=start+pd.Timedelta(days=3))])
    touches = pd.DataFrame([dict(touch_id=1, placement_id="a", user_id_hash="u", timestamp=start+pd.Timedelta(days=1)),
                            dict(touch_id=2, placement_id="b", user_id_hash="u", timestamp=start+pd.Timedelta(days=4))])
    orders = pd.DataFrame([dict(order_id="o", user_id_hash="u", amount=100., variable_costs=0., timestamp=start+pd.Timedelta(days=8))])
    truth = pd.DataFrame([dict(order_id="o", driver_placement_id="b", amount=100., timestamp=start+pd.Timedelta(days=8))])
    return dict(placements=placements, start=start, end=start+pd.Timedelta(days=40), config=Config(),
                frames={"fact_touch": touches, "fact_order": orders, "order_truth": truth})


def test_equal_age_cutoff_does_not_redistribute_revenue():
    result = evaluate_horizons(horizon_example())
    d7 = result[(result.horizon_days == 7) & (result.model == "linear")].set_index("placement_id")
    assert d7.loc["a", "attributed_revenue"] == 0  # order is after A's D7
    assert d7.loc["b", "attributed_revenue"] == 50  # B does not receive A's excluded share
    assert d7.loc["b", "tagged_revenue"] == 100
    assert d7.loc["b", "attributed_romi_pct"] == 150


def test_young_posts_not_ranked_and_future_orders_invisible():
    sim = horizon_example()
    result = evaluate_horizons(sim, as_of=sim["start"]+pd.Timedelta(days=7))
    d7 = result[(result.horizon_days == 7) & (result.model == "last")].set_index("placement_id")
    assert d7.loc["a", "mature"]
    assert d7.loc["a", "attributed_revenue"] == 0
    assert not d7.loc["b", "mature"]
    assert np.isnan(d7.loc["b", "attributed_revenue"])
    assert np.isnan(d7.loc["b", "tagged_romi_pct"])


def test_validation_catches_broken_links_and_lost_revenue(history):
    config = Config(n_users=10, base_daily_purchase=.2)
    sim = simulate(history, build_placements(history["demand"].index.min()), config)
    sim["frames"]["fact_order"].loc[0, "lead_id"] = -999
    with pytest.raises(ValueError, match="Missing lead"):
        validate_simulation(sim)
    sim = simulate(history, build_placements(history["demand"].index.min()), config)
    sim["frames"]["fact_order_item"].loc[0, "amount"] = 0
    with pytest.raises(ValueError, match="sum mismatch"):
        validate_simulation(sim)


def test_export_isolated_and_bot_reports_work(history, tmp_path):
    sim = simulate(history, build_placements(history["demand"].index.min()), Config(n_users=20))
    folder = export_database(sim, tmp_path)
    another = export_database(sim, tmp_path)
    assert another != folder
    with sqlite3.connect(folder / "synthetic.db") as conn:
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
        for model in attribution.MODELS:
            assert not attribution.report(conn, model).empty
        assert conn.execute("SELECT COUNT(*) FROM telegram_inbox").fetchone()[0] == 0
    assert json.loads((folder / "manifest.json").read_text())["config"]["seed"] == 42


def test_many_runs_use_same_generator_and_config(history):
    config = Config(n_users=5, n_runs=2, seed=91, base_daily_purchase=0)
    p = build_placements(history["demand"].index.min()).assign(daily_lift=0)
    scores = run_many(history, p, config)
    assert set(scores.seed) == {91, 92}
    assert set(scores.model) == set(attribution.MODELS)
    assert (scores.mae_pp == 0).all()


def test_real_history_and_no_fallback():
    root = Path(__file__).resolve().parents[1]
    history = load_history(root / "data/raw/base.csv")
    assert len(history["orders"]) == 628
    with pytest.raises(FileNotFoundError):
        load_history(root / "does-not-exist.csv")
