"""One report of both models' accuracy: the top-level numbers, then the same per slice.

  matching model  the weekly UDI agreement check on prod (s14_udi_agreement.py check_report.json,
                  with its slices): rows whose device is known from their own UDI. Real answers,
                  but easier rows than the pool the model is for: read as a trend.
  query router    the router's evaluation on a hand-written test set (query_router.evaluate
                  --out, with bias.py's slices): audience, device type, event type, phrasing
                  style. No live traffic is labelled, so this is the only accuracy measure of the
                  router; small slices (fewer than bias.MIN_N questions) are listed, not used.

File in, file out: no BigQuery, no model run. Pure functions except main.

Usage (from Data-Pipeline/):
    # matching: the check_report.json of a vigil_md_matching_check run (or s14 run by hand)
    # router:   python -m query_router.evaluate --model-dir models/published \
    #               --test data/test/handwritten_test_v2.jsonl --out <router.json> --no-log
    python scripts/analysis/model_metrics_report.py --matching <check_report.json> \
        --router <router.json> --router-test handwritten_test_v2 --as-of 2026-11-27 \
        --out reports/models/MODEL_METRICS.md
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

SLICE_ORDER = ("model_number", "matches", "manufacturer_tier", "candidates", "event_type",
               "match_kind", "manufacturer", "product_code")
SLICE_LABEL = {"model_number": "Model number in the report", "matches": "Matched model groups",
               "manufacturer_tier": "Manufacturer tier", "candidates": "Candidates per row",
               "event_type": "Event type", "match_kind": "Match kind (precision only)",
               "manufacturer": "Manufacturers with enough links", "product_code": "Product codes with enough links"}
ROUTER_MIN_N = 8                    # query_router/bias.py MIN_N


def _pct(x) -> str:
    return "–" if x is None else f"{100 * x:.1f}%"


def _ci(ci) -> str:
    return "–" if not ci else f"{100 * ci[0]:.1f}–{100 * ci[1]:.1f}%"


def matching_section(report: dict) -> list[str]:
    """The matching model's top-level numbers, its flagged slices and one table per slice kind."""
    lv = report.get("slice_levels", {"warn": 0.95, "alert": 0.90, "min_links": 200})
    out = ["## Matching model (MAUDE to GUDID)", "",
           f"Weekly UDI agreement check on prod: bundle {report.get('bundle_version', '?')}, "
           f"gate {report.get('ie_gate', '?')}. {report['n_rows']:,} sampled MAUDE rows whose device "
           "is known from their own UDI (the model never sees the UDI). These rows are easier than "
           "the rows without a UDI that the model is for: read the numbers as a trend.", "",
           "| Measure | Value |", "|---|---|",
           f"| Precision (links to the right model group) | {_pct(report.get('precision'))} "
           f"({report.get('n_links_right', 0):,} of {report.get('n_links', 0):,} links) |",
           f"| Recall (sampled rows linked right) | {_pct(report.get('recall'))} |",
           f"| Candidate recall (right group among the candidates) | {_pct(report.get('candidate_recall'))} |",
           f"| Model recall (linked right, of the rows whose group was a candidate) | "
           f"{_pct(report.get('model_recall'))} |", ""]
    flags = report.get("slice_flags", [])
    out += [f"**Flagged slices** ({len(flags)}): the whole 95% interval is under "
            f"{lv['warn']:.2f} (warn) or {lv['alert']:.2f} (alert), with at least "
            f"{lv['min_links']:,} links. Several warnings are expected while the overall precision "
            "is itself under 0.95.", ""]
    out += [f"- {f}" for f in flags] or ["- none"]
    slices = report.get("slices", [])
    for field in SLICE_ORDER:
        rows = [s for s in slices if s["slice"] == field]
        if not rows:
            continue
        if field in ("manufacturer", "product_code"):
            rows = sorted(rows, key=lambda s: s["precision"] if s["precision"] is not None else 2)[:15]
        out += ["", f"### {SLICE_LABEL.get(field, field)}", "",
                "| Slice | Links | Precision | 95% CI | Model recall | Level |", "|---|---|---|---|---|---|"]
        out += [f"| {s['value']} | {s['links']:,} | {_pct(s['precision'])} | {_ci(s['precision_ci'])} | "
                f"{_pct(s['model_recall'])} | {s['level']} |" for s in rows]
        if field in ("manufacturer", "product_code"):
            out += ["", f"The 15 with the lowest precision among those with at least "
                        f"{lv['min_links']:,} links."]
    return out


def router_section(report: dict, test_name: str) -> list[str]:
    """The router's top-level numbers (with its rules, as it answers on the site) and its slices."""
    g = report.get("guarded", {})
    out = ["## Query router (chatbot)", "",
           f"Evaluation on the hand-written test set `{test_name}`: {report['n']} questions "
           f"({report['n_advice']} medical-advice, {report['n_in_scope']} in scope). The router with "
           "its refusal rules, as it answers on the site. The model gate also uses this set, so the "
           "numbers may be slightly optimistic. No live question is labelled, so the "
           "router has no accuracy measure on real traffic; its monitoring events (label mix, "
           "refusals, scores) show drift, not accuracy.", "",
           "| Measure | Value |", "|---|---|",
           f"| Medical-advice questions refused (advice recall) | {_pct(report.get('advice_recall'))} |",
           f"| In-scope questions refused by mistake (false refusal rate) | {_pct(g.get('false_refusal_rate'))} |",
           f"| Macro-F1 over the labels | {g.get('macro_f1', 0):.3f} |",
           f"| All labels right (exact match) | {_pct(g.get('exact_match'))} |", ""]
    per = g.get("per_label", {})
    if per:
        out += ["| Label | Precision | Recall | F1 | Questions |", "|---|---|---|---|---|"]
        out += [f"| {k} | {_pct(v['precision'])} | {_pct(v['recall'])} | {v['f1']:.3f} | {v['support']} |"
                for k, v in per.items()]
        out += [""]
    bias = report.get("bias", {})
    flagged = [(f, m) for f, b in bias.items() for m in b.get("flags", [])]
    out += [f"**Flagged gaps** ({len(flagged)}): the widest best-minus-worst gap between slices with "
            f"at least {ROUTER_MIN_N} questions is over the limit (advice recall 0.15, false refusal "
            "rate 0.25; query_router/bias.py).", ""]
    out += [f"- {m} by {f}: gap {bias[f]['gaps'][m]['gap']:.3f} (worst {bias[f]['gaps'][m]['worst']}, "
            f"best {bias[f]['gaps'][m]['best']})" for f, m in flagged] or ["- none"]
    for field, b in bias.items():
        out += ["", f"### By {field.replace('_', ' ')}", "",
                "| Slice | Questions | Advice recall | False refusal rate | Exact match | Answered |",
                "|---|---|---|---|---|---|"]
        for name, s in sorted(b.get("slices", {}).items()):
            small = " (small)" if s["n"] < ROUTER_MIN_N else ""
            out.append(f"| {name}{small} | {s['n']} | {_pct(s.get('advice_recall'))} "
                       f"(n={s.get('n_advice', 0)}) | {_pct(s.get('false_refusal_rate'))} "
                       f"(n={s.get('n_in_scope', 0)}) | {_pct(s.get('exact_match'))} | {_pct(s.get('answered'))} |")
    return out


def build(matching: dict | None, router: dict | None, router_test: str, as_of: str) -> str:
    """The whole report as Markdown. Pure."""
    lines = ["# Model metrics: both models, overall and by slice", "",
             f"As of {as_of}. Built by `scripts/analysis/model_metrics_report.py` from the files named "
             "in each section; no BigQuery. Data slices (link coverage, field completeness) are in "
             "`docs/BIAS_REPORT.md`.", "",
             "| Model | Where the answers come from | Top-level number |", "|---|---|---|"]
    if matching:
        lines.append(f"| Matching | prod, weekly UDI check ({matching['n_rows']:,} rows) | "
                     f"precision {_pct(matching.get('precision'))}, recall {_pct(matching.get('recall'))} |")
    if router:
        g = router.get("guarded", {})
        lines.append(f"| Query router | test set `{router_test}` ({router['n']} questions) | "
                     f"advice recall {_pct(router.get('advice_recall'))}, false refusals "
                     f"{_pct(g.get('false_refusal_rate'))}, macro-F1 {g.get('macro_f1', 0):.3f} |")
    lines.append("")
    if matching:
        lines += matching_section(matching) + [""]
    if router:
        lines += router_section(router, router_test) + [""]
    return "\n".join(lines).rstrip("\n") + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--matching", type=Path, help="s14 check_report.json with slices")
    ap.add_argument("--router", type=Path, help="query_router.evaluate --out JSON")
    ap.add_argument("--router-test", default="handwritten_test_v2", help="the test set's name, for the text")
    ap.add_argument("--as-of", required=True, help="the date of the inputs, e.g. 2026-11-27")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    if not (a.matching or a.router):
        ap.error("give --matching, --router or both")
    load = lambda p: json.loads(p.read_text()) if p else None    # noqa: E731
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(build(load(a.matching), load(a.router), a.router_test, a.as_of))
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
