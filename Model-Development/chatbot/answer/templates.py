"""The fixed text of every answer: only the blanks are filled in.

Rules (README, "Handoff: building the answer"):
  - Only numbers returned by the query layer; "N reports describe...", never a
    safety judgement (a test rejects every word in BANNED_WORDS).
  - MAUDE counts reports, not failure rates, and a report does not prove that
    the device caused the event.
  - For a device (UDI-linked entity), say "reports linked by UDI".
  - (b)(4) and (b)(6) in report text mark text the FDA withheld.

The medical refusal, scope and help replies are in query_router/guardrails.py.
Editing a template changes templates_fingerprint(), which every answer records.
"""

import hashlib

# Headline: one per entity, always shown.
HEADLINE = {
    'device': ('**{name}**{company}: {reports} linked by UDI in FDA MAUDE'
               '{span}.'),
    'product_code': '**Product code {code}**{title}: {reports} in FDA MAUDE'
                    '{span}.',
    'manufacturer': ('**{name}** (manufacturer): {reports} naming this '
                     'manufacturer in FDA MAUDE{span}.'),
}
NOT_FOUND = ('I found **{name}** in the device list, but no FDA MAUDE reports '
             'or recalls for it.')
NO_REPORTS = 'No FDA MAUDE reports were found for **{name}**.'

# Sections, in the order they are shown.
IDENTITY_DEVICE = ('**Who makes it:** {name} is listed in FDA GUDID by '
                   '{company}.{models}{codes}')
IDENTITY_OTHER = '**What it is:** {title}.{names}{codes}'
COUNTS = '**Reports by event type:** {by_type}.'
COUNTS_YEARS = 'Reports per year (latest {n} years with reports): {years}.'
PROBLEMS_DEVICE = '**Most reported device problems:** {terms}.'
PROBLEMS_PATIENT = '**Most reported patient problems:** {terms}.'
PROBLEMS_NO_HARM = '{n} said there was no patient harm.'
PROBLEMS_NONE = '**Problems:** no problem terms are recorded in these reports.'
RECALLS = '**Recalls:** {n} found{matched}. Newest first:'
RECALL_ITEM = '- {klass}, {date}{status}{count}: {reason}{action}'
RECALLS_MORE = '- …and {n} more.'
RECALLS_NONE = '**Recalls:** none found{matched}.'
RECALL_MATCH = {
    'device': ' whose product description names this device',
    'product_code': ' for this product code',
    'manufacturer': ' by this manufacturer',
}
NARRATIVES = ('**What the reports describe** ({n}, newest first, one event '
              'type at a time):')
NARRATIVE_ITEM = '- *{event}, received {date}* (report {key}): "{text}"'
NARRATIVES_NONE = ('**What the reports describe:** no report texts were found '
                   'for this {kind}.')
WITHHELD_NOTE = ('(b)(4) and (b)(6) mark text the FDA withheld (confidential '
                 'or personal).')

# Always last.
DISCLAIMER = ('MAUDE reports are sent in by manufacturers, health '
              'professionals and patients. A report does not prove that the '
              'device caused the event, and report counts are not failure '
              'rates.')
UDI_NOTE = ('Reports without a UDI are not linked to a device, so the real '
            'number can be higher.')
SOURCES = '**Sources:** {tables} (BigQuery, data as of {as_of}).'
SOURCE_NAMES = {
    'entity_catalog': 'FDA MAUDE and GUDID',
    'entity_counts': 'FDA MAUDE reports',
    'entity_problems': 'MAUDE problem codes',
    'entity_recalls': 'FDA recalls and enforcement',
    'entity_reports': 'FDA MAUDE reports',
    'maude_text': 'MAUDE report text',
    'device_catalog': 'FDA GUDID',
}

# Replies when there is nothing to look up or something went wrong.
UNCLEAR = ("I'm not sure what you're asking. I can tell you how many reports "
           'a device has (deaths, injuries, malfunctions), what problems are '
           'reported, whether it was recalled, what the reports describe, or '
           'who makes it. Could you rephrase?')
NEED_DEVICE = ('Which device do you mean? Give its brand name (for example '
               '"DreamStation"), the company, or its 3-letter FDA product '
               'code.')
NOT_FOUND_SEARCH = ('I couldn\'t find "{phrase}" in the FDA device list. Try '
                    'the brand name, the company, or the 3-letter FDA product '
                    'code.')
AMBIGUOUS = '"{phrase}" matches more than one entry. Which one do you mean?'
AMBIGUOUS_ITEM = '- {title}{subtitle} ({reports})'
TOO_MANY = 'I can compare up to {n} devices at a time; here are the first {n}.'
BUDGET = ('That question needs more data than I can read for one question. '
          'Try asking about a single device, or one part at a time (for '
          'example its recalls).')
SERVICE_DOWN = ("I can't reach the FDA data right now. Please try again in a "
                'few minutes.')

# Words no template may contain: they would judge a device or advise the user.
# Report texts and recall reasons are quoted from the FDA and may contain them.
BANNED_WORDS = frozenset((
    'safe', 'safer', 'safest', 'unsafe', 'dangerous', 'danger', 'risky',
    'reliable', 'unreliable', 'better', 'worse', 'best', 'worst', 'recommend',
    'should', 'trustworthy', 'concerning', 'alarming', 'good', 'bad', 'high',
    'low', 'rare', 'common', 'only', 'just'))


def all_templates() -> dict[str, str]:
    """Returns every fixed text in this module, by name."""
    out = {}
    for name, value in globals().items():
        if not name.isupper() or name == 'BANNED_WORDS':
            continue
        if isinstance(value, str):
            out[name] = value
        elif isinstance(value, dict):
            out.update({f'{name}.{k}': v for k, v in value.items()})
    return dict(sorted(out.items()))


def templates_fingerprint() -> str:
    """Returns a 12-character hash of every template (logged per answer)."""
    text = '\n'.join(f'{k}={v}' for k, v in all_templates().items())
    return hashlib.sha256(text.encode()).hexdigest()[:12]
