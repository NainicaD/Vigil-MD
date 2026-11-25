"""What an export file holds, independent of the format: pdf.py and xlsx.py render it.

Every file carries its title, when it was made, the "data as of" date, the filters it was made
with, its row count and the notes the screen shows (what counts mean, the sources, "not medical
advice"), so a reader knows what it covers.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

NOT_ADVICE = ("Vigil MD shows what FDA adverse-event reports and recalls say. "
              "It does not give medical advice.")
SOURCES = ("Source: FDA data from openFDA (MAUDE adverse-event reports, device recalls and "
           "enforcement reports) and AccessGUDID (device identifiers).")
COUNTS_MEANING = ("Report counts are numbers of reports, not failure rates: a report does not "
                  "prove that the device caused the event.")
CLASS_NOTE = "Recall class is empty where the FDA enforcement report gives none."
PDF_CUT_NOTE = ("In the PDF, long product names and reasons are cut at about 300 characters "
                "(marked …); the Excel file has them in full.")
INDEX_FRESHNESS = ("Taken from the site's search index, built when the site was last deployed "
                   "(see 'Data as of'); device pages read the daily tables and can be newer.")
KIND_LABEL = {"device": "Device", "manufacturer": "Manufacturer", "product_code": "Product code"}


@dataclass(frozen=True)
class Column:
    key: str
    title: str
    kind: str = "text"            # text | int | date
    width: float = 14             # Excel column width (characters)
    pdf_mm: float | None = None   # PDF column width; None = Excel only
    cut: int | None = None        # PDF: cut longer text at a word, marked with "…"


@dataclass
class Table:
    name: str                     # Excel sheet name (at most 31 characters)
    title: str                    # PDF heading
    columns: list[Column]
    rows: list[dict]


@dataclass
class Document:
    title: str
    made_at: dt.datetime
    data_as_of: str | None
    filters: list[tuple[str, str]]
    row_count: int
    row_label: str                # "recalls", "search results", ...
    notes: list[str]
    facts: list[tuple[str, str]] = field(default_factory=list)   # e.g. the device's totals
    tables: list[Table] = field(default_factory=list)

    @property
    def header(self) -> list[tuple[str, str]]:
        return [("Made", self.made_at.strftime("%Y-%m-%d %H:%M UTC")),
                ("Data as of", self.data_as_of or "unknown"),
                ("Rows", f"{self.row_count:,} {self.row_label}")]


RECALL_COLUMNS = [
    Column("recall_number", "Recall number", width=14, pdf_mm=22),
    Column("record_date", "Date", "date", 11, 18),
    Column("recall_class", "Class", width=9, pdf_mm=14),
    Column("recall_status", "Status", width=16, pdf_mm=22),
    Column("firm", "Firm", width=28, pdf_mm=34),
    # FDA product descriptions can list every model number (3,923 characters seen in vigil_md_dev):
    # cut in the PDF like the reason, so no table row is taller than a page.
    Column("product", "Product", width=40, pdf_mm=44, cut=300),
    Column("category", "Category", width=22, pdf_mm=24),
    Column("root_cause", "Root cause", width=26, pdf_mm=26),
    Column("reason_text", "Reason", width=60, pdf_mm=69, cut=300),
    Column("action", "Action", width=50),
    Column("product_code", "Product code", width=10),
    Column("manufacturer_name_norm", "Manufacturer (normalized)", width=24),
]
MATCHED_BY = Column("matched_by", "Matched by", width=14)          # device pages only

SEARCH_COLUMNS = [
    Column("title", "Title", width=40, pdf_mm=78),
    Column("kind", "Kind", width=14, pdf_mm=24),
    Column("subtitle", "Company", width=32, pdf_mm=62),
    Column("product_codes", "Product codes", width=18, pdf_mm=40),
    Column("reports", "Reports", "int", 10, 22),
    Column("deaths", "Deaths", "int", 10, 22),
    Column("injuries", "Injuries", "int", 10, 22),
]


def as_of(value: str | None) -> str | None:
    """"2026-11-25T03:00:00+00:00" → "2026-11-25"."""
    return str(value)[:10] if value else None


def oldest(dates: dict) -> str | None:
    """The oldest table date: the data is only as new as that (as on the device page)."""
    days = sorted(str(v)[:10] for v in (dates or {}).values() if v)
    return days[0] if days else None


def recalls_document(rows: list[dict], filters: list[tuple[str, str]], built_at: str | None,
                     made_at: dt.datetime) -> Document:
    return Document(
        title="Vigil MD: recalls", made_at=made_at, data_as_of=as_of(built_at), filters=filters,
        row_count=len(rows), row_label="recalls",
        notes=[INDEX_FRESHNESS, CLASS_NOTE, PDF_CUT_NOTE, SOURCES, NOT_ADVICE],
        tables=[Table("Recalls", "Recalls, newest first", RECALL_COLUMNS, rows)])


def search_document(hits: list, q: str, built_at: str | None, made_at: dt.datetime) -> Document:
    rows = [{"title": h.title, "kind": KIND_LABEL.get(h.kind, h.kind), "subtitle": h.subtitle,
             "product_codes": ", ".join(h.product_codes), "reports": h.reports,
             "deaths": h.deaths, "injuries": h.injuries} for h in hits]
    return Document(
        title="Vigil MD: search results", made_at=made_at, data_as_of=as_of(built_at),
        filters=[("Search text", q)], row_count=len(rows), row_label="search results",
        notes=[INDEX_FRESHNESS, COUNTS_MEANING, SOURCES, NOT_ADVICE],
        tables=[Table("Results", "Search results, in the order shown on the site",
                      SEARCH_COLUMNS, rows)])


def entity_document(view: dict, filters: list[tuple[str, str]], made_at: dt.datetime) -> Document:
    """A device, product code or manufacturer page (data.entity_view() output)."""
    e, counts, problems = view["entity"], view["counts"], view["problems"]
    kind = KIND_LABEL.get(e["type"], e["type"])
    name = e.get("title") or e["id"]
    facts = [(kind, name)]
    if e.get("subtitle"):
        facts.append(("Company", e["subtitle"]))
    if e.get("product_codes"):
        facts.append(("Product codes", ", ".join(e["product_codes"])))
    facts.append(("Reports", f"{counts['total_reports']:,}"))
    for event, n in counts["by_event_type"].items():
        facts.append((f"  {event}", f"{n:,}"))
    if problems.get("no_harm_reports"):
        facts.append(("No patient harm reported", f"{problems['no_harm_reports']:,} reports"))
    facts.append(("Recalls matching the filters",
                  f"{view['recalls']['total']:,} of {view['unfiltered_recalls']:,}"))

    events = sorted({ev for by in counts["by_year"].values() for ev in by})
    year_cols = ([Column("year", "Year", width=8, pdf_mm=20)]
                 + [Column(ev, ev, "int", 14, 30) for ev in events]
                 + [Column("total", "Total", "int", 10, 24)])
    year_rows = [{"year": str(y), **by, "total": sum(by.values())}
                 for y, by in sorted(counts["by_year"].items(), key=lambda kv: int(kv[0]))]
    problem_rows = ([{"type": "Device problem", **t} for t in problems.get("device", [])]
                    + [{"type": "Patient problem", **t} for t in problems.get("patient", [])])
    notes = [n for n in (view.get("counts_note"),) if n]
    notes += ["Problems cover all years and event types: they are not filtered.",
              COUNTS_MEANING, CLASS_NOTE, PDF_CUT_NOTE, SOURCES, NOT_ADVICE]
    recall_cols = RECALL_COLUMNS[:10] + [MATCHED_BY]
    return Document(
        title=f"Vigil MD: {name}", made_at=made_at, data_as_of=oldest(view.get("data_as_of")),
        filters=filters, row_count=view["recalls"]["total"], row_label="recalls",
        notes=notes, facts=facts,
        tables=[
            Table("Summary", "Summary", [Column("item", "Item", width=34, pdf_mm=90),
                                         Column("value", "Value", width=40, pdf_mm=120)],
                  [{"item": k.strip(), "value": v} for k, v in facts]),
            Table("Reports by year", "Reports by year and event type", year_cols, year_rows),
            Table("Problems", "Top reported problems",
                  [Column("type", "Type", width=16, pdf_mm=40),
                   Column("term", "Problem", width=48, pdf_mm=150),
                   Column("reports", "Reports", "int", 10, 30)], problem_rows),
            Table("Recalls", "Recalls, newest first", recall_cols, view["all_recalls"]),
        ])
