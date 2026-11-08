"""Integration test: the whole chatbot on vigil_md_dev against references.

Marked "integration": skipped by a normal pytest run and in CI. Run by hand
(needs gcloud credentials, the vigil_md_dev summary tables, its search index
from python -m device_search.build_index, and models/baseline):

  pytest -m integration -q tests/test_answer_integration.py

Cost: about 0.8 GB for the 48 questions when nothing is cached (0.25 GB or
less on a repeat run).
"""

import pathlib

import pytest

from answer import evaluate as e2e
from answer import pipeline
from query_router import generate

pytestmark = pytest.mark.integration

ROOT = pathlib.Path(__file__).resolve().parents[1]
MIN_ACCURACY = 0.80  # 0.854 on 2026-11-08 (rules cf0ad77c93da).
MIN_REFUSAL = 0.95  # The medical refusal comes first exactly when it should.
CAP_GB = 0.2  # Per question.


@pytest.fixture(scope='module')
def report():
    if not (ROOT / 'data/search/vigil_md_dev.sqlite').exists():
        pytest.skip('no vigil_md_dev search index: '
                    'python -m device_search.build_index')
    if not (ROOT / 'models/baseline/meta.json').exists():
        pytest.skip('no router model: python -m query_router.train')
    try:
        bot = pipeline.Chatbot.open('vigil_md_dev')
        return e2e.run(bot, generate.read_jsonl(ROOT / e2e.E2E))
    except Exception as e:  # pylint: disable=broad-except  # No credentials.
        pytest.skip(f'BigQuery not reachable: {type(e).__name__}')


def test_end_to_end_accuracy(report):
    s = report['summary']
    assert s['accuracy'] >= MIN_ACCURACY, s['wrong']


def test_refusals_and_fixed_replies_are_always_right(report):
    s = report['summary']
    assert s['checks']['refusal'] >= MIN_REFUSAL
    for status in ('refused', 'help', 'scope'):
        assert s['by_expected_status'][status]['accuracy'] == 1.0, status


def test_no_question_costs_more_than_the_cap(report):
    assert report['summary']['billed_gb_max'] <= CAP_GB
