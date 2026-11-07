"""Device search, shared by the website's search box and the chatbot.

    index = DeviceIndex.open("vigil_md_dev")
    index.search("dreamstaton")            # ranked hits: devices, companies, product codes
    index.resolve("how many deaths for the dreamstaton?")
    # Resolution(status="resolved" | "several" | "ambiguous" | "none", matches=[...])
    index.query_key(hit)                   # what the query layer needs for that hit

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
OTHER_WORD_WEIGHT = 0.9    # a match on a device's company counts this much
DOMINANT_RATIO = 3         # the top hit is taken if it has 3x the reports of the next one
KIND_ORDER = {"device": 0, "company": 1, "product_code": 2}

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
    id: str
    kind: str
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


def device_words(question: str) -> list[str]:
    """The question's words that could name a device: stopwords, 4-digit years and single
    letters removed. Pure function."""
    return [w for w in words(question)
            if w not in STOPWORDS and len(w) > 1 and not re.fullmatch(r"(19|20)\d\d", w)]


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
        return Hit(row["id"], row["kind"], row["title"], row["subtitle"] or "", row["reports"],
                   row["deaths"], row["injuries"],
                   [c for c in (row["product_codes"] or "").split(",") if c],
                   row["company_norm"], round(score, 4), json.loads(row["data"] or "{}"))

    def get(self, entity_id: str) -> Hit | None:
        row = self.con.execute("SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone()
        return self._hit(row, 1.0) if row else None

    def by_udi(self, udi: str) -> Hit | None:
        row = self.con.execute("SELECT entity_id FROM udis WHERE udi_di = ?", (udi,)).fetchone()
        return self.get(row[0]) if row else None

    def search(self, text: str, limit: int = 10, candidates: int = 300) -> list[Hit]:
        """Ranked hits for free text (a search box query or the device words of a question)."""
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
        hits = self.search(phrase, limit=5)
        if not hits or hits[0].score < RESOLVE_SCORE:
            return Resolution("none" if not hits else "ambiguous", hits[:3], phrase)
        top, rest = hits[0], [h for h in hits[1:] if round(h.score, 2) == round(hits[0].score, 2)]
        if not rest or top.reports >= DOMINANT_RATIO * max(1, rest[0].reports):
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
            hit = self.get("pc:" + code)
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

    def udis_for(self, entity_id: str) -> list[str]:
        return [r[0] for r in self.con.execute(
            "SELECT udi_di FROM udis WHERE entity_id = ? ORDER BY udi_di", (entity_id,))]

    def query_key(self, hit: Hit) -> dict:
        """What the query layer needs for a hit: key_type, key, and a recall_key for devices
        (recalls have no UDI, so they are found by the device name in the recall text)."""
        if hit.kind == "device":
            return {"key_type": "udi_dis", "key": self.udis_for(hit.id),
                    "recall_key": ("brand", norm(hit.title))}
        if hit.kind == "company":
            return {"key_type": "manufacturer", "key": hit.company_norm, "recall_key": None}
        return {"key_type": "product_code", "key": hit.product_codes[0], "recall_key": None}
