"""Draw the hand-label sample and write the labeling sheet (Day 1, N5).

Three read-only queries (vigil_md_prod plus vigil_md_model.device_split and alias_table):
  sql/hand_labels/01_cells.sql       rows of the test-split no-UDI frame per cell (stratum)
  sql/hand_labels/02_draw.sql        the rows drawn from each cell, in a fixed order (salt)
  sql/hand_labels/03_candidates.sql  up to 3 ranked test-split GUDID candidates per drawn row

The allocation is done here: --hard-share of --n goes to the hard strata (one per hard-case reason),
split evenly between them; the rest goes to the easy strata (one per cell), in proportion to their
size with a floor per cell. meta_sample_weight = rows in the stratum / rows drawn from it, so any
rate can be weighted back to the frame.

Output (committed: small, public FDA data, must be diffable):
  labels/hand_label_sample_v<batch>.csv            1 + k rows per MAUDE row: cand_rank 0 is
                                                    "none of these is the same device"
  labels/hand_label_sample_v<batch>.manifest.json  parameters, cells, quotas, weights, bytes

Every query is dry-run first; if one is above --max-gb nothing runs (exit 3). Exit 2 means broken
SQL or config, including an existing sheet without --overwrite. Each real job sets
maximum_bytes_billed. Claude runs only --print-only and pytest; a person runs the rest.

Usage (from Model-Development/):
  $PY scripts/make_hand_label_sheet.py --project vigil-md-1792696816 --print-only
  $PY scripts/make_hand_label_sheet.py --project vigil-md-1792696816 --estimate-only
  $PY scripts/make_hand_label_sheet.py --project vigil-md-1792696816
  # batch 2: new salt, leave out batch 1's rows
  $PY scripts/make_hand_label_sheet.py --project vigil-md-1792696816 --batch 2 --salt v2 \
      --exclude labels/hand_label_sample_v1.csv
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import logging
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bq_utils  # noqa: E402
import same_device_features as feat  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SQL_DIR = ROOT / "sql" / "hand_labels"
LABELS_DIR = ROOT / "labels"
QUERY_IDS = ("01_cells", "02_draw", "03_candidates")
EXIT_OK, EXIT_CONFIG, EXIT_OVER_BUDGET, EXIT_DATA = 0, 2, 3, 3   # 3 = data problem, no retry
ROLE = "hand"                    # pair_id role for hand-labeled pairs
NONE_DI = "NONE"                 # pair_id stand-in DI for the cand_rank 0 row
ROW_KEY_FIELDS = ("maude_row_id", "m_mfr", "m_model", "m_cat", "m_name", "m_model_raw", "m_catalog_raw")

COLUMNS = (
    # identity
    "pair_id", "maude_row_id", "cand_rank", "sample_batch", "sample_salt",
    # strata
    "stratum_id", "strat_cand_bucket", "strat_mfr_size", "strat_udi_status", "strat_model_usable",
    "strat_year", "strat_split_source", "is_hard_case", "hard_case_reason", "meta_sample_weight",
    # MAUDE side
    "mdr_report_key", "device_seq", "maude_manufacturer_raw", "maude_manufacturer_norm",
    "maude_device_name", "maude_model_raw", "maude_model_key", "maude_catalog_raw",
    "maude_catalog_key", "maude_product_code", "maude_date", "maude_udi_di_raw",
    # candidate side
    "cand_udi_di", "cand_labeler", "cand_labeler_raw", "cand_device_name", "cand_model",
    "cand_catalog", "cand_product_codes", "cand_n_dis_for_model", "cand_is_distinctive",
    "cand_match_kind", "cand_has_suffix_sibling", "cand_raw_identifier_equal", "cand_rule", "cand_split",
    # blank, for the labeler
    "label", "labeler", "labeled_at", "confidence", "rule_cited", "notes",
    # convenience
    "openfda_url", "accessgudid_url",
)
LABELER_COLUMNS = ("label", "labeler", "labeled_at", "confidence", "rule_cited", "notes")
RULE_NAMES = {0: "none_of_these", 1: "id_same_labeler_or_alias", 2: "id_other_labeler",
              3: "id_contains_same_labeler", 4: "name_same_labeler"}

log = logging.getLogger("make_hand_label_sheet")


# ---------------------------------------------------------------------------- SQL

def load_sql(project: str) -> dict[str, str]:
    files = {qf.qid: qf for qf in bq_utils.load_query_dir(SQL_DIR)}
    missing = set(QUERY_IDS) - set(files)
    if missing:
        raise FileNotFoundError(f"missing SQL files in {SQL_DIR}: {sorted(missing)}")
    out = {}
    for qid in QUERY_IDS:
        qf = files[qid]
        if len(qf.parts) != 1:
            raise ValueError(f"{qf.path.name}: expected one query, found {len(qf.parts)} parts")
        sql = qf.parts[0][1].replace("__MODEL_DATASET__", f"{project}.{bq_utils.MODEL_DATASET}")
        sql = bq_utils.render(sql, project, macros=feat.SQL_MACROS)
        bq_utils.assert_select_prod_model(sql)
        out[qid] = sql
    return out


def params_for(qid: str, *, salt: str, quotas: dict[str, int] | None = None,
               exclude: list[str] | None = None, rows: list[dict] | None = None, max_cands: int = 3):
    from google.cloud import bigquery
    S, A, St = bigquery.ScalarQueryParameter, bigquery.ArrayQueryParameter, bigquery.StructQueryParameter
    if qid == "01_cells":
        return []
    if qid == "02_draw":
        structs = [St(None, S("stratum_id", "STRING", k), S("n", "INT64", v)) for k, v in sorted(quotas.items())]
        return [A("quotas", "STRUCT", structs), A("exclude_row_ids", "STRING", sorted(exclude or [])),
                S("salt", "STRING", salt)]
    structs = [St(None, *[S(f, "STRING", None if r.get(f) is None else str(r[f])) for f in ROW_KEY_FIELDS])
               for r in rows]
    return [A("rows", "STRUCT", structs), S("salt", "STRING", salt), S("max_cands", "INT64", max_cands)]


# Stand-ins for the dry runs of 02 and 03 before 01 has run. Scan size does not depend on them.
DRY_QUOTAS = {"cand_0|top_10|easy": 1}
DRY_ROWS = [{"maude_row_id": "dryrun", "m_mfr": "x", "m_model": "X1", "m_cat": None, "m_name": "x",
             "m_model_raw": "X1", "m_catalog_raw": None}]


def dry_run_bytes(client, sql: str, params) -> int:
    from google.cloud import bigquery
    job = client.query(sql, job_config=bigquery.QueryJobConfig(
        dry_run=True, use_query_cache=False, query_parameters=params))
    return int(job.total_bytes_processed or 0)


def run_query(client, sql: str, params, max_bytes: int) -> tuple[list[dict], int]:
    from google.cloud import bigquery
    job = client.query(sql, job_config=bigquery.QueryJobConfig(
        query_parameters=params, maximum_bytes_billed=max_bytes))
    rows = [dict(r.items()) for r in job.result()]
    return rows, int(job.total_bytes_processed or 0)


# ---------------------------------------------------------------------------- allocation

def _allocate_group(cells: dict[str, int], n: int, floor: int) -> dict[str, int]:
    """Floor first (never above a cell's size), then the rest proportional to cell size by largest
    remainder, never above a cell's size. Deterministic: ties go by stratum_id."""
    quota = {k: min(floor, size) for k, size in cells.items()}
    left = n - sum(quota.values())
    if left < 0:
        raise ValueError(f"the floor of {floor} per cell needs {sum(quota.values())} rows, more than {n}")
    while left > 0:
        room = {k: cells[k] - quota[k] for k in cells if cells[k] > quota[k]}
        if not room:
            break                                   # the group has fewer rows than asked for
        total = sum(cells[k] for k in room)
        exact = {k: left * cells[k] / total for k in room}
        add = {k: min(int(exact[k]), room[k]) for k in room}
        rest = left - sum(add.values())
        for k in sorted(room, key=lambda k: (-(exact[k] - int(exact[k])), k)):
            if rest == 0:
                break
            if add[k] < room[k]:
                add[k] += 1
                rest -= 1
        given = sum(add.values())
        if given == 0:
            break
        for k, a in add.items():
            quota[k] += a
        left -= given
    return {k: v for k, v in quota.items() if v > 0}


def _allocate_equal(cells: dict[str, int], n: int) -> dict[str, int]:
    """Round-robin one row at a time over the strata in stratum_id order, skipping a stratum once it is
    full. Every reason gets the same number of rows, unless it has fewer rows than that."""
    quota = {k: 0 for k in cells}
    left = n
    while left > 0:
        still_open = sorted(k for k in cells if quota[k] < cells[k])
        if not still_open:
            break
        for k in still_open:
            if left == 0:
                break
            quota[k] += 1
            left -= 1
    return {k: v for k, v in quota.items() if v > 0}


def allocate(cell_sizes: dict[str, int], n: int, hard_share: float, floor: int) -> dict[str, int]:
    """Split n between hard strata (stratum_id ends in |hard: one per reason, allocated evenly) and
    easy strata (ends in |easy: one per cell, proportional with a floor). If the hard strata have
    fewer rows than their share, the easy strata fill the total."""
    hard = {k: v for k, v in cell_sizes.items() if k.endswith("|hard") and v > 0}
    easy = {k: v for k, v in cell_sizes.items() if k.endswith("|easy") and v > 0}
    if set(cell_sizes) - set(hard) - set(easy) - {k for k, v in cell_sizes.items() if v <= 0}:
        raise ValueError("every stratum_id must end in |hard or |easy")
    n_hard = min(round(n * hard_share), sum(hard.values()))
    n_easy = min(n - n_hard, sum(easy.values()))
    return {**_allocate_equal(hard, n_hard), **_allocate_group(easy, n_easy, floor)}


def weights(cell_sizes: dict[str, int], drawn: Counter) -> dict[str, float]:
    """Inverse inclusion probability per cell: rows in the cell / rows drawn from it."""
    return {k: cell_sizes[k] / drawn[k] for k in drawn if drawn[k] > 0}


# ---------------------------------------------------------------------------- sheet

def maude_row_id(mdr_report_key: str, device_seq) -> str:
    """Same as the SQL: first 16 hex of sha256('mdr|device_seq'), '' for a missing device_seq (E11)."""
    seq = "" if device_seq is None or device_seq == "" else str(device_seq)
    return hashlib.sha256(f"{mdr_report_key}|{seq}".encode()).hexdigest()[:16]


def _seq(v):
    return None if v is None or v == "" else int(v)


def build_sheet(drawn: list[dict], cands: list[dict], cell_weights: dict[str, float],
                batch: int, salt: str) -> list[dict]:
    by_row: dict[str, list[dict]] = {}
    for c in cands:
        by_row.setdefault(c["maude_row_id"], []).append(c)
    out = []
    for d in sorted(drawn, key=lambda d: (d["stratum_id"], d["maude_row_id"])):
        mdr, seq = str(d["mdr_report_key"]), _seq(d.get("device_seq"))
        date = d.get("m_date")
        base = {
            "maude_row_id": d["maude_row_id"], "sample_batch": batch, "sample_salt": salt,
            "stratum_id": d["stratum_id"], "strat_cand_bucket": d["cand_bucket"],
            "strat_mfr_size": d["mfr_size"], "strat_udi_status": d["udi_status"],
            "strat_model_usable": d["model_usable"], "strat_year": str(date)[:4] if date else "",
            "strat_split_source": d["split_source"], "is_hard_case": d["is_hard"],
            "hard_case_reason": d.get("hard_case_reason") or "",
            "meta_sample_weight": round(cell_weights[d["stratum_id"]], 6),
            "mdr_report_key": mdr, "device_seq": "" if seq is None else seq,
            "maude_manufacturer_raw": d.get("m_mfr_raw"), "maude_manufacturer_norm": d.get("m_mfr"),
            "maude_device_name": d.get("m_name"), "maude_model_raw": d.get("m_model_raw"),
            "maude_model_key": d.get("m_model"), "maude_catalog_raw": d.get("m_catalog_raw"),
            "maude_catalog_key": d.get("m_cat"), "maude_product_code": d.get("m_pc"),
            "maude_date": date, "maude_udi_di_raw": d.get("m_udi_di_raw"),
            "openfda_url": f"https://api.fda.gov/device/event.json?search=mdr_report_key:{mdr}",
        }
        none_row = {**base, "pair_id": feat.pair_id(mdr, seq, NONE_DI, ROLE), "cand_rank": 0,
                    "cand_rule": RULE_NAMES[0], "cand_device_name": "(none of these is the same device)",
                    "accessgudid_url": ""}
        out.append(none_row)
        for c in sorted(by_row.get(d["maude_row_id"], []), key=lambda c: int(c["cand_rank"])):
            out.append({**base, **{k: v for k, v in c.items() if k.startswith("cand_")},
                        "pair_id": feat.pair_id(mdr, seq, c["cand_udi_di"], ROLE),
                        "cand_rank": int(c["cand_rank"]),
                        "cand_rule": RULE_NAMES.get(int(c["cand_rule"]), str(c["cand_rule"])),
                        "accessgudid_url": f"https://accessgudid.nlm.nih.gov/devices/{c['cand_udi_di']}"})
    return [{col: _cell(r.get(col)) for col in COLUMNS} for r in out]


def _cell(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    return v


def check_sheet(rows: list[dict]) -> None:
    """The guarantees the sheet must keep. Raises ValueError: a broken sheet is never written."""
    ids = [r["pair_id"] for r in rows]
    dup = [k for k, n in Counter(ids).items() if n > 1]
    if dup:
        raise ValueError(f"duplicate pair_id: {dup[:5]}")
    per_row = Counter(r["maude_row_id"] for r in rows if r["cand_rank"] == 0)
    if any(n != 1 for n in per_row.values()) or len(per_row) != len({r["maude_row_id"] for r in rows}):
        raise ValueError("every MAUDE row needs exactly one cand_rank 0 row")
    if any(r[c] != "" for r in rows for c in LABELER_COLUMNS):
        raise ValueError("labeler columns must start blank")
    if any(str(c).startswith("feat_") for c in COLUMNS):
        raise ValueError("the sheet must carry no feat_* column")


def read_exclusions(paths: list[Path]) -> list[str]:
    ids = set()
    for p in paths:
        with p.open(newline="", encoding="utf-8") as f:
            ids |= {r["maude_row_id"] for r in csv.DictReader(f)}
    return sorted(ids)


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(COLUMNS), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------------------- main

def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--project", required=True)
    p.add_argument("--n", type=int, default=150, help="MAUDE rows to draw")
    p.add_argument("--hard-share", type=float, default=0.35, help="share of rows from hard cells")
    p.add_argument("--floor", type=int, default=2, help="minimum rows per non-empty cell")
    p.add_argument("--max-cands", type=int, default=3, help="candidates per row (plus the none row)")
    p.add_argument("--batch", type=int, default=1)
    p.add_argument("--salt", default="v1")
    p.add_argument("--exclude", type=Path, nargs="*", default=[], help="earlier batch sheets to leave out")
    p.add_argument("--max-gb", type=float, default=10.0, help="per-query scan limit")
    p.add_argument("--print-only", action="store_true", help="print the SQL, no GCP call")
    p.add_argument("--estimate-only", action="store_true", help="dry runs only (free)")
    p.add_argument("--overwrite", action="store_true", help="replace this batch's sheet (only before labeling)")
    return p.parse_args(argv)


def main(argv=None, client=None, labels_dir: Path = LABELS_DIR) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args(argv)
    if args.n < 1 or not 0 <= args.hard_share <= 1 or args.floor < 0 or args.max_cands < 1:
        log.error("--n and --max-cands must be positive, --hard-share in [0, 1], --floor >= 0")
        return EXIT_CONFIG
    try:
        sql = load_sql(args.project)
    except (ValueError, FileNotFoundError) as e:
        log.error("%s", e)
        return EXIT_CONFIG
    if args.print_only:
        print("\n\n".join(f"-- ===== {qid} =====\n{s}" for qid, s in sql.items()))
        return EXIT_OK

    out_csv = labels_dir / f"hand_label_sample_v{args.batch}.csv"
    out_manifest = labels_dir / f"hand_label_sample_v{args.batch}.manifest.json"
    if not args.estimate_only and out_csv.exists() and not args.overwrite:
        log.error("%s exists. Use a new --batch, or --overwrite only if nobody has started labeling it.",
                  out_csv)
        return EXIT_CONFIG
    try:
        exclude = read_exclusions(args.exclude)
    except (OSError, KeyError) as e:
        log.error("cannot read --exclude: %s", e)
        return EXIT_CONFIG

    if client is None:
        from google.cloud import bigquery
        client = bigquery.Client(project=args.project)
    max_bytes = int(args.max_gb * 1e9)
    dry = {"01_cells": params_for("01_cells", salt=args.salt),
           "02_draw": params_for("02_draw", salt=args.salt, quotas=DRY_QUOTAS, exclude=exclude),
           "03_candidates": params_for("03_candidates", salt=args.salt, rows=DRY_ROWS, max_cands=args.max_cands)}
    est = {}
    for qid in QUERY_IDS:
        try:
            est[qid] = dry_run_bytes(client, sql[qid], dry[qid])
        except Exception as e:  # a compile error comes back from the dry run
            log.error("dry run of %s failed: %s", qid, getattr(e, "message", None) or e)
            return EXIT_CONFIG
        log.info("%s: estimated %.2f GB", qid, est[qid] / 1e9)
    over = [q for q, b in est.items() if b > max_bytes]
    if over:
        log.error("over --max-gb %.1f: %s. Nothing was run.", args.max_gb, over)
        return EXIT_OVER_BUDGET
    if args.estimate_only:
        return EXIT_OK

    done = {}
    cells, done["01_cells"] = run_query(client, sql["01_cells"], dry["01_cells"], max_bytes)
    repeated = [k for k, v in Counter(c["stratum_id"] for c in cells).items() if v > 1]
    if repeated:
        log.error("01_cells returned a stratum more than once (its size would be undercounted): %s", repeated)
        return EXIT_CONFIG
    sizes = {c["stratum_id"]: int(c["n_rows"]) for c in cells}
    if not sizes:
        log.error("the frame is empty: no test-split no-UDI rows")
        return EXIT_CONFIG
    try:
        quotas = allocate(sizes, args.n, args.hard_share, args.floor)
    except ValueError as e:
        log.error("allocation: %s (lower --floor or raise --n)", e)
        return EXIT_CONFIG
    log.info("frame: %d rows in %d cells; drawing %d (%d hard)", sum(sizes.values()), len(sizes),
             sum(quotas.values()), sum(v for k, v in quotas.items() if k.endswith("|hard")))

    drawn, done["02_draw"] = run_query(
        client, sql["02_draw"], params_for("02_draw", salt=args.salt, quotas=quotas, exclude=exclude), max_bytes)
    got = Counter(d["stratum_id"] for d in drawn)
    short = {k: (got[k], q) for k, q in quotas.items() if got[k] != q}
    if short:
        log.warning("cells drawn short of their quota (got, asked): %s", short)
    cands, done["03_candidates"] = run_query(
        client, sql["03_candidates"],
        params_for("03_candidates", salt=args.salt, rows=drawn, max_cands=args.max_cands), max_bytes)

    cell_weights = weights(sizes, got)
    rows = build_sheet(drawn, cands, cell_weights, args.batch, args.salt)
    try:
        check_sheet(rows)
    except ValueError as e:
        log.error("sheet failed its checks, nothing written: %s", e)
        return EXIT_DATA
    write_csv(out_csv, rows)
    n_cands = Counter(c["maude_row_id"] for c in cands)
    manifest = {
        "run_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "project": args.project, "batch": args.batch, "salt": args.salt,
        "params": {"n": args.n, "hard_share": args.hard_share, "floor": args.floor,
                   "max_cands": args.max_cands, "excluded_rows": len(exclude)},
        "constants_sha256": hashlib.sha256(json.dumps(feat.constants(), sort_keys=True).encode()).hexdigest(),
        "query_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in sorted(SQL_DIR.glob("*.sql"))},
        "bytes_processed": done,
        "frame_rows": sum(sizes.values()),
        "maude_rows_drawn": len(drawn),
        "sheet_rows": len(rows),
        "rows_with_no_candidate": sum(1 for d in drawn if n_cands[d["maude_row_id"]] == 0),
        "hard_case_reasons": dict(Counter(d.get("hard_case_reason") or "none" for d in drawn)),
        "cells": {k: {"frame_rows": sizes[k], "quota": quotas.get(k, 0), "drawn": got[k],
                      "weight": round(cell_weights.get(k, 0.0), 6)} for k in sorted(sizes)},
    }
    out_manifest.write_text(json.dumps(manifest, indent=2, default=str) + "\n", encoding="utf-8")
    log.info("wrote %s (%d MAUDE rows, %d sheet rows) and %s", out_csv, len(drawn), len(rows), out_manifest.name)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
