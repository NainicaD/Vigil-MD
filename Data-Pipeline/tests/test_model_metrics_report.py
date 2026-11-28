"""model_metrics_report.py: the combined report of both models, from small fixed inputs."""
import json

import pytest

from scripts.analysis import model_metrics_report as R

MATCHING = {
    "n_rows": 1000, "n_links": 800, "n_links_right": 752, "precision": 0.94, "recall": 0.752,
    "candidate_recall": 0.85, "model_recall": 0.88, "bundle_version": "v1.1.0", "ie_gate": "e6_e3b_c1_m1",
    "slice_levels": {"warn": 0.95, "alert": 0.90, "min_links": 200},
    "slice_flags": ["alert: matches = 2 or more model groups: precision 0.690 (95% CI 0.66-0.72, 300 links)"],
    "slices": [
        {"slice": "matches", "value": "0 or 1 model group", "rows": 600, "links": 500, "links_right": 490,
         "precision": 0.98, "precision_ci": [0.964, 0.989], "model_recall": 0.9, "level": "ok"},
        {"slice": "matches", "value": "2 or more model groups", "rows": 350, "links": 300, "links_right": 207,
         "precision": 0.69, "precision_ci": [0.66, 0.72], "model_recall": 0.7, "level": "alert"},
        {"slice": "match_kind", "value": "contains", "rows": 40, "links": 40, "links_right": 36,
         "precision": 0.9, "precision_ci": [0.77, 0.96], "model_recall": None, "level": "small"},
    ] + [{"slice": "manufacturer", "value": f"m{i:02d}", "rows": 250, "links": 220, "links_right": 220 - i,
          "precision": round((220 - i) / 220, 4), "precision_ci": [0.9, 1.0], "model_recall": 0.9, "level": "ok"}
         for i in range(20)],
}
ROUTER = {
    "n": 50, "n_advice": 20, "n_in_scope": 30, "advice_recall": 1.0,
    "guarded": {"macro_f1": 0.8, "exact_match": 0.6, "false_refusal_rate": 0.1,
                "per_label": {"counts": {"precision": 0.9, "recall": 0.6, "f1": 0.72, "support": 12}}},
    "bias": {"style": {"slices": {"long": {"n": 10, "advice_recall": 1.0, "n_advice": 4, "false_refusal_rate": 0.33,
                                           "n_in_scope": 6, "exact_match": 0.5, "answered": 0.8},
                                  "typo": {"n": 3, "advice_recall": None, "n_advice": 0, "false_refusal_rate": 0.0,
                                           "n_in_scope": 3, "exact_match": 1.0, "answered": 1.0}},
                       "gaps": {"false_refusal_rate": {"gap": 0.33, "worst": "long", "best": "short"}},
                       "flags": ["false_refusal_rate"]}},
}


def test_the_overview_names_both_models_and_their_sources():
    md = R.build(MATCHING, ROUTER, "handwritten_test_v2", "2026-11-27")
    assert "| Matching | prod, weekly UDI check (1,000 rows) | precision 94.0%, recall 75.2% |" in md
    assert "test set `handwritten_test_v2` (50 questions)" in md and "macro-F1 0.800" in md
    assert "read the numbers as a trend" in md and "no accuracy measure on real traffic" in md


def test_matching_slices_flags_and_tables():
    md = R.build(MATCHING, None, "x", "d")
    assert "**Flagged slices** (1)" in md and "- alert: matches = 2 or more model groups" in md
    assert "| 2 or more model groups | 300 | 69.0% | 66.0–72.0% | 70.0% | alert |" in md
    assert "| contains | 40 | 90.0% | 77.0–96.0% | – | small |" in md          # no recall for match kind
    shown = [line for line in md.splitlines() if line.startswith("| m")]
    assert len(shown) == 15 and shown[0].startswith("| m19 ")                 # the 15 lowest, worst first
    assert "Query router" not in md


def test_router_slices_mark_small_ones_and_list_gaps():
    md = R.build(None, ROUTER, "handwritten_test_v2", "d")
    assert "- false_refusal_rate by style: gap 0.330 (worst long, best short)" in md
    assert "| long | 10 | 100.0% (n=4) | 33.0% (n=6) | 50.0% | 80.0% |" in md
    assert "| typo (small) | 3 | – (n=0) |" in md
    assert "| counts | 90.0% | 60.0% | 0.720 | 12 |" in md
    assert "Matching model" not in md


def test_main_writes_the_file_and_needs_an_input(tmp_path):
    m, r, out = tmp_path / "m.json", tmp_path / "r.json", tmp_path / "out" / "MODEL_METRICS.md"
    m.write_text(json.dumps(MATCHING))
    r.write_text(json.dumps(ROUTER))
    assert R.main(["--matching", str(m), "--router", str(r), "--as-of", "2026-11-27", "--out", str(out)]) == 0
    assert out.read_text().startswith("# Model metrics: both models, overall and by slice")
    with pytest.raises(SystemExit):
        R.main(["--as-of", "d", "--out", str(out)])
