from features import (
    normalize_key, identifier_match, raw_identifier_equal,
    build_alias_table, manufacturer_match,
    build_di_counts, model_key_di_count, is_distinctive,
    build_labeler_keys, suffix_conflict,
)

def test_formatting_differences_match():  # E2
    assert normalize_key("IP-200") == normalize_key("IP200") == "IP200"
    assert normalize_key("5925-040-12") == "592504012"
    assert normalize_key(" cl2078-blue ") == "CL2078BLUE"


def test_placeholders_are_missing():  # E6
    for value in ["NA", "N/A", "n/a", "NI", "UNK", "UNKNOWN"]:
        assert normalize_key(value) is None


def test_empty_short_or_nan_is_missing():
    assert normalize_key("") is None
    assert normalize_key("7") is None
    assert normalize_key(None) is None
    assert normalize_key(float("nan")) is None


def test_hyphen_collision_is_a_known_risk():  # E2 exception
    # different devices that collapse to one key; the raw-equality flag guards this
    assert normalize_key("142-200") == normalize_key("14-2200")


def test_formatting_only_is_exact():  # E2
    assert identifier_match(["IP-200", None], ["IP200", None]) == (1, 0)


def test_catalog_matches_model_across_fields():  # E7
    # MAUDE model is a placeholder, MAUDE catalog = GUDID model
    assert identifier_match(["NA", "1006402"], ["1006402", None]) == (1, 0)


def test_whole_token_match():  # E2
    assert identifier_match(["CV-190", None], ["OLYMPUS CV-190", None]) == (0, 1)


def test_placeholders_never_match():  # E6
    assert identifier_match(["NA", "UNK"], ["NA", "UNK"]) == (0, 0)


def test_suffix_variant_is_not_a_match():  # E3
    assert identifier_match(["GP200", None], ["GP200S", None]) == (0, 0)


def test_known_gap_gf_uct180():  # E2 example the rule does not cover (flagged to Hitika)
    assert identifier_match(["GF-UCT180", None], ["OLYMPUS GF TYPE UCT180", None]) == (0, 0)


def test_raw_equal_same_text():
    assert raw_identifier_equal(["IP-200", None], ["IP-200", None]) == 1


def test_raw_equal_ignores_case():
    assert raw_identifier_equal(["cl2078-blue", None], [None, "CL2078-BLUE"]) == 1


def test_sontec_collision_is_flagged():  # E2 exception
    # keys match, raw text does not: the model gets a warning sign
    assert identifier_match(["142-200", None], ["14-2200", None]) == (1, 0)
    assert raw_identifier_equal(["142-200", None], ["14-2200", None]) == 0


def test_raw_equal_ignores_placeholders():  # E6
    assert raw_identifier_equal(["NA", None], ["NA", None]) == 0


def test_alias_learned_from_enough_devices():  # E5
    rows = [
        ("abbott medical", "st. jude medical", "DI1"),
        ("abbott medical", "st. jude medical", "DI2"),
        ("abbott medical", "st. jude medical", "DI3"),
    ]
    assert ("abbott medical", "st. jude medical") in build_alias_table(rows)


def test_one_off_pair_is_not_an_alias():  # open question 3
    rows = [
        ("typo medical", "real medical", "DI1"),
        ("typo medical", "real medical", "DI1"),   # same device twice still counts once
    ]
    assert build_alias_table(rows) == set()


def test_equal_names_are_not_aliases():
    rows = [("medtronic", "medtronic", f"DI{i}") for i in range(5)]
    assert build_alias_table(rows) == set()


def test_manufacturer_equal_and_alias():
    aliases = {("abbott medical", "st. jude medical")}
    assert manufacturer_match("Medtronic ", "medtronic", aliases) == (1, 0)
    assert manufacturer_match("abbott medical", "st. jude medical", aliases) == (0, 1)
    assert manufacturer_match("abbott medical", "zimvie us corp", aliases) == (0, 0)


def test_manufacturer_missing():
    assert manufacturer_match(None, "medtronic", set()) == (0, 0)


def test_family_label_is_not_distinctive():  # E4
    rows = [("Brosmed", "REV01", f"DI{i}") for i in range(25)]
    counts = build_di_counts(rows)
    assert model_key_di_count("brosmed", "REV01", counts) == 25
    assert is_distinctive("brosmed", "REV01", counts) == 0


def test_small_group_is_distinctive():  # E4
    rows = [("Medline", "DYNJ85712", f"DI{i}") for i in range(3)]
    counts = build_di_counts(rows)
    assert is_distinctive("medline", "DYNJ85712", counts) == 1


def test_formatting_variants_count_as_one_key():  # E2 + E4
    rows = [("Steris", "CL2078-BLUE", "DI1"), ("Steris", "CL2078BLUE", "DI2")]
    counts = build_di_counts(rows)
    assert model_key_di_count("steris", "cl2078 blue", counts) == 2


def test_same_di_counts_once():
    rows = [("Steris", "CL2078", "DI1"), ("Steris", "CL2078", "DI1")]
    assert build_di_counts(rows)[("steris", "CL2078")] == 1


def test_unknown_model_is_not_distinctive():
    assert model_key_di_count("steris", "NOPE99", {}) == 0
    assert is_distinctive("steris", "NOPE99", {}) == 0
    assert is_distinctive("steris", "NA", {}) == 0   # placeholder

def make_keys(rows):
    """Helper: build labeler keys from (labeler, model, di) rows."""
    return build_labeler_keys(build_di_counts(rows))


def test_conflict_when_exact_sibling_exists():  # E3a
    keys = make_keys([("Globus", "GP200", "DI1"), ("Globus", "GP200S", "DI2")])
    # MAUDE says GP200, candidate GP200S, but GP200 exists exactly
    assert suffix_conflict(["GP200", None], ["GP200S", None], "Globus", keys) == (1, 0)


def test_conflict_doc_example_reverse():  # E3 example: MAUDE GP200S, candidate GP200
    keys = make_keys([("Globus", "GP200", "DI1"), ("Globus", "GP200S", "DI2")])
    assert suffix_conflict(["GP200S", None], ["GP200", None], "Globus", keys) == (1, 0)


def test_prefix_only_when_no_exact_record():  # E3b
    keys = make_keys([("Globus", "GP200S", "DI1"), ("Globus", "GP200T", "DI2")])
    assert suffix_conflict(["GP200", None], ["GP200S", None], "Globus", keys) == (0, 1)


def test_globus_real_example():  # E3, from Q07
    keys = make_keys([("Globus", "394.126C", "DI1"), ("Globus", "394.126CS", "DI2")])
    assert suffix_conflict(["394.126C", None], ["394.126CS", None], "Globus", keys) == (1, 0)


def test_unrelated_and_exact_are_not_suffix_variants():
    keys = make_keys([("Globus", "GP200", "DI1")])
    assert suffix_conflict(["GP200", None], ["XR500", None], "Globus", keys) == (0, 0)
    assert suffix_conflict(["GP200", None], ["GP200", None], "Globus", keys) == (0, 0)


def test_suffix_longer_than_3_is_ignored():
    keys = make_keys([("Globus", "GP200", "DI1")])
    assert suffix_conflict(["GP200", None], ["GP200ABCD", None], "Globus", keys) == (0, 0)