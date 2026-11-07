"""MLflow tracking: store location, required tags, nested runs, registry, evaluation logging.

Runs against the temporary store set up in conftest.py (never Model-Development/mlflow/).
"""
import json
import re

import pytest

from query_router import evaluate as ev
from query_router import generate as gen
from query_router import tracking
from query_router.guardrails import rules_fingerprint
from query_router.train import flat_metrics, train_and_log


def test_tests_use_the_temporary_store(mlflow_test_store):
    assert tracking.tracking_uri() == f"sqlite:///{mlflow_test_store / 'mlflow.db'}"
    assert str(tracking.artifact_root()).startswith(str(mlflow_test_store))


def test_default_store_is_model_development_mlflow(monkeypatch):
    monkeypatch.delenv("MLFLOW_TRACKING_URI")
    monkeypatch.delenv("VIGIL_MLFLOW_ARTIFACTS")
    assert tracking.tracking_uri().endswith("Model-Development/mlflow/mlflow.db")
    assert tracking.artifact_root().parts[-3:] == ("Model-Development", "mlflow", "artifacts")


def test_data_version_follows_content(tmp_path):
    a = tmp_path / "train.jsonl"
    a.write_text('{"text": "x"}\n')
    v1 = tracking.data_version([a])
    assert re.fullmatch(r"[0-9a-f]{12}", v1) and tracking.data_version([a]) == v1
    a.write_text('{"text": "y"}\n')
    assert tracking.data_version([a]) != v1


def test_git_info_has_commit_and_dirty_flag():
    info = tracking.git_info()
    assert re.fullmatch(r"[0-9a-f]{40}|unknown", info["git_commit"])
    assert info["git_dirty"] in ("true", "false", "unknown")


def test_flat_metric_names():
    m = {"macro_f1": 0.8, "exact_match": 0.6, "out_of_scope_recall": 0.9,
         "false_refusal_rate": 0.1, "advice_recall": 0.99,
         "per_label": {"counts": {"f1": 0.7}}}
    assert flat_metrics(m, "val") == {
        "val_macro_f1": 0.8, "val_exact_match": 0.6, "val_out_of_scope_recall": 0.9,
        "val_false_refusal_rate": 0.1, "val_advice_recall": 0.99, "val_f1_counts": 0.7}


@pytest.fixture(scope="module")
def logged(tmp_path_factory):
    """One small tracked training run, registered with the alias champion."""
    data = gen.generate(n_train=600, n_val=200, n_heldout=200, seed=5)
    data_dir = tmp_path_factory.mktemp("data")
    for split, rows in data.items():
        gen.write_jsonl(rows, data_dir / f"{split}.jsonl")
    model_dir = tmp_path_factory.mktemp("model")
    meta = train_and_log(data_dir, model_dir, c_grid=(1.0, 2.0), alias="champion")
    return meta, data_dir, model_dir


def test_training_run_has_required_tags_params_and_metrics(logged):
    from mlflow import MlflowClient
    meta, data_dir, _ = logged
    run = MlflowClient().get_run(meta["mlflow_run_id"])
    tags = run.data.tags
    assert set(tracking.REQUIRED_TAGS) <= set(tags)
    assert tags["workstream"] == "chatbot" and tags["rules_fingerprint"] == rules_fingerprint()
    assert tags["data_version"] == tracking.data_version(
        [data_dir / "train.jsonl", data_dir / "val.jsonl"])
    assert run.data.params["C"] == str(meta["params"]["C"]) and run.data.params["c_grid"] == "1.0,2.0"
    for key in ("val_macro_f1", "val_advice_recall", "val_false_refusal_rate", "val_f1_counts"):
        assert key in run.data.metrics


def test_one_nested_run_per_c(logged):
    from mlflow import MlflowClient
    meta, _, _ = logged
    run = MlflowClient().get_run(meta["mlflow_run_id"])
    children = MlflowClient().search_runs(
        [run.info.experiment_id], f"tags.mlflow.parentRunId = '{run.info.run_id}'")
    assert sorted(c.data.params["C"] for c in children) == ["1.0", "2.0"]


def test_model_registered_with_alias_and_files_logged(logged):
    from mlflow import MlflowClient
    meta, _, model_dir = logged
    client = MlflowClient()
    version = client.get_model_version_by_alias(tracking.REGISTERED_MODEL, "champion")
    assert str(version.version) == meta["model_version"] and version.run_id == meta["mlflow_run_id"]
    saved = json.loads((model_dir / "meta.json").read_text())
    assert saved["mlflow_run_id"] == meta["mlflow_run_id"]
    files = {f.path for f in client.list_artifacts(meta["mlflow_run_id"], "files")}
    assert {"files/meta.json", "files/model.joblib"} <= files


def test_registered_model_has_a_text_signature_and_loads(logged):
    import mlflow
    meta, _, _ = logged
    uri = f"models:/{tracking.REGISTERED_MODEL}/{meta['model_version']}"
    info = mlflow.models.get_model_info(uri)
    assert info.signature is not None
    assert info.signature.inputs.to_dict()[0]["tensor-spec"]["dtype"] == "str"
    model = mlflow.sklearn.load_model(uri)
    assert model.predict_proba(["was the DreamStation recalled"]).shape == (1, 6)


def test_experiment_artifacts_live_under_the_store(logged, mlflow_test_store):
    import mlflow
    exp = mlflow.get_experiment_by_name(tracking.EXPERIMENT)
    assert exp.artifact_location.startswith((mlflow_test_store / "artifacts").as_uri())


def test_evaluate_adds_heldout_metrics_to_the_training_run(logged, capsys):
    from mlflow import MlflowClient
    meta, data_dir, model_dir = logged
    ev.main(["--model-dir", str(model_dir), "--test", str(data_dir / "heldout.jsonl"),
             "--false-alarms", "data/test/false_alarm_set.jsonl"])
    run = MlflowClient().get_run(meta["mlflow_run_id"])
    for key in ("heldout_macro_f1", "heldout_advice_recall", "heldout_advice_recall_rules",
                "heldout_false_refusal_rate_model", "falsealarm_flagged"):
        assert key in run.data.metrics, key
    assert run.data.tags["heldout_rules_fingerprint"] == rules_fingerprint()
    assert "logged heldout_* metrics" in capsys.readouterr().out


def test_evaluate_without_a_run_id_logs_nothing(logged, tmp_path, capsys):
    _, data_dir, model_dir = logged
    meta = json.loads((model_dir / "meta.json").read_text())
    meta.pop("mlflow_run_id")
    (tmp_path / "meta.json").write_text(json.dumps(meta))
    (tmp_path / "model.joblib").write_bytes((model_dir / "model.joblib").read_bytes())
    ev.main(["--model-dir", str(tmp_path), "--test", str(data_dir / "heldout.jsonl"),
             "--false-alarms", ""])
    assert "no mlflow_run_id" in capsys.readouterr().out
