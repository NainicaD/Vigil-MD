"""Builds the router's monitoring baseline from a labelled test set (no BigQuery, no cost).

  python -m monitoring.baseline                                   # models/baseline, v2 test set
  python -m monitoring.baseline --model-dir models/published      # the router Cloud Run serves
  python -m monitoring.baseline --from-events reference.jsonl      # a window of real traffic

Two kinds of baseline:
  test set         (bootstrap, before there is traffic) the router run on the hand-written test
                   set. That set is about 40% medical-advice questions by design, so real traffic
                   will differ on the refusal share; use it only until there is traffic.
  reference window (production) the events of a period judged normal, e.g. the first 2-4 weeks
                   after launch. Drift then means "different from normal use".
The drift check compares live events with the baseline's router only (rules fingerprint), so
rebuild the baseline whenever a new router is published.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from query_router.predict import QueryRouter

from . import drift, stats

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / "data" / "test" / "handwritten_test_v2.jsonl"
DEFAULT_MODEL = ROOT / "models" / "baseline"
DEFAULT_OUT = ROOT / "monitoring" / "baseline.json"


def record(router: QueryRouter, question: str) -> dict:
    """The monitoring record of one question (the fields an event carries)."""
    out = router.classify(question)
    return {"labels": out["labels"], "reply_kind": out["reply_kind"], "unclear": out["unclear"],
            "top_score": max(out["scores"].values()) if out["scores"] else None,
            "unknown_word_share": out["unknown_word_share"],
            "question_words": len(question.split())}


def build(router: QueryRouter, questions: list[str], meta: dict) -> dict:
    return {"built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "rules_fingerprint": meta.get("rules_fingerprint"),
            "model_version": meta.get("model_version"),
            "data": meta.get("data"),
            "profile": stats.profile([record(router, q) for q in questions])}


def from_events(events: list[dict], source: str) -> dict:
    """A reference-window baseline: the events of the most common router in the window."""
    fps = [e.get("rules_fingerprint") for e in events]
    if not fps:
        raise SystemExit("no monitoring events in the file")
    fp = max(set(fps), key=fps.count)
    same = [e for e in events if e.get("rules_fingerprint") == fp]
    versions = {e.get("model_version") for e in same}
    return {"built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "rules_fingerprint": fp,
            "model_version": versions.pop() if len(versions) == 1 else None,
            "data": f"events:{source}",
            "profile": stats.profile([stats.record_from_event(e) for e in same])}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL)
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA)
    ap.add_argument("--from-events", type=Path,
                    help="build from monitoring events (a reference window) instead of a test set")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args(argv)

    if args.from_events:
        base = from_events(drift.read_events(args.from_events), str(args.from_events))
        args.out.write_text(json.dumps(base, indent=2) + "\n")
        print(f"baseline: {base['profile']['n']} events of router {base['rules_fingerprint']} "
              f"-> {args.out}")
        return 0
    router = QueryRouter.load(args.model_dir)
    meta = json.loads((args.model_dir / "meta.json").read_text())
    manifest = args.model_dir / "manifest.json"
    version = json.loads(manifest.read_text())["version"] if manifest.exists() else None
    questions = [json.loads(line)["text"] for line in args.data.read_text().splitlines()
                 if line.strip()]
    try:
        data = str(args.data.resolve().relative_to(ROOT))
    except ValueError:
        data = str(args.data)
    base = build(router, questions, {"rules_fingerprint": meta.get("rules_fingerprint"),
                                     "model_version": version, "data": data})
    args.out.write_text(json.dumps(base, indent=2) + "\n")
    p = base["profile"]
    print(f"baseline: {p['n']} questions, refusal share {p['refusal_share']}, unclear share "
          f"{p['unclear_share']}, rules {base['rules_fingerprint']} -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
