"""Unit tests for scripts/analysis/bias_mitigation.py. No GCP calls."""
import csv

import pytest

from scripts.analysis import bias_mitigation as bm
from scripts.analysis.bias_slices import assert_read_only


def cell(et="Malfunction", tier="top_10", yg="2021_2023", flag="N", pc="AAA", n=100, l=50):
    return {"event_type": et, "manufacturer_tier": tier, "year_group": yg,
            "adverse_event_flag": flag, "product_code": pc, "n_rows": n, "n_linked": l}


# ── SQL ──
def test_cells_sql_is_read_only_and_uses_the_branch_rules():
    sql = bm.render_cells_sql("p")
    assert_read_only(sql)
    assert "SELECT DISTINCT udi_di FROM `p.vigil_md_prod.gudid`" in sql      # exact_linking rule
    assert "LEFT JOIN gudid_di AS g ON t.udi_di = g.udi_di" in sql           # no TRIM/UPPER
    assert "'top_10'" in sql and "'11_100'" in sql and "'long_tail'" in sql  # tiers from bias_slices
    for g in ("2016_2017", "2018_2020", "2021_2023", "2024_2026", "unknown"):
        assert f"'{g}'" in sql
    assert "GROUP BY 1, 2, 3, 4, 5" in sql


# ── Weights ──
def test_weights_restore_the_weighting_variable_exactly():
    cells = [cell(et="Malfunction", n=100, l=50), cell(et="Death", n=100, l=10)]
    w, used = bm.cell_weights(cells, bm.VERSIONS["event_type"], min_linked=1)
    assert w[1] / w[0] == pytest.approx(5.0)                  # (100/10) / (100/50)
    assert bm.shares(cells, "event_type", w) == pytest.approx(bm.shares(cells, "event_type"))
    assert used == {"event_type": 2}


def test_weights_are_normalised_to_the_population():
    cells = [cell(n=100, l=50), cell(et="Death", n=300, l=30)]
    w, _ = bm.cell_weights(cells, bm.VERSIONS["full"], min_linked=1)
    assert sum(c["n_linked"] * x for c, x in zip(cells, w)) == pytest.approx(400)


def test_small_cell_falls_back_to_a_coarser_level():
    cells = [cell(yg="2021_2023", n=100, l=50), cell(yg="2016_2017", n=100, l=5)]
    w, used = bm.cell_weights(cells, bm.VERSIONS["full"], min_linked=30)
    assert used == {"event_type+manufacturer_tier+year_group": 1, "event_type+manufacturer_tier": 1}
    assert w[1] / w[0] == pytest.approx((200 / 55) / (100 / 50))


def test_cell_with_no_links_gets_zero_weight():
    cells = [cell(et="Malfunction", n=100, l=50), cell(et="(null)", n=10, l=0)]
    w, _ = bm.cell_weights(cells, [("event_type",)], min_linked=1)
    assert w[1] == 0.0


def test_unweighted_is_all_ones():
    w, used = bm.cell_weights([cell(), cell()], None)
    assert w == [1.0, 1.0] and used == {"unweighted": 2}


def test_trimming_caps_the_largest_weight():
    cells = [cell(n=100, l=90), cell(et="Death", n=100, l=1)]
    w, _ = bm.cell_weights(cells, [("event_type",)], min_linked=1)
    t = bm.trim_weights(cells, w, mult=2.0)
    mean_t = sum(c["n_linked"] * x for c, x in zip(cells, t)) / 91
    assert max(t) / mean_t < max(w) / (sum(c["n_linked"] * x for c, x in zip(cells, w)) / 91)


# ── Evaluation ──
def test_tvd():
    assert bm.tvd({"a": 0.5, "b": 0.5}, {"a": 0.5, "b": 0.5}) == 0
    assert bm.tvd({"a": 1.0}, {"b": 1.0}) == 1
    assert bm.tvd({"a": 0.6, "b": 0.4}, {"a": 0.5, "b": 0.5}) == pytest.approx(0.1)


def test_kish_ess_equal_and_unequal_weights():
    cells = [cell(l=50), cell(et="Death", l=50)]
    assert bm.kish_ess(cells, [1.0, 1.0]) == pytest.approx(100)
    assert bm.kish_ess(cells, [1.0, 3.0]) < 100


def test_held_out_variable_improves_when_correlated():
    # Death reports link rarely and are mostly flagged Y: weighting on event_type
    # should move the held-out adverse_event_flag toward the population.
    cells = [cell(et="Malfunction", flag="N", n=900, l=600),
             cell(et="Death", flag="Y", n=100, l=10)]
    summary, rows = bm.analyse(cells, min_linked=1, with_fairlearn=False)
    before = summary["versions"]["unweighted"]["tvd"]["adverse_event_flag"]
    after = summary["versions"]["event_type"]["tvd"]["adverse_event_flag"]
    assert after < before
    assert {r["role"] for r in rows} == {"weighting", "held_out"}


def test_analyse_reports_all_four_versions():
    summary, _ = bm.analyse([cell(), cell(et="Death", l=10)], min_linked=1, with_fairlearn=False)
    assert set(summary["versions"]) == {"unweighted", "event_type", "full", "full_trimmed"}
    assert summary["maude_rows"] == 200 and summary["linked_rows"] == 60


# ── Fairlearn ──
def test_fairlearn_rates_are_exact():
    pytest.importorskip("fairlearn")
    cells = [cell(et="Malfunction", n=100, l=50), cell(et="Death", n=100, l=10),
             cell(et="Death", tier="long_tail", n=100, l=30)]
    res = bm.fairlearn_link_rates(cells, "event_type")
    assert res["by_group"]["Malfunction"] == pytest.approx(0.5)
    assert res["by_group"]["Death"] == pytest.approx(40 / 200)
    assert res["overall"] == pytest.approx(90 / 300)
    assert res["difference"] == pytest.approx(0.3)


def test_cross_check_against_link_coverage(tmp_path):
    p = tmp_path / "link_coverage.csv"
    with open(p, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["metric", "slice_name", "slice_value", "rate"])
        w.writeheader()
        w.writerow({"metric": "link_maude_to_gudid", "slice_name": "event_type",
                    "slice_value": "Death", "rate": "0.2"})
        w.writerow({"metric": "other", "slice_name": "event_type", "slice_value": "Death", "rate": "0.9"})
    res = bm.cross_check({"event_type": {"by_group": {"Death": 0.2001}}}, p)
    assert res["status"] == "ok" and res["values_compared"] == 1
    assert res["max_abs_gap"]["event_type"] == pytest.approx(0.0001)
    assert bm.cross_check({}, tmp_path / "missing.csv")["status"].startswith("skipped")


def test_unrepresentable_share():
    cells = [cell(et="Malfunction", n=90, l=50), cell(et="(null)", n=10, l=0)]
    u = bm.unrepresentable(cells)
    assert u["event_type"] == pytest.approx(0.10) and u["manufacturer_tier"] == 0
