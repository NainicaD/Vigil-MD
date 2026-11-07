"""Tests for the hand-label agreement report (N6). Offline: CSVs in a temp folder."""
import csv
import json

import pytest

import hand_label_agreement as ag
import make_hand_label_sheet as hs


def _write(path, rows):
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(hs.COLUMNS))
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in hs.COLUMNS})


def _row(pid, label, reason="", rank=1, mrow="m1"):
    return {"pair_id": pid, "maude_row_id": mrow, "cand_rank": rank, "label": label,
            "hard_case_reason": reason, "rule_cited": "E3", "notes": ""}


def test_kappa_known_value():
    # 20 items: both MATCH 9, both NON_MATCH 6, MATCH/NON_MATCH 3, NON_MATCH/MATCH 2
    pairs = ([("MATCH", "MATCH")] * 9 + [("NON_MATCH", "NON_MATCH")] * 6
             + [("MATCH", "NON_MATCH")] * 3 + [("NON_MATCH", "MATCH")] * 2)
    # po = 0.75; pe = (12*11 + 8*9) / 400 = 0.51; kappa = 0.24 / 0.49
    assert ag.cohen_kappa(pairs) == pytest.approx(0.24 / 0.49)


def test_kappa_edges():
    assert ag.cohen_kappa([]) is None
    assert ag.cohen_kappa([("MATCH", "MATCH")] * 5) == 1.0
    assert ag.cohen_kappa([("MATCH", "NON_MATCH"), ("NON_MATCH", "MATCH")]) == pytest.approx(-1.0)


def test_report_groups_by_reason_and_lists_disagreements(tmp_path):
    first = [_row("a", "MATCH", "E3b_prefix_only"), _row("b", "NON_MATCH", "E4_non_distinctive"),
             _row("c", "MATCH"), _row("d", "", "E9_kit_wording"), _row("e", "MATCH")]
    second = [_row("a", "INSUFFICIENT_EVIDENCE"), _row("b", "NON_MATCH"), _row("c", "MATCH"),
              _row("d", "MATCH")]                       # d: unlabeled in first, e: not in second
    _write(tmp_path / "first.csv", first)
    _write(tmp_path / "second.csv", second)
    out = tmp_path / "out"
    assert ag.main(["--first", str(tmp_path / "first.csv"), "--second", str(tmp_path / "second.csv")],
                   out_dir=out) == ag.EXIT_OK
    rep = json.loads((out / "agreement_v1.json").read_text())
    assert rep["overall"]["n"] == 3 and rep["overall"]["agree"] == 2
    assert rep["focus_E3b_E4_E9"]["n"] == 2 and rep["focus_E3b_E4_E9"]["agree"] == 1
    assert set(rep["by_hard_case_reason"]) == {"E3b_prefix_only", "E4_non_distinctive", "none"}
    assert rep["only_first"] == 1 and rep["only_second"] == 1
    with (out / "disagreements_v1.csv").open() as f:
        d = list(csv.DictReader(f))
    assert [(x["pair_id"], x["label_first"], x["label_second"]) for x in d] == [
        ("a", "MATCH", "INSUFFICIENT_EVIDENCE")]


@pytest.mark.parametrize("bad", ["MAYBE", "match", "IE"])
def test_a_label_outside_the_allowed_set_is_a_data_error(tmp_path, bad):
    _write(tmp_path / "first.csv", [_row("a", bad)])
    _write(tmp_path / "second.csv", [_row("a", "MATCH")])
    assert ag.main(["--first", str(tmp_path / "first.csv"), "--second", str(tmp_path / "second.csv")],
                   out_dir=tmp_path / "out") == ag.EXIT_DATA


def test_repeated_pair_id_and_no_overlap_are_data_errors(tmp_path):
    _write(tmp_path / "first.csv", [_row("a", "MATCH"), _row("a", "MATCH")])
    _write(tmp_path / "second.csv", [_row("a", "MATCH")])
    args = ["--first", str(tmp_path / "first.csv"), "--second", str(tmp_path / "second.csv")]
    assert ag.main(args, out_dir=tmp_path / "out") == ag.EXIT_DATA
    _write(tmp_path / "first.csv", [_row("a", "MATCH")])
    _write(tmp_path / "second.csv", [_row("b", "MATCH")])
    assert ag.main(args, out_dir=tmp_path / "out") == ag.EXIT_DATA


def test_missing_file_or_column_is_a_config_error(tmp_path):
    (tmp_path / "first.csv").write_text("pair_id,label\na,MATCH\n")
    _write(tmp_path / "second.csv", [_row("a", "MATCH")])
    args = ["--first", str(tmp_path / "first.csv"), "--second", str(tmp_path / "second.csv")]
    assert ag.main(args, out_dir=tmp_path / "out") == ag.EXIT_CONFIG
    args = ["--first", str(tmp_path / "nope.csv"), "--second", str(tmp_path / "second.csv")]
    assert ag.main(args, out_dir=tmp_path / "out") == ag.EXIT_CONFIG


def test_guide_and_validator_agree_on_the_label_values():
    """The guide, this script and the plan's validator (C3, --kind hand) must allow the same labels."""
    guide = (ag.ROOT / "docs" / "HAND_LABEL_GUIDE.md").read_text()
    for label in ag.LABELS:
        assert f"`{label}`" in guide
    for col in hs.LABELER_COLUMNS:
        assert f"`{col}`" in guide, f"the guide must explain the {col} column"
