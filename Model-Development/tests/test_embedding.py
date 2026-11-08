"""Tests for the embedding export and the retrieval prototype. No GCP, no model download:
BigQuery is faked and a small hashing encoder stands in for the real models."""
import csv
import json
import re
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from vigil_model import bq_utils
from vigil_model import embedding_retrieval as er
from pipeline import export_embedding_data as ex

PROJECT = "test-project"
SQL_FILES = sorted(ex.SQL_DIR.glob("*.sql"))
PLACEHOLDER_RE = (r"^(UNK|UNKN|UNKNOWN|NA|NI|NONE|NAV|ASKU|NOTAPPLICABLE|NOTAVAILABLE|NOTKNOWN"
                  r"|UNAVAILABLE|NOTPROVIDED|0+|\*+|\?+|X+)$")
GROUP_KEY_EXPR = "CONCAT('M|', IFNULL(g.mfr, ''), '|', g.mk), CONCAT('DI|', g.udi_di)"


# ---------------------------------------------------------------------------- SQL files

def test_the_three_queries_exist():
    assert [p.stem for p in SQL_FILES] == list(ex.QUERY_IDS)


@pytest.mark.parametrize("path", SQL_FILES, ids=lambda p: p.stem)
def test_every_query_has_a_header_and_renders_read_only(path):
    qf = bq_utils.load_query_file(path)
    for key in bq_utils.HEADER_KEYS:
        assert qf.header.get(key), f"{path.name}: missing '-- {key}:'"
    sql = bq_utils.render(qf.parts[0][1], PROJECT)
    assert f"`{PROJECT}.vigil_md_prod." in sql
    bq_utils.assert_read_only(sql)


@pytest.mark.parametrize("path", SQL_FILES[1:], ids=lambda p: p.stem)
def test_placeholder_rule_is_the_shared_one(path):
    assert set(re.findall(r"r'(\^\(UNK[^']*)'", path.read_text())) == {PLACEHOLDER_RE}


def _block(path):
    text = path.read_text()
    return text[text.index("-- BEGIN gudid_group_keys"):text.index("-- END gudid_group_keys")]


def test_group_key_block_is_identical_in_02_and_03():
    assert _block(ex.SQL_DIR / "02_gudid_groups.sql") == _block(ex.SQL_DIR / "03_maude_queries.sql")


def test_group_key_matches_the_test_set_candidates():
    """Results must join to the labeling sheet, so the group_key must be built the same way."""
    for path in (ex.SQL_DIR / "02_gudid_groups.sql",
                 ex.SQL_DIR.parent / "test_set" / "02_candidates.sql"):
        assert GROUP_KEY_EXPR in path.read_text(), path.name


def test_maude_query_never_returns_the_udi():
    """The UDI is the answer; if it reached the text, retrieval would be scored on a leak."""
    text = (ex.SQL_DIR / "03_maude_queries.sql").read_text()
    final_select = text[text.rindex("\nSELECT"):].lower()
    assert not re.search(r"\b(udi_di|udi|rep_udi_di|primary_di)\b", final_select)
    assert "gudid_mfrs" in final_select          # the word "gudid" alone is not a leak


# ---------------------------------------------------------------------------- export

def _labeler(name, tier="top_10"):
    return {"mfr": name, "tier": tier, "link_rank": 1, "n_linked_rows": 100, "n_dis": 10}


def _group(key, n=1):
    return {"group_key": key, "n_dis": n, "rep_udi_di": "0" + key, "mfr": "acme",
            "manufacturer_name_raw": "Acme Inc", "device_name_raw": "Pump",
            "model_number_raw": "X1", "catalog_number": "", "catalog_examples": "",
            "product_codes": "FRN"}


def _query(qid, keys, **kw):
    return {"query_id": qid, "pool_tuples": 50, "n_rows": 3, "true_group_keys": keys,
            "gudid_mfrs": "acme", "id_agrees": True, "mfr_equal": True, "has_identifier": True,
            "mfr": "acme", "manufacturer_name_raw": "ACME", "device_name_raw": "PUMP",
            "model_number_raw": "X-1", "catalog_number": "", "product_code": "FRN",
            "record_date": "2024-01-01", "example_mdr_report_key": "1", "example_device_seq": 0,
            **kw}


def test_summarize_counts_queries_without_an_answer_in_the_index():
    s = ex.summarize([_labeler("acme")], [_group("M|acme|X1")],
                     [_query("a", "M|acme|X1"), _query("b", "M|acme|ZZ;M|acme|X1"),
                      _query("c", "M|other|Q")])
    assert s["queries_answer_not_in_index"] == 1
    assert s["gudid_groups"] == 1 and s["queries"] == 3 and s["query_pool_tuples"] == 50


class FakeClient:
    def __init__(self, rows, gb=(1.0, 0.5, 2.0)):
        self.rows, self.gb, self.calls = rows, gb, []

    def query(self, sql, job_config=None):
        qid = ("01_labelers" if "@n_top" in sql else
               "02_gudid_groups" if "model_groups AS" in sql else "03_maude_queries")
        self.calls.append(qid)
        size = int(self.gb[ex.QUERY_IDS.index(qid)] * 1e9)
        rows = self.rows[qid]
        return SimpleNamespace(
            total_bytes_processed=size,
            result=lambda: [SimpleNamespace(items=lambda r=r: r.items()) for r in rows])


ROWS = {"01_labelers": [_labeler("acme")], "02_gudid_groups": [_group("M|acme|X1")],
        "03_maude_queries": [_query("a", "M|acme|X1")]}


@pytest.fixture
def out_root(tmp_path, monkeypatch):
    monkeypatch.setattr(ex, "OUT_ROOT", tmp_path)
    return tmp_path


def test_export_writes_three_csvs_and_a_manifest(fake_bigquery, out_root):
    client = FakeClient(ROWS)
    assert ex.main(["--project", PROJECT, "--seed", "s"], client=client) == ex.EXIT_OK
    assert client.calls == list(ex.QUERY_IDS) * 2          # three dry runs, then three runs
    for name in ("labelers.csv", "gudid_groups.csv", "maude_queries.csv"):
        assert (out_root / "s" / name).exists()
    m = json.loads((out_root / "s" / "manifest.json").read_text())
    assert m["queries_answer_not_in_index"] == 0 and m["labelers_by_tier"] == {"top_10": 1}


def test_export_over_budget_runs_nothing(fake_bigquery, out_root):
    client = FakeClient(ROWS, gb=(1.0, 0.5, 99.0))
    assert ex.main(["--project", PROJECT], client=client) == ex.EXIT_OVER_BUDGET
    assert client.calls == list(ex.QUERY_IDS)
    assert not any(out_root.iterdir())


def test_export_does_not_overwrite(fake_bigquery, out_root):
    (out_root / "s").mkdir()
    (out_root / "s" / "manifest.json").write_text("{}")
    client = FakeClient(ROWS)
    assert ex.main(["--project", PROJECT, "--seed", "s"], client=client) == ex.EXIT_EXISTS
    assert client.calls == []


def test_export_stops_when_no_labeler_matches(fake_bigquery, out_root):
    client = FakeClient({**ROWS, "01_labelers": []})
    assert ex.main(["--project", PROJECT], client=client) == ex.EXIT_BAD_SQL
    assert client.calls == list(ex.QUERY_IDS) + ["01_labelers"]


# ---------------------------------------------------------------------------- text

def test_placeholders_and_missing_values_are_dropped():
    assert er.usable_identifier("N/A") == ""
    assert er.usable_identifier("unk") == ""
    assert er.usable_identifier("0000") == ""
    assert er.usable_identifier("x") == ""
    assert er.usable_identifier(float("nan")) == ""
    assert er.usable_identifier(" IP-200 ") == "ip-200"


def test_record_text_template():
    t = er.record_text("acme", "Infusion  Pump", "IP-200", "CAT1;N/A;CAT1", "FRN")
    assert t == "acme | infusion pump | model ip-200 | catalog cat1 | code frn"
    assert er.record_text(None, "Pump", "NA", None, None) == "pump"
    assert er.record_text("acme", "pump", "X1", None, "FRN", fields=("mfr", "model")) == "acme | model x1"


def test_both_sides_get_the_same_text_for_the_same_device():
    g = pd.DataFrame([_group("M|acme|X1")]).astype(object)
    q = pd.DataFrame([_query("a", "M|acme|X1", model_number_raw="X1", device_name_raw="Pump")]).astype(object)
    assert er.gudid_texts(g) == er.maude_texts(q)


# ---------------------------------------------------------------------------- search and metrics

def _unit(rng, n, d):
    m = rng.normal(size=(n, d)).astype(np.float32)
    return m / np.linalg.norm(m, axis=1, keepdims=True)


def test_topk_matches_a_full_sort_and_does_not_depend_on_chunking():
    rng = np.random.default_rng(0)
    Q, X = _unit(rng, 37, 16), _unit(rng, 200, 16)
    want = np.argsort(-(Q @ X.T), axis=1, kind="stable")[:, :10]
    idx, scores = er.topk_search(Q, X, 10)
    assert (idx == want).all()
    assert (np.diff(scores, axis=1) <= 1e-7).all()             # best first
    idx_small, _ = er.topk_search(Q, X, 10, max_cells=450)      # 2 queries per chunk
    assert (idx_small == idx).all()


def test_topk_breaks_ties_by_index_and_caps_k():
    Q = np.ones((1, 2), np.float32) / np.sqrt(2)
    X = np.tile(Q, (4, 1))
    idx, _ = er.topk_search(Q, X, 10)
    assert idx.tolist() == [[0, 1, 2, 3]]


def test_topk_works_on_sparse_input():
    sparse = pytest.importorskip("scipy.sparse")
    rng = np.random.default_rng(1)
    Q, X = _unit(rng, 5, 8), _unit(rng, 30, 8)
    dense, _ = er.topk_search(Q, X, 5)
    sp, _ = er.topk_search(sparse.csr_matrix(Q), sparse.csr_matrix(X), 5)
    assert (dense == sp).all()


def test_first_hit_rank_and_metrics():
    topk = np.array([[3, 1, 2], [5, 6, 7], [9, 8, 4]])
    ranks = er.first_hit_rank(topk, [{1, 2}, {0}, {9}])
    assert ranks.tolist() == [2, 0, 1]
    m = er.retrieval_metrics(ranks, ks=(1, 3))
    assert m["recall@1"] == pytest.approx(1 / 3) and m["recall@3"] == pytest.approx(2 / 3)
    assert m["mrr"] == pytest.approx((0.5 + 0 + 1) / 3)


def test_wilson_interval_known_value():
    lo, hi = er.wilson(8, 10)
    assert abs(lo - 0.490) < 0.001 and abs(hi - 0.943) < 0.001
    assert er.wilson(0, 0) == (0.0, 0.0)


def test_slice_flag_needs_size_gap_and_interval():
    n = 200
    queries = pd.DataFrame({"tier": ["top_10"] * 100 + ["101_plus"] * 100,
                            "id_agrees": [True] * n, "mfr_equal": [True] * n,
                            "has_identifier": [True] * n})
    ranks = np.array([1] * 100 + [1] * 40 + [0] * 60)          # 100% vs 40%, overall 70%
    rows = {(r["slice"], r["value"]): r for r in er.slice_table("m", ranks, queries)}
    assert rows[("tier", "101_plus")]["disparity_flag"] is True
    assert rows[("tier", "top_10")]["disparity_flag"] is True
    assert rows[("id_agrees", "True")]["disparity_flag"] is False   # the whole set: no gap


# ---------------------------------------------------------------------------- end to end

class HashEncoder:
    """Character trigrams hashed into 512 dimensions. Needs numpy only."""
    name = "hash"

    def fit(self, texts):
        pass

    def encode(self, texts):
        m = np.zeros((len(texts), 512), np.float32)
        for i, t in enumerate(texts):
            t = f"  {t}  "
            for j in range(len(t) - 2):
                m[i, int(er.hashlib.md5(t[j:j + 3].encode()).hexdigest()[:6], 16) % 512] += 1
        return m / np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-9)


def _write(path, rows):
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


@pytest.fixture
def tiny_data(tmp_path, monkeypatch):
    monkeypatch.setattr(er, "REPORTS", tmp_path / "reports")
    d = tmp_path / "data"
    d.mkdir()
    models = ["IP-200", "GP200", "GP200S", "CV-190", "LZ-77", "RX-5000"]
    groups = [dict(_group(f"M|acme|{m.replace('-', '')}"), model_number_raw=m,
                   device_name_raw=f"device {m[:2]}") for m in models]
    queries = [_query(f"q{i}", g["group_key"], model_number_raw=g["model_number_raw"],
                      device_name_raw=g["device_name_raw"])
               for i, g in enumerate(groups)]
    queries[0]["catalog_number"] = "N/A"              # a placeholder must not change the text
    _write(d / "gudid_groups.csv", groups)
    _write(d / "maude_queries.csv", queries)
    _write(d / "labelers.csv", [_labeler("acme")])
    return d


def test_end_to_end_finds_exact_copies_at_rank_one(tiny_data):
    assert er.main(["--data", str(tiny_data), "--run-name", "t", "--no-cache"],
                   encoders=[HashEncoder()]) == 0
    out = er.REPORTS / "t"
    metrics = json.loads((out / "metrics.json").read_text())["results"]["hash"]
    assert metrics["recall@1"] == 1.0 and metrics["n_queries"] == 6 and metrics["n_index"] == 6
    slices = pd.read_csv(out / "slices.csv")
    assert set(slices["slice"]) == set(er.SLICE_COLS)
    assert "| hash |" in (out / "comparison.md").read_text()


def test_end_to_end_uses_the_cache_for_dense_methods(tiny_data):
    args = ["--data", str(tiny_data), "--run-name", "c"]
    assert er.main(args, encoders=[HashEncoder()]) == 0
    cached = sorted(p.name for p in (tiny_data / "cache").glob("*.npy"))
    assert len(cached) == 2                                      # gudid and maude
    assert er.main(args, encoders=[HashEncoder()]) == 0
    assert sorted(p.name for p in (tiny_data / "cache").glob("*.npy")) == cached


def test_unknown_field_is_an_error(tiny_data):
    assert er.main(["--data", str(tiny_data), "--fields", "mfr,colour"],
                   encoders=[HashEncoder()]) == 2


def test_unknown_method_is_an_error():
    with pytest.raises(ValueError):
        er.make_encoder("word2vec", 8, "cpu")
