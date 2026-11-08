"""Tests for the router's model operations: bias slices, sensitivity, the
Optuna objective, the promotion gate, and the end-to-end checks. No network.
"""

import json

import numpy as np
import pytest

from answer import evaluate as e2e
from query_router import bias
from query_router import gate
from query_router import guardrails
from query_router import labels
from query_router import sensitivity
from query_router import tune

OOS = labels.LABELS.index(labels.OUT_OF_SCOPE)


def vec(*names):
    return labels.to_vector(names)


# Bias slices.

def make_rows():
    """8 advice rows per event type; injury misses 2 of them."""
    rows, y, pred = [], [], []
    for event, misses in (('death', 0), ('injury', 2)):
        for i in range(8):
            rows.append({'event_type': event, 'oos_kind': 'advice',
                         'labels': ['out_of_scope']})
            y.append(vec('out_of_scope'))
            pred.append(vec() if i < misses else vec('out_of_scope'))
        for _ in range(8):
            rows.append({'event_type': event, 'labels': ['counts']})
            y.append(vec('counts'))
            pred.append(vec('counts'))
    return rows, np.array(y), np.array(pred)


def test_slice_report_finds_the_gap_and_flags_it():
    rows, y, pred = make_rows()
    report = bias.slice_report(y, pred, rows)
    assert set(report) == {'event_type'}  # Only fields that are present.
    slices = report['event_type']['slices']
    assert slices['injury']['advice_recall'] == 0.75
    assert slices['death']['advice_recall'] == 1.0
    assert slices['injury']['false_refusal_rate'] == 0.0
    gap = report['event_type']['gaps']['advice_recall']
    assert gap == {'gap': 0.25, 'worst': 'injury', 'best': 'death'}
    assert report['event_type']['flags'] == ['advice_recall']
    assert bias.max_gap(report, 'advice_recall') == 0.25


def test_small_slices_do_not_count_for_gaps():
    common = {'false_refusal_rate': None, 'n_in_scope': 0, 'exact_match': 1.0,
              'answered': None}
    slices = {'a': {**common, 'advice_recall': 0.0, 'n_advice': 2, 'n': 2},
              'b': {**common, 'advice_recall': 1.0, 'n_advice': 20, 'n': 20}}
    assert bias.gaps(slices)['advice_recall']['gap'] is None


# Sensitivity.

def test_sweep_trades_recall_for_false_refusals():
    rows = [{'labels': ['out_of_scope'], 'oos_kind': 'advice'},
            {'labels': ['out_of_scope'], 'oos_kind': 'advice'},
            {'labels': ['counts']}, {'labels': ['counts']}]
    scores = np.array([0.9, 0.3, 0.6, 0.1])
    rules = np.array([False, False, False, False])
    curve = sensitivity.sweep(scores, rules, rows, grid=(0.2, 0.5, 0.95))
    assert [(p['advice_recall'], p['false_refusal_rate']) for p in curve] == [
        (1.0, 0.5), (0.5, 0.5), (0.0, 0.0)]
    with_rule = sensitivity.sweep(scores, np.array([False, True, False, False]),
                                  rows, grid=(0.95,))
    assert with_rule[0]['advice_recall'] == 0.5  # The rule always counts.
    summary = sensitivity.summarize(curve, chosen=0.5, target=0.98)
    assert summary['at_chosen']['threshold'] == 0.5
    assert summary['max_safe_threshold'] == 0.2


# Optuna objective.

def test_objective_never_prefers_an_unsafe_trial():
    safe = tune.objective_value({'macro_f1': 0.60, 'advice_recall': 0.99})
    unsafe = tune.objective_value({'macro_f1': 0.95, 'advice_recall': 0.90})
    assert safe > unsafe


# Gate.

RULES = json.loads(gate.RULES_FILE.read_text())


def side(**metrics):
    base = {'advice_recall': 0.95, 'answered_facts_with_refusal': 0.95,
            'false_refusal_rate': 0.13, 'falsealarm_flagged': 0.35,
            'macro_f1': 0.78, 'bias_max_gap_advice_recall': 0.1,
            'bias_max_gap_false_refusal_rate': 0.1}
    return {'metrics': {**base, **metrics}, 'model': 'x', 'data': 'test',
            'rules_fingerprint': guardrails.rules_fingerprint()}


def failed(checks):
    return [name for name, state, _ in checks if state == 'FAIL']


def test_gate_promotes_a_safe_clear_improvement():
    ok, checks = gate.decide(side(false_refusal_rate=0.09), side(), RULES)
    assert ok, failed(checks)


def test_gate_keeps_the_champion_when_nothing_improves():
    ok, checks = gate.decide(side(), side(), RULES)
    assert not ok and failed(checks)[0].startswith('better by')


def test_gate_keeps_the_champion_on_a_safety_regression():
    ok, checks = gate.decide(side(false_refusal_rate=0.05, advice_recall=0.93),
                             side(), RULES)
    assert not ok
    assert 'advice_recall not worse by > 0.01' in failed(checks)


@pytest.mark.parametrize('metrics,rule', [
    ({'advice_recall': 0.85}, 'advice_recall >= 0.9'),
    ({'bias_max_gap_advice_recall': 0.3}, 'bias_max_gap_advice_recall <= 0.15'),
])
def test_gate_floors_and_ceilings(metrics, rule):
    champion = side(**metrics)
    ok, checks = gate.decide(side(false_refusal_rate=0.05, **metrics), champion,
                             RULES)
    assert not ok and rule in failed(checks)


def test_gate_needs_the_same_rules_and_data():
    other = side()
    other['rules_fingerprint'] = '000000000000'
    assert 'same rules' in failed(gate.decide(side(false_refusal_rate=0.05),
                                              other, RULES)[1])


def test_regression_mode_needs_no_gain_and_only_warns_on_ceilings():
    same = side(bias_max_gap_advice_recall=0.25)
    ok, checks = gate.decide(same, same, RULES, regression_only=True)
    assert ok
    assert ('bias_max_gap_advice_recall <= 0.15', 'WARN', '0.25') in checks
    worse = side(macro_f1=0.70)
    assert not gate.decide(worse, side(), RULES, regression_only=True)[0]


def test_champion_metrics_in_git_match_the_frozen_rules():
    champion = json.loads(gate.CHAMPION_METRICS.read_text())
    assert champion['rules_fingerprint'] == guardrails.rules_fingerprint()
    assert champion['data'] == str(gate.TEST)


# End-to-end checks.

def answer(status, entities=(), reply_kind=None, text='', kinds=()):
    return {'status': status, 'entities': list(entities),
            'reply_kind': reply_kind, 'text': text,
            'sections': [{'kind': k} for k in kinds]}


def test_e2e_check_status_entity_refusal_and_sections():
    row = {'labels': ['counts', 'recalls', 'out_of_scope'],
           'expected_status': 'answered', 'expected_entity': 'dreamstation',
           'refusal_expected': True}
    good = answer('answered', [{'title': 'DreamStation', 'id': 'dev:1'}],
                  'medical_refusal', guardrails.REFUSAL + ' …',
                  ('medical_refusal', 'headline', 'counts'))
    result = e2e.check(row, good)
    assert result == {'status': True, 'entity': True, 'refusal': True,
                      'sections': 0.5, 'correct': True}
    no_refusal = answer('answered', [{'title': 'DreamStation'}])
    assert e2e.check(row, no_refusal)['refusal'] is False
    other = answer('answered', [{'title': 'Impella'}], 'medical_refusal',
                   guardrails.REFUSAL)
    assert e2e.check(row, other)['entity'] is False


def test_e2e_summary_groups_by_status_and_lists_wrong_answers():
    def result(status, correct):
        row = {'text': status, 'labels': [], 'expected_status': status}
        return {'row': row,
                'status': status, 'billed_gb': 0.01, 'seconds': 1.0,
                'checks': {'status': correct, 'entity': True, 'refusal': True,
                           'sections': None, 'correct': correct}}
    s = e2e.summarize([result('answered', True), result('answered', False),
                       result('help', True)])
    assert s['accuracy'] == round(2 / 3, 4)
    assert s['by_expected_status']['answered'] == {'n': 2, 'accuracy': 0.5}
    assert len(s['wrong']) == 1 and s['billed_gb_total'] == 0.03


def test_reference_files_are_valid():
    for path in ('data/test/handwritten_test.jsonl', 'data/test/e2e_dev.jsonl'):
        for line in open(path, encoding='utf-8'):
            row = json.loads(line)
            labels.to_vector(row['labels'])  # Raises on an unknown label.
            assert ('oos_kind' in row) == ('out_of_scope' in row['labels']) or (
                'expected_status' in row)
