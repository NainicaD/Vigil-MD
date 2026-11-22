"""Drift check: live router events against the baseline.

  python -m monitoring.drift --events events.jsonl [--out report.json]
  python -m monitoring.drift --from-bigquery --days 7 [--max-gb 0.1] [--summary summary.md]

events.jsonl holds one monitoring event per line (api/events.py), or Cloud Logging entries
that carry it in jsonPayload. Only events of the baseline's router (same rules fingerprint)
are compared.

--from-bigquery reads the window from vigil_md_monitoring (monitoring/bq_events.py: dry run,
byte cap). --summary writes a short Markdown report (for the GitHub job summary or an e-mail);
--issue-body writes the retraining issue the daily job opens on drift (report + checklist).

Exit codes (as in the data pipeline): 0 no drift (ok or warn), 2 no decision (too few events,
no event from the baseline's router, or no events table yet), 3 drift: retraining should be
considered, 1 an error (e.g. the query would be over its byte cap).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import stats

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = ROOT / "monitoring" / "baseline.json"
DEFAULT_LIMITS = ROOT / "monitoring" / "thresholds.json"
EXIT_OK, EXIT_NO_DECISION, EXIT_DRIFT = 0, 2, 3


def read_events(path: Path) -> list[dict]:
    """Monitoring events from a JSONL file; other lines (e.g. other log entries) are skipped."""
    events = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        event = entry.get("jsonPayload", entry)
        if event.get("event") == "router_decision":
            events.append(event)
    return events


def check(events: list[dict], baseline: dict, limits: dict) -> dict:
    """The drift report. Events of another router are counted, not compared."""
    fp = baseline.get("rules_fingerprint")
    same = [e for e in events if e.get("rules_fingerprint") == fp]
    report = stats.compare(baseline["profile"],
                           stats.profile([stats.record_from_event(e) for e in same]), limits)
    report.update({"rules_fingerprint": fp, "events_total": len(events),
                   "events_other_router": len(events) - len(same)})
    if events and not same:
        report["result"] = "insufficient_data"
        report["note"] = (f"no event is from the baseline's router ({fp}): rebuild the baseline "
                          f"for the router that is serving (python -m monitoring.baseline)")
    return report


def exit_code(report: dict) -> int:
    return {"drift": EXIT_DRIFT, "insufficient_data": EXIT_NO_DECISION}.get(report["result"],
                                                                         EXIT_OK)


def summary_markdown(report: dict) -> str:
    """A short report: the result, the counts and the checks that warn or drift."""
    icon = {"ok": "OK", "warn": "WARN", "drift": "DRIFT", "insufficient_data": "NO DECISION"}
    lines = [f"### Router monitoring: {icon.get(report['result'], report['result'])}", "",
             f"- events compared: {report['n']} (minimum {report['min_events']}); "
             f"from another router: {report.get('events_other_router', 0)}",
             f"- baseline router: `{report.get('rules_fingerprint')}`"]
    if report.get("source"):
        src = report["source"]
        lines.append(f"- source: `{src.get('table')}`, last {src.get('days')} days, "
                     f"billed {src.get('billed_gb')} GB")
    if report.get("note"):
        lines.append(f"- note: {report['note']}")
    flagged = [c for c in report["checks"] if c["level"] in ("warn", "drift")]
    if flagged:
        lines += ["", "| check | kind | value | baseline | now | level |", "|---|---|---|---|---|---|"]
        lines += [f"| {c['name']} | {c['kind']} | {c['value']} | {c.get('baseline', '')} | "
                  f"{c.get('current', '')} | {c['level']} |" for c in flagged]
    return "\n".join(lines) + "\n"


RETRAIN_CHECKLIST = """\
### What to do (router retraining)

The router is trained on generated questions (query_router/templates.py, slots.json), so
retraining without new examples gives the same model. Drift means users ask in ways those
templates do not cover.

- [ ] Read the checks above: which signal moved (unknown words: new vocabulary or language;
      out_of_scope / refusal share: more or fewer advice questions; top score / unclear share:
      the router is less sure; a label share: users ask for other things).
- [ ] Write new templates or slot values for the kinds of questions that drifted
      (query_router/templates.py, slots.json), and new hand-written test questions by someone
      who has not seen the templates (data/test/README.md): a changed router needs a fresh test set.
- [ ] Open a pull request: CI retrains the router and runs the model gate (query_router/gate.py).
- [ ] Merge to main: chatbot-publish publishes the gated router and deploys it (deploy check,
      rollback on failure).
- [ ] Rebuild the baseline for the new router (python -m monitoring.baseline --model-dir
      models/published) and close this issue.
"""


def issue_markdown(report: dict, run_url: str | None = None) -> str:
    """The body of the "Router drift detected" issue: the report, the run, the checklist."""
    run = f"\nDaily check run: {run_url}\n" if run_url else ""
    return summary_markdown(report) + run + "\n" + RETRAIN_CHECKLIST


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--events", type=Path, help="a JSONL file of events")
    src.add_argument("--from-bigquery", action="store_true",
                     help="read the window from vigil_md_monitoring")
    ap.add_argument("--days", type=int, default=7, help="window for --from-bigquery")
    ap.add_argument("--max-gb", type=float, default=0.1, help="byte cap for --from-bigquery")
    ap.add_argument("--summary", type=Path, help="write a Markdown summary")
    ap.add_argument("--issue-body", type=Path, help="on drift, write the retraining issue's body")
    ap.add_argument("--run-url", help="link to this run, for the issue")
    ap.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    ap.add_argument("--thresholds", type=Path, default=DEFAULT_LIMITS)
    ap.add_argument("--out", type=Path, help="write the full report as JSON")
    args = ap.parse_args(argv)

    baseline = json.loads(args.baseline.read_text())
    limits = json.loads(args.thresholds.read_text())
    source = None
    if args.from_bigquery:
        from . import bq_events
        try:
            events, source = bq_events.read_window(args.days, max_gb=args.max_gb)
        except bq_events.NoEventsTable as e:
            events, source = [], {"table": bq_events.TABLE, "days": args.days, "note": str(e)}
        except bq_events.BudgetExceeded as e:
            print(f"error: {e}")
            return 1
    else:
        events = read_events(args.events)
    report = check(events, baseline, limits)
    if source is not None:
        report["source"] = source
        if source.get("note") and not report.get("note"):
            report["note"] = source["note"]
    if args.out:
        args.out.write_text(json.dumps(report, indent=2) + "\n")
    if args.summary:
        args.summary.write_text(summary_markdown(report))
    if args.issue_body and report["result"] == "drift":
        args.issue_body.write_text(issue_markdown(report, args.run_url))
    print(f"result: {report['result']} ({report['n']} events compared, "
          f"{report['events_other_router']} from another router, min {report['min_events']})")
    for c in report["checks"]:
        if c["level"] in ("warn", "drift"):
            extra = (f" (baseline {c['baseline']}, now {c['current']})"
                     if c["kind"] == "share" else "")
            print(f"  {c['level'].upper():5} {c['name']}: {c['kind']} {c['value']}{extra}")
    if report.get("note"):
        print(f"  note: {report['note']}")
    return exit_code(report)


if __name__ == "__main__":
    sys.exit(main())
