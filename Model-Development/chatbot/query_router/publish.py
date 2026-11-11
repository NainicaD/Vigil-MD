"""Publish the approved router to Google Cloud Artifact Registry, or download a version.

MLflow records how every model was trained and keeps the "champion" alias (gate.py). This step
puts a copy of the approved model on the team's shelf in GCP, the generic Artifact Registry
repository vigil-md-models, as package chatbot-query-router. Every version stays there, so a
worse model can be rolled back by downloading an older version.

Only the approved model is published: the model is scored again on the gate's test set and its
numbers must equal query_router/champion_metrics.json (the champion the gate approved, in git)
under the same guardrail rules. The working tree must be clean, so every published model can be
rebuilt from its commit.

A version is named <rules fingerprint>-<commit> (e.g. 5d1584bb53b3-3a76436) and holds
model.joblib, meta.json and manifest.json (commit, MLflow run id and version, metrics, SHA-256
of each file). Publishing a version that already exists does nothing.

Usage (from Model-Development/chatbot/; needs gcloud and write access to the repository):
  python -m query_router.publish --model-dir models/baseline --print-only
  python -m query_router.publish --model-dir models/baseline
  python -m query_router.publish --list
  python -m query_router.publish --download 5d1584bb53b3-3a76436 --out models/published

Exit codes: 0 published, skipped or downloaded; 1 not the approved model; 2 bad input
(missing files, uncommitted changes, checksum mismatch); 3 gcloud failed.
"""

import argparse
import datetime
import hashlib
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

PROJECT = 'vigil-md-1792696816'
LOCATION = 'us-central1'
REPOSITORY = 'vigil-md-models'
PACKAGE = 'chatbot-query-router'
MODEL_FILES = ('model.joblib', 'meta.json')
TOLERANCE = 1e-4      # metrics are rounded to 4 decimals
EXIT_NOT_APPROVED, EXIT_BAD_INPUT, EXIT_GCLOUD = 1, 2, 3


def approval_problems(candidate: dict, champion: dict) -> list[str]:
    """Why the model is not the approved champion; empty if it is. Pure.

    Args:
      candidate: gate.model_metrics() of the model to publish.
      champion: champion_metrics.json.
    """
    problems = []
    for key in ('rules_fingerprint', 'data'):
        if candidate.get(key) != champion.get(key):
            problems.append(f'{key}: {candidate.get(key)} vs champion {champion.get(key)}')
    for name, want in champion['metrics'].items():
        got = candidate['metrics'].get(name)
        if want is None and got is None:
            continue
        if want is None or got is None or abs(got - want) > TOLERANCE:
            problems.append(f'{name}: {got} vs champion {want}')
    return problems


def version_name(rules_fingerprint: str, git_commit: str) -> str:
    """<rules fingerprint>-<short commit>. Pure."""
    return f'{rules_fingerprint}-{git_commit[:7]}'


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manifest(meta: dict, metrics: dict, git: dict, files: dict[str, str], version: str) -> dict:
    """What a published version says about itself. Pure."""
    return {'package': PACKAGE, 'version': version,
            'git_commit': git['git_commit'],
            'rules_fingerprint': meta['rules_fingerprint'],
            'mlflow_run_id': meta.get('mlflow_run_id'),
            'mlflow_model_version': meta.get('model_version'),
            'registered_model': meta.get('registered_model'),
            'test_data': metrics['data'], 'test_metrics': metrics['metrics'],
            'sha256': files,
            'published_at': datetime.datetime.now(datetime.timezone.utc).isoformat(
                timespec='seconds')}


def _where(project: str) -> list[str]:
    return [f'--project={project}', f'--location={LOCATION}', f'--repository={REPOSITORY}']


def upload_cmd(source: pathlib.Path, version: str, project: str = PROJECT) -> list[str]:
    return ['gcloud', 'artifacts', 'generic', 'upload', f'--package={PACKAGE}',
            f'--version={version}', f'--source-directory={source}', *_where(project)]


def exists_cmd(version: str, project: str = PROJECT) -> list[str]:
    return ['gcloud', 'artifacts', 'versions', 'describe', version, f'--package={PACKAGE}',
            *_where(project), '--format=value(name)']


def list_cmd(project: str = PROJECT) -> list[str]:
    return ['gcloud', 'artifacts', 'versions', 'list', f'--package={PACKAGE}', *_where(project),
            '--sort-by=~createTime', '--format=table(name.basename(),createTime)']


def download_cmd(version: str, out: pathlib.Path, project: str = PROJECT) -> list[str]:
    return ['gcloud', 'artifacts', 'generic', 'download', f'--package={PACKAGE}',
            f'--version={version}', f'--destination={out}', *_where(project)]


def verify(folder: pathlib.Path) -> list[str]:
    """Files whose SHA-256 differs from manifest.json; empty if all match."""
    want = json.loads((folder / 'manifest.json').read_text())['sha256']
    return [name for name, digest in want.items()
            if not (folder / name).exists() or sha256(folder / name) != digest]


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    print('+', ' '.join(cmd), flush=True)
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def publish(model_dir: pathlib.Path, project: str, print_only: bool) -> int:
    # Imported here so --list and --download need no scikit-learn.
    # pylint: disable-next=import-outside-toplevel
    from query_router import gate, tracking
    missing = [f for f in MODEL_FILES if not (model_dir / f).exists()]
    if missing:
        print(f'{model_dir} has no {", ".join(missing)}', file=sys.stderr)
        return EXIT_BAD_INPUT
    meta = json.loads((model_dir / 'meta.json').read_text())
    test = gate.TEST if gate.TEST.exists() else gate.FALLBACK
    candidate = gate.model_metrics(model_dir, test, gate.FALSE_ALARMS)
    champion = json.loads(gate.CHAMPION_METRICS.read_text())
    problems = approval_problems(candidate, champion)
    if problems:
        print('not the approved champion (query_router/champion_metrics.json); not published:')
        for p in problems:
            print(f'  {p}')
        return EXIT_NOT_APPROVED
    git = tracking.git_info()
    if git['git_dirty'] != 'false':
        print(f'uncommitted changes (git_dirty={git["git_dirty"]}): commit first, so the '
              'published model can be rebuilt from its commit', file=sys.stderr)
        if not print_only:
            return EXIT_BAD_INPUT
    version = version_name(meta['rules_fingerprint'], git['git_commit'])
    print(f'approved: {PACKAGE} {version} (MLflow version {meta.get("model_version")}, '
          f'run {meta.get("mlflow_run_id")})')
    with tempfile.TemporaryDirectory() as tmp:
        stage = pathlib.Path(tmp) / version
        stage.mkdir()
        files = {}
        for name in MODEL_FILES:
            shutil.copy2(model_dir / name, stage / name)
            files[name] = sha256(stage / name)
        (stage / 'manifest.json').write_text(
            json.dumps(manifest(meta, candidate, git, files, version), indent=2) + '\n')
        if print_only:
            print('--print-only: would run')
            print('  ' + ' '.join(exists_cmd(version, project)))
            print('  ' + ' '.join(upload_cmd(stage, version, project)))
            print((stage / 'manifest.json').read_text())
            return 0
        if _run(exists_cmd(version, project), check=False).returncode == 0:
            print(f'{version} is already published: nothing to do')
            return 0
        done = _run(upload_cmd(stage, version, project), check=False)
        if done.returncode:
            print(done.stderr, file=sys.stderr)
            return EXIT_GCLOUD
    print(f'published {PACKAGE} {version} to {LOCATION}-generic.pkg.dev/{project}/{REPOSITORY}')
    return 0


def download(version: str, out: pathlib.Path, project: str) -> int:
    out.mkdir(parents=True, exist_ok=True)
    done = _run(download_cmd(version, out, project), check=False)
    if done.returncode:
        print(done.stderr, file=sys.stderr)
        return EXIT_GCLOUD
    bad = verify(out)
    if bad:
        print(f'checksum mismatch for {", ".join(bad)}', file=sys.stderr)
        return EXIT_BAD_INPUT
    print(f'downloaded {PACKAGE} {version} to {out}; checksums match')
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='Publish the approved router to Artifact Registry.')
    ap.add_argument('--model-dir', type=pathlib.Path, default=pathlib.Path('models/baseline'))
    ap.add_argument('--project', default=PROJECT)
    ap.add_argument('--print-only', action='store_true', help='check and show; no upload')
    ap.add_argument('--list', action='store_true', help='list the published versions')
    ap.add_argument('--download', metavar='VERSION', help='download a published version')
    ap.add_argument('--out', type=pathlib.Path, default=pathlib.Path('models/published'))
    a = ap.parse_args(argv)
    if a.list:
        done = _run(list_cmd(a.project), check=False)
        print(done.stdout or done.stderr)
        return 0 if done.returncode == 0 else EXIT_GCLOUD
    if a.download:
        return download(a.download, a.out, a.project)
    return publish(a.model_dir, a.project, a.print_only)


if __name__ == '__main__':
    raise SystemExit(main())
