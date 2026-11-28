"""Build the candidate pairs that s12_score_pairs.py scores, and save them as parquet.

Runs sql/scoring/01_candidates.sql on one dataset (default vigil_md_dev, the test extract):
MAUDE rows with no usable UDI link that changed after --since, each with the GUDID model groups
that the test-set candidate rules propose. Writes vigil_md_model.match_candidates_<dataset>
(the only dataset this folder writes to), then streams it into --out batch by batch with the
BigQuery Storage Read API (memory: one batch; prod has ~37 M pairs).

--since auto (default): MAX(built_at) of <dataset>.link_maude_gudid_model, the last load of model
links. No such table: every row. --full: every row (backfill, new model version).
--model-version (bundle version + gate): with --since auto, a link table whose label
model_version differs (written by load_model_links.py) means every row, so links made by two
versions never mix. A run of every pool row writes <out folder>/full.flag; the DAG then gives
the load step --full.

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


UDI_CHECK_ONLY = "-- udi_check only"      # SQL lines kept only in mode udi_check


def select_sql(project: str, dataset: str, mode: str = "pool") -> str:
    """The candidate SELECT on one dataset, checked to read only that dataset. Pure.
    Lines marked UDI_CHECK_ONLY (the event type, a slice of s14) are dropped in mode 'pool', so
    the daily run's query, its cost and its table's columns are as before."""
    check_dataset(dataset)
    sql = bq_utils.load_query_file(SQL_FILE).parts[0][1]
    if mode != "udi_check":
        sql = "\n".join(line for line in sql.split("\n") if UDI_CHECK_ONLY not in line)
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
    return (f"CREATE OR REPLACE TABLE `{target_table(project, dataset, mode)}` AS\n"
            f"{select_sql(project, dataset, mode)}")


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


def model_label(model_version: str) -> str:
    """A BigQuery label value (lowercase letters, digits, - and _; at most 63 characters).
    Same rule as Data-Pipeline load_model_links.model_label: change both together."""
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in model_version.lower())[:63]


def last_load(client, project: str, dataset: str, model_version: str | None = None):
    """(since, reason). since: when model links were last loaded into this dataset, or None to
    score every row: no link table yet, or its links were made by another bundle/gate (the table
    label model_version, written by load_model_links.py), so old and new links never mix."""
    from google.api_core.exceptions import NotFound
    table = f"{project}.{dataset}.{LINK_TABLE}"
    try:
        labels = client.get_table(table).labels or {}
    except NotFound:
        return None, "no model links yet"
    if model_version and labels.get("model_version") != model_label(model_version):
        return None, (f"links were made by {labels.get('model_version') or 'an unknown version'}, "
                      f"this run uses {model_label(model_version)}")
    rows = list(client.query(f"SELECT MAX(built_at) AS t FROM `{table}`").result())
    return (rows[0]["t"] if rows else None), "changed rows only"


def download(client, table: str, out: Path, bqstorage_client=None) -> tuple[int, int | None]:
    """Stream the candidate table into a parquet file batch by batch (BigQuery Storage Read API,
    about $1.1 per TiB): memory stays at one batch, whatever the number of pairs (prod ~37 M).
    Returns (pairs, meta_n_pool_rows)."""
    import pyarrow.parquet as pq
    if bqstorage_client is None:
        from google.cloud import bigquery_storage
        bqstorage_client = bigquery_storage.BigQueryReadClient()
    out.parent.mkdir(parents=True, exist_ok=True)
    writer, n, pool = None, 0, None
    try:
        for batch in client.list_rows(table).to_arrow_iterable(bqstorage_client=bqstorage_client):
            if batch.num_rows == 0:
                continue
            if writer is None:
                writer = pq.ParquetWriter(out, batch.schema)
                pool = int(batch.column(batch.schema.get_field_index("meta_n_pool_rows"))[0].as_py())
            writer.write_batch(batch)
            n += batch.num_rows
    finally:
        if writer is not None:
            writer.close()
    return n, pool


def main(argv=None, client=None, bqstorage_client=None) -> int:
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
    ap.add_argument("--model-version",
                    help="bundle version + gate (e.g. v1.1.0+e6_e3b_c1_m1): with --since auto, a "
                         "link table made by another version means a full run")
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
        since, reason = None, "--full" if a.full else "udi_check sample"
    elif a.since == "auto":
        since, reason = last_load(client, a.project, a.dataset, a.model_version)
    else:
        since, reason = dt.datetime.fromisoformat(a.since), "--since"
    print(f"dataset {a.dataset}; MAUDE rows changed after: {since or 'any time (full)'} ({reason})")

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

    if not client.get_table(table).num_rows:            # table metadata: no cost
        print("no candidate pairs: nothing to score")
        return EXIT_NONE
    n_pairs, pool = download(client, table, a.out, bqstorage_client)
    print(f"{n_pairs:,} pairs (pool: {pool:,} MAUDE rows) -> {a.out}")
    # The load step must replace every link (--full) when this run scored every row; the DAG
    # reads this file. A rerun into the same folder that is not full removes an old flag.
    flag = a.out.parent / "full.flag"
    if since is None and a.mode == "pool":
        flag.write_text(reason + "\n")
    elif flag.exists():
        flag.unlink()

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
