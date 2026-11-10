"""Chatbot v1, end to end, no LLM: a question in, an answer with sources out.

  bot = Chatbot.open()          # Router model, search index, BigQuery.
  bot.answer('Was the DreamStation recalled, and why?')
  # {'status': 'answered', 'text': '**DreamStation** ...', 'sections': [...],
  #  'entities': [...], 'sources': [...], ...}

Steps (README, "Handoff: building the answer"):
  1. Query router: a fixed reply (help, scope, medical refusal), the parts
     asked for, or unclear.
  2. Device search: which entity (resolved, several, ambiguous or none).
  3. Query layer, one Budget per question: device_summary always; narratives
     and identity when asked.
  4. Templates (render.py): the medical refusal first when the question also
     asked for advice (policy facts_with_refusal); disclaimer and sources last.

status: answered | refused | help | scope | unclear | need_device |
        not_found | ambiguous | budget | error
"""

import logging
import pathlib
import re

from google.api_core import exceptions as api_exceptions
from google.auth import exceptions as auth_exceptions

from answer import render
from answer import templates
from device_search import search
from query_layer import backend as ql_backend
from query_layer import config
from query_layer import summary
from query_router import guardrails
from query_router import predict

SECTION_ORDER = ('identity', 'counts', 'problems', 'recalls', 'narratives')
# Event types named in a question: narratives then show only those ("describe the death
# reports"). Counts, problems and recalls are not filtered. "dead" is left out on purpose:
# "the pump went dead" is a malfunction, not a death.
EVENT_PATTERNS = (
    ('Death', re.compile(r'\b(?:deaths?|died|fatal(?:ity|ities)?)\b', re.I)),
    ('Injury', re.compile(r'\binjur(?:y|ies|ed|ing)\b', re.I)),
    ('Malfunction', re.compile(r'\bmalfunction(?:s|ed|ing)?\b', re.I)),
)


def asked_event_types(question: str) -> tuple[str, ...]:
    """Returns the event types the question names, in the fixed order; all three if none."""
    named = tuple(et for et, pat in EVENT_PATTERNS if pat.search(question))
    return named or tuple(et for et, _ in EVENT_PATTERNS)
MAX_ENTITIES = 3  # Devices compared in one answer ("X vs Y vs Z").
DEFAULT_MODEL_DIR = (
    pathlib.Path(__file__).resolve().parents[1] / 'models' / 'baseline')
# BigQuery, login or network failures: the user gets SERVICE_DOWN. Anything
# else is a bug and is raised.
DATA_ERRORS = (api_exceptions.GoogleAPIError, auth_exceptions.GoogleAuthError,
               OSError)


class Chatbot:
    """Wires the router, the device index and the query layer.

    Each part is passed in, so tests use fakes and a device page can share one
    index with the website.
    """

    def __init__(self, router, index, backend, max_gb: float | None = None):
        """Initializes the chatbot.

        Args:
          router: Has classify(question) -> dict, like query_router's
            QueryRouter.
          index: Has resolve(question) and query_key(hit), like
            device_search's DeviceIndex.
          backend: A query_layer BigQueryBackend.
          max_gb: Cost cap per question; None uses the query layer's default.
        """
        self.router = router
        self.index = index
        self.backend = backend
        self.max_gb = max_gb

    @classmethod
    def open(cls, dataset: str | None = None,
             model_dir: str | pathlib.Path = DEFAULT_MODEL_DIR,
             max_gb: float | None = None) -> 'Chatbot':
        """Loads the router model, the search index and a BigQuery backend.

        Args:
          dataset: BigQuery dataset and index name; None uses
            VIGIL_BQ_DATASET (default vigil_md_prod).
          model_dir: The router model directory.
          max_gb: Cost cap per question.

        Returns:
          A ready Chatbot.
        """
        dataset = dataset or config.DATASET
        return cls(predict.QueryRouter.load(model_dir),
                   search.DeviceIndex.open(dataset),
                   ql_backend.BigQueryBackend(dataset=dataset), max_gb)

    def answer(self, question: str,
               entity: tuple[str, str] | None = None) -> dict:
        """Answers one question.

        Args:
          question: What the user typed.
          entity: (entity_type, entity_id) to skip the device search, e.g. on
            a device page or for a follow-up question.

        Returns:
          A JSON-friendly dict: status, text, sections, labels, entities,
          sources, data_as_of, budget_gb, search and both fingerprints.
        """
        route = self.router.classify(question)
        out = {'question': question, 'labels': route['labels'],
               'run_queries': route['run_queries'],
               'reply_kind': route['reply_kind'],
               'rule_hits': route.get('rule_hits', []), 'entities': [],
               'sections': [], 'sources': [], 'data_as_of': {},
               'budget_gb': None, 'search': None,
               'template_fingerprint': templates.templates_fingerprint(),
               'rules_fingerprint': guardrails.rules_fingerprint()}
        sections = out['sections']
        if route['reply']:
            sections.append({'kind': route['reply_kind'],
                             'text': route['reply']})

        def done(status: str) -> dict:
            out['status'] = status
            out['text'] = '\n\n'.join(s['text'] for s in sections)
            return out

        if route['reply_kind'] in ('help', 'scope'):
            return done(route['reply_kind'])
        if not route['run_queries']:
            if route['reply_kind'] == 'medical_refusal':
                return done('refused')
            sections.append({'kind': 'unclear', 'text': templates.UNCLEAR})
            return done('unclear')

        if entity:
            keys = [{'entity_type': entity[0], 'entity_id': entity[1]}]
        else:
            res = self.index.resolve(question)
            out['search'] = {
                'status': res.status, 'phrase': res.phrase,
                'matches': [{'type': h.kind, 'id': h.entity_id,
                             'title': h.title, 'score': h.score}
                            for h in res.matches]}
            if res.status == 'none':
                if res.phrase:
                    sections.append({'kind': 'not_found',
                                     'text': templates.NOT_FOUND_SEARCH.format(
                                         phrase=res.phrase)})
                    return done('not_found')
                sections.append({'kind': 'need_device',
                                 'text': templates.NEED_DEVICE})
                return done('need_device')
            if res.status == 'ambiguous':
                sections.append({'kind': 'ambiguous', 'text': '\n'.join(
                    render.ambiguous(res.phrase, res.matches))})
                return done('ambiguous')
            # "X vs X" can resolve both parts to one entity: keep it once.
            keys = list({h.id: self.index.query_key(h)
                         for h in res.matches}.values())
            if len(keys) > MAX_ENTITIES:
                sections.append({'kind': 'note', 'text':
                                 templates.TOO_MANY.format(n=MAX_ENTITIES)})
                keys = keys[:MAX_ENTITIES]

        budget = (ql_backend.Budget() if self.max_gb is None
                  else ql_backend.Budget(max_gb=self.max_gb))
        asked = set(route['run_queries'])
        try:
            for key in keys:
                sections.extend(self._entity_sections(key, asked, budget, out))
        except ql_backend.QueryBudgetExceeded:
            logging.warning('Over budget: %s', question)
            sections.append({'kind': 'budget', 'text': templates.BUDGET})
            return done('budget')
        except DATA_ERRORS:
            logging.exception('Query layer failed')
            sections.append({'kind': 'error', 'text': templates.SERVICE_DOWN})
            return done('error')
        finally:
            out['budget_gb'] = {'max': budget.max_gb,
                                'spent': round(budget.spent_gb, 4)}

        found = {e['type'] for e in out['entities'] if e['found']}
        sections.append({'kind': 'footer', 'text': '\n'.join(render.footer(
            found, out['sources'], out['data_as_of']))})
        return done('answered' if found else 'not_found')

    def _entity_sections(self, key: dict, asked: set[str],
                         budget: ql_backend.Budget, out: dict) -> list[dict]:
        """Fetches one entity's facts and renders its sections.

        Args:
          key: {'entity_type': ..., 'entity_id': ...}.
          asked: The router's run_queries.
          budget: The question's budget, shared by every query.
          out: The answer being built; entities and sources are added to it.

        Returns:
          The headline and the asked sections, in SECTION_ORDER.
        """
        et, eid = key['entity_type'], key['entity_id']
        facts = summary.device_summary(self.backend, et, eid, budget)
        has_reports = bool(facts['counts']['total_reports'])
        out['entities'].append({**facts['entity'], 'total_reports':
                                facts['counts']['total_reports']})
        self._cite(facts, out)
        blocks = {}
        if facts['entity']['found']:
            if 'identity' in asked:
                ident = summary.identity(self.backend, et, eid, budget)
                self._cite(ident, out)
                blocks['identity'] = render.identity(ident)
            if 'counts' in asked:
                blocks['counts'] = render.counts(facts)
            if 'problems' in asked and has_reports:
                blocks['problems'] = render.problems(facts)
            if 'recalls' in asked:
                blocks['recalls'] = render.recalls(facts)
            if 'narratives' in asked and has_reports:
                # Narratives are the only optional part that can be large (the biggest
                # product codes): over the cap they are left out, the facts stay.
                try:
                    narr = summary.narratives(self.backend, et, eid, budget=budget,
                                              event_types=asked_event_types(
                                                  out['question']))
                except ql_backend.QueryBudgetExceeded:
                    logging.warning('Narratives over budget: %s', eid)
                    blocks['narratives'] = [templates.NARRATIVES_BUDGET.format(
                        kind=et.replace('_', ' '))]
                else:
                    self._cite(narr, out)
                    blocks['narratives'] = render.narratives(narr)
        result = [{'kind': 'headline', 'entity': eid,
                   'text': render.headline(facts)}]
        for kind in SECTION_ORDER:
            if blocks.get(kind):
                result.append({'kind': kind, 'entity': eid,
                               'text': '\n'.join(blocks[kind])})
        return result

    @staticmethod
    def _cite(result: dict, out: dict) -> None:
        out['sources'].extend(result.get('sources', []))
        out['data_as_of'].update(result.get('data_as_of') or {})
