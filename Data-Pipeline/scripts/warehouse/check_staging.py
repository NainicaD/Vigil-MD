"""Check one day of STAGING before it is merged into PROD.

Read-only: it never writes. It reads the raw and staging partitions of one
ingest date (partition pruning), compares them with the thresholds in
scripts/warehouse/check_config.py, and prints one line per check:

  PASS   within limits
  WARN   outside the warn limit (logged, the pipeline continues)
  FAIL   outside the fail limit (exit code 3 = data failure, same as
         run_validation.py; the merge task must not run and is not retried)

Checks:
  raw_to_staged_gap   rows lost between raw and staging (null keys, duplicates);
                      zero staged rows when raw has rows is always a FAIL
  volume              staged rows against the previous load of the same source
  null_rate:<col>     percent NULL or blank, against the baseline
  dates               future or pre-1970 dates
  product_codes       values that are not three capital letters (if configured)
  allowed_values      new values in category columns
  mix_shift           share of each category against the baseline
  text_length         values longer than the configured maximum
  whitespace_only     values that are only spaces

A day with nothing loaded for the source (no raw rows) exits 0: most daily
runs are no-ops. A source with no entry in check_config.py is skipped with a
warning.

Usage:
    python scripts/warehouse/check_staging.py \
        --project vigil-md-1792696816 --source enforcement --ingest-date 2026-10-26

    # Print the SQL only. No GCP calls:
    python scripts/warehouse/check_staging.py ... --print-only

    # Report FAILs without failing (first loads, while thresholds are tuned):
    python scripts/warehouse/check_staging.py ... --warn-only
"""
from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.logger import get_logger
from scripts.warehouse import check_config, schemas
log = get_logger("check_staging")

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
# Same convention as scripts/validation/run_validation.py: 3 = a check failed
# (a data failure, not worth retrying). Any other non-zero code is an unexpected error.
EXIT_OK, EXIT_DATA_FAIL = 0, 3
# Values that stand in for "missing" without being NULL (case-insensitive)
PLACEHOLDER_REGEX = r"(?i)^(n/?a|none|null|unknown|unk|tbd|-+)$"
ALL_PARTITIONS = "_ingest_date >= DATE '1900-01-01'"   # keeps the partition filter satisfied


@dataclass(frozen=True)
class Result:
    check: str
    status: str          # PASS, WARN or FAIL
    value: str           # what was measured
    limit: str           # the limit it was compared with
    note: str = ""


# ── Config validation (pure) ──────────────────────────────────────────────────

def _ident(name: str) -> str:
    """Column names go into SQL text, so allow plain identifiers only."""
    if not IDENT.match(name):
        raise ValueError(f"not a plain column name: {name!r}")
    return name


def category_columns(cfg: dict) -> list[str]:
    cols = list(cfg.get("allowed_values", {})) + list(cfg.get("mix_baseline_pct", {}))
    return list(dict.fromkeys(cols))


def validate_config(source: str, cfg: dict) -> None:
    """Raise unless every configured column exists in the staging schema."""
    known = {n for n, _, _ in schemas.FLAT_COLUMNS[source]}
    date_col = cfg["date_column"]
    if date_col not in cfg["null_baseline_pct"]:
        raise ValueError(f"{source}: date_column {date_col} needs a null_baseline_pct entry")
    used = set(cfg["null_baseline_pct"]) | set(category_columns(cfg)) \
        | set(cfg.get("text_max_length", {})) | set(cfg.get("placeholder_baseline_pct", {})) \
        | {date_col}
    pc = cfg.get("product_code")
    if pc:
        if pc.get("kind") not in ("scalar", "array"):
            raise ValueError(f"{source}: product_code.kind must be 'scalar' or 'array'")
        used.add(pc["column"])
    unknown = sorted(used - known)
    if unknown:
        raise ValueError(f"{source}: config uses columns not in schemas.py: {unknown}")
    for col in used:
        _ident(col)


# ── SQL (pure) ────────────────────────────────────────────────────────────────

def render_counts_sql(raw: str, stg: str, expected_expr: str | None = None) -> str:
    """Raw and staged row counts for the ingest date.

    A source whose staging changes the row count (MAUDE: one row per device)
    passes expected_expr, a SQL expression summed over the raw rows that gives
    the number of rows staging should produce.
    """
    expected = ""
    if expected_expr:
        expected = (f",\n  (SELECT IFNULL(SUM({expected_expr}), 0) FROM `{raw}` "
                    "WHERE _ingest_date = @ingest_date) AS expected_staged_rows")
    return f"""\
SELECT
  (SELECT COUNT(*) FROM `{raw}` WHERE _ingest_date = @ingest_date) AS raw_rows,
  (SELECT COUNT(*) FROM `{stg}` WHERE _ingest_date = @ingest_date) AS staged_rows{expected}"""


def render_previous_sql(stg: str) -> str:
    """The most recent earlier load of the source (rows in its staging partition)."""
    return f"""\
SELECT _ingest_date AS ingest_date, COUNT(*) AS rows_total
FROM `{stg}`
WHERE {ALL_PARTITIONS} AND _ingest_date < @ingest_date
GROUP BY _ingest_date
ORDER BY _ingest_date DESC
LIMIT 1"""


def render_profile_sql(stg: str, cfg: dict) -> str:
    """One scan of the day's staging partition: nulls, dates, codes, lengths."""
    date_col = _ident(cfg["date_column"])
    parts = ["COUNT(*) AS rows_total"]
    for col in cfg["null_baseline_pct"]:
        col = _ident(col)
        if col == date_col:
            continue    # a DATE column: TRIM would fail, handled below with IS NULL
        parts.append(f"COUNTIF({col} IS NULL OR TRIM({col}) = '') AS miss__{col}")
        parts.append(f"COUNTIF({col} IS NOT NULL AND TRIM({col}) = '') AS ws__{col}")
    parts += [
        f"COUNTIF({date_col} IS NULL) AS miss__{date_col}",
        f"COUNTIF({date_col} > CURRENT_DATE()) AS future_dates",
        f"COUNTIF({date_col} < DATE '1970-01-01') AS before_1970",
    ]
    pc = cfg.get("product_code")
    if pc:
        col = _ident(pc["column"])
        if pc["kind"] == "array":
            parts.append(
                f"COUNTIF(EXISTS(SELECT 1 FROM UNNEST({col}) AS pc "
                "WHERE NOT REGEXP_CONTAINS(pc, r'^[A-Z]{3}$'))) AS bad_product_codes")
            parts.append(f"COUNTIF(ARRAY_LENGTH({col}) = 0) AS empty_product_codes")
        else:
            parts.append(
                f"COUNTIF({col} IS NOT NULL AND "
                f"NOT REGEXP_CONTAINS({col}, r'^[A-Z]{{3}}$')) AS bad_product_codes")
    for col in cfg.get("placeholder_baseline_pct", {}):
        col = _ident(col)
        parts.append(
            f"COUNTIF(REGEXP_CONTAINS(TRIM({col}), r'{PLACEHOLDER_REGEX}')) AS ph__{col}")
    for col in cfg.get("text_max_length", {}):
        col = _ident(col)
        parts.append(f"MAX(LENGTH({col})) AS maxlen__{col}")
    select = ",\n  ".join(parts)
    return f"""\
SELECT
  {select}
FROM `{stg}`
WHERE _ingest_date = @ingest_date"""


def render_category_sql(stg: str, column: str) -> str:
    column = _ident(column)
    return f"""\
SELECT {column} AS value, COUNT(*) AS n
FROM `{stg}`
WHERE _ingest_date = @ingest_date
GROUP BY value"""


# ── Evaluation (pure) ─────────────────────────────────────────────────────────

def _pct(part: float, whole: float) -> float:
    return 100.0 * part / whole if whole else 0.0


def _rate_status(over_fail: bool, over_warn: bool, small: bool) -> tuple[str, str]:
    """Status for a rate check. Small batches never FAIL on a rate."""
    if over_fail:
        return (WARN, "small batch: FAIL downgraded to WARN") if small else (FAIL, "")
    return (WARN, "") if over_warn else (PASS, "")


def evaluate_gap(cfg: dict, raw_rows: int, staged_rows: int) -> list[Result]:
    g = cfg.get("gap")
    if not g:
        return []
    limit = f"warn >{g['warn_pct']}%, fail >{g['fail_pct']}%"
    if raw_rows > 0 and staged_rows == 0:
        return [Result("raw_to_staged_gap", FAIL, f"0 of {raw_rows} rows staged", limit,
                       "every raw row was dropped (NULL keys?); the merge would do nothing")]
    if staged_rows > raw_rows:
        return [Result("raw_to_staged_gap", WARN, f"{staged_rows} staged > {raw_rows} raw", limit,
                       "more staged rows than raw rows")]
    lost = raw_rows - staged_rows
    gap = _pct(lost, raw_rows)
    status = FAIL if gap > g["fail_pct"] else WARN if gap > g["warn_pct"] else PASS
    return [Result("raw_to_staged_gap", status, f"{lost} rows ({gap:.3f}%)", limit)]


def evaluate_volume(cfg: dict, staged_rows: int, previous: dict | None) -> list[Result]:
    v = cfg.get("volume")
    if not v:
        return []
    limit = f"warn drop >{v['warn_drop_pct']}%, fail drop >{v['fail_drop_pct']}%"
    if not previous or not previous.get("rows_total"):
        return [Result("volume", PASS, f"{staged_rows} rows", limit, "no previous load to compare")]
    prev = previous["rows_total"]
    drop = _pct(prev - staged_rows, prev)
    status = FAIL if drop > v["fail_drop_pct"] else WARN if drop > v["warn_drop_pct"] else PASS
    return [Result("volume", status, f"{staged_rows} vs {prev} before ({-drop:+.1f}%)", limit,
                   f"previous load {previous.get('ingest_date')}")]


def evaluate_nulls(cfg: dict, profile: dict, small: bool) -> list[Result]:
    rows = profile["rows_total"]
    pts = cfg["null_points"]
    out = []
    for col, base in cfg["null_baseline_pct"].items():
        pct = _pct(profile[f"miss__{col}"], rows)
        delta = pct - base
        status, note = _rate_status(delta > pts["fail"], delta > pts["warn"], small)
        out.append(Result(f"null_rate:{col}", status, f"{pct:.2f}%",
                          f"baseline {base:.2f}% +{pts['warn']}/+{pts['fail']} pts", note))
    return out


def evaluate_dates(cfg: dict, profile: dict, small: bool) -> list[Result]:
    rows = profile["rows_total"]
    bad = profile["future_dates"] + profile["before_1970"]
    pct = _pct(bad, rows)
    fail_pct = cfg["bad_date_fail_pct"]
    status, note = _rate_status(pct > fail_pct, bad > 0, small)
    return [Result("dates", status,
                   f"{profile['future_dates']} future, {profile['before_1970']} before 1970",
                   f"warn >0, fail >{fail_pct}% of rows", note)]


def evaluate_product_codes(cfg: dict, profile: dict, small: bool) -> list[Result]:
    """Invalid codes against a baseline (default 0%), plus empty arrays for 'array' kind."""
    pc = cfg.get("product_code")
    if not pc:
        return []
    rows = profile["rows_total"]
    bad = profile["bad_product_codes"]
    pct = _pct(bad, rows)
    base = pc.get("baseline_bad_pct", 0.0)
    warn_pts = pc.get("warn_points", 0.0)
    fail_pts = pc.get("fail_points", cfg["bad_date_fail_pct"])
    delta = pct - base
    status, note = _rate_status(delta > fail_pts, delta > warn_pts, small)
    out = [Result("product_codes", status, f"{bad} invalid ({pct:.2f}%)",
                  f"baseline {base:.2f}% +{warn_pts}/+{fail_pts} pts", note)]
    if pc["kind"] == "array":
        empty = profile.get("empty_product_codes", 0)
        empty_pct = _pct(empty, rows)
        empty_base = pc.get("baseline_empty_pct", 0.0)
        pts = cfg["null_points"]
        e_status, e_note = _rate_status(empty_pct - empty_base > pts["fail"],
                                        empty_pct - empty_base > pts["warn"], small)
        out.append(Result("empty_product_codes", e_status, f"{empty} rows ({empty_pct:.2f}%)",
                          f"baseline {empty_base:.2f}% +{pts['warn']}/+{pts['fail']} pts", e_note))
    return out


def evaluate_placeholders(cfg: dict, profile: dict, small: bool) -> list[Result]:
    """Values like N/A or '---' that are not NULL, against a per-column baseline."""
    pts = cfg.get("placeholder_points") or cfg["null_points"]
    rows = profile["rows_total"]
    out = []
    for col, base in cfg.get("placeholder_baseline_pct", {}).items():
        pct = _pct(profile[f"ph__{col}"], rows)
        delta = pct - base
        status, note = _rate_status(delta > pts["fail"], delta > pts["warn"], small)
        out.append(Result(f"placeholders:{col}", status, f"{pct:.2f}%",
                          f"baseline {base:.2f}% +{pts['warn']}/+{pts['fail']} pts", note))
    return out


def evaluate_whitespace(cfg: dict, profile: dict) -> list[Result]:
    """Values that are only spaces. Tolerance: whitespace_max_pct of rows (default 0)."""
    date_col = cfg["date_column"]
    rows = profile["rows_total"]
    max_pct = cfg.get("whitespace_max_pct", 0.0)
    limit = f"warn >{max_pct}% of rows" if max_pct else "expected none"
    counts = {c: profile.get(f"ws__{c}", 0) for c in cfg["null_baseline_pct"] if c != date_col}
    hits = {c: n for c, n in counts.items() if n}
    over = {c: n for c, n in hits.items() if _pct(n, rows) > max_pct}
    if over:
        return [Result("whitespace_only", WARN, ", ".join(f"{c}={n}" for c, n in over.items()),
                       limit, "values that are only spaces survive NULLIF")]
    if hits:
        return [Result("whitespace_only", PASS, ", ".join(f"{c}={n}" for c, n in hits.items()),
                       limit, "within tolerance")]
    return [Result("whitespace_only", PASS, "none", limit)]


def evaluate_text_lengths(cfg: dict, profile: dict) -> list[Result]:
    out = []
    for col, limit in cfg.get("text_max_length", {}).items():
        longest = profile.get(f"maxlen__{col}") or 0
        status = WARN if longest > limit else PASS
        out.append(Result(f"text_length:{col}", status, f"longest {longest}", f"max {limit}"))
    return out


def evaluate_categories(cfg: dict, categories: dict) -> list[Result]:
    out = []
    for col, allowed in cfg.get("allowed_values", {}).items():
        counts = categories.get(col, {})
        new = sorted(str(v) for v in counts if v is not None and v not in allowed)
        limit = "allowed: " + ", ".join(allowed)
        if new:
            out.append(Result(f"allowed_values:{col}", WARN, "new: " + ", ".join(new[:5]), limit))
        else:
            out.append(Result(f"allowed_values:{col}", PASS, "no new values", limit))
    warn_pts = cfg.get("mix_shift_warn_points")
    for col, baseline in cfg.get("mix_baseline_pct", {}).items():
        counts = categories.get(col, {})
        total = sum(counts.values())
        if not warn_pts or total < cfg["min_rows_for_pct"]:
            continue
        shifts = {cat: abs(_pct(counts.get(cat, 0), total) - base) for cat, base in baseline.items()}
        worst = max(shifts, key=shifts.get)
        status = WARN if shifts[worst] > warn_pts else PASS
        out.append(Result(f"mix_shift:{col}", status,
                          f"{worst} moved {shifts[worst]:.1f} pts "
                          f"({_pct(counts.get(worst, 0), total):.1f}% vs {baseline[worst]:.1f}%)",
                          f"warn >{warn_pts} pts"))
    return out


def evaluate_all(cfg: dict, counts: dict, profile: dict,
                 categories: dict, previous: dict | None) -> list[Result]:
    """Every check for one source and one ingest date. Pure function."""
    small = profile["rows_total"] < cfg["min_rows_for_pct"]
    results: list[Result] = []
    expected = counts.get("expected_staged_rows")
    if expected is None:
        expected = counts["raw_rows"]
    results += evaluate_gap(cfg, expected, counts["staged_rows"])
    results += evaluate_volume(cfg, counts["staged_rows"], previous)
    results += evaluate_nulls(cfg, profile, small)
    results += evaluate_dates(cfg, profile, small)
    results += evaluate_product_codes(cfg, profile, small)
    results += evaluate_placeholders(cfg, profile, small)
    results += evaluate_whitespace(cfg, profile)
    results += evaluate_text_lengths(cfg, profile)
    results += evaluate_categories(cfg, categories)
    return results


def exit_code(results: list[Result], warn_only: bool = False) -> int:
    """EXIT_DATA_FAIL (3) when any check FAILed (unless warn-only), else 0."""
    failed = any(r.status == FAIL for r in results)
    return EXIT_DATA_FAIL if (failed and not warn_only) else EXIT_OK


def format_results(results: list[Result]) -> str:
    lines = []
    for r in results:
        line = f"{r.status:<4}  {r.check:<34} {r.value:<46} {r.limit}"
        lines.append(line + (f"   [{r.note}]" if r.note else ""))
    return "\n".join(lines)


def summary(results: list[Result]) -> str:
    n = {s: sum(1 for r in results if r.status == s) for s in (PASS, WARN, FAIL)}
    return f"{n[PASS]} pass, {n[WARN]} warn, {n[FAIL]} fail"


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    summary_line = (__doc__ or "Check one day of STAGING before it is merged into PROD.").splitlines()[0]
    parser = argparse.ArgumentParser(description=summary_line)
    parser.add_argument("--project",     required=True)
    parser.add_argument("--source",      required=True, choices=schemas.SOURCES)
    parser.add_argument("--ingest-date", required=True,
                        type=lambda s: date.fromisoformat(s).isoformat())
    parser.add_argument("--warn-only",   action="store_true",
                        help="Report FAILs but exit 0 (use while thresholds are tuned)")
    parser.add_argument("--print-only",  action="store_true",
                        help="Print the SQL, then exit. No GCP calls.")
    args = parser.parse_args()

    cfg = check_config.get_config(args.source)
    if cfg is None:
        log.warning("%s: no entry in check_config.py yet (baseline pending). Nothing checked.",
                    args.source)
        return
    validate_config(args.source, cfg)

    raw = schemas.raw_table(args.project, args.source)
    stg = schemas.staging_table(args.project, args.source)

    if args.print_only:
        print(f"-- check_staging: {args.source}, @ingest_date = {args.ingest_date}")
        print("-- 1. counts (0 raw rows → exit 0):\n" + render_counts_sql(raw, stg, cfg.get("expected_staged_expr")) + "\n")
        print("-- 2. previous load:\n" + render_previous_sql(stg) + "\n")
        print("-- 3. profile:\n" + render_profile_sql(stg, cfg) + "\n")
        for col in category_columns(cfg):
            print(f"-- 4. categories of {col}:\n" + render_category_sql(stg, col) + "\n")
        return

    from google.cloud import bigquery
    client = bigquery.Client(project=args.project)
    job_cfg = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("ingest_date", "DATE", args.ingest_date)])

    def rows_of(sql: str) -> list[dict]:
        return [dict(r.items()) for r in client.query(sql, job_config=job_cfg).result()]

    counts = rows_of(render_counts_sql(raw, stg, cfg.get("expected_staged_expr")))[0]
    if counts["raw_rows"] == 0:
        log.info("%s: no raw rows for %s, so nothing was loaded. Nothing to check.",
                 args.source, args.ingest_date)
        return

    previous_rows = rows_of(render_previous_sql(stg))
    previous = previous_rows[0] if previous_rows else None
    profile = rows_of(render_profile_sql(stg, cfg))[0]
    categories = {col: {r["value"]: r["n"] for r in rows_of(render_category_sql(stg, col))}
                  for col in category_columns(cfg)}

    results = evaluate_all(cfg, counts, profile, categories, previous)
    print(f"\n=== check_staging {args.source} {args.ingest_date}: "
          f"{counts['raw_rows']} raw, {counts['staged_rows']} staged ===")
    print(format_results(results))
    print(f"=== {summary(results)} ===")

    failed = [r for r in results if r.status == FAIL]
    if failed and args.warn_only:
        log.warning("%d check(s) FAILED but --warn-only is set, so the run continues", len(failed))
    code = exit_code(results, args.warn_only)
    if code:
        # run_script puts stderr into the task error, so the alert shows what failed
        print(f"{args.source} {args.ingest_date}: {len(failed)} check(s) FAILED:\n"
              + format_results(failed), file=_sys.stderr)
        raise SystemExit(code)


if __name__ == "__main__":
    main()