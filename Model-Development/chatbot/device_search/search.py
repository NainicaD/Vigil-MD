"""Device search, shared by the website's search box and the chatbot.

    index = DeviceIndex.open("vigil_md_dev")
    index.search("dreamstaton")            # ranked hits: devices, companies, product codes
    index.resolve("how many deaths for the dreamstaton?")
    # Resolution(status="resolved" | "several" | "ambiguous" | "none", matches=[...])
    index.query_key(hit)                   # {"entity_type", "entity_id"} for the query layer

Ranking: how well the user's words match the entry (typos allowed: difflib similarity,
prefixes of 4+ letters), then the number of reports. Words in a device's name and model
numbers count fully; its company name counts a little less, so "medtronic" ranks the
company first and "philips dreamstation" still finds the DreamStation.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .paths import index_path
from .normalize import norm, phrase_score, trigrams, word_similarity, words

MIN_SCORE = 0.75           # below this a hit is not shown
RESOLVE_SCORE = 0.85       # the top hit must reach this to be taken without asking
SUGGEST_SCORE = 0.80       # below this the closest matches are not offered as choices: "not found"
OTHER_WORD_WEIGHT = 0.9    # a match on a device's company counts this much
DOMINANT_RATIO = 3         # equal matches in one family: the top is taken at 3x the next one's reports
CROSS_COMPANY_RATIO = 20   # equal matches from different companies: only at 20x ("breast implants" asks)
KIND_ORDER = {"device": 0, "manufacturer": 1, "product_code": 2}

# Words in a question that are about what to fetch, not which device
STOPWORDS = set("""
a about after all also an and any are as at be been being by can compare compared
count counts deaths death describe did do does died ever events for from get give had
has have how i in injuries injury injured is it its last list made make makes
malfunction malfunctions manufacturer many me most my number of on or past people
please problem problems recall recalled recalls report reported reports show since so
summarize summary tell than that the there these this those to top total versus vs was
were what when which who why with year years you your company firm patients patient
happened happen issues issue common main more any reason reasons class device devices
fda maude goes go going went wrong work works working known about udi code product
safe safety unsafe dangerous risky good bad better best reliable ok okay worth should
stop stopped use using used keep still normal worried trust think
""".split())
_UDI = re.compile(r"\b\d{8,14}\b")
_CODE = re.compile(r"\b[A-Z]{3}\b")
_SPLIT = re.compile(r"\s+(?:vs\.?|versus|compared (?:to|with)|and|or)\s+|,", re.IGNORECASE)


@dataclass
class Hit:
    id: str                         # index key "kind|entity_id"
    kind: str                       # device | manufacturer | product_code (= entity_type)
    entity_id: str
    title: str
    subtitle: str
    reports: int
    deaths: int
    injuries: int
    product_codes: list[str]
    company_norm: str | None
    score: float
    data: dict = field(default_factory=dict)


@dataclass
class Resolution:
    status: str                     # resolved | several | ambiguous | none
    matches: list[Hit]
    phrase: str                     # the words that were searched


def score_entry(query_words: list[str], title_words: list[str], other_words: list[str]) -> float:
    """Mean over query words of the best similarity: full weight on title words, a little
    less on other words. Pure function."""
    if not query_words:
        return 0.0
    total = 0.0
    for q in query_words:
        best = max((word_similarity(q, t) for t in title_words), default=0.0)
        other = max((word_similarity(q, t) for t in other_words), default=0.0) * OTHER_WORD_WEIGHT
        total += max(best, other)
    return total / len(query_words)


def name_parts(question: str) -> set[str]:
    """Words that belong to a name even if they are stopwords or single letters: parts of a
    hyphenated name ("V-Go", "Accu-Chek"), a capitalised word right after a device word
    ("DreamStation Go") and a one-digit model number right after one ("HeartMate 3",
    "SAPIEN 3 Ultra"). Pure function."""
    keep = {w for m in re.finditer(r"\w+(?:-\w+)+", question) for w in words(m.group())}
    tokens = question.split()
    for prev, tok in zip(tokens, tokens[1:]):
        prev_words = words(prev)
        if (prev_words and prev_words[-1] not in STOPWORDS
                and not prev.endswith((".", "?", "!", ","))
                and (tok[:1].isupper() or re.fullmatch(r"\d[™®]?[?.!,]?", tok))):
            keep.update(words(tok))
    return keep


def device_words(question: str) -> list[str]:
    """The question's words that could name a device: stopwords, 4-digit years and single
    letters removed, unless they are part of a name (name_parts). Pure function."""
    keep = name_parts(question)
    return [w for w in words(question)
            if w in keep or (w not in STOPWORDS and len(w) > 1
                             and not re.fullmatch(r"(19|20)\d\d", w))]


def _matched_only_in_suffix(hit: "Hit", typed: set[str]) -> bool:
    """True for a manufacturer entry "COMPANY - SUFFIX" that shares no typed word with
    COMPANY (the typed words matched its suffix only). Pure function."""
    if hit.kind != "manufacturer" or " - " not in hit.title:
        return False
    return not set(words(hit.title.split(" - ", 1)[0])) & typed


class DeviceIndex:
    def __init__(self, con: sqlite3.Connection):
        self.con = con
        self.con.row_factory = sqlite3.Row
        self.meta = dict(self.con.execute("SELECT key, value FROM meta").fetchall())

    @classmethod
    def open(cls, dataset_or_path: str | Path) -> "DeviceIndex":
        p = Path(dataset_or_path)
        path = p if p.suffix == ".sqlite" else index_path(str(dataset_or_path))
        if not path.exists():
            raise FileNotFoundError(f"{path} not found: run python -m device_search.build_index")
        return cls(sqlite3.connect(f"file:{path}?mode=ro", uri=True))

    def _hit(self, row: sqlite3.Row, score: float) -> Hit:
        return Hit(row["id"], row["kind"], row["entity_id"], row["title"], row["subtitle"] or "",
                   row["reports"],
                   row["deaths"], row["injuries"],
                   [c for c in (row["product_codes"] or "").split(",") if c],
                   row["company_norm"], round(score, 4), json.loads(row["data"] or "{}"))

    def get(self, entity_id: str) -> Hit | None:
        row = self.con.execute("SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone()
        return self._hit(row, 1.0) if row else None

    def by_udi(self, udi: str) -> Hit | None:
        row = self.con.execute("SELECT entity_id FROM udis WHERE udi_di = ?", (udi,)).fetchone()
        return self.get(row[0]) if row else None

    def search(self, text: str, limit: int = 10, candidates: int = 3000) -> list[Hit]:
        """Ranked hits for free text (a search box query or the device words of a question).

        candidates: entries taken from the trigram index before exact scoring. On the PROD
        index (337,729 devices) 300 missed "t:slim x2 control iq", crowded out by entries
        sharing common trigrams; 3,000 finds it (about 0.4 s instead of 0.07 s)."""
        q = words(text)
        if not q:
            return []
        if len(q) == 1 and _UDI.fullmatch(q[0]):
            hit = self.by_udi(q[0])
            return [hit] if hit else []
        grams = sorted({g for w in q for g in trigrams(w)})
        if grams:
            match = " OR ".join(f'"{g}"' for g in grams)
            ids = [r[0] for r in self.con.execute(
                "SELECT id FROM entities_fts WHERE entities_fts MATCH ? ORDER BY bm25(entities_fts) "
                "LIMIT ?", (match, candidates))]
        else:   # words shorter than 3 letters: compare with every entry's title words
            ids = [r[0] for r in self.con.execute("SELECT id FROM entities")]
        hits = []
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            for row in self.con.execute(
                    f"SELECT * FROM entities WHERE id IN ({','.join('?' * len(chunk))})", chunk):
                s = score_entry(q, row["title_words"].split(), row["other_words"].split())
                if s >= MIN_SCORE:
                    hits.append(self._hit(row, s))
        hits.sort(key=lambda h: (-round(h.score, 2), -h.reports, KIND_ORDER[h.kind], h.title))
        return hits[:limit]

    def _resolve_phrase(self, phrase: str) -> Resolution:
        res = self._resolve_exact(phrase)
        if res.status == "resolved" or len(phrase.split()) < 2:
            return res
        # Chatty words ("…stop using mine", "…most often") lower every score: retry with
        # only the words that match an entry confidently ("mine" is only a typo-level match
        # for "MiniMe"); keep the retry only if it resolves to a device. A dropped word can be
        # a model name that is not in the index ("Medtronic Azure", "Philips Trilogy"), so a
        # retry that leaves only a company or product code is not taken: the user asked about
        # one device, not the whole firm. A word with a digit is a model number and is never
        # dropped ("MiniMed 780G").
        kept = [w for w in phrase.split() if re.search(r"\d", w)
                or any(h.score >= RESOLVE_SCORE for h in self.search(w, limit=1))]
        if kept and len(kept) < len(phrase.split()):
            retry = self._resolve_exact(" ".join(kept))
            if retry.status == "resolved" and retry.matches[0].kind == "device":
                return retry
        return res

    def _resolve_exact(self, phrase: str) -> Resolution:
        hits = self.search(phrase, limit=5)
        if not hits or hits[0].score < SUGGEST_SCORE:
            # nothing close enough to offer ("Zorbix 3000" is not "SPIFE 3000")
            return Resolution("none", [], phrase)
        if hits[0].score < RESOLVE_SCORE:
            return Resolution("ambiguous", hits[:3], phrase)
        top, rest = hits[0], [h for h in hits[1:] if round(h.score, 2) == round(hits[0].score, 2)]
        # Equal scores: the one entry whose whole name was typed wins ("Natrelle INSPIRA Breast
        # Implants" over "Natrelle Inspira SoftTouch Breast Implants").
        typed = set(words(phrase))
        whole = [h for h in [top] + rest if set(words(h.title)) <= typed]
        if rest and len(whole) == 1:
            return Resolution("resolved", whole, phrase)
        # Some manufacturer spellings carry a product after " - " ("MANNKIND CORPORATION -
        # V-GO"). If the typed words match such an entry only in that suffix, it is the
        # product that was asked about: the entry leaves the tie and the devices decide
        # ("V-Go"). Typing the company ("mannkind") or a plain company name ("MENTOR TEXAS",
        # "OMNIPOD") keeps the entry, so those ties are unchanged.
        tied = [top] + rest
        kept = [h for h in tied if not _matched_only_in_suffix(h, typed)]
        if len(kept) < len(tied) and any(h.kind == "device" for h in kept):
            top, rest = kept[0], kept[1:]
        # manufacturer entries are spellings of company names (one firm can have several,
        # until the matching team groups them); devices count as one family per company
        def family(h):
            return h.kind == top.kind and (top.kind == "manufacturer"
                                           or h.company_norm == top.company_norm)
        # Each group is compared with its own ratio: the top must beat the next entry of its
        # family by DOMINANT_RATIO and the next entry of other companies by CROSS_COMPANY_RATIO
        # (Alaris: 51,260 vs 5,636 from the same company and 0 from another company).
        same = [h.reports for h in rest if family(h)]
        other = [h.reports for h in rest if not family(h)]
        dominates = (top.reports >= DOMINANT_RATIO * max(1, max(same, default=0))
                     and top.reports >= CROSS_COMPANY_RATIO * max(1, max(other, default=0)))
        if not rest or dominates:
            # one match; one product family where one entry dominates (DreamStation vs
            # DreamStation Go); or one entry far above other companies' (Impella vs a small
            # accessory). Similar matches across companies ("breast implants") ask.
            return Resolution("resolved", [top], phrase)
        return Resolution("ambiguous", [top] + rest[:2], phrase)

    def resolve(self, question: str) -> Resolution:
        """Which device(s) a chat question is about.

        resolved   one clear match
        several    a comparison ("X vs Y", "X and Y") where each part resolves
        ambiguous  close matches: the bot shows them and asks which one
        none       no device words, or nothing found: the bot asks which device
        Exact identifiers win: a UDI (8-14 digits), then a known product code in capitals."""
        for udi in _UDI.findall(question):
            hit = self.by_udi(udi)
            if hit:
                return Resolution("resolved", [hit], udi)
        for code in _CODE.findall(question):
            hit = self.get("product_code|" + code)
            if hit:
                return Resolution("resolved", [hit], code)
        parts = [p for p in _SPLIT.split(question) if device_words(p)]
        if len(parts) > 1:
            resolved = [self._resolve_phrase(" ".join(device_words(p))) for p in parts]
            if all(r.status == "resolved" for r in resolved):
                return Resolution("several", [r.matches[0] for r in resolved],
                                  " | ".join(r.phrase for r in resolved))
        phrase = " ".join(device_words(question))
        if not phrase:
            return Resolution("none", [], "")
        return self._resolve_phrase(phrase)

    def udis_for(self, hit: Hit) -> list[str]:
        return [r[0] for r in self.con.execute(
            "SELECT udi_di FROM udis WHERE entity_id = ? ORDER BY udi_di", (hit.id,))]

    @staticmethod
    def query_key(hit: Hit) -> dict:
        """What the query layer needs: device_summary(backend, **index.query_key(hit))."""
        return {"entity_type": hit.kind, "entity_id": hit.entity_id}
