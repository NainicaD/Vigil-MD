"""Tests for answer/: template text, rendering and the whole pipeline.

The pipeline runs with a fake router, a fake search index and the query
layer's fake BigQuery client: no network, no cost.
"""

import json
import re

import pytest

from answer import pipeline
from answer import render
from answer import templates
from device_search import search
from query_layer import summary
from query_router import guardrails
from tests import test_query_layer

DEV = 'dev:f0cdc15b4d63'
FakeClient = test_query_layer.FakeClient


class FakeRouter:
    """The real guardrail routing on fixed model labels."""

    def __init__(self, labels):
        self.labels = labels

    def classify(self, question):
        """Returns what QueryRouter.classify() returns for these labels."""
        d = guardrails.route(self.labels, question)
        return {**d, 'scores': {},
                'unclear': not d['labels'] and d['reply_kind'] is None}


class FakeIndex:
    """Returns one fixed Resolution and counts the calls."""

    query_key = staticmethod(search.DeviceIndex.query_key)

    def __init__(self, resolution):
        self.resolution = resolution
        self.calls = 0

    def resolve(self, question):
        """Returns the fixed Resolution, whatever the question."""
        del question  # Unused.
        self.calls += 1
        return self.resolution


def hit(eid=DEV, title='DreamStation', subtitle='Respironics', reports=30356):
    """Returns a device search hit."""
    return search.Hit(f'device|{eid}', 'device', eid, title, subtitle, reports,
                      0, 0, [], None, 0.95)


ROWS = {
    'catalog': [{'entity_type': 'device', 'entity_id': DEV,
                 'title': 'DreamStation', 'subtitle': 'Respironics',
                 'product_codes': ['BZD']}],
    'counts': [
        {'year': 2023, 'event_type': 'Malfunction', 'reports': 900},
        {'year': 2024, 'event_type': 'Malfunction', 'reports': 1200},
        {'year': 2024, 'event_type': 'Injury', 'reports': 40},
        {'year': 2024, 'event_type': 'Death', 'reports': 1},
        {'year': 2025, 'event_type': '(none)', 'reports': 5},
    ],
    'problems': [
        {'problem_type': 'device',
         'problem_term': 'Material Integrity Problem', 'reports': 700},
        {'problem_type': 'device', 'problem_term': 'Contamination',
         'reports': 300},
        {'problem_type': 'patient', 'problem_term': 'Cough', 'reports': 20},
        {'problem_type': 'no_harm', 'problem_term': None, 'reports': 800},
    ],
    'recalls': [{'recall_number': 'Z-1', 'recall_class': 'Class I',
                 'record_date': '2021-06-14', 'recall_status': 'Terminated',
                 'product': 'DreamStation', 'firm': 'Respironics',
                 'reason_text': 'Foam may degrade into particles.',
                 'root_cause': 'Design', 'action': 'Register the device.',
                 'matched_by': 'device_name', 'total_recalls': 4}],
    'narrative_candidates': [{'mdr_report_key': '11', 'event_type': 'Death',
                              'record_date': '2024-02-01'}],
    'narrative_texts': [{'mdr_report_key': '11', 'mdr_text_key': '1',
                         'event_type': 'Death', 'date_received': '2024-02-01',
                         'text_length': 90,
                         'text': 'The patient (b)(6) died. Cause unknown.'}],
    'device_details': [{'device_id': DEV, 'name': 'DreamStation',
                        'company': 'Respironics, Inc.',
                        'product_codes': ['BZD'],
                        'models': ['DSX500', 'DSX600'], 'n_models': 12}],
}


def make_bot(labels, resolution=None, rows=None, client=None):
    """Returns (Chatbot, fake BigQuery client, fake index)."""
    client =client or FakeClient(rows=ROWS if rows is None else rows)
    index = FakeIndex(
        resolution or search.Resolution('resolved', [hit()], 'dreamstation'))
    bot = pipeline.Chatbot(FakeRouter(labels), index,
                           test_query_layer.backend(client))
    return bot, client, index


def facts(**overrides):
    """A device_summary() result for the DreamStation rows."""
    result = {
        'entity': {'type': 'device', 'id': DEV, 'found': True,
                   'title': 'DreamStation', 'subtitle': 'Respironics'},
        'counts': summary.summarize_counts(ROWS['counts']),
        'problems': summary.summarize_problems(ROWS['problems']),
        'recalls': {'total': 4, 'shown': [
            {k: v for k, v in ROWS['recalls'][0].items()
             if k != 'total_recalls'}]},
    }
    result.update(overrides)
    return result


# Templates.

@pytest.mark.parametrize('name,text', templates.all_templates().items())
def test_no_template_judges_a_device_or_advises(name, text):
    banned = set(re.findall(r'[a-z]+', text.lower())) & templates.BANNED_WORDS
    assert not banned, (name, banned)


def _banned_in(text):
    """Banned words in answer text, leaving out what is quoted from the FDA ("...")."""
    unquoted = re.sub(r'"[^"]*"', '', text)
    return set(re.findall(r'[a-z]+', unquoted.lower())) & templates.BANNED_WORDS


def rendered_answers():
    """Answer text the bot writes itself: the whole pipeline with every factual label, and
    each render function on its other paths (no reports, not found, no recalls, ...)."""
    bot = make_bot(['counts', 'problems', 'recalls', 'narratives', 'identity'])[0]
    out = bot.answer('Tell me everything about the DreamStation')
    texts = [s['text'] for s in out['sections'] if s['kind'] != 'medical_refusal']
    empty = facts()
    empty['counts'] = {**empty['counts'], 'total_reports': 0}
    empty['problems'] = {'device': [], 'patient': [], 'no_harm_reports': 0}
    empty['recalls'] = {'total': 0, 'shown': []}
    missing = facts()
    missing['entity'] = {**missing['entity'], 'found': False}
    texts += render.counts(facts()) + render.problems(empty) + render.recalls(empty)
    texts += [render.headline(empty), render.headline(missing)]
    texts += render.narratives({'entity': {'type': 'product_code', 'id': 'BZD'},
                                'excerpts': []})
    texts += render.identity({'entity': {'type': 'product_code', 'id': 'BZD'},
                              'details': {'title': 'Product code BZD',
                                          'top_names': ['DreamStation'],
                                          'product_codes': ['BZD']}})
    texts += render.ambiguous('implants', [hit(), hit('dev:2', 'Other', '', 0)])
    texts += render.footer({'device'}, out['sources'], out['data_as_of'])
    return texts


def test_rendered_answers_never_judge_or_advise():
    # templates.py is checked above; this checks what render.py adds around it
    # ("Models include", "Action:", "other or not stated", ...).
    texts = rendered_answers()
    assert len(texts) > 10
    for text in texts:
        assert not _banned_in(text), (text, _banned_in(text))


def test_the_rendered_check_catches_a_banned_word():
    assert _banned_in('Recalls: 3 found. This device is safe.') == {'safe'}
    assert not _banned_in('Reason: "the device is not safe to use"')   # FDA quote


def test_template_fingerprint_is_stable_and_covers_every_template():
    fingerprint = templates.templates_fingerprint()
    assert fingerprint == templates.templates_fingerprint()
    assert len(fingerprint) == 12
    assert {'DISCLAIMER', 'HEADLINE.device', 'RECALL_ITEM'} <= set(
        templates.all_templates())


def test_disclaimer_says_reports_are_not_proof_or_rates():
    assert 'does not prove' in templates.DISCLAIMER
    assert 'not failure rates' in templates.DISCLAIMER


# Render (pure).

def test_numbers_and_plurals():
    assert render.reports(1) == '1 report'
    assert render.reports(30356) == '30,356 reports'
    assert render.plural(1, 'death', 'deaths') == '1 death'
    assert render.join(['a', 'b', 'c']) == 'a, b and c'


def test_headline_for_a_device_says_linked_by_udi():
    assert render.headline(facts()) == (
        '**DreamStation** (Respironics): 2,146 reports linked by UDI in FDA '
        'MAUDE (2023–2025).')


def test_headline_for_a_product_code_and_a_manufacturer():
    pc = facts(entity={'type': 'product_code', 'id': 'LZG', 'found': True,
                       'title': 'Insulin pump'})
    assert render.headline(pc).startswith(
        '**Product code LZG** (Insulin pump): 2,146 reports')
    mfr = facts(entity={'type': 'manufacturer', 'id': 'medtronic',
                        'found': True, 'title': 'MEDTRONIC'})
    assert 'naming this manufacturer' in render.headline(mfr)


def test_headline_when_nothing_is_found():
    empty = {'total_reports': 0, 'by_event_type': {}, 'by_year': {}}
    result = facts(entity={'type': 'device', 'id': DEV, 'found': False},
                   counts=empty)
    assert 'no FDA MAUDE reports or recalls' in render.headline(result)
    result['entity'].update(found=True, title='X')
    assert render.headline(result) == (
        'No FDA MAUDE reports were found for **X**.')


def test_counts_lists_event_types_then_other_and_years():
    lines = render.counts(facts())
    assert lines[0] == ('**Reports by event type:** 1 death, 40 injuries, '
                        '2,100 malfunctions and 5 other or not stated.')
    assert lines[1] == ('Reports per year (latest 3 years with reports): '
                        '2023: 900, 2024: 1,241, 2025: 5.')


def test_problems_top_terms_and_no_harm():
    lines = render.problems(facts())
    assert lines == [
        '**Most reported device problems:** material integrity problem (700) '
        'and contamination (300).',
        '**Most reported patient problems:** cough (20).',
        '800 reports said there was no patient harm.',
    ]


def test_recalls_newest_first_with_class_and_more():
    lines = render.recalls(facts())
    assert lines[0] == ('**Recalls:** 4 found whose product description names '
                        'this device. Newest first:')
    assert lines[1].startswith(
        '- Class I, 2021-06-14 (terminated): "Foam may degrade')
    assert lines[-1] == '- …and 3 more.'  # 4 in total, 1 listed.
    assert render.recalls(facts(recalls={'total': 0, 'shown': []})) == [
        '**Recalls:** none found whose product description names this '
        'device.']


def test_identical_recalls_share_one_line():
    rec = {k: v for k, v in ROWS['recalls'][0].items() if k != 'total_recalls'}
    other = {**rec, 'record_date': '2020-01-01'}
    lines = render.recalls(facts(recalls={'total': 5,
                                          'shown': [rec, rec, rec, other]}))
    assert lines[1].startswith('- Class I, 2021-06-14 (terminated), 3 recalls:')
    assert lines[2].startswith('- Class I, 2020-01-01 (terminated):')
    assert lines[3] == '- …and 1 more.'  # 5 in total, 4 listed.


def test_product_code_title_that_repeats_the_code_is_left_out():
    result = facts(entity={'type': 'product_code', 'id': 'OZD', 'found': True,
                           'title': 'Product code OZD'})
    assert render.headline(result).startswith('**Product code OZD**: ')


def test_bare_numbers_are_not_listed_as_models():
    details = {**ROWS['device_details'][0],
               'models': ['00', 'DSX500', '00606959027251'], 'n_models': 3}
    line = render.identity({'entity': {'type': 'device', 'id': DEV},
                            'details': details})[0]
    assert 'Models include DSX500 (3 in total).' in line


@pytest.mark.parametrize('value,expected', [
    ('Class II', 'Class II'), ('2', 'Class II'), ('class iii', 'Class III'),
    (None, 'class not listed'), ('Not Yet Classified', 'Not Yet Classified'),
    ('Unclassified', 'Unclassified'),
])
def test_recall_class(value, expected):
    assert render.recall_class(value) == expected


def test_long_recall_reasons_are_cut_at_a_word():
    result = facts()
    result['recalls']['shown'][0]['reason_text'] = 'word ' * 100
    reason = re.search(r'"(.*?)"', render.recalls(result)[1]).group(1)
    assert len(reason) <= render.REASON_CHARS + 1
    assert reason.endswith('…')


def test_narratives_quote_reports_and_explain_withheld_text():
    narr = {'entity': {'type': 'device', 'id': DEV}, 'excerpts': [
        {'mdr_report_key': '11', 'event_type': 'Death',
         'date_received': '2024-02-01', 'text': 'Patient (b)(6)\n died.',
         'shortened': False}]}
    lines = render.narratives(narr)
    assert lines[1] == (
        '- *Death, received 2024-02-01* (report 11): "Patient (b)(6) died."')
    assert lines[-1] == templates.WITHHELD_NOTE
    narr['excerpts'] = []
    assert render.narratives(narr) == [
        '**What the reports describe:** no report texts were found for this '
        'device.']


def test_identity_for_a_device_and_a_product_code():
    dev = render.identity({'entity': {'type': 'device', 'id': DEV},
                           'details': ROWS['device_details'][0]})
    assert dev == [
        '**Who makes it:** DreamStation is listed in FDA GUDID by Respironics, '
        'Inc. Models include DSX500; DSX600 (12 in total). Product code: BZD.']
    pc = render.identity({'entity': {'type': 'product_code', 'id': 'LZG'},
                          'details': {'title': 'Insulin pump',
                                      'top_names': ['MiniMed 780G'],
                                      'product_codes': ['LZG']}})
    assert pc == ['**What it is:** Insulin pump. Names in the reports include '
                  'MiniMed 780G.']
    assert not render.identity({'entity': {'type': 'device', 'id': DEV},
                                'details': None})


def test_footer_names_sources_and_the_udi_note_only_for_devices():
    sources = [{'tables': ['entity_counts', 'entity_recalls']}]
    as_of = {'entity_counts': '2026-11-07T06:00:00',
             'entity_recalls': '2026-11-06T06:00:00'}
    lines = render.footer({'device'}, sources, as_of)
    assert lines[:2] == [templates.DISCLAIMER, templates.UDI_NOTE]
    assert lines[2] == ('**Sources:** FDA MAUDE reports, FDA recalls and '
                        'enforcement (BigQuery, data as of 2026-11-06).')
    assert templates.UDI_NOTE not in render.footer({'product_code'}, sources,
                                                   as_of)


# Pipeline.

def kinds(out):
    """Returns the kinds of an answer's sections, in order."""
    return [s['kind'] for s in out['sections']]


def test_counts_question_end_to_end():
    out = make_bot(['counts'])[0].answer(
        'How many deaths has the DreamStation had?')
    assert out['status'] == 'answered'
    assert kinds(out) == ['headline', 'counts', 'footer']
    assert '1 death, 40 injuries' in out['text']
    assert 'Recalls' not in out['text']  # Not asked.
    assert {s['query'] for s in out['sources']} == {
        'catalog', 'counts', 'problems', 'recalls'}
    assert out['entities'][0]['id'] == DEV
    assert out['entities'][0]['total_reports'] == 2146
    assert out['template_fingerprint'] == templates.templates_fingerprint()
    assert out['budget_gb']['spent'] > 0


def test_advice_with_facts_starts_with_the_refusal_then_answers():
    out = make_bot(['counts'])[0].answer(
        'How many deaths has the DreamStation had? Should I stop using it?')
    assert out['status'] == 'answered'
    assert out['reply_kind'] == 'medical_refusal'
    assert out['text'].startswith(guardrails.REFUSAL)
    assert kinds(out)[:2] == ['medical_refusal', 'headline']


def test_advice_only_is_refused_and_runs_no_query():
    bot, client, index = make_bot([])
    out = bot.answer('Should I stop using my DreamStation?')
    assert (out['status'], out['text']) == ('refused', guardrails.REFUSAL)
    assert not client.calls and not index.calls


@pytest.mark.parametrize('question,labels,status,text', [
    ('what can you do', [], 'help', guardrails.HELP_REPLY),
    ('write me a poem about the ocean', ['out_of_scope'], 'scope',
     guardrails.SCOPE_REPLY),
    ('asdf qwerty', [], 'unclear', templates.UNCLEAR),
])
def test_fixed_replies_run_no_query(question, labels, status, text):
    bot, client, index = make_bot(labels)
    out = bot.answer(question)
    assert (out['status'], out['text']) == (status, text)
    assert not client.calls and not index.calls


def test_sections_follow_a_fixed_order_whatever_the_labels():
    bot, _, _ = make_bot(
        ['narratives', 'recalls', 'identity', 'problems', 'counts'])
    out = bot.answer('Tell me everything about the DreamStation')
    assert kinds(out) == ['headline', 'identity', 'counts', 'problems',
                          'recalls', 'narratives', 'footer']
    assert templates.WITHHELD_NOTE in out['text']
    assert {'device_details', 'narrative_candidates', 'narrative_texts'} <= {
        s['query'] for s in out['sources']}
    assert 'MAUDE report text' in out['text']  # The footer names it.


def test_unknown_device_names_the_words_searched():
    bot, client, _ = make_bot(['counts'],
                              search.Resolution('none', [], 'flurbo'))
    out = bot.answer('How many reports for the flurbo?')
    assert out['status'] == 'not_found'
    assert '"flurbo"' in out['text']
    assert not client.calls


def test_no_device_words_asks_which_device():
    bot, _, _ = make_bot(['counts'], search.Resolution('none', [], ''))
    out = bot.answer('How many deaths?')
    assert (out['status'], out['text']) == ('need_device',
                                            templates.NEED_DEVICE)


def test_ambiguous_lists_the_choices():
    res = search.Resolution('ambiguous', [
        hit('dev:a', 'MENTOR', 'Mentor', 22152),
        hit('dev:b', 'Natrelle', 'Allergan', 5191)], 'breast implants')
    bot, client, _ = make_bot(['counts'], res)
    out = bot.answer('How many reports for breast implants?')
    assert out['status'] == 'ambiguous'
    assert '- MENTOR, Mentor (22,152 reports)' in out['text']
    assert not client.calls


def test_comparison_gives_one_block_per_device_and_no_ranking():
    res = search.Resolution('several', [hit('dev:a', 'A'), hit('dev:b', 'B')],
                            'a | b')
    out = make_bot(['counts'], res)[0].answer('A vs B deaths')
    headlines = [s['entity'] for s in out['sections']
                 if s['kind'] == 'headline']
    assert headlines == ['dev:a', 'dev:b']
    assert not re.search(r'\b(better|worse|safer)\b', out['text'])


def test_the_same_device_twice_is_answered_once():
    res = search.Resolution('several', [hit(), hit()], 'x | x')
    out = make_bot(['counts'], res)[0].answer('DreamStation vs dreamstation')
    assert kinds(out).count('headline') == 1


def test_comparison_is_capped():
    res = search.Resolution('several', [hit(f'dev:{i}') for i in range(5)],
                            'x')
    out = make_bot(['counts'], res)[0].answer('compare five')
    assert kinds(out).count('headline') == 3
    assert templates.TOO_MANY.format(n=3) in out['text']


def test_entity_passed_in_skips_the_search():
    bot, _, index = make_bot(['recalls'])
    out = bot.answer('Was it recalled?', entity=('device', DEV))
    assert not index.calls
    assert out['status'] == 'answered'
    assert '**Recalls:** 4 found' in out['text']


def test_found_but_no_reports_skips_problems_and_narratives():
    rows = {**ROWS, 'counts': [], 'problems': [], 'recalls': []}
    bot, client, _ = make_bot(['problems', 'narratives', 'recalls'], rows=rows)
    out = bot.answer('What problems and reports for the DreamStation?')
    assert 'No FDA MAUDE reports were found' in out['text']
    assert '**Recalls:** none found' in out['text']
    assert not any(n.startswith('narrative') for n, _ in client.real_runs())


def test_entity_missing_from_the_tables_is_not_found():
    bot, _, _ = make_bot(['counts'], rows={})
    out = bot.answer('How many reports?', entity=('device', 'dev:gone'))
    assert out['status'] == 'not_found'
    assert 'no FDA MAUDE reports or recalls' in out['text']


def test_over_budget_gives_the_budget_reply():
    client = FakeClient(rows=ROWS, estimates={'counts': 0.6e9})
    out = make_bot(['counts'], client=client)[0].answer(
        'How many reports for the DreamStation?')
    assert (out['status'], out['text']) == ('budget', templates.BUDGET)
    assert not client.real_runs()


def test_narratives_over_budget_keep_the_facts():
    # The largest product codes' report texts do not fit one question: only the
    # narratives are left out, with a note; counts and the footer stay.
    client = FakeClient(rows=ROWS, estimates={'narrative_texts': 0.6e9})
    out = make_bot(['counts', 'narratives'], client=client)[0].answer(
        'Show me reports for the DreamStation')
    assert out['status'] == 'answered'
    assert kinds(out) == ['headline', 'counts', 'narratives', 'footer']
    narr = [s['text'] for s in out['sections'] if s['kind'] == 'narratives'][0]
    assert narr == templates.NARRATIVES_BUDGET.format(kind='device')
    assert 'narrative_texts' not in {s['query'] for s in out['sources']}


@pytest.mark.parametrize('question,expected', [
    ('Describe the death reports for the Impella', ('Death',)),
    ('What do the injury and malfunction reports say?', ('Injury', 'Malfunction')),
    ('Show me reports where a patient died', ('Death',)),
    ('Describe the reports for the DreamStation', ('Death', 'Injury', 'Malfunction')),
    ('Describe reports where the pump went dead', ('Death', 'Injury', 'Malfunction')),
])
def test_asked_event_types(question, expected):
    assert pipeline.asked_event_types(question) == expected


def test_narratives_for_a_named_event_type_only():
    client = FakeClient(rows=ROWS)
    out = make_bot(['narratives'], client=client)[0].answer(
        'Describe the death reports for the DreamStation')
    narr = [s['text'] for s in out['sections'] if s['kind'] == 'narratives'][0]
    assert narr.startswith('**What the death reports describe** (1, newest first):')
    # The candidate query asked for deaths only.
    cfgs = [cfg for name, cfg in client.calls if name == 'narrative_candidates']
    sent = [list(p.values) for cfg in cfgs for p in cfg.query_parameters
            if p.name == 'event_types']
    assert sent and all(v == ['Death'] for v in sent)


def test_no_reports_of_the_named_event_type():
    rows = {**ROWS, 'narrative_candidates': [], 'narrative_texts': []}
    out = make_bot(['narratives'], rows=rows)[0].answer(
        'Describe the injury reports for the DreamStation')
    narr = [s['text'] for s in out['sections'] if s['kind'] == 'narratives'][0]
    assert narr == templates.NARRATIVES_NONE_OF_TYPE.format(events='injury',
                                                            kind='device')


def test_unfiltered_narratives_keep_the_general_header():
    out = make_bot(['narratives'])[0].answer('Describe the reports for the DreamStation')
    narr = [s['text'] for s in out['sections'] if s['kind'] == 'narratives'][0]
    assert narr.startswith(templates.NARRATIVES.format(n=1))


def test_narratives_reject_an_unknown_event_type():
    with pytest.raises(ValueError):
        summary.narratives(test_query_layer.backend(FakeClient(rows=ROWS)), 'device', DEV,
                           event_types=('Recall',))


def test_data_failure_gives_a_fixed_reply_not_a_traceback():
    class Broken(FakeClient):
        """Fails every query as a network error would."""

        def query(self, sql, job_config):
            raise ConnectionError('network down')

    out = make_bot(['counts'], client=Broken())[0].answer(
        'How many reports for the DreamStation?')
    assert (out['status'], out['text']) == ('error', templates.SERVICE_DOWN)


def test_a_bug_is_raised_not_hidden_as_a_data_failure():
    rows = {**ROWS, 'counts': [{'year': 2024, 'event_type': 'Death'}]}
    with pytest.raises(KeyError):
        make_bot(['counts'], rows=rows)[0].answer('DreamStation deaths')


def test_answer_is_json_serialisable():
    out = make_bot(['counts', 'recalls'])[0].answer(
        'DreamStation recalls and counts')
    json.dumps(out)
