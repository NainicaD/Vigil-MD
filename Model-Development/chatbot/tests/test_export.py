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


@pytest.fixture(scope="module")
def index_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("export") / "test.sqlite"
    rows, udis = bi.entity_rows(test_device_search.CATALOG, test_device_search.DEVICE_DETAILS,
                                test_device_search.ALIASES)
    recalls, unmapped = bi.recall_rows(FIXTURE_RECALLS, bi.load_categories())
    assert not unmapped                 # every fixture root_cause has a category
    bi.write_index(path, rows, udis, {"dataset": "test", "built_at": "2026-11-24T06:00:00+00:00"},
                   recalls)
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
    assert bi.INDEX_VERSION == 3


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
    assert summary["Reports"] == f"{page['counts']['total_reports']:,}"
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
