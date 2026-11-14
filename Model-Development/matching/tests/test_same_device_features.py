"""Behaviour of vigil_model/same_device_features.py. Offline: no BigQuery. The golden fixture (H4) is separate."""
import hashlib
import re

import pytest

from vigil_model import same_device_features as f
from vigil_model.same_device_features import DeviceSide, MatchContext, MatchKind


def side(model=None, cat=None, mfr="acme", name=None, pcs=()):
    return DeviceSide(mfr, name, model, cat, tuple(pcs))


@pytest.mark.parametrize("raw,key", [("IP-200", "IP200"), ("ip 200", "IP200"), ("5925-040-12", "592504012"),
                                     ("  a.b/c_d ", "ABCD"), (None, None), ("", "")])
def test_identifier_key(raw, key):
    assert f.identifier_key(raw) == key


@pytest.mark.parametrize("raw", ["NA", "N/A", "NI", "UNKNOWN", "0000", "XXX", "?", "A", "", None, "n.a."])
def test_placeholders_are_unusable(raw):
    assert f.usable_key(raw) is None


def test_nbsp_is_not_stripped_like_re2():
    assert f.identifier_key("AB C") == "AB C"


def test_raw_equal_guards_hyphen_collision():
    assert f.identifier_key("142-200") == f.identifier_key("14-2200")
    assert not f.raw_equal("142-200", "14-2200")
    assert f.raw_equal(" IP-200", "IP-200 ") and not f.raw_equal("", "")


def test_token_containment():
    assert f.contains_as_token("OLYMPUS CV-190", "CV190")
    assert not f.contains_as_token("OLYMPUS CV-1900", "CV190")
    assert not f.contains_as_token(None, "CV190")


@pytest.mark.parametrize("m,g,kind", [
    (side("IP-200"), side("IP200"), MatchKind.SAME_MODEL_KEY),
    (side("ABC123"), side("X1", "ABC123"), MatchKind.MAUDE_MODEL_EQ_GUDID_CATALOG),
    (side("NA", "1006402"), side("1006402"), MatchKind.MAUDE_CATALOG_EQ_GUDID_MODEL),
    (side("QQ1", "CAT55"), side("ZZ9", "CAT-55"), MatchKind.CATALOG_EQ_CATALOG),
    (side("N/A"), side("GP200"), MatchKind.MAUDE_MODEL_MISSING_OR_PLACEHOLDER),
    (side("GP200"), side(None), MatchKind.GUDID_MODEL_MISSING),
    (side("GP200"), side("GP200S"), MatchKind.PREFIX_RELATED),
    (side("GF-UCT180"), side("OLYMPUS GF TYPE UCT180 GFUCT180X"), MatchKind.CONTAINS),
    (side("37711"), side("97714"), MatchKind.CONFLICT),
])
def test_identifier_match_follows_q08_order(m, g, kind):
    assert f.identifier_match(m, g) is kind


def test_na_equals_na_is_not_a_match():
    assert f.identifier_match(side("NA"), side("NA")) is MatchKind.MAUDE_MODEL_MISSING_OR_PLACEHOLDER


def test_conflicting_identifier_both_directions():
    sib = frozenset({"GP200S"})
    assert f.has_conflicting_identifier(side("GP200S"), side("GP200"), sib)
    assert not f.has_conflicting_identifier(side("GP200S"), side("GP200S"), sib)
    assert not f.has_conflicting_identifier(side("GP200S"), side("GP200SXXXX"), sib)  # 4 characters away
    assert not f.has_conflicting_identifier(side("GP200S"), side("GP200"), frozenset())


def test_exact_key_match_is_not_a_conflict():
    # pair de53171f: model 914ESP agrees, catalog 914ESPA is only a suffix away
    assert not f.has_conflicting_identifier(side("914ESP"), side("914ESP", "914ESPA"), frozenset({"914ESP"}))


def test_distinctive_threshold():
    assert f.is_distinctive(20) and not f.is_distinctive(21) and not f.is_distinctive(None)


def test_pair_features_keys_are_exactly_the_contract():
    ctx = MatchContext(frozenset({("a", "b")}), 3)
    feats = f.pair_features(side("GP200", mfr="a", name="Infusion Pump Kit", pcs=["FRN"]),
                            side("GP200", mfr="b", name="Pump", pcs=["FRN", "MEA"]), ctx)
    assert tuple(feats) == f.FEATURE_COLUMNS and len(f.feature_vector(feats)) == len(f.FEATURE_COLUMNS)
    assert feats["feat_labeler_alias_equal"] and not feats["feat_labeler_equal"]
    assert feats["feat_product_code_agree"] is True and feats["feat_maude_name_has_kit_word"]
    assert feats["feat_device_name_token_overlap"] == pytest.approx(1 / 3)
    f.assert_no_leakage(feats)


def test_feature_vector_rejects_unknown_and_missing():
    with pytest.raises(KeyError):
        f.feature_vector({"feat_match_kind": "x"})


def test_name_leak_guard():
    f.assert_no_leakage(f.FEATURE_COLUMNS)
    with pytest.raises(f.LeakageError):
        f.assert_no_leakage(["feat_ok", "udi_di"])


def test_value_leak_guard_catches_derived_leaks():
    di = "00812345678901"
    f.assert_no_value_leakage({"feat_a": "same_model_key", "feat_b": 12, "feat_c": True}, di)
    for leaked in (di, "x" + di, di[:10], int(di[:8])):
        with pytest.raises(f.LeakageError):
            f.assert_no_value_leakage({"feat_a": leaked}, di)
    f.assert_no_value_leakage({"key_gudid_udi_di": di}, di)  # only feat_* columns are checked


def test_split_and_pair_id_match_their_documented_formulas():
    k = "acme|GP200"
    assert f.split_bucket(k) == int(hashlib.sha256(b"v1|acme|GP200").hexdigest()[:8], 16) % 100
    assert f.split_of(k) in ("train", "test")
    assert f.pair_id("1", None, "D", "pos") == f.pair_id("1", None, "D", "pos") != f.pair_id("1", 0, "D", "pos")
    assert f.pair_id("1", None, "D", "pos") == hashlib.sha256(b"1||D|pos").hexdigest()[:32]


def test_split_is_roughly_20_percent():
    n = sum(f.split_of(f"k{i}") == "test" for i in range(5000))
    assert 0.17 < n / 5000 < 0.23


def test_device_key_needs_an_identifier():
    assert f.device_key(side("N/A", "CAT-9", mfr="acme")) == "acme|CAT9"
    with pytest.raises(ValueError):
        f.device_key(side("NA", None))


def test_sql_match_kind_has_q08_labels_in_order():
    sql = f.sql_match_kind("m", "g")
    assert re.findall(r"(?:THEN|ELSE) '([a-z_]+)'", sql) == [k.value for k in f.MatchKind]


def test_sql_emitters_carry_the_shared_constants():
    assert f.STRIP_CHARS_RE in f.sql_identifier_key("x") and f.PLACEHOLDER_RE in f.sql_is_placeholder("k")
    assert f"< {f.TEST_SPLIT_PCT}" in f.sql_split("k") and "'v1'" in f.sql_split("k")
    assert "''" in f.sql_pair_id("a", "b", "c", "'pos'")  # NULL device_seq falls back to ''


@pytest.mark.parametrize("kind,model_missing,cat_missing,gated", [
    ("maude_model_missing_or_placeholder", "True", "True", True),    # E6: no usable identifier
    ("maude_model_missing_or_placeholder", True, False, False),      # catalog still usable
    ("prefix_related", "False", "False", True),                      # E3b: a prefix alone is not evidence
    ("same_model_key", "False", "1.0", False),
    ("contains", False, False, False),
])
def test_insufficient_evidence_gate(kind, model_missing, cat_missing, gated):
    feat = {"feat_match_kind": kind, "feat_maude_model_missing": model_missing,
            "feat_maude_catalog_missing": cat_missing}
    assert f.insufficient_evidence(feat) is gated
    assert f.insufficient_evidence(feat, "e6_e3b") is gated


@pytest.mark.parametrize("at_end,pc_agree,gated", [
    ("False", "False", True),       # C1: 3401 inside H7491213401M0, product codes LWS vs DQX
    (False, False, True),           # parquet booleans
    ("True", "False", False),       # the key ends the GUDID id: left to the model
    ("False", "True", False),       # product codes agree
    ("False", None, False),         # product code unknown on one side: not evidence either way
    ("False", float("nan"), False),
])
def test_c1_gate_sends_mid_string_contains_with_disagreeing_codes_to_ie(at_end, pc_agree, gated):
    feat = {"feat_match_kind": "contains", "feat_maude_model_missing": False, "feat_maude_catalog_missing": True,
            "feat_contains_at_end": at_end, "feat_product_code_agree": pc_agree}
    assert f.insufficient_evidence(feat, "e6_e3b_c1") is gated
    assert f.insufficient_evidence(feat, "e6_e3b") is False          # v1.0.0's gate never does
    assert f.insufficient_evidence(dict(feat, feat_match_kind="same_model_key")) is False


def test_unknown_gate_version_is_refused():
    with pytest.raises(ValueError):
        f.insufficient_evidence({"feat_match_kind": "contains", "feat_maude_model_missing": False,
                                 "feat_maude_catalog_missing": False}, "e7")


@pytest.mark.parametrize("m,g,extra,at_end", [
    ("1260", "M001711260", 6, True),        # the hand false positive: a short key ends a long catalog code
    ("0673", "M0067301400", 7, False),      # in the middle
    ("U1054260B", "1054260", 2, False),     # GUDID key inside the MAUDE key
])
def test_contains_features(m, g, extra, at_end):
    feats = f.pair_features(side(model=m), side(model=g), MatchContext(frozenset(), 3))
    assert feats["feat_match_kind"] == MatchKind.CONTAINS
    assert feats["feat_contains_extra_len"] == extra and feats["feat_contains_at_end"] is at_end


def test_contains_features_are_none_for_other_kinds_and_cosine_comes_from_ctx():
    feats = f.pair_features(side(model="GP200"), side(model="GP200"), MatchContext(frozenset(), 3, text_cosine=0.42))
    assert feats["feat_contains_extra_len"] is None and feats["feat_contains_at_end"] is None
    assert feats["feat_text_cosine"] == 0.42


def test_rule_hash_ignores_the_feature_list():
    c = f.constants()
    assert f.rule_constants_sha256(c) == f.rule_constants_sha256({**c, "FEATURE_COLUMNS": ["feat_x"]})
    assert f.rule_constants_sha256(c) != f.rule_constants_sha256({**c, "MIN_KEY_LEN": 3})


def test_fs1_is_the_baseline_feature_list_of_label_set_v1():
    """P0/E1 were trained on these 19 columns; editing fs1 would silently change those experiments."""
    import json
    from pathlib import Path
    manifest = Path(__file__).resolve().parents[1] / "labels" / "label_set_v1.manifest.json"
    assert list(f.FEATURE_SETS["fs1"]) == json.loads(manifest.read_text())["constants"]["FEATURE_COLUMNS"]


def test_feature_sets_are_ordered_subsets_and_named_back():
    for name, cols in f.FEATURE_SETS.items():
        assert f.feature_columns_in(cols) == list(cols), name        # subset of FEATURE_COLUMNS, canonical order
        assert f.feature_set_name(cols) == name
    assert f.DEFAULT_FEATURE_SET in f.FEATURE_SETS
    assert f.FEATURE_SETS["fs3"] == f.FEATURE_COLUMNS                   # the newest set holds every column
    assert f.feature_set_name(["feat_match_kind"]) == "custom"
    with pytest.raises(ValueError):
        f.feature_set("fs99")
    assert f.feature_columns_in(["pair_id", "feat_text_cosine", "feat_match_kind"]) == ["feat_match_kind",
                                                                                        "feat_text_cosine"]


def test_labeler_similarity_ranks_spelling_variants_above_unrelated_labelers():
    sim = f.labeler_similarity
    assert sim("acme", "acme") == 1.0 and sim(None, "acme") is None and sim("", "acme") is None
    same_company = sim("s.i.n. implant system sin", "sistema de implante nacional s/a")   # logreg's missed match
    renamed = sim("carefusion sd", "carefusion 303")
    unrelated = sim("dentsply sirona", "resmed")                                       # a cross-labeler collision
    assert renamed > same_company > unrelated
    feats = f.pair_features(side(model="X1", mfr="carefusion sd"), side(model="X1", mfr="carefusion 303"),
                            MatchContext(frozenset(), 3))
    assert feats["feat_labeler_sim"] == renamed
