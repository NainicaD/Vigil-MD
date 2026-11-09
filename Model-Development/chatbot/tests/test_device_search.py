"""Tests for device_search: normalization, index building, search ranking, question resolution.

Uses a small made-up index written to a temporary file, from rows shaped like the summary
tables entity_catalog and device_catalog. No GCP.
"""
import hashlib

import pytest

from device_search.build_index import alias_words, entity_rows, write_index
from device_search.normalize import norm, phrase_score, trigrams, word_similarity
from device_search.search import DeviceIndex, device_words, score_entry

ALIASES = {"philips": ["respironics"], "abbott": ["st jude", "thoratec"]}

DEVICES = [
    {"company_norm": "respironics", "brand_key": "dreamstation", "name": "DreamStation",
     "company": "Respironics, Inc.", "udi_dis": ["00000000000101", "00000000000102"],
     "models": ["DSX500"], "n_models": 1, "product_codes": ["BZD"], "reports": 26000, "deaths": 40, "injuries": 900},
    {"company_norm": "respironics", "brand_key": "dreamstation go", "name": "DreamStation Go",
     "company": "Respironics, Inc.", "udi_dis": ["00000000000103"], "models": [], "n_models": 0,
     "product_codes": ["BZD"], "reports": 5000, "deaths": 0, "injuries": 3},
    {"company_norm": "insulet", "brand_key": "omnipod dash pdm", "name": "Omnipod DASH PDM",
     "company": "Insulet Corporation", "udi_dis": ["00000000000201"], "models": ["PDM-INT1"], "n_models": 1,
     "product_codes": ["LZG"], "reports": 590, "deaths": 1, "injuries": 20},
    {"company_norm": "abiomed", "brand_key": "impella", "name": "Impella", "company": "Abiomed, Inc.",
     "udi_dis": ["00000000000301"], "models": ["CP"], "n_models": 1, "product_codes": ["OZD"],
     "reports": 28000, "deaths": 5000, "injuries": 2000},
    {"company_norm": "allergan", "brand_key": "natrelle breast implants", "name": "Natrelle Breast Implants",
     "company": "Allergan", "udi_dis": ["00000000000401"], "models": [], "n_models": 0,
     "product_codes": ["FTR"], "reports": 5000, "deaths": 0, "injuries": 4900},
    {"company_norm": "mentor texas", "brand_key": "mentor memorygel breast implant",
     "name": "MENTOR MemoryGel Breast Implant", "company": "MENTOR TEXAS L.P.", "udi_dis": ["00000000000501"],
     "models": [], "n_models": 0, "product_codes": ["FTR"], "reports": 1200, "deaths": 0, "injuries": 1100},
    {"company_norm": "acme cables", "brand_key": "impella adapter cable", "name": "Impella Adapter Cable",
     "company": "Acme Cables", "udi_dis": ["00000000000701"], "models": [], "n_models": 0,
     "product_codes": ["OZD"], "reports": 2, "deaths": 0, "injuries": 0},
    {"company_norm": "mannkind", "brand_key": "v-go disposable insulin delivery",
     "name": "V-Go Disposable Insulin Delivery", "company": "MannKind Corporation",
     "udi_dis": ["00000000000801"], "models": [], "n_models": 0, "product_codes": ["LZG"],
     "reports": 105, "deaths": 0, "injuries": 12},
    {"company_norm": "dexcom", "brand_key": "dexcom g6", "name": "Dexcom G6", "company": "Dexcom, Inc.",
     "udi_dis": ["00000000000901"], "models": [], "n_models": 0, "product_codes": ["MDS"],
     "reports": 3000, "deaths": 0, "injuries": 300},
    {"company_norm": "sientra", "brand_key": "sientra silicone gel breast implant",
     "name": "SIENTRA Silicone Gel Breast Implant", "company": "Sientra, Inc.", "udi_dis": ["00000000000601"],
     "models": [], "n_models": 0, "product_codes": ["FTR"], "reports": 1, "deaths": 0, "injuries": 1},
]
COMPANIES = [
    {"company_norm": "mannkind corporation - v-go", "company": "MANNKIND CORPORATION - V-GO",
     "reports": 253, "deaths": 0, "injuries": 40, "product_codes": ["LZG"]},
    {"company_norm": "dexcom", "company": "DEXCOM", "reports": 9000, "deaths": 2,
     "injuries": 800, "product_codes": ["MDS"]},
    {"company_norm": "respironics", "company": "RESPIRONICS, INC.", "reports": 200000, "deaths": 4000,
     "injuries": 46000, "product_codes": ["BZD"]},
    {"company_norm": "medtronic minimed", "company": "MEDTRONIC MINIMED", "reports": 32000, "deaths": 20,
     "injuries": 3000, "product_codes": ["LZG"]},
    {"company_norm": "sientra", "company": "SIENTRA INC", "reports": 4500, "deaths": 0, "injuries": 4400,
     "product_codes": ["FTR"]},
]
CODES = [{"product_code": "LZG", "reports": 60000, "deaths": 33, "injuries": 4200,
          "top_names": ["OMNIPOD DASH INSULIN MANAGEMENT SYSTEM"]}]


def dev_id(company_norm, brand_key):
    """The pipeline's device_catalog id (build_summaries.py, checked against BigQuery)."""
    return "dev:" + hashlib.sha1(f"{company_norm}|{brand_key}".encode()).hexdigest()[:12]


# The same entries as summary-table rows
CATALOG = (
    [{"entity_type": "device", "entity_id": dev_id(d["company_norm"], d["brand_key"]),
      "title": d["name"], "subtitle": d["company"], "company_norm": d["company_norm"],
      "product_codes": d["product_codes"], "top_names": [], "reports": d["reports"],
      "deaths": d["deaths"], "injuries": d["injuries"]} for d in DEVICES]
    + [{"entity_type": "manufacturer", "entity_id": c["company_norm"], "title": c["company"],
        "subtitle": None, "company_norm": c["company_norm"], "product_codes": c["product_codes"],
        "top_names": [], "reports": c["reports"], "deaths": c["deaths"], "injuries": c["injuries"]}
       for c in COMPANIES]
    + [{"entity_type": "product_code", "entity_id": p["product_code"],
        "title": f"Product code {p['product_code']}", "subtitle": "; ".join(p["top_names"]),
        "company_norm": None, "product_codes": [p["product_code"]], "top_names": p["top_names"],
        "reports": p["reports"], "deaths": p["deaths"], "injuries": p["injuries"]} for p in CODES])
DEVICE_DETAILS = [{"device_id": dev_id(d["company_norm"], d["brand_key"]), "models": d["models"],
                   "n_models": d["n_models"], "udi_dis": d["udi_dis"]} for d in DEVICES]


@pytest.fixture(scope="module")
def index(tmp_path_factory):
    path = tmp_path_factory.mktemp("search") / "test.sqlite"
    rows, udis = entity_rows(CATALOG, DEVICE_DETAILS, ALIASES)
    write_index(path, rows, udis, {"dataset": "test"})
    return DeviceIndex.open(path)


# ── normalization ─────────────────────────────────────────────────────────────

def test_norm_and_trigrams():
    assert norm("DreamStation® Auto-CPAP ") == "dreamstation auto cpap"
    assert norm("t:slim X2") == "t slim x2" and norm(None) == ""
    assert trigrams("dream") == ["dre", "rea", "eam"] and trigrams("ab") == []


def test_word_similarity_allows_typos_and_prefixes():
    assert word_similarity("dreamstaton", "dreamstation") > 0.9
    assert word_similarity("dream", "dreamstation") == 0.95
    assert word_similarity("pump", "dreamstation") < 0.5
    assert word_similarity("implants", "implant") == 1.0 and word_similarity("gas", "ga") < 1.0
    assert phrase_score(["omnipod", "dash"], ["omnipod", "dash", "pdm"]) == 1.0


def test_company_words_count_a_little_less():
    assert score_entry(["medtronic"], ["medtronic"], []) == 1.0
    assert score_entry(["medtronic"], ["minimed"], ["medtronic"]) == pytest.approx(0.9)


def test_aliases_attach_to_matching_company_names_only():
    assert alias_words("respironics", ALIASES) == ["philips"]
    assert alias_words("st jude medical", ALIASES) == ["abbott"]
    assert alias_words("judeco", ALIASES) == []          # whole words only


def test_device_id_matches_the_pipeline():
    # BigQuery gave dev:f0cdc15b4d63 for respironics|dreamstation (build_summaries.py)
    assert dev_id("respironics", "dreamstation") == "dev:f0cdc15b4d63"


def test_entries_use_the_query_layer_entity_types(index):
    kinds = {r[0] for r in index.con.execute("SELECT DISTINCT kind FROM entities")}
    assert kinds == {"device", "manufacturer", "product_code"}


def test_device_words_drop_question_and_judgement_words():
    assert device_words("How many deaths were reported for the DreamStation since 2020?") == ["dreamstation"]
    assert device_words("is the mentor memorygel safe") == ["mentor", "memorygel"]
    assert device_words("how many reports?") == []


# ── search ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("query, first", [
    ("dreamstaton", "DreamStation"),                     # typo
    ("DreamStation", "DreamStation"),
    ("philips dreamstation", "DreamStation"),            # owner name via alias
    ("omnipod dash", "Omnipod DASH PDM"),
    ("impella", "Impella"),
    ("medtronic", "MEDTRONIC MINIMED"),                  # company
    ("philips", "RESPIRONICS, INC."),                    # alias → company
    ("sientra", "SIENTRA INC"),
    ("lzg", "Product code LZG"),
    ("DSX500", "DreamStation"),                          # model number
])
def test_search_ranks_the_expected_entry_first(index, query, first):
    assert index.search(query)[0].title == first


def test_equal_scores_rank_by_reports(index):
    titles = [h.title for h in index.search("dreamstation")]
    assert titles.index("DreamStation") < titles.index("DreamStation Go")


def test_equal_scores_rank_by_reports_across_kinds(index):
    # the company (4,500 reports) before a device of the same name with 1 report
    assert [h.kind for h in index.search("sientra")[:2]] == ["manufacturer", "device"]


def test_a_udi_in_the_question_wins_over_device_words(index):
    r = index.resolve("is UDI 00000000000201 the same as the dreamstation?")
    assert r.status == "resolved" and r.matches[0].title == "Omnipod DASH PDM" and r.phrase == "00000000000201"


def test_search_by_udi_and_unknown_text(index):
    assert index.search("00000000000102")[0].title == "DreamStation"
    assert index.search("zzzzqqq") == [] and index.search("  ") == []


# ── resolve ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("question, status, titles", [
    ("How many deaths for the dreamstaton?", "resolved", ["DreamStation"]),
    ("Was the Philips DreamStation recalled and why?", "resolved", ["DreamStation"]),
    ("compare the impella and the dreamstation", "several", ["Impella", "DreamStation"]),
    ("Any recalls for product code LZG?", "resolved", ["Product code LZG"]),
    ("who makes UDI 00000000000201", "resolved", ["Omnipod DASH PDM"]),
    ("what goes wrong with breast implants", "ambiguous", None),   # several companies' products
    ("how many reports since 2020?", "none", []),
    ("tell me about the zzzzqqq", "none", []),
])
def test_resolve(index, question, status, titles):
    r = index.resolve(question)
    assert r.status == status, (question, r)
    if titles is not None:
        assert [h.title for h in r.matches] == titles
    if status == "ambiguous":
        assert 1 < len(r.matches) <= 3


def test_dominant_device_is_taken_without_asking(index):
    # DreamStation (26,000 reports) vs DreamStation Go (5,000), same company: 5.2x, taken
    assert index.resolve("dreamstation problems").status == "resolved"


def test_equal_matches_from_different_companies_ask_unless_one_is_far_ahead(index):
    # Natrelle (Allergan, 5,000) vs MENTOR (1,200): 4x, enough within one company, not across
    r = index.resolve("breast implant problems")
    assert r.status == "ambiguous" and len({h.company_norm for h in r.matches}) > 1
    # Impella (28,000) vs another company's Impella Adapter Cable (2): far ahead, taken
    r = index.resolve("impella deaths")
    assert r.status == "resolved" and r.matches[0].title == "Impella"


def test_a_dominant_manufacturer_spelling_is_taken(index):
    # one firm can have several name spellings until the matching team groups them
    r = index.resolve("philips recalls")
    assert r.status == "resolved" and r.matches[0].entity_id == "respironics"


def test_unknown_udi_falls_back_to_words(index):
    assert index.resolve("who makes UDI 99999999999999").status == "none"


def test_query_key_for_each_kind(index):
    dev = index.resolve("dreamstation").matches[0]
    assert index.query_key(dev) == {"entity_type": "device", "entity_id": "dev:f0cdc15b4d63"}
    assert index.udis_for(dev) == ["00000000000101", "00000000000102"]
    co = index.search("sientra")[0]
    assert index.query_key(co) == {"entity_type": "manufacturer", "entity_id": "sientra"}
    pc = index.search("lzg")[0]
    assert index.query_key(pc) == {"entity_type": "product_code", "entity_id": "LZG"}


def test_index_file_is_read_only(index):
    import sqlite3
    with pytest.raises(sqlite3.OperationalError):
        index.con.execute("DELETE FROM entities")


def test_missing_index_says_how_to_build_it(tmp_path):
    with pytest.raises(FileNotFoundError, match="build_index"):
        DeviceIndex.open(tmp_path / "none.sqlite")


# ── names with stopwords, exact names, chatty words (found in the vigil_md_dev e2e run) ──

def test_name_parts_keep_stopwords_inside_names():
    assert device_words("how many malfunction reports does the V-Go have") == ["v", "go"]
    assert device_words("deaths for the DreamStation Go?") == ["dreamstation", "go"]
    assert device_words("what can go wrong with the DreamStation") == ["dreamstation"]
    assert device_words("Should I stop using it?") == []


def test_a_typed_whole_name_wins_a_tie(index):
    # "Natrelle Breast Implants" scores the same for every title containing those words; the
    # entry whose whole name was typed is taken.
    r = index.resolve("What problems are reported for Natrelle Breast Implants?")
    assert r.status == "resolved" and r.matches[0].title == "Natrelle Breast Implants"
    assert index.resolve("How many reports for breast implants?").status == "ambiguous"


def test_dreamstation_go_is_not_the_dreamstation(index):
    r = index.resolve("How many deaths for the DreamStation Go, and was it recalled?")
    assert [h.title for h in r.matches] == ["DreamStation Go"]


def test_chatty_words_do_not_block_a_clear_name(index):
    r = index.resolve("Was the DreamStation recalled? Should I stop using mine?")
    assert r.status == "resolved" and r.matches[0].title == "DreamStation"


def test_a_model_number_is_never_dropped(index):
    # Not in the index: must not fall back to the company (MEDTRONIC MINIMED).
    assert index.resolve("Any recalls for the MiniMed 780G?").status != "resolved"


@pytest.mark.parametrize("question", [
    "How many deaths for the MiniMed Paradigm?",       # company in the index, model not
    "respironics trilogy deaths",
])
def test_an_unknown_model_name_does_not_fall_back_to_the_company(index, question):
    # Dropping the unknown model name leaves only the company: answering about the whole firm
    # would answer a different question, so the bot asks or says not found instead.
    r = index.resolve(question)
    assert not (r.status == "resolved" and r.matches[0].kind != "device"), r


def test_a_device_wins_a_tie_with_a_manufacturer_named_after_it(index):
    # "V-Go" scores the same for the device and for "MANNKIND CORPORATION - V-GO": the chat
    # question is about the product.
    for q in ("how many malfunction reports does the V-Go have",
              "Is the V-Go reliable? Also how many injury reports does it have?"):
        r = index.resolve(q)
        assert r.status == "resolved" and r.matches[0].title == "V-Go Disposable Insulin Delivery"


def test_a_typed_company_name_still_resolves_to_the_company(index):
    # "Dexcom" ties the DEXCOM entry and the Dexcom G6 (a plain company name, no " - "
    # suffix); "mannkind" matches the company part of "MANNKIND CORPORATION - V-GO".
    for q in ("What recalls has Dexcom had?", "What recalls has MannKind had?"):
        r = index.resolve(q)
        assert r.status == "resolved" and r.matches[0].kind == "manufacturer", q


def test_matched_only_in_suffix():
    from device_search.search import Hit, _matched_only_in_suffix
    def m(title, kind="manufacturer"):
        return Hit("x", kind, "x", title, "", 0, 0, 0, [], None, 1.0)
    assert _matched_only_in_suffix(m("MANNKIND CORPORATION - V-GO"), {"v", "go"})
    assert not _matched_only_in_suffix(m("MANNKIND CORPORATION - V-GO"), {"mannkind", "go"})
    assert not _matched_only_in_suffix(m("MENTOR TEXAS"), {"mentor"})
    assert not _matched_only_in_suffix(m("V-Go - Pump", kind="device"), {"pump"})

