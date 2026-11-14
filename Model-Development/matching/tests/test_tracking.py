import hashlib
import json

import pytest

from vigil_model.tracking import lineage_tags, tracking_uri


def test_tracking_uri_prefers_env(monkeypatch):
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "sqlite:///elsewhere.db")
    assert tracking_uri() == "sqlite:///elsewhere.db"
    monkeypatch.delenv("MLFLOW_TRACKING_URI")
    assert tracking_uri().startswith("sqlite:///") and tracking_uri().endswith("mlflow.db")


def test_lineage_tags_from_manifest(tmp_path, monkeypatch):
    monkeypatch.delenv("VIGIL_EXP", raising=False)
    m = tmp_path / "label_set.manifest.json"
    m.write_text(json.dumps({"caps_and_ratios": {"easy_anchor_pct": 25, "neg_per_anchor": 2}}))
    tags = lineage_tags(m, "E1-hard-neg")
    assert tags["exp"] == "E1-hard-neg"
    assert tags["label_set_manifest_sha256"] == hashlib.sha256(m.read_bytes()).hexdigest()
    assert tags["caps_easy_anchor_pct"] == "25" and tags["caps_neg_per_anchor"] == "2"
    assert tags["git_commit"]


def test_lineage_tags_without_manifest_uses_env(tmp_path, monkeypatch):
    monkeypatch.setenv("VIGIL_EXP", "F1-cosine")
    tags = lineage_tags(tmp_path / "missing.json")
    assert tags["exp"] == "F1-cosine" and tags["label_set_manifest_sha256"] == "unknown"
    assert lineage_tags(None)["label_set_manifest_sha256"] == "unknown"


def test_run_outputs_per_experiment(monkeypatch):
    from vigil_model.tracking import ROOT, run_outputs
    monkeypatch.delenv("VIGIL_EXP", raising=False)
    assert run_outputs() == (ROOT / "data/scores", ROOT / "data/cutoffs.json")        # CI's paths
    assert run_outputs("E1-agree-neg") == (ROOT / "data/runs/E1-agree-neg/scores",
                                           ROOT / "data/runs/E1-agree-neg/cutoffs.json")
    monkeypatch.setenv("VIGIL_EXP", "F1.cosine")
    assert run_outputs()[1] == ROOT / "data/runs/F1.cosine/cutoffs.json"


@pytest.mark.parametrize("bad", ["../v1", "a/b", "has space", ".."])
def test_experiment_id_cannot_escape_the_runs_folder(bad):
    from vigil_model.tracking import run_outputs
    with pytest.raises(ValueError):
        run_outputs(bad)
