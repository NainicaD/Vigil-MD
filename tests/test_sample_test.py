"""Tests for scripts/warehouse/sample_test.py (the sample end-to-end test).

Only the safety rules and the pure helpers. No GCP.
"""
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.warehouse import common, convert
from scripts.warehouse import sample_test as st

REPO = str(Path(__file__).resolve().parents[1])


def run_script(*args, suffix=None):
    env = {k: v for k, v in os.environ.items() if k != "VIGIL_BQ_DATASET_SUFFIX"}
    if suffix is not None:
        env["VIGIL_BQ_DATASET_SUFFIX"] = suffix
    return subprocess.run([sys.executable, "scripts/warehouse/sample_test.py", *args],
                          cwd=REPO, env=env, capture_output=True, text=True)


@pytest.mark.parametrize("step", ["reset", "seed", "check", "scenarios", "all"])
def test_refuses_without_the_test_suffix(step):
    r = run_script(step)
    assert r.returncode != 0 and "Refusing: set VIGIL_BQ_DATASET_SUFFIX=_test" in (r.stdout + r.stderr)


def test_print_only_makes_no_gcp_call():
    r = run_script("reset", "--print-only", suffix="_test")
    assert r.returncode == 0, r.stderr
    assert "gs://vigil-md-test-nd" in r.stdout and "vigil_md_prod_test" in r.stdout


def test_test_bucket_is_not_the_production_bucket():
    assert st.TEST_BUCKET != st.SRC_BUCKET == "vigil-md-raw-nd"


def test_seed_layout_matches_the_bulk_layout():
    recs = {"enforcement": [{"a": 1}] * 3, "recall": [{"b": 1}] * 2, "maude": [{"m": i} for i in range(5)]}
    layout = st.seed_layout(recs)
    paths = [p for p, _, _ in layout]
    got = {s: common.pick_latest(paths, s) for s in ("enforcement", "recall", "maude")}
    assert [g["partition_key"] for g in got["maude"]] == [
        "2024q1/device-event-0001-of-0001.json.zip", "2024q2/device-event-0001-of-0001.json.zip"]
    assert len(got["enforcement"]) == len(got["recall"]) == 1
    maude = [r for p, _, r in layout if "/event/" in p]
    assert sum(len(r) for r in maude) == 5               # every MAUDE record seeded once


def test_seed_zip_is_readable_by_the_converter():
    data = st.openfda_zip([{"recall_number": "Z-1"}], st.ENF_MEMBER)
    out = io.StringIO()
    rows, _ = convert.convert_zip(io.BytesIO(data), out, "enforcement")
    assert rows == 1 and '"recall_number":"Z-1"' in out.getvalue()
