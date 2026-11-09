"""Turns query-layer results into answer text with the fixed templates.

Pure functions: no BigQuery and no model, so every sentence can be tested from
a dict.
"""

import re

from answer import templates
from query_layer import summary

EVENT_ORDER = ('Death', 'Injury', 'Malfunction')
EVENT_WORDS = {
    'Death': ('death', 'deaths'),
    'Injury': ('injury', 'injuries'),
    'Malfunction': ('malfunction', 'malfunctions'),
}
TOP_TERMS = 3  # Problem terms shown per type.
YEARS_SHOWN = 5  # Latest years in the per-year line.
RECALLS_SHOWN = 3  # Newest recalls listed.
REASON_CHARS = 200  # Recall reason and action, cut at a word.
_ROMAN = {'1': 'I', '2': 'II', '3': 'III', 'I': 'I', 'II': 'II', 'III': 'III'}
_WITHHELD = re.compile(r'\(b\)\s*\((?:4|6)\)', re.IGNORECASE)


def num(n: int | None) -> str:
    """Formats a count with thousands separators ("30,356")."""
    return f'{int(n or 0):,}'


def plural(n: int | None, one: str, many: str) -> str:
    """Returns e.g. "1 death" or "2 deaths"."""
    return f'{num(n)} {one if int(n or 0) == 1 else many}'


def reports(n: int | None) -> str:
    """Returns e.g. "1 report" or "30,356 reports"."""
    return plural(n, 'report', 'reports')


def join(items: list[str]) -> str:
    """Joins items as "a, b and c"."""
    if len(items) <= 2:
        return ' and '.join(items)
    return ', '.join(items[:-1]) + ' and ' + items[-1]


def shorten(text: str | None, max_chars: int) -> str:
    """Collapses whitespace and cuts text at a word, marked with "…"."""
    text = ' '.join((text or '').split())
    return summary.excerpt(text, len(text), max_chars)


def year_span(by_year: dict) -> str:
    """Returns " (2021–2025)", " (2024)" or "" for the years with reports."""
    years = sorted(int(y) for y in by_year)
    if not years:
        return ''
    if years[0] == years[-1]:
        return f' ({years[0]})'
    return f' ({years[0]}–{years[-1]})'


def headline(result: dict) -> str:
    """Returns the first line about one entity.

    Args:
      result: A device_summary() result.

    Returns:
      The entity's name and report total, or a not-found sentence.
    """
    entity, counts_ = result['entity'], result['counts']
    if entity['type'] == 'product_code':
        name = f'product code {entity["id"]}'
    else:
        name = entity.get('title') or entity['id']
    if not entity['found']:
        return templates.NOT_FOUND.format(name=name)
    if not counts_['total_reports']:
        return templates.NO_REPORTS.format(name=name)
    total = reports(counts_['total_reports'])
    span = year_span(counts_['by_year'])
    if entity['type'] == 'device':
        company = entity.get('subtitle')
        return templates.HEADLINE['device'].format(
            name=name, company=f' ({company})' if company else '',
            reports=total, span=span)
    if entity['type'] == 'product_code':
        title = entity.get('title') or ''
        same = (entity['id'].lower(), f'product code {entity["id"]}'.lower())
        return templates.HEADLINE['product_code'].format(
            code=entity['id'],
            title=f' ({title})' if title.lower() not in ('',) + same else '',
            reports=total, span=span)
    return templates.HEADLINE['manufacturer'].format(
        name=name, reports=total, span=span)


def counts(result: dict) -> list[str]:
    """Returns the reports by event type and by year (device_summary())."""
    c = result['counts']
    if not c['total_reports']:
        return []
    by_type = c['by_event_type']
    parts = [plural(by_type[et], *EVENT_WORDS[et])
             for et in EVENT_ORDER if by_type.get(et)]
    other = sum(n for et, n in by_type.items() if et not in EVENT_ORDER)
    if other:
        parts.append(f'{num(other)} other or not stated')
    lines = [templates.COUNTS.format(by_type=join(parts))]
    years = sorted(c['by_year'].items(), key=lambda kv: int(kv[0]))
    years = years[-YEARS_SHOWN:]
    if len(years) > 1:
        per_year = ', '.join(f'{y}: {num(sum(v.values()))}' for y, v in years)
        lines.append(templates.COUNTS_YEARS.format(n=len(years),
                                                   years=per_year))
    return lines


def _terms(items: list[dict]) -> str:
    return join([f'{t["term"].lower()} ({num(t["reports"])})'
                 for t in items[:TOP_TERMS]])


def problems(result: dict) -> list[str]:
    """Returns the top device and patient problems (device_summary())."""
    p = result['problems']
    lines = []
    if p['device']:
        lines.append(
            templates.PROBLEMS_DEVICE.format(terms=_terms(p['device'])))
    if p['patient']:
        lines.append(
            templates.PROBLEMS_PATIENT.format(terms=_terms(p['patient'])))
    if p.get('no_harm_reports'):
        lines.append(
            templates.PROBLEMS_NO_HARM.format(n=reports(p['no_harm_reports'])))
    return lines or [templates.PROBLEMS_NONE]


def recall_class(value: str | None) -> str:
    """Returns "Class I" to "Class III", or FDA's own words for other values.

    Args:
      value: The FDA classification, e.g. "Class II", "2" or "Not Yet
        Classified".

    Returns:
      Text for the answer.
    """
    text = (value or '').strip()
    if not text:
        return 'class not listed'
    roman = _ROMAN.get(re.sub(r'(?i)^class\s*', '', text).upper())
    return f'Class {roman}' if roman else text


def recalls(result: dict) -> list[str]:
    """Returns the recall count and the newest recalls (device_summary())."""
    r = result['recalls']
    matched = templates.RECALL_MATCH.get(result['entity']['type'], '')
    if not r['total']:
        return [templates.RECALLS_NONE.format(matched=matched)]
    lines = [templates.RECALLS.format(n=num(r['total']), matched=matched)]
    # FDA files one recall number per model: identical ones share a line.
    groups = {}
    for rec in r['shown']:
        key = tuple(rec.get(k) for k in ('recall_class', 'record_date',
                                         'recall_status', 'reason_text',
                                         'action'))
        groups.setdefault(key, []).append(rec)
    listed = 0
    for rec, *same in list(groups.values())[:RECALLS_SHOWN]:
        listed += 1 + len(same)
        status = rec.get('recall_status')
        reason = shorten(rec.get('reason_text') or 'reason not given',
                         REASON_CHARS)
        action = rec.get('action')
        lines.append(templates.RECALL_ITEM.format(
            klass=recall_class(rec.get('recall_class')),
            date=(rec.get('record_date') or 'date not given')[:10],
            status=f' ({status.lower()})' if status else '',
            count=f', {1 + len(same)} recalls' if same else '',
            reason=f'"{reason}"',
            action=(f' Action: "{shorten(action, REASON_CHARS)}"'
                    if action else '')))
    if r['total'] > listed:
        lines.append(templates.RECALLS_MORE.format(n=num(r['total'] - listed)))
    return lines


def narratives(result: dict) -> list[str]:
    """Returns the quoted report excerpts (a narratives() result)."""
    excerpts = result.get('excerpts') or []
    if not excerpts:
        kind = result['entity']['type'].replace('_', ' ')
        return [templates.NARRATIVES_NONE.format(kind=kind)]
    lines = [templates.NARRATIVES.format(n=len(excerpts))]
    for e in excerpts:
        lines.append(templates.NARRATIVE_ITEM.format(
            event=e['event_type'] or 'Event type not given',
            date=(e['date_received'] or 'date not given')[:10],
            key=e['mdr_report_key'], text=' '.join(e['text'].split())))
    if any(_WITHHELD.search(e['text']) for e in excerpts):
        lines.append(templates.WITHHELD_NOTE)
    return lines


def identity(result: dict) -> list[str]:
    """Returns who makes a device, or what a product code or firm is.

    Args:
      result: An identity() result.

    Returns:
      One line, or no line when nothing was found.
    """
    d = result.get('details')
    if not d:
        return []
    entity_type = result['entity']['type']
    codes = list(d.get('product_codes') or [])
    codes_txt = ''
    if codes and entity_type != 'product_code':
        codes_txt = (f' Product code{"s" if len(codes) > 1 else ""}: '
                     f'{", ".join(codes[:5])}.')
    if entity_type == 'device':
        # GUDID's model field also holds bare numbers (UDIs, "00"): skip them.
        models = [m for m in d.get('models') or []
                  if m and len(m) > 2 and not m.isdigit()]
        n_models = d.get('n_models') or len(models)
        models_txt = ''
        if models:
            more = (f' ({num(n_models)} in total)'
                    if n_models > len(models[:5]) else '')
            # Model names can contain commas, so they are joined with "; ".
            models_txt = f' Models include {"; ".join(models[:5])}{more}.'
        company = (d.get('company') or 'a company not listed').rstrip('.')
        return [templates.IDENTITY_DEVICE.format(
            name=d.get('name') or 'This device', company=company,
            models=models_txt, codes=codes_txt)]
    names = [n for n in d.get('top_names') or [] if n]
    names_txt = (f' Names in the reports include {", ".join(names[:5])}.'
                 if names else '')
    return [templates.IDENTITY_OTHER.format(
        title=d.get('title') or result['entity']['id'], names=names_txt,
        codes=codes_txt)]


def ambiguous(phrase: str, matches: list) -> list[str]:
    """Returns the close matches as choices (device_search Hits)."""
    lines = [templates.AMBIGUOUS.format(phrase=phrase)]
    for h in matches:
        lines.append(templates.AMBIGUOUS_ITEM.format(
            title=h.title, subtitle=f', {h.subtitle}' if h.subtitle else '',
            reports=reports(h.reports)))
    return lines


def footer(entity_types: set[str], sources: list[dict],
           data_as_of: dict[str, str]) -> list[str]:
    """Returns the disclaimer, the UDI note and the sources.

    Args:
      entity_types: Types of the entities found; "device" adds the UDI note.
      sources: The query layer's "sources" of every query run.
      data_as_of: Table name -> last modified time (ISO).

    Returns:
      The closing lines; the date is the oldest table update.
    """
    lines = [templates.DISCLAIMER]
    if 'device' in entity_types:
        lines.append(templates.UDI_NOTE)
    tables = sorted({t for s in sources for t in s.get('tables', [])})
    if tables:
        names = dict.fromkeys(templates.SOURCE_NAMES.get(t, t) for t in tables)
        dates = sorted(v[:10] for v in data_as_of.values() if v)
        lines.append(templates.SOURCES.format(
            tables=', '.join(names), as_of=dates[0] if dates else 'unknown'))
    return lines
