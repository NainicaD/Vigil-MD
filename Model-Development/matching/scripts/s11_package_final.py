"""Package one trained model as a self-contained bundle for the team (Artifact Registry, chatbot).

The bundle folder holds model.json (XGBoost), tfidf.pkl (the F1 encoder s04 fitted on the training GUDID
texts; the model learned its cosines from this vocabulary), meta.json (cutoff, feature columns, lineage,
hand-sheet results), README.txt and manifest.json (sha256 of each file, the registry's layout).
vigil_model/predict.py loads it and applies the IE gate (E4).

Before the manifest is written, the bundle is reloaded and scored on the hand sheet: its scores must equal
the scores s06 wrote for the same run, so the bundle reproduces the evaluated model exactly.

Versioning: the MLflow registered model <package> gets an auto-incremented version with an alias
(challenger/champion/baseline) and tags semver + git_sha. Artifact Registry gets version v<semver> plus
annotations mlflow=mlflow-v<N> and git=git-<sha7>. Generic repositories do not support tags.

Publishing (ci/publish.py): uploads only if ci/gate.py passed (gate.json in the scores dir) and the whole
working tree is committed; skips a version that already exists, and a bundle with the same model.json,
tfidf.pkl, cutoff and IE gate as the newest published version. --print-only prints the commands instead.
In CI (--lineage ci) the MLflow store is thrown away after the job, so the mlflow annotation names the
GitHub run (gh-run-<id>, whose artifact keeps mlflow.db) and --semver auto bumps the newest PATCH.

Usage (from anywhere, after s06, s08 and ci/gate.py with VIGIL_EXP=final_v1; code must be committed):
    python scripts/s11_package_final.py --exp final_v1 --semver 1.0.0 [--print-only]
"""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import os

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ci.publish import next_patch, publish  # noqa: E402
from vigil_model import predict  # noqa: E402
from vigil_model.same_device_features import IE_GATE_VERSION  # noqa: E402
from vigil_model.tracking import run_outputs, tracking_uri  # noqa: E402

parser = argparse.ArgumentParser(description="Package a trained model as a bundle")
parser.add_argument("--exp", required=True)
parser.add_argument("--model", default="xgb_no_match_kind")
parser.add_argument("--semver", required=True,
                    help="MAJOR: input/output schema change, MINOR: new features or architecture, PATCH: retraining; "
                         "'auto': newest registry version with PATCH + 1")
parser.add_argument("--package", default="matching-same-device",
                    help="Artifact Registry package and MLflow registered model name")
parser.add_argument("--alias", default="challenger", choices=["challenger", "champion", "baseline"])
parser.add_argument("--tfidf", type=Path, default=ROOT / "data/label_set_v2_fs2_features.tfidf.pkl")
parser.add_argument("--hand", type=Path, default=ROOT / "data/hand_features_v2_fs2.csv")
parser.add_argument("--manifest", type=Path, default=ROOT / "labels/label_set_v2.manifest.json")
parser.add_argument("--out", type=Path, default=ROOT / "dist")
parser.add_argument("--print-only", action="store_true", help="print the gcloud commands, upload nothing")
parser.add_argument("--lineage", default="local", choices=["local", "ci"],
                    help="ci: annotate mlflow=gh-run-$GITHUB_RUN_ID instead of the MLflow model version")
parser.add_argument("--gate", type=Path, help="ci/gate.py's gate.json (default: <scores dir>/gate.json)")
args = parser.parse_args()
if args.semver == "auto":
    args.semver = next_patch(args.package)
    print(f"--semver auto: {args.semver}")
if not re.fullmatch(r"\d+\.\d+\.\d+", args.semver):
    sys.exit(f"--semver {args.semver!r}: use MAJOR.MINOR.PATCH, e.g. 1.0.0")
semver, version = args.semver, f"v{args.semver}"

# the git-<sha> tag must point at code that contains this bundle's loader
SERVING = ["vigil_model/predict.py", "vigil_model/same_device_features.py", "vigil_model/text_cosine.py",
           "vigil_model/embedding_retrieval.py", "scripts/s11_package_final.py"]
dirty = subprocess.run(["git", "status", "--porcelain", "--", *SERVING], cwd=ROOT, capture_output=True,
                       text=True, check=True).stdout.strip()
if dirty:
    sys.exit(f"commit the serving code first, so the git tag points at it:\n{dirty}")
sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                     check=True).stdout.strip()

import mlflow  # noqa: E402
import mlflow.xgboost  # noqa: E402
from mlflow.exceptions import MlflowException  # noqa: E402

mlflow.set_tracking_uri(tracking_uri())
runs = mlflow.search_runs(experiment_names=["vigil-md-linkage"],
                          filter_string=f"tags.exp = '{args.exp}' and tags.model_name = '{args.model}'",
                          order_by=["start_time DESC"])
# training runs have parameters; the s08 hand-label runs with the same name do not
runs = runs[runs["params.n_estimators"].notna()] if "params.n_estimators" in runs.columns else runs.iloc[0:0]
if not len(runs):
    sys.exit(f"no s06 training run for exp={args.exp} model={args.model}")
run = runs.iloc[0]
run_id = run["run_id"]
print(f"run {run_id} ({len(runs)} found, newest taken)")

try:
    model = mlflow.xgboost.load_model(f"runs:/{run_id}/model")
except Exception:
    # MLflow 3 can store models outside the run (same lookup as eval/s10_explain_model.py)
    found = mlflow.search_logged_models(experiment_ids=[run["experiment_id"]],
                                        filter_string=f"source_run_id = '{run_id}'", output_format="list")
    model = mlflow.xgboost.load_model(f"models:/{found[0].model_id}")

scores_dir, cutoffs_path = run_outputs(args.exp)
cutoff = json.loads(cutoffs_path.read_text())[args.model]
if cutoff is None:
    sys.exit(f"{args.model} has no 99%-precision cutoff in {cutoffs_path}")
s06_scores = pd.read_csv(scores_dir / f"{args.model}_hand_scores.csv", dtype={"pair_id": str})
if set(s06_scores["mlflow_run_id"]) != {run_id}:
    sys.exit(f"{scores_dir} was written by another run than {run_id}: rerun s06 or pick that run")

bundle_dir = args.out / f"{args.package}-{version}"
if bundle_dir.exists():
    shutil.rmtree(bundle_dir)
bundle_dir.mkdir(parents=True)
model.save_model(bundle_dir / "model.json")
shutil.copyfile(args.tfidf, bundle_dir / "tfidf.pkl")
meta = {
    "version": version, "model_name": args.model, "cutoff": float(cutoff),
    "feature_columns": list(model.get_booster().feature_names),
    "feature_set": run.get("tags.feature_set"), "ie_gate": IE_GATE_VERSION,
    "mlflow_run_id": run_id, "git_commit_at_training": run.get("tags.git_commit"),
    "git_commit_at_packaging": sha,
    "label_set_manifest_sha256": hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
    "tfidf_sha256": hashlib.sha256(args.tfidf.read_bytes()).hexdigest(),
    "training_data_sha256": run.get("tags.training_data_sha256"),
    "snapshot_date": run.get("tags.snapshot_date"),
}
(bundle_dir / "meta.json").write_text(json.dumps(meta, indent=2))

# round trip: reload the bundle as a teammate would and score the hand sheet
bundle = predict.load(bundle_dir)
hand = pd.read_csv(args.hand, dtype=str)
out = pd.concat([hand[["pair_id", "label"]], bundle.decide(hand)], axis=1)
both = out.merge(s06_scores[["pair_id", "score"]], on="pair_id", suffixes=("", "_s06"))
assert len(both) == len(hand), "hand features and s06 scores have different pairs"
worst = float(np.abs(both["score"] - both["score_s06"]).max())
assert worst < 1e-5, f"bundle scores differ from s06 by up to {worst}: encode() drifted from s06"

accept = out["decision"] == predict.MATCH
decided = out["label"].isin(["MATCH", "NON_MATCH"])
tp = int((accept & (out["label"] == "MATCH")).sum())
fp = int((accept & (out["label"] == "NON_MATCH")).sum())
fn = int((~accept & (out["label"] == "MATCH")).sum())
ie = out["label"] == "INSUFFICIENT_EVIDENCE"
meta["hand_sheet_v1"] = {
    "decided_pairs": int(decided.sum()), "precision": round(tp / (tp + fp), 4) if tp + fp else None,
    "recall": round(tp / (tp + fn), 4) if tp + fn else None, "false_positives": fp,
    "ie_pairs_accepted": f"{int((accept & ie).sum())}/{int(ie.sum())}",
}
(bundle_dir / "meta.json").write_text(json.dumps(meta, indent=2))
print(f"round trip ok (max score diff {worst:.1e}); hand sheet: {meta['hand_sheet_v1']}")

(bundle_dir / "README.txt").write_text(f"""Vigil MD same-device linkage model {version}
Model {args.model}, label_set_v2 (E1 + E3), feature set {meta['feature_set']}, IE gate {IE_GATE_VERSION}, cutoff {cutoff:.6f}.
Load: from vigil_model.predict import load; bundle = load("<this folder>"); bundle.decide(features_df)
Decisions: MATCH / NON_MATCH / INSUFFICIENT_EVIDENCE (gate: no usable MAUDE identifier, prefix_related, or a
contains hit in the middle of the GUDID identifier with disagreeing product codes).

For team testing, not validated. The hand labels follow the same rule tree the model learns, so the
hand-sheet precision measures agreement with the labeling guide, not independent ground truth. The sheet
is small (83 NON_MATCH pairs): 2-3 false positives are within noise. Known misses: other contains pairs with
short numeric keys, and some INSUFFICIENT_EVIDENCE pairs with agreeing identifiers are still accepted.
tfidf.pkl is a pickle: only load bundles from the team registry.
""")

# MLflow: the release gets its own hyphenated registered model (same name as the Artifact Registry package),
# an auto-incremented version, an alias for its role, and semver + git_sha tags
client = mlflow.MlflowClient()
source = f"vigil-md-linkage-{args.model}"            # s06 registers every training run here
found = client.search_model_versions(f"name = '{source}' and run_id = '{run_id}'")
if not found:
    sys.exit(f"no registered version of {source} for run {run_id}")
try:
    client.create_registered_model(args.package)
except MlflowException:
    pass                                             # already exists
same = [v for v in client.search_model_versions(f"name = '{args.package}'") if v.tags.get("semver") == semver]
if same and same[0].run_id != run_id:
    sys.exit(f"{args.package} semver {semver} is already version {same[0].version} of run {same[0].run_id}")
if same:
    release = same[0]                                # rerun of the same release: no new version
else:
    release = client.copy_model_version(f"models:/{source}/{found[0].version}", args.package)
    client.set_model_version_tag(args.package, release.version, "semver", semver)
    client.set_model_version_tag(args.package, release.version, "git_sha", sha)
client.set_registered_model_alias(args.package, args.alias, release.version)
print(f"MLflow {args.package} version {release.version} @{args.alias} (from {source} v{found[0].version})")

# same layout as the chatbot-query-router versions in Artifact Registry (vigil-md-models)
files = ["model.json", "tfidf.pkl", "meta.json", "README.txt"]
manifest = {
    "package": args.package, "version": version, "semver": semver,
    "tags": [f"mlflow-v{release.version}", f"git-{sha[:7]}"],
    "git_commit": sha, "git_commit_at_training": meta["git_commit_at_training"],
    "mlflow_run_id": run_id, "mlflow_model_version": release.version, "mlflow_alias": args.alias,
    "registered_model": args.package,
    "test_data": "labels/hand_label_sample_v1.csv", "test_metrics": meta["hand_sheet_v1"],
    "sha256": {f: hashlib.sha256((bundle_dir / f).read_bytes()).hexdigest() for f in files},
}
mlflow_ref, git_ref = manifest["tags"]
if args.lineage == "ci":
    # CI's MLflow store lives only for the job; the run's artifact keeps it (model-training.yml)
    mlflow_ref = f"gh-run-{os.environ.get('GITHUB_RUN_ID', 'unknown')}"
    manifest["tags"][0] = mlflow_ref
(bundle_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
print(f"bundle ready: {bundle_dir}  ({', '.join(files)}, manifest.json)")
sys.exit(publish(bundle_dir, version, args.package, {"mlflow": mlflow_ref, "git": git_ref},
                 gate_json=args.gate or scores_dir / "gate.json", root=ROOT, print_only=args.print_only))
