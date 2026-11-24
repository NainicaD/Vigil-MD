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


# ── sync_bundle (Artifact Registry replaced by a fake) ───────────────────────

import json  # noqa: E402

import vigil_helpers as H  # noqa: E402


class FakeResponse:
    def __init__(self, status=200, body=None, content=b""):
        self.status_code, self._body, self.content = status, body, content

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeRegistry:
    """Answers like Artifact Registry. versions: {version: {file name: bytes}}.
    down=True: every call fails, like a laptop without network."""

    def __init__(self, versions, down=False, corrupt=None, pages=1):
        self.versions, self.down, self.corrupt, self.pages = versions, down, corrupt, pages
        self.downloads = []                   # file ids asked for

    def get(self, url, params=None, timeout=None):
        if self.down:
            raise ConnectionError("network is unreachable")
        if url.endswith("/versions"):
            names = [f".../versions/{v}" for v in self.versions]
            page = int((params or {}).get("pageToken", 0))
            per = -(-len(names) // self.pages)          # split into self.pages pages
            body = {"versions": [{"name": n} for n in names[page * per:(page + 1) * per]]}
            if page + 1 < self.pages:
                body["nextPageToken"] = str(page + 1)
            return FakeResponse(body=body)
        file_id = url.rsplit("/files/", 1)[1].removesuffix(":download").replace("%3A", ":")
        self.downloads.append(file_id)
        _, version, name = file_id.split(":")
        data = self.versions[version].get(name)
        if data is None:
            return FakeResponse(status=404)
        if self.corrupt == name:
            data = b"not what the manifest says"
        return FakeResponse(content=data)


def _published(version: str) -> dict:
    """The files of one published bundle, with a manifest that matches them."""
    files = {name: f"{version} {name}".encode() for name in C.BUNDLE_FILES}
    manifest = {"version": version, "sha256": {n: hashlib.sha256(d).hexdigest() for n, d in files.items()}}
    return {**files, "manifest.json": json.dumps(manifest).encode()}


def _root(tmp_path: Path) -> Path:
    """A Data-Pipeline folder next to an empty Model-Development/matching/models."""
    (tmp_path / "Data-Pipeline").mkdir()
    (tmp_path / "Model-Development" / "matching" / "models").mkdir(parents=True)
    return tmp_path / "Data-Pipeline"


def _models(tmp_path: Path) -> Path:
    return tmp_path / "Model-Development" / "matching" / "models"


def _put_on_disk(tmp_path: Path, version: str, package: str = "matching-same-device") -> None:
    folder = _models(tmp_path) / f"{package}-{version}"
    folder.mkdir()
    for name, data in _published(version).items():
        (folder / name).write_bytes(data)


def test_sync_downloads_the_newest_version(tmp_path):
    root = _root(tmp_path)
    reg = FakeRegistry({"v1.1.0": _published("v1.1.0"), "v1.1.3": _published("v1.1.3")})
    got = H.sync_bundle(session=reg, root=root)
    assert got == {"bundle": "../Model-Development/matching/models/matching-same-device-v1.1.3",
                   "version": "v1.1.3", "new": True}
    folder = _models(tmp_path) / "matching-same-device-v1.1.3"
    assert sorted(p.name for p in folder.iterdir()) == sorted([*C.BUNDLE_FILES, "manifest.json"])
    assert not list(_models(tmp_path).glob(".*partial"))      # the temporary folder is gone
    assert (root / got["bundle"]).is_dir()                    # the path works from Data-Pipeline/


def test_sync_uses_a_bundle_already_on_disk_without_downloading(tmp_path):
    root = _root(tmp_path)
    _put_on_disk(tmp_path, "v1.1.3")
    reg = FakeRegistry({"v1.1.3": _published("v1.1.3")})
    got = H.sync_bundle(session=reg, root=root)
    assert got["version"] == "v1.1.3" and got["new"] is False
    assert reg.downloads == []


def test_sync_reads_every_page_of_versions(tmp_path):
    root = _root(tmp_path)
    reg = FakeRegistry({v: _published(v) for v in ["v1.0.0", "v1.1.0", "v1.1.9", "v1.1.10"]}, pages=2)
    assert H.sync_bundle(session=reg, root=root)["version"] == "v1.1.10"


def test_sync_without_network_uses_the_newest_bundle_on_disk(tmp_path):
    root = _root(tmp_path)
    _put_on_disk(tmp_path, "v1.1.0")
    _put_on_disk(tmp_path, "v1.1.2")
    _put_on_disk(tmp_path, "v0.0.1", package="matching-same-device-test")   # another package: ignored
    got = H.sync_bundle(session=FakeRegistry({}, down=True), root=root)
    assert got == {"bundle": "../Model-Development/matching/models/matching-same-device-v1.1.2",
                   "version": "v1.1.2", "new": False}


def test_sync_without_network_and_nothing_on_disk_fails(tmp_path):
    root = _root(tmp_path)
    with pytest.raises(RuntimeError, match="could not be reached"):
        H.sync_bundle(session=FakeRegistry({}, down=True), root=root)


def test_sync_never_uses_a_damaged_download(tmp_path):
    root = _root(tmp_path)
    reg = FakeRegistry({"v1.1.3": _published("v1.1.3")}, corrupt="tfidf.pkl")
    with pytest.raises(RuntimeError, match="damaged"):
        H.sync_bundle(session=reg, root=root)
    assert list(_models(tmp_path).iterdir()) == []            # no folder, no leftover


def test_sync_never_uses_a_damaged_bundle_on_disk(tmp_path):
    root = _root(tmp_path)
    _put_on_disk(tmp_path, "v1.1.3")
    (_models(tmp_path) / "matching-same-device-v1.1.3" / "model.json").write_bytes(b"changed")
    with pytest.raises(RuntimeError, match="damaged"):
        H.sync_bundle(session=FakeRegistry({"v1.1.3": _published("v1.1.3")}), root=root)


def test_a_download_that_breaks_halfway_falls_back_and_leaves_nothing(tmp_path):
    root = _root(tmp_path)
    _put_on_disk(tmp_path, "v1.1.0")
    broken = _published("v1.1.3")
    del broken["tfidf.pkl"]                                   # the registry answers 404 for it
    got = H.sync_bundle(session=FakeRegistry({"v1.1.0": _published("v1.1.0"), "v1.1.3": broken}),
                        root=root)
    assert got["version"] == "v1.1.0" and got["new"] is False
    assert sorted(p.name for p in _models(tmp_path).iterdir()) == ["matching-same-device-v1.1.0"]


def test_a_test_package_is_kept_apart(tmp_path):
    root = _root(tmp_path)
    reg = FakeRegistry({"v0.0.1": _published("v0.0.1")})
    got = H.sync_bundle(package="matching-same-device-test", session=reg, root=root)
    assert got["bundle"].endswith("/matching-same-device-test-v0.0.1")
    assert reg.downloads[0] == "matching-same-device-test:v0.0.1:manifest.json"
