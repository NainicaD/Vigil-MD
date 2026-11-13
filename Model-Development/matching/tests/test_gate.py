"""Tests for the model gate (ci/gate.py), publishing (ci/publish.py) and the IPW weights
(vigil_model/ipw.py). Offline: no GCP call, no training run; gcloud and git are faked."""
import json
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import ci.gate as gate
import ci.publish as pub
from vigil_model import ipw

ROOT = Path(__file__).resolve().parents[1]
RULES = json.loads((ROOT / "ci/gate.json").read_text())


def _hand(precision=0.96, recall=1.0, top10_precision=0.9, top10_accepted=19, ie_gate=True):
    m = {"all_precision": precision, "all_recall": recall, "strict_precision": 1.0,
         "ie_accept_rate": 0.29, "non_match_accept_rate": 0.04}
    for col, slices in {"strat_model_usable": {"TRUE": (0.95, 54), "FALSE": (1.0, 20)},
                        "strat_mfr_size": {"top_10": (top10_precision, top10_accepted), "11_100": (1.0, 34),
                                           "smaller": (1.0, 3)}}.items():
        for value, (p, acc) in slices.items():
            m.update({f"slice_{col}_{value}_precision": p, f"slice_{col}_{value}_recall": 1.0,
                      f"slice_{col}_{value}_accepted": acc, f"slice_{col}_{value}_matches": acc,
                      f"slice_{col}_{value}_n": acc + 10})
    return {"model_name": "xgb_no_match_kind", "cutoff": 0.6, "ie_gate": ie_gate, "metrics": m}


def _scores_dir(base, **kw):
    d = base / "scores"
    d.mkdir(parents=True, exist_ok=True)
    (d / "xgb_no_match_kind_hand.json").write_text(json.dumps(_hand(**kw)))
    (d / "train_metrics.json").write_text(json.dumps(
        {"xgb_no_match_kind": {"test_precision": 0.9925, "test_recall": 0.965, "cutoff_p99": 0.6}}))
    return d


@pytest.fixture
def champion(tmp_path):
    d = _scores_dir(tmp_path / "champ")
    path = tmp_path / "champion.json"
    assert gate.main(["--scores-dir", str(d), "--write-champion", "--champion", str(path)]) == 0
    return path


def _run(tmp_path, champion, **kw):
    d = _scores_dir(tmp_path, **kw)
    code = gate.main(["--scores-dir", str(d), "--champion", str(champion)])
    return code, json.loads((d / "gate.json").read_text())


# ---------------------------------------------------------------------------- gate

def test_same_model_passes_and_writes_its_reports(tmp_path, champion):
    code, result = _run(tmp_path, champion)
    assert code == 0 and result["passed"]
    assert (tmp_path / "scores/bias_report.md").read_text().startswith("# Bias report")


def test_a_broken_floor_fails(tmp_path, champion):
    code, result = _run(tmp_path, champion, recall=0.95)
    assert code == 1 and not result["passed"]
    assert any(c["name"].startswith("all_recall >=") and c["status"] == "FAIL" for c in result["checks"])


def test_worse_than_champion_beyond_tolerance_fails(tmp_path, champion):
    code, result = _run(tmp_path, champion, precision=0.90)
    failed = [c["name"] for c in result["checks"] if c["status"] == "FAIL"]
    assert code == 1 and failed == ["all_precision not worse by > 0.04"]


def test_bias_gap_over_the_ceiling_fails(tmp_path, champion):
    code, result = _run(tmp_path, champion, top10_precision=0.80)
    assert code == 1
    failed = [c["name"] for c in result["checks"] if c["status"] == "FAIL"]
    assert failed == ["bias_max_gap_precision <= 0.15"]
    assert result["metrics"]["bias_max_gap_precision"] == pytest.approx(0.20)


def test_ceiling_the_champion_already_breaks_only_warns_if_not_worse(tmp_path):
    champ_dir = _scores_dir(tmp_path / "champ", top10_precision=0.80)
    champ = tmp_path / "champion.json"
    gate.main(["--scores-dir", str(champ_dir), "--write-champion", "--champion", str(champ)])
    code, result = _run(tmp_path, champ, top10_precision=0.82)          # gap 0.18, champion 0.20
    assert code == 0
    assert [c["status"] for c in result["checks"] if c["name"].startswith("bias_max_gap_precision")] == ["WARN"]
    code, _ = _run(tmp_path, champ, top10_precision=0.78)               # wider than the champion
    assert code == 1


def test_slices_under_min_n_are_left_out_of_the_gap():
    sl = gate.slices(_hand(top10_precision=0.5, top10_accepted=gate.MIN_N - 1)["metrics"])
    assert gate.bias_gaps(sl)["bias_max_gap_precision"] == pytest.approx(0.05)   # only TRUE vs FALSE


def test_different_ie_gate_fails(tmp_path, champion):
    code, _ = _run(tmp_path, champion, ie_gate=False)
    assert code == 1


def test_report_only_never_fails_and_missing_input_is_exit_2(tmp_path, champion):
    d = _scores_dir(tmp_path, recall=0.5)
    assert gate.main(["--scores-dir", str(d), "--champion", str(champion), "--report-only"]) == 0
    assert gate.main(["--scores-dir", str(tmp_path / "nowhere"), "--champion", str(champion)]) == 2


def test_committed_champion_matches_the_released_model():
    champ = json.loads((ROOT / "ci/champion_metrics.json").read_text())
    m = champ["metrics"]
    assert champ["ie_gate"] is True and champ["model_name"] == "xgb_no_match_kind"
    assert champ["ie_gate_version"] == "e6_e3b_c1"                     # release v1.1.0
    assert m["all_precision"] == 1.0 and m["all_false_pos"] == 0 and m["all_recall"] == 1.0
    assert m["ie_accepted"] == 14 and m["test_precision"] == pytest.approx(0.9925, abs=1e-4)


def test_committed_champion_passes_the_committed_rules():
    champ = json.loads((ROOT / "ci/champion_metrics.json").read_text())
    passed, checks = gate.decide(champ, champ, RULES)
    assert passed, [c for c in checks if c[1] == "FAIL"]


# ---------------------------------------------------------------------------- publish

class FakeRun:
    """Records gcloud/git calls; `describe` returns `exists`, `list` returns `versions`, and `download --name f`
    writes published[f] (the newest version's manifest.json / meta.json) to the destination."""

    def __init__(self, exists=False, versions="", published=None):
        self.calls, self.exists, self.versions, self.published = [], exists, versions, published or {}

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        if "describe" in cmd:
            return subprocess.CompletedProcess(cmd, 0 if self.exists else 1, "", "")
        if "download" in cmd:
            name, dest = cmd[cmd.index("--name") + 1], cmd[cmd.index("--destination") + 1]
            (Path(dest) / name).write_text(json.dumps(self.published[name]))
        return subprocess.CompletedProcess(cmd, 0, self.versions, "")


def _bundle(path, model="m1", tfidf="t1", cutoff=0.6, ie_gate="e6_e3b_c1"):
    """manifest.json + meta.json of a bundle; returns them as published{} for FakeRun."""
    files = {"manifest.json": {"sha256": {"model.json": model, "tfidf.pkl": tfidf, "meta.json": "differs-per-run"}},
             "meta.json": {"cutoff": cutoff, "ie_gate": ie_gate, "git_commit_at_packaging": "abc"}}
    for name, content in files.items():
        (path / name).write_text(json.dumps(content))
    return files


LATEST = "projects/p/locations/l/repositories/r/packages/matching-same-device/versions/v1.1.0"


def _gate_file(tmp_path, passed=True, report_only=False):
    p = tmp_path / "gate.json"
    p.write_text(json.dumps({"passed": passed, "report_only": report_only,
                             "checks": [{"name": "all_recall >= 0.99", "status": "PASS" if passed else "FAIL"}]}))
    return p


def _publish(tmp_path, run, monkeypatch, dirty=(), **gate_kw):
    monkeypatch.setattr(pub, "dirty_files", lambda root: list(dirty))
    return pub.publish(tmp_path, "v1.0.1", "matching-same-device", {"mlflow": "gh-run-7", "git": "git-abc1234"},
                       gate_json=_gate_file(tmp_path, **gate_kw), root=tmp_path, print_only=False, run=run)


def test_publish_uploads_then_annotates(tmp_path, monkeypatch):
    _bundle(tmp_path)
    run = FakeRun()                                                             # empty registry
    assert _publish(tmp_path, run, monkeypatch) == 0
    assert [c[3] for c in run.calls] == ["describe", "list", "upload", "update"]
    assert "--annotations=mlflow=gh-run-7,git=git-abc1234" in run.calls[-1]


def test_publish_skips_a_bundle_that_decides_like_the_newest_version(tmp_path, monkeypatch):
    published = _bundle(tmp_path)              # same model.json, tfidf.pkl, cutoff and gate as v1.1.0
    run = FakeRun(versions=LATEST, published=published)
    assert _publish(tmp_path, run, monkeypatch) == 0
    assert not any("upload" in c for c in run.calls)
    assert all("v1.1.0" in c for c in run.calls if "download" in c)


@pytest.mark.parametrize("change", [{"model": "m2"}, {"tfidf": "t2"}, {"cutoff": 0.7}, {"ie_gate": "e6_e3b"}])
def test_publish_uploads_when_anything_that_decides_differs(tmp_path, monkeypatch, change):
    published = _bundle(tmp_path, **change)    # what v1.1.0 holds
    _bundle(tmp_path)                          # the new bundle
    run = FakeRun(versions=LATEST, published=published)
    assert _publish(tmp_path, run, monkeypatch) == 0
    assert any("upload" in c for c in run.calls)


@pytest.mark.parametrize("gate_kw", [{"passed": False}, {"report_only": True}])
def test_publish_refuses_when_the_gate_did_not_pass(tmp_path, monkeypatch, gate_kw):
    run = FakeRun()
    assert _publish(tmp_path, run, monkeypatch, **gate_kw) == 1
    assert run.calls == []


def test_publish_refuses_a_dirty_tree(tmp_path, monkeypatch):
    run = FakeRun()
    assert _publish(tmp_path, run, monkeypatch, dirty=[" M vigil_model/predict.py"]) == 2
    assert run.calls == []


def test_publish_skips_an_existing_version(tmp_path, monkeypatch):
    run = FakeRun(exists=True)
    assert _publish(tmp_path, run, monkeypatch) == 0
    assert len(run.calls) == 1 and "describe" in run.calls[0]


def test_print_only_runs_nothing(tmp_path, capsys):
    run = FakeRun()
    assert pub.publish(tmp_path, "v1.0.1", "p", {"mlflow": "m", "git": "g"}, gate_json=tmp_path / "none.json",
                       root=tmp_path, print_only=True, run=run) == 0
    assert run.calls == [] and "gcloud artifacts generic upload" in capsys.readouterr().out


def test_next_patch_bumps_the_newest_version():
    names = "\n".join(f"projects/p/locations/l/repositories/r/packages/m/versions/{v}"
                      for v in ("v1.0.0", "v1.2.3", "v1.10.0", "not-semver"))
    assert pub.next_patch("m", run=FakeRun(versions=names)) == "1.10.1"
    with pytest.raises(SystemExit):
        pub.next_patch("m", run=FakeRun(versions=""))


# ---------------------------------------------------------------------------- IPW weights

def test_every_training_tier_and_year_gets_a_report_weight():
    cells = ipw.cell_weights()
    df = pd.DataFrame({"meta_mfr_tier": ["top_10", "11_100", "long_tail", "mfr_missing", "smaller"] * 2,
                       "meta_year": [2016, 2019, 2022, 2025, 2026, 2017, 2020, 2023, 2024, 2018]})
    for tier, year in zip(df["meta_mfr_tier"], df["meta_year"]):
        assert (ipw.TIER_ALIASES.get(tier, tier), ipw.year_group(year)) in cells, (tier, year)
    w = ipw.row_weights(df)
    assert w.mean() == pytest.approx(1.0) and (w > 0).all() and len(np.unique(w)) > 1


def test_unknown_cells_and_missing_columns_get_weight_one():
    assert ipw.year_group(2015) == ipw.year_group(None) == "unknown"
    w = ipw.row_weights(pd.DataFrame({"meta_mfr_tier": ["nowhere", "nowhere"], "meta_year": [1990, 1991]}))
    assert list(w) == [1.0, 1.0]
    assert list(ipw.row_weights(pd.DataFrame({"x": [1, 2, 3]}))) == [1.0, 1.0, 1.0]
