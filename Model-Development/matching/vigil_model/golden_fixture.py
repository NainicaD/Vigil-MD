"""Golden fixture: proves Python and BigQuery apply the same rules (H4).

tests/fixtures/normalization_golden.csv holds hand-picked raw inputs and the expected outputs
(keys, match kind, device key, split, pair id). Three checks use it:
  1. pytest (tests/test_golden_fixture.py): same_device_features.py reproduces every expected value.
  2. build_labels.py --fixture: the SAME rules, expanded from the SQL macros, run on BigQuery over the
     same rows. Any difference is printed. It reads no table, so it bills nothing.
  3. The file stores constants_sha256. Change a rule constant and checks 1 and 2 refuse to run until
     you regenerate the file and review the diff.

Cell syntax: empty = NULL, <EMPTY> = empty string, \\uXXXX = that character (control characters and
non-ASCII are always written this way, so the file stays readable and diff-able).

Regenerate after a deliberate rule change (then READ the diff, the values are not self-proving):
  ~/.venvs/vigil-airflow/bin/python Model-Development/matching/scripts/golden_fixture.py --regen
Print the BigQuery SQL to run it by hand in the console (no GCP call):
  ~/.venvs/vigil-analysis/bin/python Model-Development/matching/scripts/golden_fixture.py --print-sql
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vigil_model import bq_utils  # noqa: E402
from vigil_model import same_device_features as feat  # noqa: E402

FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "normalization_golden.csv"
INPUT_COLS = ("case_id", "note", "mfr", "m_model_raw", "m_cat_raw", "g_model_raw", "g_cat_raw",
              "mdr_report_key", "device_seq", "udi_di", "role")
OUTPUT_COLS = ("m_model_key", "m_cat_key", "g_model_key", "g_cat_key", "match_kind", "device_key", "split",
               "pair_id")
EXIT_OK, EXIT_STALE, EXIT_MISMATCH = 0, 2, 4
HASH_PREFIX = "# constants_sha256="
COMMENTS = (
    "# Golden fixture: Python / BigQuery parity (H4). Empty cell = NULL, <EMPTY> = empty string, \\uXXXX = that character.",
    "# Expected columns are written by golden_fixture.py --regen. Review them by hand. Verify on BigQuery: build_labels.py --fixture",
)

log = logging.getLogger("golden_fixture")

# The CSV `mfr` column is GUDID's normalized labeler; the MAUDE side of these cases has no labeler.
SQL = """
WITH raw AS (
  SELECT JSON_VALUE(r, '$.case_id') AS case_id,
         JSON_VALUE(r, '$.mfr') AS mfr,
         JSON_VALUE(r, '$.m_model_raw') AS m_model_raw,
         JSON_VALUE(r, '$.m_cat_raw') AS m_cat_raw,
         JSON_VALUE(r, '$.g_model_raw') AS g_model_raw,
         JSON_VALUE(r, '$.g_cat_raw') AS g_cat_raw,
         JSON_VALUE(r, '$.mdr_report_key') AS mdr_report_key,
         SAFE_CAST(JSON_VALUE(r, '$.device_seq') AS INT64) AS device_seq,
         JSON_VALUE(r, '$.udi_di') AS udi_di,
         JSON_VALUE(r, '$.role') AS role
  FROM UNNEST(JSON_QUERY_ARRAY(r'''__ROWS_JSON__''')) AS r
),
keyed AS (
  SELECT *,
         {{ identifier_key(m_model_raw) }} AS m_model,
         {{ identifier_key(m_cat_raw) }} AS m_cat,
         {{ identifier_key(g_model_raw) }} AS g_model,
         {{ identifier_key(g_cat_raw) }} AS g_cat,
         {{ device_key(mfr, g_model_raw, g_cat_raw) }} AS device_key
  FROM raw
)
SELECT case_id,
       m_model AS m_model_key, m_cat AS m_cat_key, g_model AS g_model_key, g_cat AS g_cat_key,
       {{ match_kind(m, g) }} AS match_kind,
       device_key,
       IF(device_key IS NULL, NULL, {{ split(device_key) }}) AS split,
       {{ pair_id(mdr_report_key, device_seq, udi_di, role) }} AS pair_id
FROM keyed
ORDER BY case_id
"""


# --- cell syntax --------------------------------------------------------------------------------
def decode(cell: str | None) -> str | None:
    if cell is None or cell == "":
        return None
    if cell == "<EMPTY>":
        return ""
    return re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), cell)


def encode(value) -> str:
    if value is None:
        return ""
    if value == "":
        return "<EMPTY>"
    out = []
    for ch in str(value):
        o = ord(ch)
        if o > 0xFFFF:
            raise ValueError(f"character above U+FFFF is not supported in the fixture: {ch!r}")
        out.append(f"\\u{o:04x}" if o < 32 or o > 126 or ch == "\\" else ch)
    return "".join(out)


# --- file ---------------------------------------------------------------------------------------
def load(path: Path = FIXTURE) -> tuple[str | None, list[dict]]:
    """(constants_sha256 stored in the file or None, rows with decoded cells)."""
    lines = path.read_text(encoding="utf-8").splitlines()
    file_hash = next((ln[len(HASH_PREFIX):].strip() for ln in lines if ln.startswith(HASH_PREFIX)), None)
    rows = [{k: decode(v) for k, v in r.items()} for r in csv.DictReader(ln for ln in lines if not ln.startswith("#"))]
    return file_hash, rows


def python_outputs(r: dict) -> dict:
    """The expected output columns for one input row, computed by the shared Python module."""
    maude = feat.DeviceSide(None, None, r["m_model_raw"], r["m_cat_raw"])
    gudid = feat.DeviceSide(r["mfr"], None, r["g_model_raw"], r["g_cat_raw"])
    try:
        key = feat.device_key(gudid)
    except ValueError:                      # no usable identifier: SQL gives NULL, so the fixture expects NULL
        key = None
    seq = None if r["device_seq"] is None else int(r["device_seq"])
    return {
        "m_model_key": feat.identifier_key(r["m_model_raw"]),
        "m_cat_key": feat.identifier_key(r["m_cat_raw"]),
        "g_model_key": feat.identifier_key(r["g_model_raw"]),
        "g_cat_key": feat.identifier_key(r["g_cat_raw"]),
        "match_kind": feat.identifier_match(maude, gudid).value,
        "device_key": key,
        "split": None if key is None else feat.split_of(key),
        "pair_id": feat.pair_id(r["mdr_report_key"], seq, r["udi_di"], r["role"]),
    }


def write(rows: list[dict], path: Path = FIXTURE) -> None:
    """Write the inputs of `rows` plus Python's outputs, with the constants hash on top."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        f.write("\n".join(COMMENTS) + "\n" + HASH_PREFIX + feat.constants_sha256() + "\n")
        w = csv.writer(f, lineterminator="\n")
        w.writerow(INPUT_COLS + OUTPUT_COLS)
        for r in rows:
            out = python_outputs(r)
            w.writerow([encode(r[c]) for c in INPUT_COLS] + [encode(out[c]) for c in OUTPUT_COLS])


# --- BigQuery -----------------------------------------------------------------------------------
def fixture_sql(rows: list[dict]) -> str:
    """Runnable SQL: the macros expanded, the input rows embedded as one raw JSON string."""
    payload = json.dumps([{c: (int(r[c]) if c == "device_seq" and r[c] is not None else r[c])
                           for c in INPUT_COLS if c != "note"} for r in rows])
    if "'''" in payload:
        raise ValueError("fixture value contains three single quotes")
    return bq_utils.expand_macros(SQL, feat.SQL_MACROS).replace("__ROWS_JSON__", payload)


def mismatches(rows: list[dict], got: dict[str, dict]) -> list[str]:
    bad = []
    for r in rows:
        g = got.get(r["case_id"])
        if g is None:
            bad.append(f"{r['case_id']}: BigQuery returned no row")
            continue
        for c in OUTPUT_COLS:
            if g.get(c) != r[c]:
                bad.append(f"{r['case_id']} {c}: expected {r[c]!r}, BigQuery gave {g.get(c)!r}  ({r['note']})")
    return bad


def check_bigquery(client, path: Path = FIXTURE, max_bytes: int = int(1e8)) -> int:
    """Run the fixture on BigQuery and compare. 0 = same, 2 = stale or unreadable file, 4 = differences."""
    from google.cloud import bigquery
    file_hash, rows = load(path)
    if file_hash != feat.constants_sha256() or not rows or any(c not in rows[0] for c in OUTPUT_COLS):
        log.error("%s is stale (constants changed or no expected columns): run golden_fixture.py --regen "
                  "and review the diff", path.name)
        return EXIT_STALE
    job = client.query(fixture_sql(rows), job_config=bigquery.QueryJobConfig(maximum_bytes_billed=max_bytes))
    got = {}
    for r in job.result():
        d = dict(r.items())
        got[d["case_id"]] = d
    bad = mismatches(rows, got)
    for line in bad:
        log.error("MISMATCH %s", line)
    log.info("fixture: %d cases, %d differences", len(rows), len(bad))
    return EXIT_MISMATCH if bad else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--regen", action="store_true", help="rewrite the expected columns from the Python module")
    ap.add_argument("--print-sql", action="store_true", help="print the BigQuery SQL (no GCP call)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    _, rows = load()
    if args.regen:
        write(rows)
        log.info("wrote %d cases to %s (constants_sha256 %s): review the diff", len(rows), FIXTURE,
                 feat.constants_sha256()[:12])
    elif args.print_sql:
        print(fixture_sql(rows))
    else:
        ap.print_help()
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
