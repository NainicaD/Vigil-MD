"""Tests for the matching data monitor (vigil_model/data_drift.py, scripts/m01_data_drift.py).
Offline: no GCP; the query is checked as text, the comparison on synthetic profiles."""
import datetime as dt
import importlib.util
import json
from pathlib import Path

import pytest

from vigil_model import bq_utils, data_drift as dd

ROOT = Path(__file__).resolve().parents[1]
LIMITS = json.loads((ROOT / "monitor" / "thresholds.json").read_text())

spec = importlib.util.spec_from_file_location("m01", ROOT / "scripts" / "m01_data_drift.py")
m01 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m01)


def row(n=100_000, n_udi=62_000, linked=40_000, no_model=8_000, codes=None, mfrs=None):
    codes = codes or {"DZE": 30_000, "FRN": 20_000, "LZG": 10_000, "MKJ": 40_000}
    mfrs = mfrs or {"medtronic": 50_000, "abbott": 30_000, "philips": 20_000}
    return {"totals": {"n": n, "n_udi": n_udi, "n_udi_in_gudid": linked, "n_no_model": no_model,
                       "n_no_catalog": 20_000, "n_no_manufacturer": 100, "n_no_product_code": 50},
            "product_codes": [{"k": k, "n": v} for k, v in codes.items()],
            "manufacturers": [{"k": k, "n": v} for k, v in mfrs.items()]}


BASE = dd.profile(row())


def test_month_start_and_windows():
    assert dd.month_start(dt.date(2026, 11, 7), 12) == dt.date(2025, 11, 1)
    assert dd.month_start(dt.date(2026, 3, 1), 8) == dt.date(2025, 7, 1)
    b = dd.baseline_window(dt.date(2026, 11, 7))
    assert "_part_date >= DATE '2025-11-01'" in b and "_part_date < DATE '2026-11-01'" in b
    assert "first_loaded_at <= TIMESTAMP '2026-11-07 23:59:59+00'" in b
    c = dd.current_window(dt.date(2026, 11, 7), dt.date(2026, 11, 19), 8)
    assert "_part_date >= DATE '2026-03-01'" in c and "first_loaded_at > TIMESTAMP '2026-11-07" in c


def test_profile_rates():
    p = BASE
    assert p["n"] == 100_000 and p["rates"]["udi_rate"] == 0.62
    assert p["rates"]["udi_link_rate"] == round(40_000 / 62_000, 6)     # of the rows with a UDI
    assert p["counts"]["product_codes"]["MKJ"] == 40_000
    empty = dd.profile({"totals": {k: 0 for k in row()["totals"]}})
    assert empty["rates"]["udi_rate"] is None and empty["counts"]["manufacturers"] == {}


def test_same_shape_is_ok():
    now = dd.profile(row(n=10_000, n_udi=6_200, linked=4_000, no_model=800,
                         codes={"DZE": 3_000, "FRN": 2_000, "LZG": 1_000, "MKJ": 4_000},
                         mfrs={"medtronic": 5_000, "abbott": 3_000, "philips": 2_000}))
    now["rates"]["no_catalog_rate"] = BASE["rates"]["no_catalog_rate"]
    report = dd.compare(BASE, now, LIMITS)
    assert report["result"] == "ok", report


def test_udi_drop_and_new_devices_are_drift():
    for kind, name in (("udi_drop", "udi_rate"), ("new_devices", "product_codes")):
        report = dd.compare(BASE, dd.simulate(BASE, kind), LIMITS)
        assert report["result"] == "drift"
        (c,) = [c for c in report["checks"] if c["name"] == name]
        assert c["level"] == "drift"
    assert dd.compare(BASE, dd.simulate(BASE, "none"), LIMITS)["result"] == "ok"
    with pytest.raises(ValueError):
        dd.simulate(BASE, "nope")


def test_too_few_new_rows_give_no_decision():
    small = dd.simulate(BASE, "udi_drop", n=LIMITS["min_rows"] - 1)
    assert dd.compare(BASE, small, LIMITS)["result"] == "insufficient_data"


def test_mix_psi_uses_the_baselines_top_keys_plus_other():
    shares = dd.mix_shares({"a": 50, "b": 30, "c": 20}, 100, ["a", "b"])
    assert shares == [0.5, 0.3, pytest.approx(0.2)]
    assert dd.mix_shares({}, 0, ["a"]) == []
    assert dd.psi([0.5, 0.5], [0.5, 0.5]) == 0.0 and dd.psi([0.5], []) is None


def test_the_profile_query_is_read_only_prod_and_pruned():
    sql = m01.build_sql("p", dd.current_window(dt.date(2026, 11, 7), dt.date(2026, 11, 19)))
    bq_utils.assert_read_only(sql)                       # one SELECT, vigil_md_prod only
    assert "`p.vigil_md_prod.maude`" in sql and "`p.vigil_md_prod.gudid`" in sql
    assert "_part_date >= DATE" in sql                   # partition pruning on the report month
    assert "{window}" not in sql


def test_check_with_a_simulation_and_exit_codes(tmp_path, capsys):
    base = tmp_path / "base.json"
    base.write_text(json.dumps({"snapshot_date": str(m01.snapshot_date()), "profile": BASE}))
    out, summ = tmp_path / "r.json", tmp_path / "s.md"
    code = m01.main(["check", "--simulate", "udi_drop", "--baseline", str(base),
                     "--out", str(out), "--summary", str(summ)])
    assert code == m01.EXIT_DRIFT
    assert json.loads(out.read_text())["simulated"] == "udi_drop"
    assert "DRIFT" in summ.read_text() and "simulated" in summ.read_text()
    assert m01.main(["check", "--simulate", "none", "--baseline", str(base)]) == m01.EXIT_OK


def test_a_baseline_for_another_label_snapshot_stops(tmp_path):
    base = tmp_path / "base.json"
    base.write_text(json.dumps({"snapshot_date": "2020-01-01", "profile": BASE}))
    assert m01.main(["check", "--simulate", "none", "--baseline", str(base)]) == m01.EXIT_STALE_BASELINE


def test_run_profile_refuses_above_the_cap():
    class Dry:
        def query(self, sql, job_config):
            import types
            assert job_config.dry_run, "only the dry run may run"
            return types.SimpleNamespace(total_bytes_processed=int(2e9))
    with pytest.raises(OverflowError):
        m01.run_profile(Dry(), "SELECT 1", max_gb=1.0)


def test_monitoring_workflow_triggers_retraining_only_on_drift():
    yaml = pytest.importorskip("yaml")
    path = ROOT.parents[1] / ".github" / "workflows" / "matching-monitoring.yml"
    wf = yaml.safe_load(path.read_text())
    assert wf[True]["schedule"][0]["cron"] and "workflow_dispatch" in wf[True]
    job = wf["jobs"]["data-drift"]
    assert job["permissions"]["actions"] == "write" and job["permissions"]["id-token"] == "write"
    trigger = next(s for s in job["steps"] if s.get("name", "").startswith("Start retraining"))
    assert trigger["if"] == "steps.drift.outputs.code == '3'"
    assert "gh workflow run model-training.yml --ref main -f refresh_data=true -f publish=true" \
        in trigger["run"]
    run = next(s for s in job["steps"] if s.get("id") == "drift")["run"]
    assert "m01_data_drift.py check" in run and "--max-gb 1" in run


def test_training_workflow_publishes_a_triggered_run_only_on_main():
    yaml = pytest.importorskip("yaml")
    wf = yaml.safe_load((ROOT.parents[1] / ".github" / "workflows" / "model-training.yml").read_text())
    inputs = wf[True]["workflow_dispatch"]["inputs"]
    assert inputs["publish"]["type"] == "boolean" and inputs["publish"]["default"] is False


def test_overall_result_takes_the_worse_of_new_rows_and_revisit():
    few = {"result": "insufficient_data"}
    assert dd.overall(few, None) == "insufficient_data"
    assert dd.overall(few, {"result": "ok"}) == "insufficient_data"
    assert dd.overall(few, {"result": "drift"}) == "drift"       # in-place change, few new rows
    assert dd.overall({"result": "ok"}, {"result": "warn"}) == "warn"
    assert dd.overall({"result": "drift"}, {"result": "ok"}) == "drift"


def test_check_revisits_the_baseline_rows_and_catches_in_place_changes(tmp_path, monkeypatch):
    """The 2026-11-17 case: few new rows, but a re-export blanked UDI-DIs of rows the model
    learned from. The revisit window (the baseline's own rows, as they are now) shows it."""
    base = tmp_path / "base.json"
    base.write_text(json.dumps({"snapshot_date": str(m01.snapshot_date()), "profile": BASE}))
    blanked = dd.profile(row(n_udi=50_000, linked=32_000))      # UDI rate 0.62 -> 0.50
    seen = []

    def fake_profile(client, sql, max_gb):
        seen.append(sql)
        p = dd.profile(row(n=228, n_udi=140, linked=90)) if "first_loaded_at >" in sql else blanked
        return p, {"estimated_gb": 0.2, "billed_gb": 0.2}
    monkeypatch.setattr(m01, "run_profile", fake_profile)
    import sys, types
    fake_bq = types.SimpleNamespace(Client=lambda project: object())
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", fake_bq)
    monkeypatch.setitem(sys.modules, "google.cloud", types.SimpleNamespace(bigquery=fake_bq))
    out, summ = tmp_path / "r.json", tmp_path / "s.md"
    code = m01.main(["check", "--project", "p", "--baseline", str(base),
                     "--out", str(out), "--summary", str(summ)])
    report = json.loads(out.read_text())
    assert len(seen) == 2                                    # new rows + revisit
    assert report["current_result"] == "insufficient_data"
    assert report["revisit"]["result"] == "drift" and report["result"] == "drift"
    assert code == m01.EXIT_DRIFT
    assert report["cost"]["billed_gb"] == 0.4
    text = summ.read_text()
    assert "baseline rows now" in text and "udi_rate" in text


def test_print_only_shows_both_windows(capsys):
    assert m01.main(["check", "--project", "p", "--print-only"]) == m01.EXIT_OK
    printed = capsys.readouterr().out
    assert "-- current" in printed and "-- revisit" in printed
    assert "first_loaded_at >" in printed and "first_loaded_at <=" in printed
