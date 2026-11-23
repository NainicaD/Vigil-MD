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
import os
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
# No label passed its threshold, no advice, but the question names one device clearly (or comes
# from a device page): its summary is shown instead of "unclear" (the design always fetches it).
SUMMARY_FALLBACK = ('counts', 'problems', 'recalls')
# Event types named in a question: narratives then show only those ("describe the death
# reports"). Counts, problems and recalls are not filtered. "dead" is left out on purpose:
# "the pump went dead" is a malfunction, not a death.
EVENT_PATTERNS = (
    ('Death', re.compile(r'\b(?:deaths?|died|fatal(?:ity|ities)?)\b', re.I)),
    ('Injury', re.compile(r'\binjur(?:y|ies|ed|ing)\b', re.I)),
    ('Malfunction', re.compile(r'\bmalfunction(?:s|ed|ing)?\b', re.I)),
)


def named_event_types(question: str) -> tuple[str, ...]:
    """Returns the event types the question names, in the fixed order; () if none."""
    return tuple(et for et, pat in EVENT_PATTERNS if pat.search(question))


def asked_event_types(question: str) -> tuple[str, ...]:
    """Returns the event types the question names, in the fixed order; all three if none."""
    return named_event_types(question) or tuple(et for et, _ in EVENT_PATTERNS)
MAX_ENTITIES = 3  # Devices compared in one answer ("X vs Y vs Z").
# No label passed its threshold, but the question names deaths, injuries or malfunctions ("are
# there any deaths associated to this", counts 0.57 against 0.75): it is a counts question. With
# no device the bot then asks which device instead of "unclear". Recorded in rule_hits.
EVENT_WORD_COUNTS = 'event_word_counts'
# Internal key of answer(): router scores and drift signals for api/events.py. Not public.
ROUTER_SIGNALS = 'router_signals'
# Short-term memory (one turn back, kept by the browser): what answer() used from `context`,
# in out['context_used']. None: the question was answered on its own.
CONTEXT_PREVIOUS_QUESTION = 'previous_question'   # the bot had asked which device (or not
                                                  # understood); this message named one
CONTEXT_ENTITY = 'entity'                         # no device named: the previous answer's
                                                  # entity was used
CONTEXT_USED = (CONTEXT_PREVIOUS_QUESTION, CONTEXT_ENTITY)
# Previous answers that leave a question waiting for a device.
PENDING_STATUSES = ('need_device', 'unclear')
# The router the chatbot loads. VIGIL_ROUTER_DIR points it at another model directory, e.g. a
# candidate under test (models/minilm_tfidf, which needs requirements-tf.txt); unset: the champion.
DEFAULT_MODEL_DIR = pathlib.Path(
    os.environ.get('VIGIL_ROUTER_DIR')
    or pathlib.Path(__file__).resolve().parents[1] / 'models' / 'baseline')
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
             max_gb: float | None = None, cache_size: int = 0) -> 'Chatbot':
        """Loads the router model, the search index and a BigQuery backend.

        Args:
          dataset: BigQuery dataset and index name; None uses
            VIGIL_BQ_DATASET (default vigil_md_prod).
          model_dir: The router model directory.
          max_gb: Cost cap per question.
          cache_size: BigQuery results kept until their tables change (0: no cache; the
            API sets it, evaluations leave it off so every question is measured).

        Returns:
          A ready Chatbot.
        """
        dataset = dataset or config.DATASET
        return cls(predict.QueryRouter.load(model_dir),
                   search.DeviceIndex.open(dataset),
                   ql_backend.BigQueryBackend(dataset=dataset, cache_size=cache_size), max_gb)

    def answer(self, question: str,
               entity: tuple[str, str] | None = None,
               context: dict | None = None) -> dict:
        """Answers one question.

        Args:
          question: What the user typed.
          entity: (entity_type, entity_id) to skip the device search, e.g. on
            a device page or for a follow-up question.
          context: The previous turn, kept by the browser (one turn back, never stored
            here): {'previous_question': str, 'previous_status': str,
            'entity': (entity_type, entity_id) | None}. Rules, in this order:
            1. An explicit `entity` wins; context is ignored.
            2. The new question is routed as always: help, scope and refusal replies are
               final, so context never turns a refusal into an answer.
            3. Pending question: the previous answer was need_device or unclear, the new
               message has no label and no event word, and its search resolves one entry:
               the previous question is answered about that entry (routed again, so its own
               refusal still applies). context_used = 'previous_question'.
            4. Carry-over: the new question names no device (search none, empty phrase), has
               a label or an event word, and context has an entity: that entity is used.
               context_used = 'entity'. A device named in the new question wins.
            5. Otherwise the question is answered on its own.

        Returns:
          A JSON-friendly dict: status, text, sections, labels, entities,
          sources, data_as_of, budget_gb, search, context_used and both fingerprints.
        """
        if entity:
            context = None                                  # rule 1
        route = self.router.classify(question)
        if context:
            out = self._answer_pending(question, route, context)   # rule 3
            if out is not None:
                return out
        carry = tuple(context['entity']) if context and context.get('entity') else None
        return self._answer(question, route, entity, carry)

    def _answer_pending(self, question: str, route: dict, context: dict) -> dict | None:
        """Rule 3: the new message only names the entry the bot asked for. Returns the
        previous question's answer about it, or None if the rule does not apply."""
        previous = (context.get('previous_question') or '').strip()
        if (not previous or context.get('previous_status') not in PENDING_STATUSES
                or route['reply_kind'] is not None or route['labels']
                or named_event_types(question)):
            return None
        res = self.index.resolve(question)
        if res.status != 'resolved' or len(res.matches) != 1:
            return None
        key = self.index.query_key(res.matches[0])
        out = self._answer(previous, self.router.classify(previous),
                           (key['entity_type'], key['entity_id']), None)
        out['context_used'] = CONTEXT_PREVIOUS_QUESTION
        out['search'] = self._search_dict(res)              # what this message resolved to
        return out

    @staticmethod
    def _search_dict(res) -> dict:
        return {'status': res.status, 'phrase': res.phrase,
                'matches': [{'type': h.kind, 'id': h.entity_id,
                             'title': h.title, 'score': h.score}
                            for h in res.matches]}

    def _answer(self, question: str, route: dict, entity: tuple[str, str] | None,
                carry: tuple[str, str] | None) -> dict:
        """Answers one routed question; `carry` is the previous answer's entity (rule 4)."""
        out = {'question': question, 'labels': route['labels'],
               'run_queries': route['run_queries'],
               'reply_kind': route['reply_kind'],
               'rule_hits': route.get('rule_hits', []), 'entities': [],
               'sections': [], 'sources': [], 'data_as_of': {},
               'budget_gb': None, 'search': None, 'context_used': None,
               'template_fingerprint': templates.templates_fingerprint(),
               'rules_fingerprint': guardrails.rules_fingerprint(),
               # For monitoring (api/events.py); the API removes it before replying.
               ROUTER_SIGNALS: {'scores': route.get('scores') or {},
                                'unclear': route.get('unclear'),
                                'unknown_word_share': route.get('unknown_word_share')}}
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
        # No label passed its threshold ("How many deaths has the HeartMate 3 had?" scored
        # 0.768 for counts against 0.77). Refusals and fixed replies are settled above, so the
        # summary fallback below only ever answers a factual question.
        fallback = not route['run_queries']
        if fallback and route['reply_kind'] == 'medical_refusal':
            return done('refused')
        if fallback and route['reply_kind'] is None and named_event_types(question):
            out['run_queries'] = ['counts']
            out['rule_hits'] = [*out['rule_hits'], EVENT_WORD_COUNTS]
            fallback = False

        def unclear() -> dict:
            sections.append({'kind': 'unclear', 'text': templates.UNCLEAR})
            return done('unclear')

        if entity:
            keys = [{'entity_type': entity[0], 'entity_id': entity[1]}]
        else:
            res = self.index.resolve(question)
            out['search'] = self._search_dict(res)
        if not entity and carry and not fallback and res.status == 'none' and not res.phrase:
            # Rule 4: no device named here; the previous answer's entity is used. Only for a
            # question with a label or an event word: "ok" alone gets UNCLEAR, not a summary.
            keys = [{'entity_type': carry[0], 'entity_id': carry[1]}]
            out['context_used'] = CONTEXT_ENTITY
        elif not entity:
            if fallback and res.status not in ('resolved', 'several'):
                return unclear()        # no clear device either: ask to rephrase
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

        if fallback:
            sections.append({'kind': 'note', 'text': templates.SUMMARY_FALLBACK_NOTE})
            out['run_queries'] = list(SUMMARY_FALLBACK)
        budget = (ql_backend.Budget() if self.max_gb is None
                  else ql_backend.Budget(max_gb=self.max_gb))
        asked = set(out['run_queries'])
        try:
            for key in keys:
                sections.extend(self._entity_sections(key, asked, budget, out))
        except ql_backend.QueryBudgetExceeded:
            logging.warning('Over budget (question of %d characters)', len(question))
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
                blocks['narratives'] = self._narratives(
                    et, eid, asked_event_types(out['question']), budget, out)
        result = [{'kind': 'headline', 'entity': eid,
                   'text': render.headline(facts)}]
        for kind in SECTION_ORDER:
            if blocks.get(kind):
                result.append({'kind': kind, 'entity': eid,
                               'text': '\n'.join(blocks[kind])})
        return result

    def _narratives(self, et: str, eid: str, event_types: tuple[str, ...],
                    budget: ql_backend.Budget, out: dict) -> list[str]:
        """Report texts. Narratives are the only optional part that can be large: the texts of
        several event types span many months of maude_text. Over the cap, one event type at a
        time is tried (Death, Injury, Malfunction), skipping a type without texts. The cap is
        checked by a dry run before the texts are read, so a refused try costs only its first
        step (which reports, about 10 MB). If none fits, a note; the facts stay."""
        tries = [event_types] + ([(t,) for t in event_types] if len(event_types) > 1 else [])
        for i, types in enumerate(tries):
            try:
                narr = summary.narratives(self.backend, et, eid, budget=budget,
                                          event_types=types)
            except ql_backend.QueryBudgetExceeded:
                logging.warning('Narratives over budget: %s %s', eid, '+'.join(types))
                continue
            if i and not narr.get('excerpts'):
                continue                # no texts of this type: try the next one
            self._cite(narr, out)
            lines = render.narratives(narr)
            if i:
                lines.append(templates.NARRATIVES_NARROWED.format(events=types[0].lower()))
            return lines
        if len(event_types) == 1:
            return [templates.NARRATIVES_BUDGET_ONE.format(events=event_types[0].lower())]
        return [templates.NARRATIVES_BUDGET]

    @staticmethod
    def _cite(result: dict, out: dict) -> None:
        out['sources'].extend(result.get('sources', []))
        out['data_as_of'].update(result.get('data_as_of') or {})
