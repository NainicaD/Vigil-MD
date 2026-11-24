"""Tests for picking up the newest published model bundle (Model Deployment guidelines 5.4):
the version pick and the sha256 check in vigil_checks. No network: files are written to tmp_path.
"""
import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "dags"))

import vigil_checks as C  # noqa: E402


# ── newest_version ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("names, expected", [
    (["v1.0.0", "v1.1.0", "v1.1.2"], "v1.1.2"),
    (["v1.1.9", "v1.1.10"], "v1.1.10"),            # numbers, not text: "v1.1.10" < "v1.1.9" as text
    (["v1.1.2", "v2.0.0", "v1.9.9"], "v2.0.0"),
    (["v0.0.1"], "v0.0.1"),                        # first version of a test package
    (["latest", "v1.1.0", "1.2.0", "v1.2"], "v1.1.0"),   # other forms are ignored
    ([], None),
    (["latest"], None),
])
def test_newest_version(names, expected):
    assert C.newest_version(names) == expected


# ── bundle_problems ──────────────────────────────────────────────────────────

def _make_bundle(folder: Path) -> dict:
    """Write the four bundle files and return a matching manifest."""
    folder.mkdir(parents=True, exist_ok=True)
    sha = {}
    for name in C.BUNDLE_FILES:
        data = f"contents of {name}".encode()
        (folder / name).write_bytes(data)
        sha[name] = hashlib.sha256(data).hexdigest()
    return {"version": "v1.1.3", "sha256": sha}


def test_a_complete_bundle_has_no_problems(tmp_path):
    manifest = _make_bundle(tmp_path / "b")
    assert C.bundle_problems(tmp_path / "b", manifest) == []


def test_a_changed_file_is_found(tmp_path):
    manifest = _make_bundle(tmp_path / "b")
    (tmp_path / "b" / "tfidf.pkl").write_bytes(b"something else")
    assert C.bundle_problems(tmp_path / "b", manifest) == ["tfidf.pkl: sha256 differs from manifest.json"]


def test_a_missing_file_is_found(tmp_path):
    manifest = _make_bundle(tmp_path / "b")
    (tmp_path / "b" / "model.json").unlink()
    assert C.bundle_problems(tmp_path / "b", manifest) == ["model.json: missing"]


def test_a_manifest_that_leaves_out_a_file_is_a_problem(tmp_path):
    manifest = _make_bundle(tmp_path / "b")
    del manifest["sha256"]["tfidf.pkl"]       # unchecked files must not be used
    assert C.bundle_problems(tmp_path / "b", manifest) == ["tfidf.pkl: not listed in manifest.json"]


def test_a_manifest_without_hashes_fails_every_file(tmp_path):
    _make_bundle(tmp_path / "b")
    assert len(C.bundle_problems(tmp_path / "b", {})) == len(C.BUNDLE_FILES)
