"""Tests for query_router.publish: only the approved model is published. No GCP: gcloud is faked."""
import json
import subprocess

import pytest

from query_router import gate, publish, tracking

CHAMPION = {'metrics': {'macro_f1': 0.77, 'advice_recall': 1.0,
                        'bias_max_gap_false_refusal_rate': 0.335, 'bias_max_gap_advice_recall': None},
            'rules_fingerprint': 'abc123abc123', 'data': 'data/test/handwritten_test_v2.jsonl'}


def candidate(**metrics):
    return {**CHAMPION, 'metrics': {**CHAMPION['metrics'], **metrics}}


def test_the_champion_itself_is_approved():
    assert publish.approval_problems(candidate(), CHAMPION) == []
    assert publish.approval_problems(candidate(macro_f1=0.77004), CHAMPION) == []   # rounding


@pytest.mark.parametrize('change,problem', [
    ({'metrics': {**CHAMPION['metrics'], 'advice_recall': 0.98}}, 'advice_recall'),
    ({'metrics': {**CHAMPION['metrics'], 'bias_max_gap_advice_recall': 0.1}}, 'bias_max_gap_advice_recall'),
    ({'rules_fingerprint': '000000000000'}, 'rules_fingerprint'),
    ({'data': 'data/generated/heldout.jsonl'}, 'data'),
])
def test_any_other_model_is_refused(change, problem):
    problems = publish.approval_problems({**CHAMPION, **change}, CHAMPION)
    assert problems and problems[0].startswith(problem)


def test_version_names_the_rules_and_the_commit():
    assert publish.version_name('5d1584bb53b3', '3a764361e4c6fefe') == '5d1584bb53b3-3a76436'


def test_manifest_names_the_approved_registry_version():
    m = publish.manifest({'rules_fingerprint': 'abc', 'model_version': '1'}, candidate(),
                         {'git_commit': 'c0ffee'}, {'model.joblib': 'x'}, 'abc-c0ffee', '12')
    assert m['approved_model_version'] == '12' and m['mlflow_model_version'] == '1'


def test_commands_target_the_model_repository_only():
    cmd = publish.upload_cmd('/tmp/x', 'v1')
    assert cmd[:4] == ['gcloud', 'artifacts', 'generic', 'upload']
    assert '--repository=vigil-md-models' in cmd and '--package=chatbot-query-router' in cmd
    assert '--location=us-central1' in cmd


def test_verify_finds_changed_files(tmp_path):
    (tmp_path / 'model.joblib').write_bytes(b'model')
    (tmp_path / 'meta.json').write_text('{}')
    sums = {n: publish.sha256(tmp_path / n) for n in publish.MODEL_FILES}
    (tmp_path / 'manifest.json').write_text(json.dumps({'sha256': sums}))
    assert publish.verify(tmp_path) == []
    (tmp_path / 'model.joblib').write_bytes(b'other')
    assert publish.verify(tmp_path) == ['model.joblib']


@pytest.fixture
def model_dir(tmp_path):
    d = tmp_path / 'model'
    d.mkdir()
    (d / 'model.joblib').write_bytes(b'model bytes')
    (d / 'meta.json').write_text(json.dumps({'rules_fingerprint': 'abc123abc123',
                                             'mlflow_run_id': 'r1', 'model_version': '12'}))
    return d


@pytest.fixture
def fake(monkeypatch, tmp_path):
    """gate scoring, the champion file and gcloud replaced; records the gcloud calls."""
    calls, state = [], {'exists': False, 'metrics': candidate(), 'dirty': 'false'}
    champion = tmp_path / 'champion_metrics.json'
    champion.write_text(json.dumps(CHAMPION))
    monkeypatch.setattr(gate, 'CHAMPION_METRICS', champion)
    monkeypatch.setattr(gate, 'model_metrics', lambda *a: state['metrics'])
    monkeypatch.setattr(tracking, 'git_info',
                        lambda: {'git_commit': '3a764361e4c6', 'git_dirty': state['dirty']})

    def run(cmd, check=True):
        calls.append(cmd)
        code = 0 if (cmd[2] != 'versions' or state['exists']) else 1
        return subprocess.CompletedProcess(cmd, code, '', '')
    monkeypatch.setattr(publish, '_run', run)
    return calls, state


def test_publishes_the_approved_model_once(model_dir, fake):
    calls, state = fake
    assert publish.publish(model_dir, 'p', print_only=False) == 0
    assert [c[2:4] for c in calls] == [['versions', 'describe'], ['generic', 'upload']]
    assert '--version=abc123abc123-3a76436' in calls[1]
    state['exists'] = True
    calls.clear()
    assert publish.publish(model_dir, 'p', print_only=False) == 0
    assert [c[2:4] for c in calls] == [['versions', 'describe']]      # already there: no upload


def test_refuses_a_model_that_is_not_the_champion(model_dir, fake):
    calls, state = fake
    state['metrics'] = candidate(advice_recall=0.9)
    assert publish.publish(model_dir, 'p', print_only=False) == publish.EXIT_NOT_APPROVED
    assert calls == []


def test_refuses_uncommitted_changes_but_print_only_still_shows(model_dir, fake, monkeypatch,
                                                                 capsys):
    calls, state = fake
    state['dirty'] = 'true'
    monkeypatch.setattr(publish, 'dirty_files', lambda: ['?? gha-creds-1a2b.json'])
    assert publish.publish(model_dir, 'p', print_only=False) == publish.EXIT_BAD_INPUT
    assert 'gha-creds-1a2b.json' in capsys.readouterr().err      # says which file
    assert publish.publish(model_dir, 'p', print_only=True) == 0
    assert calls == []


def test_missing_model_files(tmp_path):
    assert publish.publish(tmp_path, 'p', print_only=True) == publish.EXIT_BAD_INPUT
