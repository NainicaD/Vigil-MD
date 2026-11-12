"""Tests for the training CI (.github/workflows/model-training.yml and ci/). Offline: no GCP, no
training run. The workflow itself is checked as data: triggers, permissions and cost guards."""
import csv
import json
import sqlite3
from pathlib import Path

import pandas as pd
import pytest
import yaml

import ci.check_training_outputs as chk
import ci.make_ci_feature_table as gen
from vigil_model.same_device_features import DEFAULT_FEATURE_SET, FEATURE_COLUMNS, MatchKind, feature_set

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT.parents[1] / ".github" / "workflows" / "model-training.yml"


# ---------------------------------------------------------------------------- synthetic table

def test_synthetic_table_has_every_column_s06_reads():
    df = gen.make(n_devices=100, rows_per_device=3)
    for col in ("label", "meta_split", "meta_device_key", "meta_mfr_tier", *feature_set(DEFAULT_FEATURE_SET)):
        assert col in df.columns, col
    assert set(df["label"]) == {"MATCH", "NON_MATCH"}
    assert set(df["feat_match_kind"]) <= {k.value for k in MatchKind}


def test_synthetic_table_holds_only_its_feature_set():
    # s06 trains on the columns the table holds and s05 builds one set: they must be the same set
    fs2 = gen.make(n_devices=20, feature_set="fs2")
    assert [c for c in fs2.columns if c.startswith("feat_")] == list(feature_set("fs2"))
    assert "feat_labeler_sim" not in fs2.columns
    fs3 = gen.make(n_devices=20, feature_set="fs3")
    assert [c for c in fs3.columns if c.startswith("feat_")] == list(FEATURE_COLUMNS)


def test_generator_writes_a_tfidf_encoder_s05_can_load(tmp_path):
    pytest.importorskip("sklearn")
    from vigil_model import text_cosine
    out, enc = tmp_path / "f.csv", tmp_path / "f.tfidf.pkl"
    assert gen.main(["--out", str(out), "--devices", "20", "--tfidf", str(enc)]) == 0
    loaded = text_cosine.load(enc)
    assert text_cosine.pair_cosines(loaded, ["boston scientific stent"], ["boston scientific stent"])[0] > 0.99


def test_synthetic_split_is_by_device_with_both_labels_on_each_side():
    df = gen.make()
    train, test = df[df.meta_split == "train"], df[df.meta_split == "test"]
    assert set(train.meta_device_key).isdisjoint(test.meta_device_key)
    assert set(train.label) == set(test.label) == {"MATCH", "NON_MATCH"}
    assert 0.1 < len(test) / len(df) < 0.3


def test_synthetic_table_is_reproducible_and_learnable():
    a, b = gen.make(seed=3), gen.make(seed=3)
    pd.testing.assert_frame_equal(a, b)
    exact = a["feat_identifier_exact"].astype(bool)
    assert (a.loc[exact, "label"] == "MATCH").mean() > 0.8      # the features carry signal


def test_unknown_feature_column_stops_the_generator(monkeypatch):
    monkeypatch.setattr(gen, "FEATURE_COLUMNS", (*FEATURE_COLUMNS, "feat_new_thing"))
    with pytest.raises(KeyError, match="feat_new_thing"):
        gen.make(n_devices=10)


def test_generator_writes_a_csv(tmp_path):
    out = tmp_path / "f.csv"
    assert gen.main(["--out", str(out), "--devices", "20"]) == 0
    assert len(pd.read_csv(out)) == 100


# ---------------------------------------------------------------------------- output check

def _outputs(tmp_path, n=3, cutoffs=None, bad=None):
    hand = tmp_path / "hand.csv"
    pd.DataFrame({"pair_id": [f"p{i}" for i in range(n)]}).to_csv(hand, index=False)
    cut = cutoffs if cutoffs is not None else {m: 0.9 for m in chk.EXPECTED_MODELS}
    (tmp_path / "cutoffs.json").write_text(json.dumps(cut))
    scores = tmp_path / "scores"
    scores.mkdir()
    for m in chk.EXPECTED_MODELS:
        rows = [{"pair_id": f"p{i}", "score": 0.5} for i in range(n)]
        if bad and bad[0] == m:
            bad[1](rows)
        pd.DataFrame(rows).to_csv(scores / f"{m}_hand_scores.csv", index=False)
    return ["--cutoffs", str(tmp_path / "cutoffs.json"), "--scores-dir", str(scores), "--hand", str(hand),
            "--mlflow-db", str(tmp_path / "mlflow.db")]


def _mlflow_db(path, runs):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE runs (run_uuid TEXT, name TEXT, status TEXT, lifecycle_stage TEXT, start_time INT)")
    con.execute("CREATE TABLE latest_metrics (key TEXT, value REAL, run_uuid TEXT)")
    for i, (name, metrics) in enumerate(runs):
        con.execute("INSERT INTO runs VALUES (?, ?, 'FINISHED', 'active', ?)", (f"u{i}", name, i))
        con.executemany("INSERT INTO latest_metrics VALUES (?, ?, ?)", [(k, v, f"u{i}") for k, v in metrics.items()])
    con.commit()
    con.close()


def test_complete_outputs_pass_and_write_a_summary(tmp_path):
    args = _outputs(tmp_path, cutoffs={**{m: 0.9 for m in chk.EXPECTED_MODELS}, "xgb_all": None})
    summary = tmp_path / "summary.md"
    assert chk.main(args + ["--summary", str(summary)]) == chk.EXIT_OK
    text = summary.read_text()
    assert "| xgb_all | - | 3 |" in text and "never reached 99% precision on validation: xgb_all" in text


@pytest.mark.parametrize("bad", [
    ("xgb_all", lambda rows: rows.pop()),                                  # a row missing
    ("logreg_v1", lambda rows: rows[0].update(score=1.5)),                  # out of range
    ("xgb_no_match_kind", lambda rows: rows[0].update(score="nan")),        # NaN
    ("xgb_no_kind_derived", lambda rows: rows[0].update(pair_id="zz")),     # wrong pair
])
def test_bad_scores_fail_with_exit_3(tmp_path, bad):
    assert chk.main(_outputs(tmp_path, bad=bad)) == chk.EXIT_DATA


def test_missing_cutoffs_or_model_fails(tmp_path):
    args = _outputs(tmp_path)
    (tmp_path / "scores" / "xgb_all_hand_scores.csv").unlink()
    assert chk.main(args) == chk.EXIT_DATA
    (tmp_path / "cutoffs.json").unlink()
    assert chk.main(args) == chk.EXIT_DATA


def test_cutoffs_without_every_model_fail(tmp_path):
    assert chk.main(_outputs(tmp_path, cutoffs={"xgb_all": 0.9})) == chk.EXIT_DATA


def test_require_mlflow(tmp_path):
    args = _outputs(tmp_path) + ["--require-mlflow"]
    assert chk.main(args) == chk.EXIT_DATA                          # no mlflow.db
    _mlflow_db(tmp_path / "mlflow.db", [(m, {"val_average_precision": 0.9}) for m in chk.EXPECTED_MODELS[:3]])
    assert chk.main(args) == chk.EXIT_DATA                          # one model has no run


def test_summary_shows_latest_training_and_hand_metrics(tmp_path):
    runs = [(m, {"val_average_precision": 0.5}) for m in chk.EXPECTED_MODELS]
    runs += [("xgb_all", {"val_average_precision": 0.97, "cutoff_p99": 0.91}),          # newer run wins
             ("xgb_all_hand", {"retrieval_recall_at1": 0.8, "slice_x_precision": 1.0})]
    _mlflow_db(tmp_path / "mlflow.db", runs)
    summary = tmp_path / "s.md"
    assert chk.main(_outputs(tmp_path) + ["--require-mlflow", "--summary", str(summary)]) == chk.EXIT_OK
    text = summary.read_text()
    assert "0.9700" in text and "0.5000 |" in text
    assert "retrieval_recall_at1=0.8000" in text and "slice_x_precision" not in text


def test_unreadable_hand_file_is_a_config_error(tmp_path):
    args = _outputs(tmp_path)
    args[args.index("--hand") + 1] = str(tmp_path / "nope.csv")
    assert chk.main(args) == chk.EXIT_CONFIG


# ---------------------------------------------------------------------------- the workflow

@pytest.fixture(scope="module")
def wf():
    data = yaml.safe_load(WORKFLOW.read_text())
    data["on"] = data.get("on", data.get(True))       # YAML 1.1 reads the key `on` as True
    return data


def _run_lines(job):
    return "\n".join(s.get("run", "") for s in job["steps"])


def test_triggers(wf):
    on = wf["on"]
    assert set(on["push"]["branches"]) == {"main", "same-device-profiling"}
    assert "Model-Development/matching/**" in on["push"]["paths"] and "Model-Development/matching/**" in on["pull_request"]["paths"]
    assert "workflow_dispatch" in on


def test_only_the_train_job_can_reach_gcp(wf):
    jobs = wf["jobs"]
    assert wf["permissions"] == {"contents": "read"}
    for name in ("test", "smoke-train"):
        text = yaml.safe_dump(jobs[name])
        assert "id-token" not in text and "google-github-actions" not in text and "vars.GCP" not in text, name
    train = jobs["train"]
    assert train["permissions"]["id-token"] == "write"
    assert "pull_request" not in train["if"] and "github.event_name == 'push'" in train["if"]
    assert "vars.GCP_WORKLOAD_IDENTITY_PROVIDER != ''" in train["if"]
    assert not any("credentials_json" in yaml.safe_dump(s) for s in train["steps"]), "no stored keys"


def test_smoke_train_uses_synthetic_data_and_no_mlflow(wf):
    run = _run_lines(wf["jobs"]["smoke-train"])
    assert "ci/make_ci_feature_table.py" in run and "--data data/ci/features.csv" in run and "--no-mlflow" in run
    # fs2 needs feat_text_cosine: the generator writes the encoder and s05 reads it (no s04 in this job)
    assert run.count("--tfidf data/ci/features.tfidf.pkl") == 2


def test_train_runs_the_chain_in_order_with_cost_guards(wf):
    run = _run_lines(wf["jobs"]["train"])
    order = ["s03_export_label_set", "s04_build_feature_table.py --project \"$PROJECT\" --estimate-only",
             "s05_build_hand_features", "s06_train_models", "s08_evaluate_hand", "check_training_outputs.py --require-mlflow"]
    positions = [run.index(s) for s in order]
    assert positions == sorted(positions)
    assert run.count("--max-gb 2") == 3                     # dry run and both real-run branches
    assert wf["jobs"]["train"]["needs"] == ["test", "smoke-train"]


def test_train_uses_the_released_recipe(wf):
    job = wf["jobs"]["train"]
    run = _run_lines(job)
    assert "--table label_set_v2" in run and "--feature-set fs2" in run
    assert run.count("$S04_ARGS") == 3                      # dry run and both real runs build the same table
    assert "--table label_set_v2" in job["env"]["S04_ARGS"] and "--feature-set fs2" in job["env"]["S04_ARGS"]
    assert "--tfidf data/label_set_v2_fs2_features.tfidf.pkl" in run
    assert "--data data/label_set_v2_fs2_features.parquet" in run
    assert '"--ie-gate"' in run                             # s08 scores with the E4 gate, as the release does


def test_train_uploads_the_models(wf):
    upload = [s for s in wf["jobs"]["train"]["steps"] if s.get("uses", "").startswith("actions/upload-artifact")]
    assert upload and upload[0]["if"] == "always()"
    for p in ("mlflow.db", "mlruns/", "data/cutoffs.json", "data/scores/", "data/label_set_v2_fs2_features.tfidf.pkl"):
        assert f"Model-Development/matching/{p}" in upload[0]["with"]["path"]


def test_data_cache_is_keyed_on_the_label_manifest(wf):
    keys = [s["with"]["key"] for s in wf["jobs"]["train"]["steps"] if "actions/cache" in s.get("uses", "")]
    assert len(keys) == 2 and keys[0] == keys[1]
    assert "labels/label_set_v2.manifest.json" in keys[0]
