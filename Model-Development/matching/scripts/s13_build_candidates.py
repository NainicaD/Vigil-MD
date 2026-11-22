"""Build the candidate pairs that s12_score_pairs.py scores, and save them as parquet.

Runs sql/scoring/01_candidates.sql on one dataset (default vigil_md_dev, the test extract):
MAUDE rows with no usable UDI link that changed after --since, each with the GUDID model groups
that the test-set candidate rules propose. Writes vigil_md_model.match_candidates_<dataset>
(the only dataset this folder writes to), then downloads it with the row-listing API (free).

--since auto (default): MAX(built_at) of <dataset>.link_maude_gudid_model, the last load of model
links. No such table: every row. --full: every row (backfill, new model version).

--mode udi_check: instead of the pool, a fixed sample (--sample-per-10k) of rows that HAVE an exact
UDI link, with true_group_key, for the weekly agreement check (s14_udi_agreement.py). Its table is
vigil_md_model.match_candidates_check_<dataset>, so it never replaces the pool's table, which
load_model_links.py reads.

Sibling keys: which of the candidates' MAUDE model/catalog keys exist in GUDID under the
candidate's labeler (s04's SIBLING_SQL, run on the candidate table). Written to --siblings-out
for s12 --siblings. Without them feat_conflicting_identifier is always False, as in training
without the file, and suffix-sibling pairs are called MATCH too often.

Cost guard: a dry run before each query (free); stop above --max-gb; each job gets
maximum_bytes_billed. --estimate-only prices the candidate query only: the sibling query reads
the candidate table, which exists only after a real run.

Exit codes: 0 done, 2 bad input or over budget, 4 no candidates (nothing to score).

Usage (from anywhere):
    python scripts/s13_build_candidates.py --project <id> --print-only
    python scripts/s13_build_candidates.py --project <id> --estimate-only
    python scripts/s13_build_candidates.py --project <id> --out data/candidates.parquet
    python scripts/s13_build_candidates.py --project <id> --full --out data/candidates.parquet
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.build_test_set import DEFAULT_CAPS  # noqa: E402
from scripts.s04_build_feature_table import assert_select_datasets, fetch_rows, sibling_sql  # noqa: E402
from vigil_model import bq_utils  # noqa: E402

EXIT_OK, EXIT_BAD, EXIT_NONE = 0, 2, 4
SQL_FILE = ROOT / "sql" / "scoring" / "01_candidates.sql"
LINK_TABLE = "link_maude_gudid_model"       # Data-Pipeline schemas.MODEL_LINK_TABLE
SEED = "vigil-scoring-v1"                   # order of the name rules' candidates, as in the test set
CAPS = {k: v for k, v in DEFAULT_CAPS.items() if k != "cap_random"}   # random_same_mfr is not used


def check_dataset(dataset: str) -> None:
    """The same datasets the link tables may live in (Data-Pipeline build_links._check_dataset)."""
    if dataset != bq_utils.PROD_DATASET and not dataset.startswith("vigil_md_dev"):
        raise ValueError(f"--dataset must be {bq_utils.PROD_DATASET} or vigil_md_dev*, got {dataset!r}")


def select_sql(project: str, dataset: str) -> str:
    """The candidate SELECT on one dataset, checked to read only that dataset. Pure."""
    check_dataset(dataset)
    sql = bq_utils.load_query_file(SQL_FILE).parts[0][1]
    for ph, table in bq_utils.TABLE_PLACEHOLDERS.items():
        sql = sql.replace(ph, f"{project}.{dataset}.{table}")
    assert_select_datasets(sql, {dataset})
    return sql


MODES = ("pool", "udi_check")


def target_table(project: str, dataset: str, mode: str = "pool") -> str:
    """'udi_check' has its own table: load_model_links.py deletes the links of the rows in the
    'pool' table, so a check run must never replace it."""
    infix = "_check" if mode == "udi_check" else ""
    return f"{project}.{bq_utils.MODEL_DATASET}.match_candidates{infix}_{dataset}"


def create_sql(project: str, dataset: str, mode: str = "pool") -> str:
    """CREATE OR REPLACE TABLE in vigil_md_model only. Pure."""
    return f"CREATE OR REPLACE TABLE `{target_table(project, dataset, mode)}` AS\n{select_sql(project, dataset)}"


def siblings_sql(project: str, dataset: str, mode: str = "pool") -> str:
    """s04's sibling-key query on this run's candidate table (same key_* column names). Pure."""
    return sibling_sql(project, dataset, target_table(project, dataset, mode).rsplit(".", 1)[1])


def query_params(since, mode: str = "pool", sample_per_10k: int = 100):
    from google.cloud import bigquery
    return ([bigquery.ScalarQueryParameter("since", "TIMESTAMP", since),
             bigquery.ScalarQueryParameter("seed", "STRING", SEED),
             bigquery.ScalarQueryParameter("mode", "STRING", mode),
             bigquery.ScalarQueryParameter("sample_per_10k", "INT64", sample_per_10k)]
            + [bigquery.ScalarQueryParameter(k, "INT64", v) for k, v in CAPS.items()])


def last_load(client, project: str, dataset: str):
    """When model links were last loaded into this dataset, or None (never: score every row)."""
    from google.api_core.exceptions import NotFound
    try:
        rows = list(client.query(f"SELECT MAX(built_at) AS t FROM `{project}.{dataset}.{LINK_TABLE}`").result())
    except NotFound:
        return None
    return rows[0]["t"] if rows else None


def main(argv=None, client=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", required=True)
    ap.add_argument("--dataset", default="vigil_md_dev", help="where maude and gudid are read")
    ap.add_argument("--since", default="auto", help="'auto' or an ISO date/time")
    ap.add_argument("--full", action="store_true", help="every row of the pool (backfill)")
    ap.add_argument("--out", type=Path, help="candidate pairs parquet (for s12)")
    ap.add_argument("--siblings-out", type=Path,
                    help="sibling keys CSV for s12 --siblings (default: <out>.siblings.csv)")
    ap.add_argument("--mode", choices=MODES, default="pool",
                    help="pool: rows with no usable UDI link; udi_check: a sample of rows with one")
    ap.add_argument("--sample-per-10k", type=int, default=100,
                    help="udi_check: rows sampled per 10,000 UDI-linked rows (100 = 1%%)")
    ap.add_argument("--max-gb", type=float, default=10.0)
    ap.add_argument("--print-only", action="store_true", help="print the SQL; no GCP call")
    ap.add_argument("--estimate-only", action="store_true", help="dry run only (free)")
    a = ap.parse_args(argv)

    try:
        sql, sib_sql = create_sql(a.project, a.dataset, a.mode), siblings_sql(a.project, a.dataset, a.mode)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return EXIT_BAD
    if a.print_only:
        print(f"{sql};\n\n-- sibling keys\n{sib_sql};")
        return EXIT_OK
    if a.out is None and not a.estimate_only:
        print("ERROR: --out is required", file=sys.stderr)
        return EXIT_BAD

    if client is None:
        from google.cloud import bigquery
        client = bigquery.Client(project=a.project)
    from google.cloud import bigquery

    if a.full or a.mode == "udi_check":       # the check scores its whole fixed sample every time
        since = None
    elif a.since == "auto":
        since = last_load(client, a.project, a.dataset)
    else:
        since = dt.datetime.fromisoformat(a.since)
    print(f"dataset {a.dataset}; MAUDE rows changed after: {since or 'any time (full)'}")

    max_bytes = int(a.max_gb * 1e9)
    params = query_params(since, a.mode, a.sample_per_10k)
    est = int(client.query(sql, job_config=bigquery.QueryJobConfig(
        dry_run=True, use_query_cache=False, query_parameters=params)).total_bytes_processed or 0)
    print(f"estimated {est / 1e9:.2f} GB")
    if est > max_bytes:
        print(f"ERROR: over --max-gb {a.max_gb}. Nothing was run.", file=sys.stderr)
        return EXIT_BAD
    if a.estimate_only:
        return EXIT_OK

    table = target_table(a.project, a.dataset, a.mode)
    job = client.query(sql, job_config=bigquery.QueryJobConfig(
        query_parameters=params, maximum_bytes_billed=max_bytes))
    job.result()
    print(f"billed {(job.total_bytes_billed or 0) / 1e9:.2f} GB -> {table}")

    # create_bqstorage_client=False keeps this on the free tabledata.list path (as s03)
    df = client.list_rows(table).to_dataframe(create_bqstorage_client=False)
    if df.empty:
        print("no candidate pairs: nothing to score")
        return EXIT_NONE
    a.out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(a.out, index=False)
    print(f"{len(df):,} pairs for {df['maude_id'].nunique():,} MAUDE rows "
          f"(pool: {int(df['meta_n_pool_rows'].iloc[0]):,} rows) -> {a.out}")

    sib_out = a.siblings_out or a.out.with_suffix(".siblings.csv")
    sib_est = int(client.query(sib_sql, job_config=bigquery.QueryJobConfig(
        dry_run=True, use_query_cache=False)).total_bytes_processed or 0)
    print(f"sibling keys: estimated {sib_est / 1e9:.2f} GB")
    if sib_est > max_bytes:
        print(f"ERROR: sibling keys over --max-gb {a.max_gb}. Not run.", file=sys.stderr)
        return EXIT_BAD
    billed = fetch_rows(client, sib_sql, max_bytes, sib_out, ("mfr", "sib_key"))
    print(f"sibling keys: {billed / 1e9:.2f} GB -> {sib_out}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
