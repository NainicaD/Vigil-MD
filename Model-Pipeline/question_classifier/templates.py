"""Question templates and slot values for the synthetic training questions.

Templates are grouped by label and by audience, so the evaluation can check every
audience's wording. Slots: {device} brand/model name, {generic} device type, {company}
manufacturer, {code} FDA product code, {event} event type, {year}, {udi}.

Slot values in slots.json are a starting set. Replace them with values taken from the
warehouse tables (brand names, generic names, manufacturers, product codes) so the
questions name devices the bot will actually see.
"""
from __future__ import annotations

# (audience, template). Keep each template's meaning inside its label's rules in labels.py.
TEMPLATES: dict[str, list[tuple[str, str]]] = {
    "counts": [
        ("neutral", "how many {event} reports does {device} have"),
        ("neutral", "how many {event} reports are there for {device} since {year}"),
        ("neutral", "number of {event} reports for {generic} devices"),
        ("neutral", "how many reports mention {device}"),
        ("neutral", "total adverse event reports for {company}"),
        ("patient", "how many people have reported problems with {device}"),
        ("patient", "has anyone died using {device}"),
        ("patient", "how many injuries have been reported for {generic}s"),
        ("clinician", "how many {event} reports have been filed for {device} in {year}"),
        ("clinician", "what is the trend in {event} reports for {generic}s over the last few years"),
        ("researcher", "count of MDRs for product code {code} from {year} to 2025"),
        ("researcher", "number of {event} MDRs by year for {company}"),
        ("researcher", "MAUDE report counts for {code} by event type"),
        ("procurement", "compare the number of {event} reports for {device} and {device2}"),
        ("procurement", "which has more reports, {device} or {device2}"),
        ("procurement", "how many {event} reports has {company} had for its {generic}s"),
    ],
    "problems": [
        ("neutral", "what problems are reported for {device}"),
        ("neutral", "what goes wrong with {device}"),
        ("neutral", "most common problems with {generic}s"),
        ("neutral", "what device problems are reported for {company}"),
        ("patient", "what kind of issues do people have with {device}"),
        ("patient", "what are the common complaints about {generic}s"),
        ("clinician", "what patient problems are reported with {device}"),
        ("clinician", "top reported failure modes for {generic}s"),
        ("researcher", "top device problem terms for product code {code}"),
        ("researcher", "distribution of patient problem codes for {code} reports"),
        ("procurement", "what are the main reported problems with {company} {generic}s"),
        ("procurement", "known failure issues for {device}"),
    ],
    "recalls": [
        ("neutral", "was {device} ever recalled"),
        ("neutral", "has {company} had any recalls"),
        ("neutral", "why was {device} recalled"),
        ("neutral", "recalls for {generic}s"),
        ("patient", "was my {generic} recalled"),
        ("patient", "is there a recall on {device}"),
        ("clinician", "what class was the {device} recall"),
        ("clinician", "what were users told to do in the {device} recall"),
        ("researcher", "list class I recalls for product code {code}"),
        ("researcher", "root causes of recalls for {code} since {year}"),
        ("procurement", "how many recalls has {company} had since {year}"),
        ("procurement", "are there any open recalls for {device}"),
    ],
    "narratives": [
        ("neutral", "describe the injury reports for {device}"),
        ("neutral", "what happened in the death reports for {device}"),
        ("neutral", "show me some example reports for {generic}s"),
        ("neutral", "summarize the {event} reports for {device}"),
        ("patient", "what happened to people who were hurt by {device}"),
        ("patient", "tell me the stories in the reports about {generic}s"),
        ("clinician", "give me a few event descriptions for {device} injuries"),
        ("clinician", "what did reporters describe in {event} reports for {generic}s"),
        ("researcher", "sample narratives from {code} death reports in {year}"),
        ("researcher", "event description text for {device} MDRs"),
        ("procurement", "what do the injury reports for {company} {generic}s say"),
    ],
    "identity": [
        ("neutral", "who makes {device}"),
        ("neutral", "who manufactures UDI {udi}"),
        ("neutral", "what device is UDI {udi}"),
        ("neutral", "what is product code {code}"),
        ("patient", "what company makes my {generic}"),
        ("clinician", "which manufacturer makes {device}"),
        ("researcher", "what device type is product code {code}"),
        ("researcher", "map UDI {udi} to its device record"),
        ("procurement", "which companies make {generic}s"),
    ],
    "out_of_scope": [
        # medical advice
        ("patient", "should i stop using my {generic}"),
        ("patient", "is it safe to keep using {device}"),
        ("patient", "my {generic} keeps alarming, what should i do"),
        ("patient", "can i still use my {device} after the recall"),
        ("patient", "is {device} safe for my child"),
        ("patient", "i have a rash where my {generic} is, is that normal"),
        ("patient", "how much insulin should i take if my pump fails"),
        ("patient", "should my mom switch from {device} to {device2}"),
        ("patient", "im worried about my {device}, is it dangerous"),
        ("clinician", "should i keep prescribing {device} to my patients"),
        ("clinician", "would you recommend {device} for an elderly patient"),
        ("clinician", "which {generic} is safest for pediatric patients"),
        ("procurement", "which {generic} should our hospital buy"),
        ("procurement", "is {device} better than {device2}"),
        ("neutral", "is {device} a safe device"),
        ("neutral", "what is the best {generic} on the market"),
        ("neutral", "can you give me medical advice about {device}"),
        # unrelated (kept in UNRELATED below so evaluation can separate them from advice)
        ("neutral", "what is the weather today"),
        ("neutral", "write me a poem about the ocean"),
        ("neutral", "what stocks should i buy"),
        ("neutral", "who won the game last night"),
        ("neutral", "translate hello into spanish"),
        ("neutral", "tell me a joke"),
        ("neutral", "what is the capital of france"),
        ("neutral", "help me with my math homework"),
        ("neutral", "what time is it in tokyo"),
        ("neutral", "recommend a good movie"),
        ("neutral", "how do i bake bread"),
        ("neutral", "what is the news today"),
        ("neutral", "book me a flight to boston"),
        ("neutral", "who are you"),
        ("neutral", "write python code to sort a list"),
    ],
}

# out_of_scope templates that are unrelated topics, not medical advice. Evaluation reports
# advice recall (the safety number) separately from unrelated recall.
UNRELATED = {t for _, t in TEMPLATES["out_of_scope"][TEMPLATES["out_of_scope"].index(
    ("neutral", "what is the weather today")):]}

# Ways two questions are joined into one multi-label question.
JOINERS = (" and ", ", also ", ". ", "? and ", " - also ")

# Openers and closers that real users add (audience-neutral).
OPENERS = ("", "", "", "hi, ", "quick question: ", "can you tell me ", "i want to know ",
           "please ", "hey ")
CLOSERS = ("", "", "", "?", "?", " please", " thanks")
