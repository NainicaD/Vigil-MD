"""build_labels.py with a fake BigQuery: no real GCP. See conftest.fake_bigquery."""
import json
from types import SimpleNamespace

import pytest

from vigil_model import bq_utils
from scripts import s01_build_label_set as bl

PROJECT = "test-project"
PARAMS = {"__MAX_PER_DEVICE__": 200, "__NEG_PER_ANCHOR__": 2, "__CAND_POOL__": 50,
          "__EASY_ANCHOR_PCT__": 10, "__EASY_POOL__": 20000, "__AGREE_NEG_PER_ANCHOR__": 0, "__PLACEHOLDER_NEG_PER_ANCHOR__": 0,
          "__LABEL_SET__": "label_set_v1"}
ALIAS_ROWS = [{"maude_mfr": "a", "gudid_mfr": "b", "n_dis": 3}]


class FakeClient:
    def __init__(self, gb=1.0, existing=(), fail_on=None, alias_rows=ALIAS_ROWS):
        self.gb, self.existing, self.fail_on, self.alias_rows = gb, set(existing), fail_on, alias_rows
        self.real_jobs, self.configs = [], []

    def get_table(self, name):
        if name not in self.existing:
            raise RuntimeError(f"Not found: Table {name}")

    def query(self, sql, job_config=None):
        self.configs.append(job_config)
        if getattr(job_config, "dry_run", False):
            if self.fail_on and self.fail_on in sql:
                raise RuntimeError("Syntax error")
            return SimpleNamespace(total_bytes_processed=int(self.gb * 1e9))
        if "FROM `test-project.vigil_md_model.alias_table`" in sql:
            return SimpleNamespace(result=lambda: [SimpleNamespace(items=lambda r=r: r.items()) for r in self.alias_rows])
        if "label_stats_counts" in sql and sql.lstrip().startswith("SELECT"):
            row = {"meta_split": "train", "label": "MATCH", "key_match_kind": "same_model_key", "n_rows": 5}
            return SimpleNamespace(result=lambda: [row])
        self.real_jobs.append(sql)
        return SimpleNamespace(result=lambda: [], total_bytes_processed=123)


@pytest.fixture
def cfg(fake_bigquery):
    fake_bigquery.QueryJobConfig.side_effect = lambda **kw: SimpleNamespace(**kw)


@pytest.fixture
def files():
    return bq_utils.load_query_dir(bl.SQL_DIR, ["02", "03"])


@pytest.fixture
def paths(tmp_path):
    return {"manifest_path": tmp_path / "m.json", "alias_csv": tmp_path / "alias.csv"}


def good_manifest(paths):
    data = bl.alias_csv_bytes(ALIAS_ROWS)
    paths["alias_csv"].write_bytes(data)
    paths["manifest_path"].write_text(json.dumps({"alias_table_sha256": bl.sha256_hex(data)}))


def test_every_label_file_renders_and_passes_the_write_guard():
    for qf, part, sql in bl.rendered(bq_utils.load_query_dir(bl.SQL_DIR), PROJECT, PARAMS):
        assert "{{" not in sql and f"{PROJECT}.vigil_md_model." in sql


@pytest.mark.parametrize("sql", [
    "SELECT 1 FROM `p.vigil_md_prod.maude`",
    "CREATE OR REPLACE TABLE `p.vigil_md_prod.x` AS SELECT 1",
    "CREATE OR REPLACE TABLE `p.vigil_md_model.x` AS SELECT 1 FROM `p.vigil_md_staging.y`",
    "CREATE OR REPLACE TABLE `p.vigil_md_model.x` AS SELECT 1; DROP TABLE `p.vigil_md_prod.maude`",
])
def test_write_guard_rejects(sql):
    with pytest.raises(ValueError):
        bl.assert_model_write_only(sql)


def test_unknown_macro_and_wrong_arity_raise():
    with pytest.raises(ValueError):
        bl.expand_macros("{{ nope(x) }}")
    with pytest.raises(ValueError):
        bl.expand_macros("{{ identifier_key(a, b) }}")


def test_alias_csv_is_order_independent():
    rows = [{"a": "x", "b": 1}, {"a": "y", "b": 2}]
    assert bl.alias_csv_bytes(rows) == bl.alias_csv_bytes(rows[::-1])


def test_alias_gate_refuses_without_manifest_hash(cfg, files, paths):
    client = FakeClient(existing={f"{PROJECT}.vigil_md_model.device_split"})
    assert bl.run(client, PROJECT, files, PARAMS, 25, snapshot_date="d", **paths) == bl.EXIT_BAD_SQL
    assert client.real_jobs == []


def test_alias_gate_refuses_on_hash_mismatch(cfg, files, paths):
    good_manifest(paths)
    client = FakeClient(existing={f"{PROJECT}.vigil_md_model.device_split"},
                        alias_rows=[{"maude_mfr": "x", "gudid_mfr": "y", "n_dis": 9}])
    assert bl.run(client, PROJECT, files, PARAMS, 25, snapshot_date="d", **paths) == bl.EXIT_BAD_SQL
    assert client.real_jobs == []


def test_over_budget_runs_nothing(cfg, files, paths):
    good_manifest(paths)
    client = FakeClient(gb=30, existing={f"{PROJECT}.vigil_md_model.device_split"})
    assert bl.run(client, PROJECT, files, PARAMS, 25, snapshot_date="d", **paths) == bl.EXIT_OVER_BUDGET
    assert client.real_jobs == []


def test_missing_input_table_is_a_config_error(cfg, files, paths):
    good_manifest(paths)
    assert bl.run(FakeClient(), PROJECT, files, PARAMS, 25, snapshot_date="d", **paths) == bl.EXIT_BAD_SQL


def test_estimate_only_runs_no_job(cfg, files, paths):
    good_manifest(paths)
    client = FakeClient(existing={f"{PROJECT}.vigil_md_model.device_split"})
    assert bl.run(client, PROJECT, files, PARAMS, 25, estimate_only=True, **paths) == bl.EXIT_OK
    assert client.real_jobs == []


def test_real_run_needs_a_snapshot_date(cfg, files, paths):
    good_manifest(paths)
    client = FakeClient(existing={f"{PROJECT}.vigil_md_model.device_split"})
    assert bl.run(client, PROJECT, files, PARAMS, 25, **paths) == bl.EXIT_BAD_SQL


def test_full_run_builds_in_order_caps_billing_and_writes_manifest(cfg, paths, monkeypatch):
    good_manifest(paths)
    files = bq_utils.load_query_dir(bl.SQL_DIR, ["02", "03", "06"])
    monkeypatch.setattr(bl, "MANIFEST", paths["manifest_path"])
    client = FakeClient(existing={f"{PROJECT}.vigil_md_model.device_split",
                                  f"{PROJECT}.vigil_md_model.label_set_v1"})
    assert bl.run(client, PROJECT, files, PARAMS, 25, snapshot_date="2026-11-06", **paths) == bl.EXIT_OK
    targets = [bl.target_of(s) for s in client.real_jobs]
    assert targets[:4] == [f"{PROJECT}.vigil_md_model.{t}" for t in
                           ("gudid_keyed", "linked_all", "review_conflicts", "positives")]
    capped = [c for c in client.configs if getattr(c, "maximum_bytes_billed", None) == int(25e9)]
    assert len(capped) == len(client.real_jobs) > 0     # every build job carries the --max-gb cap
    m = json.loads(paths["manifest_path"].read_text())
    assert m["snapshot_date"] == "2026-11-06" and m["alias_table_sha256"]            # kept, not overwritten
    assert m["row_counts"] == {"train|MATCH|same_model_key": 5}
    assert set(m) >= {"query_sha256", "constants", "constants_sha256", "caps_and_ratios", "project"}


def test_print_only_needs_no_gcp(capsys):
    assert bl.main(["--project", PROJECT, "--print-only", "--only", "03"]) == bl.EXIT_OK
    assert "CREATE OR REPLACE TABLE `test-project.vigil_md_model.negatives_suffix`" in capsys.readouterr().out


def test_print_only_renders_the_agree_negatives_into_a_new_label_set(capsys):
    argv = ["--project", PROJECT, "--print-only", "--only", "04b", "05",
            "--label-set", "label_set_v2_e1", "--agree-neg-per-anchor", "3"]
    assert bl.main(argv) == bl.EXIT_OK
    out = capsys.readouterr().out
    assert "CREATE OR REPLACE TABLE `test-project.vigil_md_model.negatives_agree`" in out
    assert "WHERE rn <= 3" in out
    assert "CREATE OR REPLACE TABLE `test-project.vigil_md_model.label_set_v2_e1`" in out
    assert "label_set_v1" not in out


@pytest.mark.parametrize("argv", [
    ["--agree-neg-per-anchor", "2"],                                   # would overwrite the baseline
    ["--label-set", "label_set_v2_e1", "--agree-neg-per-anchor", "-1"],
    ["--label-set", "my table"],
])
def test_main_refuses_bad_label_set_args_before_any_gcp_call(argv):
    assert bl.main(["--project", PROJECT, "--print-only", *argv]) == bl.EXIT_BAD_SQL


def test_seed_manifest_copies_the_baseline_alias_hash(tmp_path):
    base, new = tmp_path / "label_set_v1.manifest.json", tmp_path / "label_set_v2_e1.manifest.json"
    base.write_text(json.dumps({"alias_table_sha256": "abc", "row_counts": {"x": 1}}))
    bl.seed_manifest(new, base)
    assert json.loads(new.read_text()) == {"alias_table_sha256": "abc", "based_on": base.name}
    new.write_text(json.dumps({"alias_table_sha256": "keep"}))
    bl.seed_manifest(new, base)                                        # an existing manifest is never touched
    assert json.loads(new.read_text()) == {"alias_table_sha256": "keep"}


def test_print_only_renders_the_placeholder_negatives_into_label_set_v2(capsys):
    argv = ["--project", PROJECT, "--print-only", "--only", "04c", "05", "--label-set", "label_set_v2",
            "--agree-neg-per-anchor", "2", "--placeholder-neg-per-anchor", "2"]
    assert bl.main(argv) == bl.EXIT_OK
    out = capsys.readouterr().out
    assert "CREATE OR REPLACE TABLE `test-project.vigil_md_model.negatives_placeholder`" in out
    assert "WHERE rn <= 2" in out and "`test-project.vigil_md_model.label_set_v2` AS" in out


def test_placeholder_negatives_cannot_go_into_the_baseline():
    assert bl.main(["--project", PROJECT, "--print-only", "--placeholder-neg-per-anchor", "1"]) == bl.EXIT_BAD_SQL
