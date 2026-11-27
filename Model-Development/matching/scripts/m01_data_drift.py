"""Matching data monitor: profile MAUDE input and compare it with the label snapshot's window.

  python scripts/m01_data_drift.py baseline --project $PROJECT --estimate-only   # free dry run
  python scripts/m01_data_drift.py baseline --project $PROJECT                    # once per label set
  python scripts/m01_data_drift.py check --project $PROJECT --out report.json --summary summary.md
  python scripts/m01_data_drift.py check --simulate udi_drop                      # demo, no query

baseline writes monitor/data_baseline.json (rows of the 12 report months before the label
snapshot); check profiles (1) the rows first loaded after the snapshot and (2) the baseline's own
rows as they are now ("revisit": catches in-place changes, e.g. a re-export that blanks UDI-DIs),
and compares both with the baseline (vigil_model/data_drift.py, limits in monitor/thresholds.json).
Drift in either is drift. Rebuild the baseline whenever the
label set is rebuilt (its snapshot date changes).

Read-only: one SELECT per window on vigil_md_prod (assert_read_only), dry-run first, refused above --max-gb,
maximum_bytes_billed on the job. Exit codes: 0 ok or warn, 2 no decision (fewer than min_rows
new rows), 3 drift (retraining should run), 4 the baseline is for another label snapshot,
5 over the byte cap.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vigil_model import bq_utils  # noqa: E402
from vigil_model import data_drift  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SQL = ROOT / "sql" / "monitor" / "data_profile.sql"
MANIFEST = ROOT / "labels" / "label_set_v2.manifest.json"
BASELINE = ROOT / "monitor" / "data_baseline.json"
LIMITS = ROOT / "monitor" / "thresholds.json"
EXIT_OK, EXIT_NO_DECISION, EXIT_DRIFT, EXIT_STALE_BASELINE, EXIT_OVER_BUDGET = 0, 2, 3, 4, 5


def snapshot_date(manifest: Path = MANIFEST) -> dt.date:
    return dt.date.fromisoformat(json.loads(manifest.read_text())["snapshot_date"])


def build_sql(project: str, window: str) -> str:
    sql = bq_utils.render(bq_utils.load_query_file(SQL).parts[0][1].replace("{window}", window), project)
    bq_utils.assert_read_only(sql)
    return sql


def run_profile(client, sql: str, max_gb: float) -> tuple[dict, dict]:
    """(profile, cost info). Raises OverflowError above max_gb (nothing billed)."""
    from google.cloud import bigquery
    est = bq_utils.estimate_bytes(client, sql)
    if est / 1e9 > max_gb:
        raise OverflowError(f"the profile would scan {est / 1e9:.3f} GB (cap {max_gb} GB)")
    job = client.query(sql, job_config=bigquery.QueryJobConfig(
        maximum_bytes_billed=max(10 * 1024 * 1024, int(max_gb * 1e9))))
    row = dict(next(iter(job.result())))
    row = {"totals": dict(row["totals"]),
           **{mix: [dict(r) for r in row[mix]] for mix in data_drift.MIXES}}
    billed = int(getattr(job, "total_bytes_billed", 0) or 0)
    return data_drift.profile(row), {"estimated_gb": round(est / 1e9, 4),
                                     "billed_gb": round(billed / 1e9, 4)}


def summary_markdown(report: dict) -> str:
    title = {"ok": "OK", "warn": "WARN", "drift": "DRIFT",
             "insufficient_data": "NO DECISION"}.get(report["result"], report["result"])
    lines = [f"### Matching data monitor: {title}", "",
             f"- new MAUDE rows since the label snapshot ({report['snapshot_date']}): "
             f"{report['n']} (decision from {report['min_rows']}): {report['current_result']}"]
    revisit = report.get("revisit")
    if revisit:
        lines.append(f"- the baseline's rows as they are now ({revisit['n']} rows): "
                     f"{revisit['result']}")
    else:
        lines.append("- the baseline's rows as they are now: not checked"
                     + (" (simulation)" if report.get("simulated") else ""))
    if report.get("cost"):
        lines.append(f"- queries: {report['cost']['billed_gb']} GB billed")
    if report.get("simulated"):
        lines.append(f"- **simulated** current window: {report['simulated']}")
    for label, part in (("new rows", report), ("baseline rows now", revisit)):
        flagged = [c for c in (part or {}).get("checks", []) if c["level"] in ("warn", "drift")]
        if flagged:
            lines += ["", f"**{label}**", "",
                      "| check | kind | value | baseline | now | level |", "|---|---|---|---|---|---|"]
            lines += [f"| {c['name']} | {c['kind']} | {c['value']} | {c.get('baseline', '')} | "
                      f"{c.get('current', '')} | {c['level']} |" for c in flagged]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("command", choices=["baseline", "check"])
    ap.add_argument("--project")
    ap.add_argument("--max-gb", type=float, default=1.0)
    ap.add_argument("--current-months", type=int, default=8,
                    help="report months read for the current window (partition pruning)")
    ap.add_argument("--estimate-only", action="store_true", help="dry run only (free)")
    ap.add_argument("--print-only", action="store_true", help="print the SQL, no GCP call")
    ap.add_argument("--simulate", choices=["none", "udi_drop", "new_devices"],
                    help="check against a synthetic current window (no query)")
    ap.add_argument("--baseline", type=Path, default=BASELINE)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--summary", type=Path)
    args = ap.parse_args(argv)

    snap = snapshot_date()
    limits = json.loads(LIMITS.read_text())
    if args.command == "baseline":
        window = data_drift.baseline_window(snap)
        windows = {"current": window}
    else:
        window = data_drift.current_window(snap, dt.date.today(), args.current_months)
        windows = {"current": window, "revisit": data_drift.baseline_window(snap)}

    revisit = None
    if args.command == "check" and args.simulate:
        base = json.loads(args.baseline.read_text())
        current, cost = data_drift.simulate(base["profile"], args.simulate), None
    else:
        if not args.project:
            ap.error("--project is needed unless --simulate")
        sqls = {name: build_sql(args.project, w) for name, w in windows.items()}
        if args.print_only:
            for name, sql in sqls.items():
                print(f"-- {name}\n{sql}\n")
            return EXIT_OK
        from google.cloud import bigquery
        client = bigquery.Client(project=args.project)
        if args.estimate_only:
            for name, sql in sqls.items():
                print(f"{args.command} ({name}): dry run "
                      f"{bq_utils.estimate_bytes(client, sql) / 1e9:.3f} GB (cap {args.max_gb} GB)")
            return EXIT_OK
        try:
            profiles, cost = {}, {"estimated_gb": 0.0, "billed_gb": 0.0}
            for name, sql in sqls.items():
                profiles[name], c = run_profile(client, sql, args.max_gb)
                cost = {k: round(cost[k] + c[k], 4) for k in cost}
        except OverflowError as e:
            print(f"STOP: {e}")
            return EXIT_OVER_BUDGET
        current, revisit = profiles["current"], profiles.get("revisit")

    if args.command == "baseline":
        out = {"built_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
               "snapshot_date": str(snap), "window": window, "cost": cost, "profile": current}
        args.baseline.write_text(json.dumps(out, indent=2) + "\n")
        print(f"baseline: {current['n']} rows, UDI rate {current['rates']['udi_rate']}, "
              f"link rate {current['rates']['udi_link_rate']} -> {args.baseline}")
        return EXIT_OK

    base = json.loads(args.baseline.read_text())
    if base["snapshot_date"] != str(snap):
        print(f"STOP: the baseline is for label snapshot {base['snapshot_date']}, the label set "
              f"is {snap}: rebuild it (m01_data_drift.py baseline)")
        return EXIT_STALE_BASELINE
    report = data_drift.compare(base["profile"], current, limits)
    revisit_report = data_drift.compare(base["profile"], revisit, limits) if revisit else None
    if revisit_report:
        revisit_report["window"] = windows["revisit"]
    report.update({"current_result": report["result"],
                   "result": data_drift.overall(report, revisit_report),
                   "revisit": revisit_report,
                   "snapshot_date": str(snap), "window": window, "cost": cost,
                   "simulated": args.simulate})
    if args.out:
        args.out.write_text(json.dumps(report, indent=2) + "\n")
    text = summary_markdown(report)
    if args.summary:
        args.summary.write_text(text)
    print(text, end="")
    return {"drift": EXIT_DRIFT, "insufficient_data": EXIT_NO_DECISION}.get(report["result"],
                                                                         EXIT_OK)


if __name__ == "__main__":
    sys.exit(main())
