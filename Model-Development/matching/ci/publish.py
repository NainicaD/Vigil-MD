"""Artifact Registry publishing for scripts/s11_package_final.py (Guidelines 2.6, 7.4, 8.7).

publish() refuses (exit 1) when ci/gate.py did not pass, refuses (exit 2) when the working tree has
uncommitted files, skips (exit 0) a version that already exists or a bundle that decides exactly like
the newest published version, then uploads the bundle and writes the mlflow/git annotations.
print_only=True only prints the commands, as s11 did before.
"""
import json
import re
import subprocess
import tempfile
from pathlib import Path

PROJECT, LOCATION, REPOSITORY = "vigil-md-1792696816", "us-central1", "vigil-md-models"


def ar_args(package: str) -> list[str]:
    return ["--project", PROJECT, "--location", LOCATION, "--repository", REPOSITORY, "--package", package]


def gate_problem(gate_json: Path) -> str | None:
    """Why the gate does not allow publishing, or None."""
    if not gate_json.exists():
        return f"{gate_json} not found: run ci/gate.py first"
    g = json.loads(gate_json.read_text())
    if not g.get("passed") or g.get("report_only"):
        failed = [c["name"] for c in g.get("checks", []) if c["status"] == "FAIL"]
        return f"the gate did not pass ({gate_json}): {', '.join(failed) or 'report-only run'}"
    return None


def dirty_files(root: Path) -> list[str]:
    """`git status --porcelain` lines; a published model must be rebuildable from its commit."""
    return subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True,
                          check=True).stdout.splitlines()


def newest(package: str, run=subprocess.run) -> tuple[int, int, int] | None:
    """The highest v<MAJOR.MINOR.PATCH> in the registry, or None."""
    out = run(["gcloud", "artifacts", "versions", "list", *ar_args(package), "--format=value(name)"],
              capture_output=True, text=True, check=True).stdout
    found = [tuple(map(int, m.groups())) for m in re.finditer(r"v(\d+)\.(\d+)\.(\d+)\s*$", out, re.M)]
    return max(found) if found else None


def next_patch(package: str, run=subprocess.run) -> str:
    """Newest version in the registry with PATCH + 1 (routine retraining)."""
    top = newest(package, run)
    if top is None:
        raise SystemExit(f"no v<semver> version of {package} in the registry: pass --semver")
    major, minor, patch = top
    return f"{major}.{minor}.{patch + 1}"


def decisions(manifest: dict, meta: dict) -> tuple:
    """What decides a pair: the model and encoder files, the cutoff and the IE gate version. The gate counts
    because v1.1.0 reused v1.0.0's files with a new gate, and that is a real change."""
    return (manifest["sha256"].get("model.json"), manifest["sha256"].get("tfidf.pkl"),
            meta.get("cutoff"), meta.get("ie_gate", "e6_e3b"))


def published_decisions(version: str, package: str, run=subprocess.run) -> tuple:
    """decisions() of a published version, from its manifest.json and meta.json only."""
    with tempfile.TemporaryDirectory() as tmp:
        for name in ("manifest.json", "meta.json"):
            run(["gcloud", "artifacts", "generic", "download", *ar_args(package), "--version", version,
                 "--name", name, "--destination", tmp], capture_output=True, text=True, check=True)
        read = {name: json.loads(next(Path(tmp).rglob(name)).read_text()) for name in ("manifest.json", "meta.json")}
    return decisions(read["manifest.json"], read["meta.json"])


def publish(bundle_dir: Path, version: str, package: str, annotations: dict, *, gate_json: Path, root: Path,
            print_only: bool, run=subprocess.run) -> int:
    upload = ["gcloud", "artifacts", "generic", "upload", *ar_args(package), "--version", version,
              "--source-directory", str(bundle_dir)]
    # generic repositories reject tags (HTTP 501), so the cross-references go in as version annotations
    annotate = ["gcloud", "artifacts", "versions", "update", version, *ar_args(package),
                "--annotations=" + ",".join(f"{k}={v}" for k, v in annotations.items())]
    if print_only:
        print("\nPublish (printed only):")
        for cmd in (upload, annotate):
            print("  " + " ".join(cmd))
        return 0
    problem = gate_problem(gate_json)
    if problem:
        print(f"not published: {problem}")
        return 1
    dirty = dirty_files(root)
    if dirty:
        print("not published: uncommitted files, so the model could not be rebuilt from its commit:")
        print("\n".join(f"  {line}" for line in dirty[:20]))
        return 2
    exists = run(["gcloud", "artifacts", "versions", "describe", version, *ar_args(package)],
                 capture_output=True, text=True)
    if exists.returncode == 0:
        print(f"{package} {version} is already published: skipped")
        return 0
    top = newest(package, run)
    if top is not None:
        latest = "v{}.{}.{}".format(*top)
        mine = decisions(json.loads((bundle_dir / "manifest.json").read_text()),
                         json.loads((bundle_dir / "meta.json").read_text()))
        if published_decisions(latest, package, run) == mine:
            print(f"not published: same model, encoder, cutoff and IE gate as {package} {latest}: skipped")
            return 0
    for cmd in (upload, annotate):
        print("+ " + " ".join(cmd), flush=True)
        run(cmd, check=True)
    print(f"published {package} {version} ({annotations})")
    return 0
