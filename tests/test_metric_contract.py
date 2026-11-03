import sqlite3

import pandas as pd
import pytest

from core import attribution, db


def sale(conn, placement, amount=100, rate=0):
    uid = "user-" + placement
    db.upsert_user(conn, uid)
    db.add_touch(conn, uid, placement, "2026-01-01")
    db.add_order(conn, uid, amount, ["a", "b", "c"], rate, ts="2026-01-02")


def test_unknown_cost_is_not_free_and_summary_is_incomplete(tmp_path):
    conn = db.connect(str(tmp_path / "test.db"))
    db.add_placement(conn, "unknown", "Unknown", None)
    db.add_placement(conn, "free", "Free", 0, placement_type="external")
    sale(conn, "unknown")
    sale(conn, "free")
    result = attribution.report(conn)
    assert pd.isna(result.loc["unknown", "cost"])
    assert pd.isna(result.loc["unknown", "romi"])
    assert result.loc["free", "cost_per_attributed_order"] == 0
    assert pd.isna(result.loc["free", "romi"])
    summary = attribution.summary(conn)
    assert not summary["cost_complete"]
    assert summary["cost"] is None and summary["romi"] is None
    assert summary["attributed"] == 200


def test_commission_unknown_and_canonical_metric_names(tmp_path):
    conn = db.connect(str(tmp_path / "test.db"))
    db.add_placement(conn, "a", "A", 10)
    sale(conn, "a", rate=.03)
    r = attribution.report(conn).loc["a"]
    assert r.attributed_romi_pct == 900
    assert r.cost_per_attributed_order == 10
    assert r.attributed_after_acquiring == 97
    assert r.romi_after_acquiring_pct == pytest.approx(870)
    # A second order without a commission estimate makes the full estimate unknown.
    db.add_order(conn, "user-a", 100, ["course"], ts="2026-01-03")
    # New touch is needed after the first purchase to credit this placement.
    db.add_touch(conn, "user-a", "a", "2026-01-02 12:00:00")
    assert pd.isna(attribution.report(conn).loc["a", "attributed_after_acquiring"])


def test_order_items_reconcile_to_cent(tmp_path):
    conn = db.connect(str(tmp_path / "test.db"))
    db.add_placement(conn, "a", "A", 10)
    sale(conn, "a", amount=100)
    assert conn.execute("SELECT SUM(amount) FROM fact_order_item").fetchone()[0] == pytest.approx(100)
    assert sorted(r[0] for r in conn.execute("SELECT amount FROM fact_order_item")) == [33.33, 33.33, 33.34]


def test_old_ambiguous_zero_cost_requires_confirmation(tmp_path):
    schema = db.SCHEMA.replace("    cost_known       INTEGER NOT NULL DEFAULT 0,  -- 1: явно известна, включая бесплатную\n", "")
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as c:
        c.executescript(schema)
        for pid, kind, cost in [("paid", "external", 100), ("own", "own", 0), ("zero", "external", 0), ("missing", "unknown", 0)]:
            c.execute("INSERT INTO dim_placement(placement_id,channel,placement_type,cost) VALUES (?,?,?,?)", (pid,pid,kind,cost))
    conn = db.connect(str(path))
    known = dict(conn.execute("SELECT placement_id,cost_known FROM dim_placement"))
    assert known == {"paid": 1, "own": 1, "zero": 0, "missing": 0}


def test_model_agreement_honors_minimum_orders(tmp_path):
    conn = db.connect(str(tmp_path / "test.db"))
    db.add_placement(conn, "a", "A", 10)
    sale(conn, "a")
    assert attribution.stable_winners(conn, min_orders=1)[0] == ["a"]
    assert attribution.stable_winners(conn, min_orders=2) == ([], [])
