"""Tests for the recall finder and the PDF / Excel exports (export/, api/export.py,
device_search/build_index.py's recalls table). No BigQuery: a small index built from fixture
rows, and the query layer's fake client for device pages.
"""
import datetime as dt
import io
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from answer import pipeline
from api import app as api
from api import export as api_export
from device_search import build_index as bi
from device_search.search import DeviceIndex
from export import data, document, filters, pdf, xlsx
from query_layer import backend as ql_backend
from tests import test_answer, test_device_search

TODAY = dt.date(2026, 11, 25)
DEV = test_answer.DEV


def recall(n, **over):
    """A recall row as recalls_sql() returns it."""
    row = {"recall_number": f"Z-{n:04d}-2024", "recall_class": "Class II",
           "record_date": dt.date(2015 + n % 11, 1 + n % 12, 1 + n % 28),
           "recall_status": "Terminated", "product": f"Infusion pump model {n}",
           "firm": "Acme Medical", "reason_text": f"Battery may fail early (lot {n}).",
           "root_cause": "Device Design", "action": "Return the device.",
           "product_code": "FRN", "manufacturer_name_norm": "acme medical"}
    row.update(over)
    return row


SPECIAL = [
    recall(900, root_cause="Software design", recall_class="Class I",
           record_date=dt.date(2022, 3, 4), recall_status="Open, Classified",
           product="Insulin pump app", firm="Medtronic MiniMed",
           manufacturer_name_norm="medtronic minimed", product_code="LZG",
           reason_text="=1+1 <b>dose</b> & co: the app may show 5 µg/mL instead of mg/dL."),
    recall(901, root_cause="Software Manufacturing/Software Deployment", recall_class="Class I",
           record_date=dt.date(2021, 6, 30), recall_status="Completed",
           firm="微创医疗器械有限公司", reason_text="Software update error; Société Médicale®."),
    recall(902, root_cause="Error in labeling", recall_class=None,
           record_date=dt.date(2019, 1, 15), reason_text="+SUM(A1) label wrong"),
    recall(903, root_cause=None, record_date=dt.date(2010, 5, 5), reason_text="@cmd unknown"),
]
FIXTURE_RECALLS = [recall(n) for n in range(120)] + SPECIAL
# enforcement records (enforcement_sql() rows) for the first 3 recalls; the DreamStation device
# is linked to recalls 0-3 (recall_links_sql() rows), so it has 4 recalls and 3 enforcement.
FIXTURE_ENFORCEMENT = [
    {"recall_number": f"Z-{n:04d}-2024", "event_id": str(80000 + n), "recall_class": "Class II",
     "record_date": dt.date(2024, 2, 1 + n), "status": "Terminated",
     "distribution_pattern": "US nationwide", "firm": "Acme Medical",
     "product": f"Infusion pump model {n}", "reason_text": f"Battery may fail early (lot {n})."}
    for n in range(3)]
FIXTURE_LINKS = [{"entity_type": "device", "entity_id": DEV, "recall_number": f"Z-{n:04d}-2024"}
                 for n in range(4)]
# the DreamStation's company shares recalls 0-1 with the device and has one more (4)
FIXTURE_COMPANY = next(c for c in test_device_search.CATALOG if c["entity_type"] == "manufacturer"
                       and c["entity_id"] == test_device_search.CATALOG[0]["company_norm"])
FIXTURE_COMPANY_LINKS = [{"entity_type": "manufacturer", "entity_id": FIXTURE_COMPANY["entity_id"],
                          "recall_number": f"Z-{n:04d}-2024"} for n in (0, 1, 4)]


@pytest.fixture(scope="module")
def index_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("export") / "test.sqlite"
    rows, udis = bi.entity_rows(test_device_search.CATALOG, test_device_search.DEVICE_DETAILS,
                                test_device_search.ALIASES)
    recalls, unmapped = bi.recall_rows(FIXTURE_RECALLS, bi.load_categories())
    assert not unmapped                 # every fixture root_cause has a category
    enforcement = bi.enforcement_rows(FIXTURE_ENFORCEMENT)
    rows = bi.with_source_counts(rows, bi.source_counts(
        FIXTURE_LINKS + FIXTURE_COMPANY_LINKS, {e["recall_number"] for e in enforcement}))
    bi.write_index(path, rows, udis, {"dataset": "test", "built_at": "2026-11-24T06:00:00+00:00"},
                   recalls, enforcement, bi.link_rows(FIXTURE_LINKS + FIXTURE_COMPANY_LINKS))
    return path


@pytest.fixture(scope="module")
def index(index_path):
    return DeviceIndex.open(index_path)


def client(index_path, rows=None, fake=None):
    """A fresh app (fresh rate limits) with the fixture index and the fake BigQuery client."""
    bot, fake_bq, _ = test_answer.make_bot(["counts"], rows=rows, client=fake)
    bot.index = DeviceIndex.open(index_path)
    return TestClient(api.create_app(bot=bot, dataset="vigil_md_test")), fake_bq


def parse(query: str, scope="recalls"):
    from urllib.parse import parse_qsl
    return filters.parse(parse_qsl(query, keep_blank_values=True), scope, today=TODAY)


# ── filters.py ────────────────────────────────────────────────────────────────

def test_valid_filters_are_parsed():
    f = parse("category=software&category=Labeling&class=i&class=III&status=open&year_from=2020"
              "&year_to=2025&text=  battery   fail  &product_code=frn&manufacturer=acme medical"
              "&format=pdf&page=2")
    assert f.categories == ("Software", "Labeling and packaging")
    assert f.classes == ("I", "III") and f.statuses == ("open",)
    assert (f.year_from, f.year_to) == (2020, 2025)
    assert f.text == "battery fail" and f.product_code == "FRN" and f.manufacturer == "acme medical"
    assert parse("event=death&event=Injury", "entity").events == ("Death", "Injury")
    assert parse("").empty


@pytest.mark.parametrize("query", [
    "category=firmware", "class=IV", "status=pending", "event=Death",        # unknown values
    "year_from=1989", "year_to=2027", "year_from=abc", "year_from=2024&year_to=2020",
    "text=a", "text=" + "x" * 101, "product_code=FR", "product_code=F1N",
    "colour=red", "manufacturer=" + "m" * 201, "year_from=2020&year_from=2021",
])
def test_bad_filters_are_refused(query):
    with pytest.raises(filters.FilterError):
        parse(query)


def test_filters_belong_to_their_page():
    with pytest.raises(filters.FilterError):
        parse("manufacturer=acme", "entity")          # recall finder only
    with pytest.raises(filters.FilterError):
        parse("event=Death", "recalls")               # device page only


def test_describe_lists_every_filter_or_none():
    assert filters.describe(parse("")) == [("Filters", "none")]
    lines = dict(filters.describe(parse("class=I&year_from=2020&text=pump")))
    assert lines == {"Recall class": "Class I", "Years": "2020 to now", "Text contains": "pump"}


# ── index build: the recalls table ────────────────────────────────────────────

def test_recalls_sql_has_the_columns_and_join():
    sql = bi.recalls_sql("vigil_md_dev")
    for col in ("r.source_record_id AS recall_number", "e.recall_class", "r.record_date",
                "r.recall_status", "r.device_name_raw AS product",
                "r.manufacturer_name_raw AS firm", "r.reason_text", "r.root_cause", "r.action",
                "r.product_code", "r.manufacturer_name_norm"):
        assert col in sql, col
    assert "`vigil-md-1792696816.vigil_md_dev.recall` AS r" in sql
    assert ("LEFT JOIN `vigil-md-1792696816.vigil_md_dev.enforcement` AS e "
            "ON e.source_record_id = r.source_record_id") in sql


def test_the_recalls_table_and_its_indexes_are_created(index_path):
    con = sqlite3.connect(index_path)
    indexes = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'recalls'")}
    assert indexes >= {"recalls_category", "recalls_class", "recalls_date",
                       "recalls_product_code", "recalls_manufacturer"}
    assert con.execute("SELECT COUNT(*) FROM recalls").fetchone()[0] == len(FIXTURE_RECALLS)
    assert con.execute("SELECT COUNT(*) FROM recalls WHERE category IS NULL OR category = ''"
                       ).fetchone()[0] == 0
    assert bi.INDEX_VERSION == 4


def test_category_comes_from_the_mapping_file():
    rows, unmapped = bi.recall_rows(SPECIAL, bi.load_categories())
    assert [r["category"] for r in rows] == ["Software", "Software", "Labeling and packaging",
                                             bi.OTHER]
    assert rows[0]["record_date"] == "2022-03-04" and not unmapped


def test_a_new_root_cause_phrase_is_reported_not_hidden():
    rows, unmapped = bi.recall_rows([recall(1, root_cause="Quantum flux"),
                                     recall(2, root_cause="Quantum flux")], bi.load_categories())
    assert unmapped == {"Quantum flux": 2}
    assert {r["category"] for r in rows} == {bi.OTHER}


def test_every_mapped_category_is_a_known_one():
    assert set(bi.load_categories().values()) <= set(bi.CATEGORIES)


def test_every_root_cause_in_a_local_index_has_a_category():
    # Runs when a built index is on this machine (data/search/, not in git): a phrase the FDA
    # added since the mapping was written fails here instead of slipping into "Other".
    from device_search.paths import OUT_DIR
    paths = sorted(OUT_DIR.glob("*.sqlite"))
    checked = 0
    for p in paths:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        if not con.execute("SELECT 1 FROM sqlite_master WHERE name = 'recalls'").fetchone():
            continue
        causes = {r[0] for r in con.execute("SELECT DISTINCT root_cause FROM recalls") if r[0]}
        missing = causes - set(bi.load_categories())
        assert not missing, f"{p.name}: add to recall_categories.json: {sorted(missing)}"
        checked += 1
    if not checked:
        pytest.skip("no built index with a recalls table on this machine")


# ── recall finder (data.py) ───────────────────────────────────────────────────

@pytest.mark.parametrize("query,expected", [
    ("", len(FIXTURE_RECALLS)),
    ("category=software", 2),
    ("class=I", 2),
    ("status=open", 1),
    ("status=completed&status=open", 2),
    ("year_from=2021&year_to=2022", sum(1 for r in FIXTURE_RECALLS
                                        if 2021 <= r["record_date"].year <= 2022)),
    ("text=µg", 1),
    ("text=BATTERY", 120),
    ("text=100%25", 0),                                 # % is literal, not a wildcard
    ("manufacturer=medtronic minimed", 1),
    ("product_code=LZG", 1),
    ("category=software&class=I&year_from=2020&year_to=2025", 2),
    ("category=software&status=terminated", 0),
])
def test_each_filter_alone_and_combined(index, query, expected):
    f = parse(query)
    assert data.count_recalls(index.con, f) == expected
    rows = data.find_recalls(index.con, f, limit=1000)
    assert len(rows) == expected
    assert all(data.recall_matches(r, f) for r in rows)          # SQL and Python agree


def test_sql_and_python_filters_agree_on_every_row(index):
    every = data.find_recalls(index.con, parse(""), limit=1000)
    for query in ("category=software", "class=I&class=II", "status=open", "year_from=2020",
                  "year_to=2018", "text=pump", "category=other&year_from=2010&year_to=2010"):
        f = parse(query)
        sql = {r["recall_number"] for r in data.find_recalls(index.con, f, limit=1000)}
        py = {r["recall_number"] for r in every if data.recall_matches(r, f)}
        assert sql == py, query


def test_recall_pages_of_50_and_the_last_page(index):
    first = data.recall_page(index.con, parse(""), 1)
    assert (first["total"], first["pages"], len(first["rows"])) == (124, 3, 50)
    dates = [r["record_date"] for r in first["rows"]]
    assert dates == sorted(dates, reverse=True)                  # newest first
    last = data.recall_page(index.con, parse(""), 3)
    assert len(last["rows"]) == 24
    assert data.recall_page(index.con, parse(""), 99)["page"] == 3    # past the end: last page
    seen = {r["recall_number"] for p in (1, 2, 3)
            for r in data.recall_page(index.con, parse(""), p)["rows"]}
    assert len(seen) == 124


def test_counts_filter_by_years_and_event_types():
    counts = {"total_reports": 10, "by_event_type": {"Malfunction": 7, "Injury": 2, "Death": 1},
              "by_year": {2023: {"Malfunction": 3}, 2024: {"Malfunction": 4, "Injury": 2,
                                                            "Death": 1}}}
    f = filters.Filters(year_from=2024, events=("Injury", "Death"))
    assert data.filter_counts(counts, f) == {"total_reports": 3,
                                             "by_event_type": {"Injury": 2, "Death": 1},
                                             "by_year": {2024: {"Injury": 2, "Death": 1}}}
    assert data.filter_counts(counts, filters.Filters()) is counts


# ── API: recall finder JSON and files ─────────────────────────────────────────

def test_recall_finder_page_json(index_path):
    c, _ = client(index_path)
    body = c.get("/api/recalls?category=software&page=1").json()
    assert body["total"] == 2 and len(body["rows"]) == 2 and body["page"] == 1
    assert body["data_as_of"] == "2026-11-24"
    assert ["Recall category", "Software"] in body["filters"]


@pytest.mark.parametrize("url", ["/api/recalls?category=firmware",
                                 "/api/export/recalls?format=pdf&class=IV",
                                 "/api/export/recalls?format=doc",
                                 "/api/export/recalls?format=xlsx&year_from=1980"])
def test_unknown_values_get_422_never_an_empty_file(index_path, url):
    c, _ = client(index_path)
    r = c.get(url)
    assert r.status_code == 422 and r.headers["content-type"].startswith("application/json")


def test_excel_file_has_about_and_recalls_sheets(index_path):
    c, _ = client(index_path)
    r = c.get("/api/export/recalls?format=xlsx&category=software&category=labeling")
    assert r.status_code == 200
    assert r.headers["content-type"] == xlsx.MEDIA_TYPE
    disp = r.headers["content-disposition"]
    assert disp.startswith('attachment; filename="vigil-md-recalls-') and disp.endswith('.xlsx"')
    wb = load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames == ["About", "Recalls"]
    ws = wb["Recalls"]
    header = [c.value for c in ws[1]]
    assert header[:3] == ["Recall number", "Date", "Class"] and "Action" in header
    assert ws.max_row - 1 == 3                                    # 2 software + 1 labeling
    assert ws.freeze_panes == "A2" and ws.auto_filter.ref.startswith("A1:")
    assert isinstance(ws["B2"].value, dt.datetime)                # a real date, sortable
    about = [r[0].value for r in wb["About"].iter_rows()]
    assert "Filters" in about and "Notes" in about
    text = json.dumps([[c.value for c in row] for row in wb["About"].iter_rows()], default=str)
    assert "Recall category" in text and "Data as of" in text and "medical advice" in text


def test_pdf_file_opens_and_says_what_it_covers(index_path):
    c, _ = client(index_path)
    r = c.get("/api/export/recalls?format=pdf&class=I")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")
    assert 'filename="vigil-md-recalls-' in r.headers["content-disposition"]


def test_the_filename_never_holds_the_visitors_text(index_path):
    c, _ = client(index_path)
    r = c.get('/api/export/search?format=xlsx&q=dreamstation"; rm -rf')
    assert r.status_code == 200
    assert "dreamstation" not in r.headers["content-disposition"]
    assert 'filename="vigil-md-search-' in r.headers["content-disposition"]


# ── safety ────────────────────────────────────────────────────────────────────

def test_no_formula_cells_in_excel(index_path):
    c, _ = client(index_path)
    wb = load_workbook(io.BytesIO(c.get("/api/export/recalls?format=xlsx").content))
    texts = []
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                assert cell.data_type != "f", (ws.title, cell.coordinate, cell.value)
                texts.append(cell.value)
    assert "=1+1 <b>dose</b> & co: the app may show 5 µg/mL instead of mg/dL." in texts
    assert "+SUM(A1) label wrong" in texts and "@cmd unknown" in texts


def test_pdf_text_is_escaped_and_prints_literally():
    from reportlab.platypus import Paragraph
    text = "=1+1 <b>dose</b> & co"
    para = Paragraph(pdf.markup(text), pdf.CELL)
    assert para.getPlainText() == text                           # not bold, not swallowed


def test_non_ascii_text_prints_without_boxes():
    # Helvetica covers µ, ® and accents; Chinese goes to the embedded Noto Sans SC.
    assert pdf.markup("5 µg Société Médicale®") == "5 µg Société Médicale®"
    marked = pdf.markup("Firm 微创医疗 Ltd")
    assert f'<font name="{pdf.FALLBACK_FONT}">微创医疗</font>' in marked
    doc = document.recalls_document([recall(1, firm="微创医疗器械有限公司",
                                            reason_text="5 µg/mL Société®")],
                                    [("Filters", "none")], None,
                                    dt.datetime(2026, 11, 25, tzinfo=dt.timezone.utc))
    out = pdf.build(doc)
    assert out.startswith(b"%PDF") and pdf.FALLBACK_FONT.encode() in out


def test_long_reasons_and_products_are_cut_in_the_pdf_only():
    for key in ("reason_text", "product"):
        col = next(c for c in document.RECALL_COLUMNS if c.key == key)
        cut = pdf._value({key: "word " * 120}, col)
        assert len(cut) <= 301 and cut.endswith("…"), key
    other = document.Column("note", "Note", pdf_mm=40)              # no cut of its own
    assert len(pdf._value({"note": "x " * 2000}, other)) <= pdf.CELL_MAX + 1


def test_a_pdf_with_very_long_texts_still_builds():
    # Found on vigil_md_dev: a 3,923-character product made a row taller than a page and
    # reportlab stopped with a LayoutError.
    rows = [recall(1, product="Model AX-" + "1234, " * 800, reason_text="word " * 400,
                   action="return " * 500)]
    doc = document.recalls_document(rows, [("Filters", "none")], None,
                                    dt.datetime(2026, 11, 25, tzinfo=dt.timezone.utc))
    assert pdf.build(doc).startswith(b"%PDF")
    xl = load_workbook(io.BytesIO(xlsx.build(doc)))["Recalls"]
    assert xl["F2"].value.startswith("Model AX-") and len(xl["F2"].value) > 3000   # full in Excel


# ── limits and errors ─────────────────────────────────────────────────────────

def test_over_the_row_limit_is_413_with_the_count(index_path, monkeypatch):
    monkeypatch.setitem(api_export.FORMATS, "pdf", (pdf, 100))
    c, _ = client(index_path)
    r = c.get("/api/export/recalls?format=pdf")
    assert r.status_code == 413
    body = r.json()
    assert body["count"] == 124 and body["limit"] == 100 and "Narrow the filters" in body["text"]
    assert c.get("/api/export/recalls?format=pdf&category=software").status_code == 200


def test_the_export_rate_limit_is_5_a_minute(index_path):
    c, _ = client(index_path)
    codes = [c.get("/api/export/recalls?format=xlsx&class=I").status_code for _ in range(6)]
    assert codes == [200] * 5 + [429]
    assert c.get("/api/recalls").status_code == 200              # the page itself is not limited


def test_a_budget_error_on_a_device_export_is_413(index_path):
    # The dry run says the counts query alone would scan 0.6 GB: over the 0.5 GB cap.
    fake = test_answer.FakeClient(rows=test_answer.ROWS, estimates={"counts": 0.6e9})
    c, _ = client(index_path, fake=fake)
    r = c.get(f"/api/export/entity/device/{DEV}?format=pdf")
    assert r.status_code == 413 and r.json()["status"] == "budget"


def test_a_data_error_on_a_device_export_is_503(index_path, monkeypatch):
    def down(*args, **kwargs):
        raise pipeline.DATA_ERRORS[-1]("BigQuery unreachable")
    monkeypatch.setattr(data.summary, "device_summary", down)
    c, _ = client(index_path)
    r = c.get(f"/api/export/entity/device/{DEV}?format=xlsx")
    assert r.status_code == 503 and r.json()["status"] == "error"


# ── same numbers as the screen ────────────────────────────────────────────────

def test_the_search_list_file_has_the_same_hits_in_the_same_order(index_path, monkeypatch):
    # /api/search on main returns at most 25; the file has up to 500, so compare the first 25.
    c, _ = client(index_path)
    screen = c.get("/api/search?q=dreamstation&limit=25").json()["hits"]
    wb = load_workbook(io.BytesIO(c.get("/api/export/search?format=xlsx&q=dreamstation").content))
    rows = list(wb["Results"].iter_rows(min_row=2, values_only=True))
    assert [r[0] for r in rows[:len(screen)]] == [h["title"] for h in screen]
    assert [r[4] for r in rows[:len(screen)]] == [h["reports"] for h in screen]


def test_a_device_file_has_the_numbers_of_the_device_page(index_path):
    c, _ = client(index_path)
    page = c.get(f"/api/entity/device/{DEV}").json()
    wb = load_workbook(io.BytesIO(c.get(f"/api/export/entity/device/{DEV}?format=xlsx").content))
    assert wb.sheetnames == ["About", "Summary", "Reports by year", "Problems", "Recalls"]
    summary = dict(wb["Summary"].iter_rows(min_row=2, values_only=True))
    assert summary["MAUDE reports (adverse events)"] == f"{page['counts']['total_reports']:,}"
    assert summary["FDA enforcement reports (of those recalls)"] == (
        f"{page['source_summary']['enforcement']:,}")
    for event, n in page["counts"]["by_event_type"].items():
        assert summary[event] == f"{n:,}"
    years = {str(r[0]): r[-1] for r in wb["Reports by year"].iter_rows(min_row=2, values_only=True)}
    assert years == {str(y): sum(v.values()) for y, v in page["counts"]["by_year"].items()}
    assert wb["Recalls"].max_row - 1 == len(page["recalls"]["shown"])


def test_the_device_page_json_with_filters(index_path):
    c, _ = client(index_path)
    body = c.get(f"/api/export/entity/device/{DEV}?format=json&event=Death&year_from=2024").json()
    assert body["counts"]["total_reports"] == 1 and body["counts"]["by_event_type"] == {"Death": 1}
    assert body["recalls"]["total"] == 0 and body["unfiltered_recalls"] == 4   # 2021 recall
    assert "all_recalls" not in body and all("sql" not in s for s in body["sources"])
    assert ["Event type", "Death"] in body["filters"]
    assert c.get(f"/api/export/entity/gadget/{DEV}?format=json").status_code == 404


# ── sources on search results and the "all records" file ─────────────────────

def test_enforcement_sql_leaves_out_scope_text_and_links_come_from_entity_recalls():
    sql = bi.enforcement_sql("vigil_md_prod")
    assert "source_record_id AS recall_number" in sql and "scope_text" not in sql
    assert "`vigil-md-1792696816.vigil_md_prod.enforcement`" in sql
    assert "`vigil-md-1792696816.vigil_md_prod.entity_recalls`" in bi.recall_links_sql("vigil_md_prod")


def test_source_counts_count_distinct_recalls_and_their_enforcement_records():
    links = FIXTURE_LINKS + [FIXTURE_LINKS[0]] + [
        {"entity_type": "product_code", "entity_id": "FRN", "recall_number": "Z-0002-2024"}]
    counts = bi.source_counts(links, {"Z-0000-2024", "Z-0002-2024"})
    assert counts[bi.entry_id("device", DEV)] == (4, 2)      # the repeated link counts once
    assert counts[bi.entry_id("product_code", "FRN")] == (1, 1)
    rows = bi.with_source_counts([{"id": "x|none"}], counts)
    assert rows[0]["recalls"] == 0 and rows[0]["enforcement"] == 0


def test_search_results_carry_their_count_per_source(index_path):
    c, _ = client(index_path)
    hits = c.get("/api/search?q=dreamstation").json()["hits"]
    dev = next(h for h in hits if h["entity_id"] == DEV)
    assert (dev["reports"], dev["recalls"], dev["enforcement"]) == (dev["reports"], 4, 3)
    assert all({"recalls", "enforcement"} <= set(h) for h in hits)


def test_an_index_without_the_counts_still_opens(tmp_path):
    """A version 3 index (no recalls/enforcement columns) gives 0, not an error."""
    path = tmp_path / "v3.sqlite"
    rows, udis = bi.entity_rows(test_device_search.CATALOG, test_device_search.DEVICE_DETAILS,
                                test_device_search.ALIASES)
    bi.write_index(path, rows, udis, {"dataset": "test"})
    con = sqlite3.connect(path)
    con.executescript("ALTER TABLE entities DROP COLUMN recalls; "
                      "ALTER TABLE entities DROP COLUMN enforcement;")
    con.close()
    hit = DeviceIndex.open(path).search("dreamstation", limit=1)[0]
    assert (hit.recalls, hit.enforcement) == (0, 0)


def test_the_multi_entity_queries_are_read_only_and_prune_by_entity():
    from query_layer import queries
    from tests.test_query_layer import T
    rq = queries.recalls_many(T, [("device", DEV), ("product_code", "FRN")], 50)
    mq = queries.device_reports_many(T, [DEV], 50)
    for q in (rq, mq):
        queries.assert_read_only(q.sql)
        assert "bucket IN UNNEST(@buckets)" in q.sql and "entity_id IN UNNEST(@ids)" in q.sql
        assert "<= @per_" in q.sql
    assert rq.params["keys"][1] == ["device|" + DEV, "product_code|FRN"]
    assert "maude`" not in mq.sql and "entity_reports" in mq.sql       # never the large table
    with pytest.raises(ValueError):
        queries.recalls_many(T, [], 50)


def _records_client(index_path, estimates=None):
    """The fixture app with fake BigQuery rows for the DreamStation results."""
    idx = DeviceIndex.open(index_path)
    hits = idx.search("dreamstation", limit=data.RESULTS_SHOWN)
    recall_rows = [{"entity_type": "device", "entity_id": DEV, "recall_number": f"Z-{n:04d}-2024",
                    "recall_class": "Class II", "record_date": dt.date(2024, 1, 1 + n),
                    "recall_status": "Terminated", "product": "DreamStation", "firm": "Respironics",
                    "reason_text": "=foam may degrade", "root_cause": "Device Design",
                    "action": "Replace.", "matched_by": "device_name", "total_recalls": 4}
                   for n in range(4)]
    report_rows = [{"entity_id": h.entity_id, "mdr_report_key": f"{9000 + i}",
                    "record_date": dt.date(2025, 5, 1 + i), "total_reports": 2}
                   for h in hits if h.kind == "device" for i in range(2)]
    fake = test_answer.FakeClient(rows={"recalls": recall_rows, "narrative_candidates": report_rows},
                                  estimates=estimates or {})
    c, fake = client(index_path, fake=fake)
    return c, fake, hits


def test_the_records_file_has_one_sheet_per_source(index_path):
    c, fake, hits = _records_client(index_path)
    r = c.get("/api/export/records?format=xlsx&q=dreamstation")
    assert r.status_code == 200
    assert r.headers["content-disposition"].startswith('attachment; filename="vigil-md-records-')
    wb = load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames == ["About", "Results", "Recalls", "Enforcement", "MAUDE reports"]
    results = list(wb["Results"].iter_rows(min_row=2, values_only=True))
    assert [x[0] for x in results] == [h.title for h in hits]             # the screen's order
    recalls = list(wb["Recalls"].iter_rows(min_row=2, values_only=True))
    assert len(recalls) == 4 and {x[0] for x in recalls} == {"DreamStation"}
    assert recalls[0][8] == "=foam may degrade"                            # text, not a formula
    enforcement = list(wb["Enforcement"].iter_rows(min_row=2, values_only=True))
    assert sorted(x[1] for x in enforcement) == ["Z-0000-2024", "Z-0001-2024", "Z-0002-2024"]
    maude = list(wb["MAUDE reports"].iter_rows(min_row=1, values_only=True))
    assert maude[0] == ("Device", "Company", "Date received", "MDR report key")
    n_devices = sum(h.kind == "device" for h in hits)
    assert len(maude) - 1 == 2 * n_devices
    about = " ".join(str(v) for row in wb["About"].iter_rows(values_only=True) for v in row if v)
    assert "MAUDE = FDA adverse-event reports" in about and "newest 50" in about
    # one query per source table: the 10 MB minimum is billed once each, maude is not read
    assert sorted(n for n, _ in fake.real_runs()) == ["narrative_candidates", "recalls"]


def test_the_records_pdf_builds(index_path):
    c, _, _ = _records_client(index_path)
    r = c.get("/api/export/records?format=pdf&q=dreamstation")
    assert r.status_code == 200 and r.content.startswith(b"%PDF")


def test_records_over_the_bigquery_cap_are_413(index_path):
    c, _, _ = _records_client(index_path, estimates={"recalls": 0.25e9})
    r = c.get("/api/export/records?format=xlsx&q=dreamstation")
    assert r.status_code == 413 and r.json()["status"] == "budget"


def test_records_files_are_limited_to_2_a_minute(index_path):
    c, _, _ = _records_client(index_path)
    codes = [c.get("/api/export/records?format=xlsx&q=dreamstation").status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    assert "record downloads" in c.get("/api/export/records?format=xlsx&q=dreamstation").json()["text"]


def test_records_with_no_results_is_an_empty_file_without_bigquery(index_path):
    c, fake, _ = _records_client(index_path)
    r = c.get("/api/export/records?format=xlsx&q=zzzzqqqq")
    assert r.status_code == 200 and not fake.real_runs()
    assert c.get("/api/export/records?format=csv&q=dreamstation").status_code == 422
    assert c.get("/api/export/records?format=xlsx&q=d").status_code == 422


def test_the_search_summary_counts_each_record_once(index_path):
    c, _ = client(index_path)
    body = c.get("/api/search?q=respironics").json()
    hits, summary = body["hits"], body["summary"]
    kinds = sorted(h["kind"] for h in hits)
    assert kinds == ["device", "device", "manufacturer"]       # the company and its 2 devices
    by_kind = {m["kind"]: m for m in summary["maude"]}
    assert set(by_kind) == {"device", "manufacturer"}            # one total per kind, not added
    assert by_kind["device"]["results"] == 2
    assert by_kind["device"]["reports"] == sum(h["reports"] for h in hits if h["kind"] == "device")
    assert by_kind["manufacturer"]["reports"] == next(h["reports"] for h in hits
                                                       if h["kind"] == "manufacturer")
    # device: recalls 0-3; company: 0, 1, 4 -> 5 distinct, not 4 + 3 = 7
    assert summary["recalls"] == 5 and sum(h["recalls"] for h in hits) == 7
    assert summary["enforcement"] == 3                          # 0, 1, 2 have enforcement


def test_the_summary_without_links_says_unknown_not_zero(tmp_path):
    path = tmp_path / "nolinks.sqlite"
    rows, udis = bi.entity_rows(test_device_search.CATALOG, test_device_search.DEVICE_DETAILS,
                                test_device_search.ALIASES)
    bi.write_index(path, rows, udis, {"dataset": "test"})
    con = sqlite3.connect(path)
    con.execute("DROP TABLE recall_links")
    con.commit()
    con.close()
    idx = DeviceIndex.open(path)
    summary = idx.source_summary(idx.search("dreamstation"))
    assert summary["recalls"] is None and summary["enforcement"] is None
    assert idx.source_summary([])["maude"] == []


def test_the_device_page_has_a_summary_by_source(index_path):
    c, _ = client(index_path)
    page = c.get(f"/api/entity/device/{DEV}").json()
    s = page["source_summary"]
    assert s["maude_reports"] == page["counts"]["total_reports"]
    assert s["recalls"] == page["recalls"]["total"] and s["filtered"] is False
    assert isinstance(s["enforcement"], int)
    filtered = c.get(f"/api/export/entity/device/{DEV}?format=json&event=Death&year_from=2024").json()
    fs = filtered["source_summary"]
    assert fs["filtered"] is True and fs["maude_reports"] == filtered["counts"]["total_reports"] == 1
    assert fs["recalls"] == filtered["recalls"]["total"] == 0 and fs["enforcement"] == 0


def test_page_source_summary_counts_enforcement_of_the_given_recalls(index):
    page = {"counts": {"total_reports": 12}, "recalls": {"total": 3}}
    s = data.page_source_summary(page, index, "device", DEV,
                                 ["Z-0000-2024", "Z-0001-2024", "Z-0099-2024", "Z-0000-2024"])
    assert s == {"maude_reports": 12, "recalls": 3, "enforcement": 2, "filtered": False}
    linked = data.page_source_summary(page, index, "device", DEV)        # from recall_links
    assert linked["enforcement"] == 3                                   # recalls 0-2 of 0-3


def test_the_files_carry_the_summary_by_source(index_path):
    c, _ = client(index_path)
    wb = load_workbook(io.BytesIO(c.get("/api/export/search?format=xlsx&q=respironics").content))
    about = {r[0]: r[1] for r in wb["About"].iter_rows(values_only=True) if r[0]}
    assert "Summary by source" in about and about["FDA recalls (each counted once)"] == "5"
    assert about["FDA enforcement reports"] == "3"
    assert "for 2 devices" in about["MAUDE reports (adverse events)"]
    assert about["MAUDE reports (adverse events)"].endswith("for 1 company")   # singular
    r = c.get("/api/export/search?format=pdf&q=respironics")
    assert r.status_code == 200 and r.content.startswith(b"%PDF")


def test_files_are_streamed_not_held_to_one_response_size(index_path):
    c, _ = client(index_path)
    r = c.get("/api/export/recalls?format=xlsx")
    assert r.status_code == 200 and "content-length" not in {k.lower() for k in r.headers}
    load_workbook(io.BytesIO(r.content))                          # the chunks make one file


def test_characters_no_font_has_print_as_their_plain_form_not_boxes():
    # all seen in PROD: control characters, accents and superscripts neither font has
    assert pdf.printable_text("Cu\x90 ᵀᴹ 10⁰ Győr∙Kft˚ a\u0308\x99b") == "Cu TM 100 Gyor·Kft° ab"
    assert pdf.printable_text("plain 5 µg Société®") == "plain 5 µg Société®"
    assert pdf.printable_text("微创 Ⅱ αβ ㄇ") == "微创 Ⅱ αβ ㄇ"            # in the font subset
    assert pdf.printable_text("\U0001F600") == "?"                         # an emoji: no font
    out = pdf.build(document.recalls_document(
        [recall(1, firm="Győr Kft\x90", reason_text="ᵀᴹ ∙ 微创")], [("Filters", "none")], None,
        dt.datetime(2026, 11, 27, tzinfo=dt.timezone.utc)))
    assert out.startswith(b"%PDF")


def test_the_font_is_the_subset_and_licensed():
    assert pdf.FONT_FILE.name == "NotoSansSC-Regular-subset.ttf"
    assert pdf.FONT_FILE.stat().st_size < 3_000_000
    assert (pdf.FONT_FILE.parent / "OFL.txt").exists()

