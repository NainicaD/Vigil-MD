"""Filters for the recall finder, the device page and their exports. Pure functions.

Every value is checked against an allow-list or a range; anything else raises FilterError,
which the API turns into HTTP 422 (never an empty file). Text is only ever passed to SQL as a
bound parameter (data.py).

Query parameters (repeat a key for several values, e.g. class=I&class=II):
  category       software, design, manufacturing, component, labeling, other
  class          I, II, III
  status         open, completed, terminated
  year_from, year_to   1990 to the current year
  text           2 to 100 characters, in a recall's product, firm or reason
  manufacturer   a manufacturer id from the search box (recall finder only)
  product_code   a 3-letter FDA product code (recall finder only)
  event          Death, Injury, Malfunction (report counts; device page only)
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from device_search.build_index import CATEGORIES

CATEGORY_SLUGS = dict(zip(("software", "design", "manufacturing", "component", "labeling", "other"),
                          CATEGORIES))
CLASSES = {"I": "Class I", "II": "Class II", "III": "Class III"}       # URL value → data value
STATUSES = {"open": "Open (ongoing)", "completed": "Completed", "terminated": "Terminated"}
EVENT_TYPES = ("Death", "Injury", "Malfunction")
MIN_YEAR = 1990
TEXT_MIN, TEXT_MAX = 2, 100
MANUFACTURER_MAX = 200
_CODE = re.compile(r"[A-Za-z]{3}")

COMMON = {"category", "class", "status", "year_from", "year_to", "text"}
SCOPES = {"recalls": COMMON | {"manufacturer", "product_code"},     # the recall finder
          "entity": COMMON | {"event"}}                               # a device page
CONTROL = {"format", "page"}                                          # not filters


class FilterError(ValueError):
    """A filter value that is not allowed; the message is safe to show to the visitor."""


@dataclass(frozen=True)
class Filters:
    categories: tuple[str, ...] = ()      # category labels, e.g. "Software"
    classes: tuple[str, ...] = ()         # "I", "II", "III"
    statuses: tuple[str, ...] = ()        # "open", "completed", "terminated"
    year_from: int | None = None
    year_to: int | None = None
    text: str | None = None
    manufacturer: str | None = None
    product_code: str | None = None
    events: tuple[str, ...] = ()          # "Death", "Injury", "Malfunction"

    @property
    def empty(self) -> bool:
        return self == Filters()


def _pick(values: list[str], allowed: dict[str, str] | tuple[str, ...], what: str,
          key=lambda v: v) -> tuple[str, ...]:
    """Allow-listed values, de-duplicated, in the allow-list's order."""
    names = list(allowed)
    wanted = set()
    for v in values:
        k = key(v.strip())
        if k not in names:
            raise FilterError(f"Unknown {what}. Allowed: {', '.join(names)}.")
        wanted.add(k)
    return tuple(n for n in names if n in wanted)


def _year(values: list[str], name: str, today: dt.date) -> int | None:
    if not values:
        return None
    if len(values) > 1:
        raise FilterError(f"Give {name} once.")
    v = values[0].strip()
    if not v.isdigit() or not MIN_YEAR <= int(v) <= today.year:
        raise FilterError(f"{name} must be a year from {MIN_YEAR} to {today.year}.")
    return int(v)


def _one(values: list[str], name: str) -> str | None:
    if not values:
        return None
    if len(values) > 1:
        raise FilterError(f"Give {name} once.")
    return values[0]


def parse(params: list[tuple[str, str]], scope: str, today: dt.date | None = None) -> Filters:
    """Query parameters (key, value) → Filters for the recall finder ("recalls") or a device
    page ("entity"). Raises FilterError for an unknown key or value."""
    today = today or dt.date.today()
    allowed = SCOPES[scope]
    got: dict[str, list[str]] = {}
    for key, value in params:
        if key in CONTROL:
            continue
        if key not in allowed:
            raise FilterError(f"Unknown filter. Allowed: {', '.join(sorted(allowed))}.")
        got.setdefault(key, []).append(value)

    text = _one(got.get("text", []), "text")
    if text is not None:
        text = " ".join(text.split())                      # trimmed, inner spaces collapsed
        if not TEXT_MIN <= len(text) <= TEXT_MAX:
            raise FilterError(f"Text must be {TEXT_MIN} to {TEXT_MAX} characters.")
    manufacturer = _one(got.get("manufacturer", []), "manufacturer")
    if manufacturer is not None:
        manufacturer = manufacturer.strip()
        if not 1 <= len(manufacturer) <= MANUFACTURER_MAX:
            raise FilterError(f"Manufacturer must be 1 to {MANUFACTURER_MAX} characters.")
    code = _one(got.get("product_code", []), "product_code")
    if code is not None:
        code = code.strip()
        if not _CODE.fullmatch(code):
            raise FilterError("Product code must be 3 letters.")
        code = code.upper()

    year_from = _year(got.get("year_from", []), "year_from", today)
    year_to = _year(got.get("year_to", []), "year_to", today)
    if year_from and year_to and year_from > year_to:
        raise FilterError("year_from must not be after year_to.")

    return Filters(
        categories=tuple(CATEGORY_SLUGS[s] for s in _pick(got.get("category", []), CATEGORY_SLUGS,
                                                          "category", key=str.lower)),
        classes=_pick(got.get("class", []), CLASSES, "class", key=str.upper),
        statuses=_pick(got.get("status", []), STATUSES, "status", key=str.lower),
        year_from=year_from, year_to=year_to, text=text, manufacturer=manufacturer,
        product_code=code,
        events=_pick(got.get("event", []), EVENT_TYPES, "event type", key=str.capitalize))


def describe(f: Filters) -> list[tuple[str, str]]:
    """The filters as (label, value) lines for a file's header; ("Filters", "none") if none."""
    lines = []
    if f.categories:
        lines.append(("Recall category", ", ".join(f.categories)))
    if f.classes:
        lines.append(("Recall class", ", ".join(CLASSES[c] for c in f.classes)))
    if f.statuses:
        lines.append(("Recall status", ", ".join(STATUSES[s] for s in f.statuses)))
    if f.year_from or f.year_to:
        lines.append(("Years", f"{f.year_from or MIN_YEAR} to {f.year_to or 'now'}"))
    if f.text:
        lines.append(("Text contains", f.text))
    if f.manufacturer:
        lines.append(("Manufacturer", f.manufacturer))
    if f.product_code:
        lines.append(("Product code", f.product_code))
    if f.events:
        lines.append(("Event type", ", ".join(f.events)))
    return lines or [("Filters", "none")]
