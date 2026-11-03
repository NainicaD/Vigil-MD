
"""Tests for JSON -> NDJSON -> GCS acquisition."""

import json
import sys

import pytest

from scripts.acquisition import upload_samples_to_gcs as mod


@pytest.fixture
def samples(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "SAMPLES", tmp_path)
    return tmp_path


def write_sample(samples, source, payload):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    (samples / f"{source}_sample.json").write_text(text, encoding="utf-8")


@pytest.mark.parametrize("payload", [
    "", '{"records": [}',
])
def test_invalid_json_raises(samples, payload):
    write_sample(samples, "maude", payload)
    with pytest.raises(json.JSONDecodeError):
        mod.to_ndjson("maude")


def test_missing_file_raises(samples):
    with pytest.raises(FileNotFoundError):
        mod.to_ndjson("maude")


@pytest.mark.parametrize("payload", [
    {},
    {"results": [{"a": 1}]},
    [{"a": 1}],
    '"text"',
])
def test_invalid_wrapper_raises(samples, payload):
    write_sample(samples, "recall", payload)
    with pytest.raises(ValueError, match="records"):
        mod.to_ndjson("recall")


@pytest.mark.parametrize("records, message", [
    (None, "must be a list"),
    ({"a": 1}, "must be a list"),
    ("abc", "must be a list"),
    ([], "empty"),
    ([{"a": 1}, None], "record 1"),
    ([1], "record 0"),
])
def test_invalid_records_raise(samples, records, message):
    write_sample(samples, "recall", {"records": records})
    with pytest.raises(ValueError, match=message):
        mod.to_ndjson("recall")


def test_conversion_preserves_records(samples):
    records = [
        {"id": 1, "text": "line1\nline2", "value": None},
        {"id": 2, "name": "Müller 医療"},
        {"id": 3, "number": 2**62},
    ]
    write_sample(samples, "maude", {"records": records})

    dst = mod.to_ndjson("maude")
    lines = dst.read_text(encoding="utf-8").splitlines()

    assert dst == samples / "maude_sample.ndjson"
    assert [json.loads(line) for line in lines] == records
    assert dst.read_text(encoding="utf-8").endswith("\n")


def test_failed_conversion_preserves_existing_output(samples):
    dst = samples / "recall_sample.ndjson"
    dst.write_text('{"existing": true}\n', encoding="utf-8")
    write_sample(samples, "recall", {"records": []})

    with pytest.raises(ValueError):
        mod.to_ndjson("recall")

    assert dst.read_text(encoding="utf-8") == '{"existing": true}\n'


def test_rerun_overwrites_output(samples):
    write_sample(samples, "maude", {"records": [{"a": 1}]})
    mod.to_ndjson("maude")

    write_sample(samples, "maude", {"records": [{"a": 9}]})
    dst = mod.to_ndjson("maude")

    assert [json.loads(line) for line in dst.read_text().splitlines()] == [
        {"a": 9}
    ]


def test_main_requires_bucket(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["prog"])

    with pytest.raises(SystemExit) as exc:
        mod.main()

    assert exc.value.code == 2
